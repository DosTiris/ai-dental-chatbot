# app/services/portal_schedule_service.py
#
# OWNER OF: the portal-only schedule mutation rules (P4-A - Portal Slot
# Schedule Controls v1, contract v1.2 SS2/SS3/SS5-B/SS5-E):
#
#   * the per-(tenant, office-local day) advisory-lock serialization
#     (acquire_schedule_day_lock - contract SS2 / Correction C3);
#   * local wall-time classification against DST transitions
#     (classify_local_wall_time - contract SS3 / Correction C2);
#   * exact slot expansion for "publish a day's hours"
#     (expand_publish_slots - contract SS5-B / Correction E);
#   * the new-pathway overlap refusal (find_first_overlap);
#   * the publish transaction (publish_day_slots);
#   * the bulk "block all open slots" transaction (block_all_open),
#     which applies slot_management_service.apply_block - the ONE shared
#     block rule (Rule 3) - to every open row.
#
# This module contains NO HTTP and NO authentication: app/routes/
# portal_schedule.py is transport wiring only, and tenancy arrives here
# already resolved (the verified PortalIdentity.client id).
#
# ---------------------------------------------------------------------------
# SERIALIZATION (contract SS2, D8) - why an advisory lock exists at all:
# the overlap refusal is a read-then-insert. Two concurrent publishes for
# the same tenant/day over an EMPTY day would both read "no rows", both
# pass, and both insert - overlapping capacity with no row to lock. The
# fix is one transaction-scoped PostgreSQL advisory lock, keyed by the
# canonical text material
#
#     mia:sched:<client_uuid>:<YYYY-MM-DD>
#
# bound explicitly AS ONE TEXT PARAMETER to
#
#     SELECT pg_advisory_xact_lock(hashtextextended(:lock_material, 0))
#
# (never implicit UUID/date concatenation inside SQL - Correction C3).
# Transaction-scoped means release is automatic at commit/rollback: no
# unlock bookkeeping, no leak on error paths, and it serializes across
# every worker process sharing the database. NO process-local mutex
# exists anywhere in this design.
#
# ACQUISITION POINT (Correction C3 wording): the lock acquisition is the
# FIRST SCHEDULE-MUTATION DB STATEMENT after authentication/tenant
# resolution and pure request parsing. Portal authentication legitimately
# performs its own binding SELECT earlier in the same session; the binding
# correctness requirement enforced here is that NO schedule-relevant read
# or write (no appointment_slots SELECT, overlap query, day sweep, or
# INSERT/UPDATE for publish/bulk) executes before the day lock is held.
#
# DIALECT SEAM (named, not hidden - Rule 4): the statement is issued only
# on the PostgreSQL dialect. On SQLite (the pure local harness) it is a
# documented no-op, mirroring the frozen repository precedent that SQLite
# ignores FOR UPDATE and serializes via its whole-database write lock.
# The real behavior is proven by the PG 17.10 concurrency bites.
#
# A 64-bit hashtextextended collision across tenants/days is
# astronomically unlikely and harmless: the only possible effect is EXTRA
# serialization, never a correctness loss.
#
# RESIDUAL BOUNDARY (D6/D11, accepted in review): the internal admin
# publish surface (POST /admin/calendar/slots, X-Admin-Key) remains
# byte-unchanged in P4-A and does NOT participate in this advisory-lock /
# overlap guarantee. It is an out-of-band operator pathway and must not be
# used concurrently for the same tenant/day while portal schedule mutation
# is occurring. P4-A does not redesign admin publication.
#
# KNOWN LIMITATION (documented, not hidden): the overlap universe is the
# contract's "existing non-cancelled slot rows ON THAT LOCAL DAY" - rows
# whose start_datetime falls inside the day's UTC window (the same
# definition every frozen read uses). A hypothetical slot STARTING on the
# previous local day and spilling past local midnight would not be in that
# universe. Publish itself can never create such a row (all generated
# boundaries lie inside one local day), and real dental slots do not
# straddle midnight; widening the universe would change the contracted
# refusal set, so it is recorded here instead.

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from app.calendar_models import AppointmentSlot, SlotStatus
from app.repositories import appointment_repository
from app.services.calendar_settings_service import (
    CalendarSettings,
    ensure_utc,
    local_day_utc_window,
)
from app.services.slot_management_service import apply_block

# --- Named limits (contract SS5-B, D10 - Rule 4: no magic numbers) ---------
SLOT_MINUTES_MIN = 10
SLOT_MINUTES_MAX = 240
SLOT_MINUTES_STEP = 5           # slot_minutes must divide by this
MAX_GENERATED_SLOTS = 100       # admin batch cap mirrored
PORTAL_DEFAULT_SLOT_MINUTES = 30  # the portal UI's prefilled value

# Canonical lock-material prefix (Correction C3). The FULL material is
#   mia:sched:<client_uuid>:<YYYY-MM-DD>
# composed by build_schedule_lock_material below and NOWHERE else.
SCHEDULE_LOCK_MATERIAL_PREFIX = "mia:sched:"

# --- Closed wall-time classification vocabulary (contract SS3, D9) ---------
WALL_VALID = "valid"
WALL_NONEXISTENT = "nonexistent"    # spring-forward gap
WALL_AMBIGUOUS = "ambiguous"        # fall-back repeated interval

# --- Closed publish outcome vocabulary (SS5-B) ------------------------------
PUBLISH_OK = "ok"
PUBLISH_INVALID = "invalid"         # -> HTTP 422; detail says which rule
PUBLISH_OVERLAP = "overlap"         # -> HTTP 409; zero inserts

# The ONE 409 wording for every overlap refusal (probe-resistant: it never
# reveals WHICH existing slot or status collided).
OVERLAP_DETAIL = (
    "One or more requested slots overlap existing slots on that day."
)


@dataclass(frozen=True)
class WallTimeResult:
    """Outcome of classifying one naive local wall datetime (SS3)."""
    status: str                       # WALL_VALID / WALL_NONEXISTENT / WALL_AMBIGUOUS
    utc_instant: Optional[datetime]   # aware UTC; present only when valid


@dataclass(frozen=True)
class PublishResult:
    """Outcome of publish_day_slots - no boolean guessing."""
    ok: bool
    reason: str                       # PUBLISH_OK / PUBLISH_INVALID / PUBLISH_OVERLAP
    detail: Optional[str] = None      # 422/409 wording (caller-safe text only)
    slots: List[AppointmentSlot] = field(default_factory=list)


@dataclass(frozen=True)
class BulkBlockResult:
    """Outcome of block_all_open (SS5-E). booked_remaining carries ONLY
    (start,end) UTC instants - never patient or appointment data."""
    blocked_count: int
    booked_remaining: List[Tuple[datetime, datetime]]


def build_schedule_lock_material(client_id: uuid.UUID, local_day: date) -> str:
    """
    Purpose: Compose the canonical advisory-lock material (Correction C3):
        mia:sched:<client_uuid>:<YYYY-MM-DD>
        with the uuid in canonical lowercase-hyphenated text form and the
        day in ISO form. Composed in Python and bound as ONE text value -
        never concatenated implicitly inside SQL.
    Inputs:  the authenticated tenant id; the office-LOCAL calendar day.
    Returns: the material string.
    Database effects: none (pure).
    """
    return f"{SCHEDULE_LOCK_MATERIAL_PREFIX}{str(client_id).lower()}:{local_day.isoformat()}"


def acquire_schedule_day_lock(db: Session, client_id: uuid.UUID, local_day: date) -> None:
    """
    Purpose: THE serialization point for portal schedule mutation (SS2, D8).
        Must be called as the first schedule-mutation DB statement of the
        publish / block-all-open transaction - before ANY appointment_slots
        read or write for the operation.
    Inputs:  request session; authenticated tenant id; office-local day.
    Returns: nothing. The transaction now HOLDS the per-(tenant, day)
        advisory lock until commit/rollback releases it automatically.
    Database effects (PostgreSQL): SELECT pg_advisory_xact_lock(
        hashtextextended(:lock_material, 0)) - blocks until the lock is
        granted, serializing publish-vs-publish, publish-vs-bulk, and
        bulk-vs-bulk for the same tenant/day across all processes.
    Dialect seam (documented no-op): on non-PostgreSQL dialects (the SQLite
        pure harness) no statement is issued - SQLite's whole-database
        write lock provides equivalent serialization there, exactly like
        the frozen FOR UPDATE precedent in appointment_repository.
    Possible failures: database errors propagate (fail closed, Rule 16).
    """
    if db.get_bind().dialect.name != "postgresql":
        return
    material = build_schedule_lock_material(client_id, local_day)
    db.execute(
        sql_text(
            "SELECT pg_advisory_xact_lock(hashtextextended(:lock_material, 0))"
        ),
        {"lock_material": material},
    )


def classify_local_wall_time(naive_wall: datetime, timezone_name: str) -> WallTimeResult:
    """
    Purpose: THE single owner of DST wall-time classification (SS3, D9 -
        Correction C2). For a NAIVE local wall datetime:
          1. construct fold=0 and fold=1 candidates in the office timezone;
          2. convert each candidate to UTC and back;
          3. a candidate is VALID only if the round-trip reproduces EXACTLY
             the original naive wall components (Y/M/D h:m:s);
          4. 0 valid candidates  -> nonexistent (spring-forward gap);
          5. 2 valid candidates with DISTINCT UTC instants -> ambiguous
             (fall-back repeated interval);
          6. exactly 1 distinct valid UTC instant -> valid, that instant
             (the normal case: both folds round-trip to the SAME instant).
        fold is a detection input ONLY - it never silently resolves
        anything (no fold=0 preference exists anywhere).
    Inputs:  naive_wall - naive datetime (raises TypeError on aware input:
        an aware value here is an upstream bug, surfaced loudly);
        timezone_name - IANA name.
    Returns: WallTimeResult(status, utc_instant-or-None).
    Database effects: none (pure).
    Possible failures: unknown timezone raises via ZoneInfo (configuration
        bug - Rule 16); TypeError on aware input.
    """
    if naive_wall.tzinfo is not None:
        raise TypeError("classify_local_wall_time expects a NAIVE wall datetime")
    tz = ZoneInfo(timezone_name)
    wanted = (naive_wall.year, naive_wall.month, naive_wall.day,
              naive_wall.hour, naive_wall.minute, naive_wall.second)

    valid_utcs: List[datetime] = []
    for fold in (0, 1):
        candidate = naive_wall.replace(tzinfo=tz, fold=fold)
        utc_instant = candidate.astimezone(timezone.utc)
        back = utc_instant.astimezone(tz)
        if (back.year, back.month, back.day,
                back.hour, back.minute, back.second) == wanted:
            valid_utcs.append(utc_instant)

    distinct = sorted(set(valid_utcs))
    if len(distinct) == 0:
        return WallTimeResult(WALL_NONEXISTENT, None)
    if len(distinct) >= 2:
        return WallTimeResult(WALL_AMBIGUOUS, None)
    return WallTimeResult(WALL_VALID, distinct[0])


def _parse_hh_mm(raw: str, field_name: str) -> Optional[int]:
    """
    Purpose: Strict "HH:MM" parser -> minutes since local midnight.
    Returns: minutes, or None for ANY malformed value (wrong shape,
        non-digits, hour > 23, minute > 59). The caller turns None into the
        single 422 wording naming the field; nothing is ever guessed.
    Database effects: none (pure).
    """
    if not isinstance(raw, str):
        return None
    parts = raw.split(":")
    if len(parts) != 2 or len(parts[0]) != 2 or len(parts[1]) != 2:
        return None
    if not (parts[0].isdigit() and parts[1].isdigit()):
        return None
    hours, minutes = int(parts[0]), int(parts[1])
    if hours > 23 or minutes > 59:
        return None
    return hours * 60 + minutes


def expand_publish_slots(
    local_day: date,
    open_time: str,
    close_time: str,
    slot_minutes: int,
    timezone_name: str,
) -> "PublishResult | List[Tuple[datetime, datetime]]":
    """
    Purpose: THE pure expansion + validation owner for publish (SS5-B, D10).
        Expands [open_time, close_time) on local_day into consecutive
        slot_minutes slots, classifying EVERY boundary (each start and each
        end, i.e. every wall instant open + k*slot_minutes for k = 0..n)
        through classify_local_wall_time and converting each boundary to
        UTC independently.
    Inputs:  the office-local day, the raw "HH:MM" strings, slot_minutes,
        and the office timezone name.
    Returns: EITHER a PublishResult carrying the 422 refusal (ok=False,
        reason=PUBLISH_INVALID, caller-safe detail) OR the generated list of
        (start_utc, end_utc) pairs, soonest first.
    Database effects: none (pure).
    Refusals (whole request - zero slots are ever generated on failure):
        * malformed open_time / close_time;
        * slot_minutes outside [10, 240] or not divisible by 5;
        * close_time <= open_time;
        * (close - open) not an EXACT multiple of slot_minutes - a
          remainder is refused loudly, never silently discarded;
        * more than MAX_GENERATED_SLOTS slots (zero slots is impossible
          once the exact-multiple rule passes with close > open);
        * ANY boundary nonexistent or ambiguous per SS3.
    """
    open_minutes = _parse_hh_mm(open_time, "open_time")
    if open_minutes is None:
        return PublishResult(False, PUBLISH_INVALID,
                             detail="open_time must be HH:MM (00:00-23:59).")
    close_minutes = _parse_hh_mm(close_time, "close_time")
    if close_minutes is None:
        return PublishResult(False, PUBLISH_INVALID,
                             detail="close_time must be HH:MM (00:00-23:59).")
    if not isinstance(slot_minutes, int) or isinstance(slot_minutes, bool):
        return PublishResult(False, PUBLISH_INVALID,
                             detail="slot_minutes must be an integer.")
    if slot_minutes < SLOT_MINUTES_MIN or slot_minutes > SLOT_MINUTES_MAX:
        return PublishResult(
            False, PUBLISH_INVALID,
            detail=f"slot_minutes must be between {SLOT_MINUTES_MIN} and "
                   f"{SLOT_MINUTES_MAX}.")
    if slot_minutes % SLOT_MINUTES_STEP != 0:
        return PublishResult(
            False, PUBLISH_INVALID,
            detail=f"slot_minutes must be divisible by {SLOT_MINUTES_STEP}.")
    if close_minutes <= open_minutes:
        return PublishResult(False, PUBLISH_INVALID,
                             detail="close_time must be after open_time.")
    span = close_minutes - open_minutes
    if span % slot_minutes != 0:
        # Correction E: an inexact span is refused, never trimmed.
        return PublishResult(
            False, PUBLISH_INVALID,
            detail="close_time minus open_time must be an exact multiple of "
                   "slot_minutes; adjust the times or the slot length.")
    count = span // slot_minutes
    if count > MAX_GENERATED_SLOTS:
        return PublishResult(
            False, PUBLISH_INVALID,
            detail=f"This request would create {count} slots; the maximum "
                   f"per publish is {MAX_GENERATED_SLOTS}.")

    # Classify EVERY boundary wall time (starts and ends) - SS3/D9.
    boundaries_utc: List[datetime] = []
    for k in range(count + 1):
        total = open_minutes + k * slot_minutes
        naive_wall = datetime.combine(
            local_day, time(hour=total // 60, minute=total % 60))
        outcome = classify_local_wall_time(naive_wall, timezone_name)
        if outcome.status == WALL_NONEXISTENT:
            return PublishResult(
                False, PUBLISH_INVALID,
                detail=f"{naive_wall.strftime('%H:%M')} does not exist on "
                       f"{local_day.isoformat()} in the office timezone "
                       f"(daylight saving transition).")
        if outcome.status == WALL_AMBIGUOUS:
            return PublishResult(
                False, PUBLISH_INVALID,
                detail=f"{naive_wall.strftime('%H:%M')} occurs twice on "
                       f"{local_day.isoformat()} in the office timezone "
                       f"(daylight saving transition); choose times outside "
                       f"the repeated interval.")
        boundaries_utc.append(outcome.utc_instant)

    return [(boundaries_utc[k], boundaries_utc[k + 1]) for k in range(count)]


def find_first_overlap(
    generated: Sequence[Tuple[datetime, datetime]],
    existing_rows: Sequence[AppointmentSlot],
) -> Optional[AppointmentSlot]:
    """
    Purpose: The pure overlap rule (SS5-B). A generated [start, end) overlaps
        an existing row iff existing.start < gen.end AND existing.end >
        gen.start (half-open geometry). CANCELLED rows never block
        publication (they are audit history, not capacity); EVERY other
        status - available, held, booked, blocked - does.
    Returns: the first overlapping existing row, or None.
    Database effects: none (pure).
    """
    for row in existing_rows:
        if row.status == SlotStatus.CANCELLED:
            continue
        row_start = ensure_utc(row.start_datetime)
        row_end = ensure_utc(row.end_datetime)
        for gen_start, gen_end in generated:
            if row_start < gen_end and row_end > gen_start:
                return row
    return None


def publish_day_slots(
    db: Session,
    client_id: uuid.UUID,
    settings: CalendarSettings,
    local_day: date,
    open_time: str,
    close_time: str,
    slot_minutes: int,
) -> PublishResult:
    """
    Purpose: The publish transaction (SS5-B). Ordering is the contract's
        locked ordering: pure parsing/expansion FIRST (fail fast, no DB
        touched), then advisory day lock, THEN the overlap read, then the
        all-or-nothing insert, then one commit.
    Inputs:  session; AUTHENTICATED tenant id; the request-level settings
        snapshot (timezone source); the local day and the raw form values.
    Returns: PublishResult - ok with the created rows, or the 422/409
        refusal with zero inserts.
    Database effects: on success, N INSERTs via the repository owner
        (create_slot) committed together; on ANY refusal or error, rollback
        (which also releases the advisory lock).
    Possible failures: database errors roll back and propagate (Rule 16).
    """
    expansion = expand_publish_slots(
        local_day, open_time, close_time, slot_minutes, settings.timezone_name
    )
    if isinstance(expansion, PublishResult):
        return expansion  # pure 422 refusal - no DB statement was issued

    try:
        # SS2: the FIRST schedule-mutation DB statement - before any
        # appointment_slots read for this operation.
        acquire_schedule_day_lock(db, client_id, local_day)

        day_start_utc, day_end_utc = local_day_utc_window(
            local_day, settings.timezone_name)
        existing = appointment_repository.list_slots_between(
            db, client_id, day_start_utc, day_end_utc)
        collision = find_first_overlap(expansion, existing)
        if collision is not None:
            db.rollback()  # zero inserts; releases the advisory lock
            return PublishResult(False, PUBLISH_OVERLAP, detail=OVERLAP_DETAIL)

        created: List[AppointmentSlot] = []
        for start_utc, end_utc in expansion:
            # Published slots are GENERIC (D5): provider_name and
            # service_key stay NULL - service-specific schedules are out of
            # P4-A scope.
            created.append(appointment_repository.create_slot(
                db, client_id, start_utc, end_utc,
                provider_name=None, service_key=None))
        db.commit()
        return PublishResult(True, PUBLISH_OK, slots=created)
    except Exception:
        db.rollback()
        raise


def block_all_open(
    db: Session,
    client_id: uuid.UUID,
    settings: CalendarSettings,
    local_day: date,
) -> BulkBlockResult:
    """
    Purpose: The bulk "Block all open slots" transaction (SS5-E, D3/D4).
        NEVER a "closure": it blocks the rows existing at execution time
        and cannot prevent later publication - durable closures are P4-B.
    Rule: advisory day lock FIRST (SS2), then lock every slot row for the
        local day (get_slots_for_update_between), then apply the ONE shared
        block rule (slot_management_service.apply_block) to every
        `available` row and every `held` row - active or expired: the bulk
        block outranks holds, and the affected patient's finalize fails
        safe as hold_lost on the frozen path (D4). `booked`, `blocked`,
        and `cancelled` rows are left byte-untouched. One commit.
    Returns: BulkBlockResult(blocked_count, booked_remaining) where
        booked_remaining is the (start_utc, end_utc) list of the day's
        still-booked slots, soonest first - times only, no patient data -
        so staff SEES that appointments still stand (Rule 16). Idempotent:
        repeating yields blocked_count 0.
    Database effects: one locked transaction; rollback on error releases
        both the row locks and the advisory lock.
    Possible failures: database errors roll back and propagate (Rule 16).
    """
    try:
        # SS2: first schedule-mutation DB statement.
        acquire_schedule_day_lock(db, client_id, local_day)

        day_start_utc, day_end_utc = local_day_utc_window(
            local_day, settings.timezone_name)
        rows = appointment_repository.get_slots_for_update_between(
            db, client_id, day_start_utc, day_end_utc)

        blocked_count = 0
        booked_remaining: List[Tuple[datetime, datetime]] = []
        for row in rows:
            if row.status in (SlotStatus.AVAILABLE, SlotStatus.HELD):
                apply_block(row)
                blocked_count += 1
            elif row.status == SlotStatus.BOOKED:
                booked_remaining.append(
                    (ensure_utc(row.start_datetime), ensure_utc(row.end_datetime)))
            # blocked / cancelled: byte-untouched.
        db.commit()
        booked_remaining.sort(key=lambda pair: pair[0])
        return BulkBlockResult(blocked_count, booked_remaining)
    except Exception:
        db.rollback()
        raise
