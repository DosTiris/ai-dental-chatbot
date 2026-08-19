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
from typing import Callable, List, Optional, Sequence, Tuple
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
# Slice 4D-A: ALSO imported as a MODULE so client_now resolves through the
# settings-service attribute at CALL TIME (the frozen P3-C seam rule the
# routes already follow): a test substituting
# calendar_settings_service.client_now is genuinely observed by the one-off
# strictly-future rule below. The names imported above stay untouched.
from app.services import calendar_settings_service
# Slice 4D-B: the OPERATIONAL closed-day owner (settings.calendar.closed_days
# parsing/membership/add/remove + the surgical write). Imported as a module;
# acyclic by design (closure_authority imports no service module).
from app.services import closure_authority
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
PUBLISH_CLOSED_DAY = "closed_day"   # -> HTTP 409; day operationally closed
                                    #    (Slice 4D-B; zero inserts; judged
                                    #    FRESH under the day advisory lock)

# The ONE 409 wording for every overlap refusal (probe-resistant: it never
# reveals WHICH existing slot or status collided).
OVERLAP_DETAIL = (
    "One or more requested slots overlap existing slots on that day."
)

# The ONE 409 wording for every operationally-closed-day creation refusal
# (Slice 4D-B). Same caller-safe register as OVERLAP_DETAIL: it names the
# state and the remedy, never internals.
CLOSED_DAY_DETAIL = (
    "That day is marked closed for the office. Reopen the day before "
    "adding availability."
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
    *,
    under_lock_check: Optional[Callable[[], Optional[PublishResult]]] = None,
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
    Under-lock seam (SLICE 4D-A v1.0.1, audit F1): under_lock_check is an
        OPTIONAL caller-supplied revalidation invoked exactly once, AFTER
        acquire_schedule_day_lock succeeds and BEFORE the overlap read.
        Returning a PublishResult refuses the whole transaction (rollback,
        zero inserts, lock released); returning None proceeds unchanged.
        Time-of-check/time-of-use closure: a request can WAIT on the
        advisory lock, so any eligibility judged before the lock can be
        stale by the time the lock is held - this seam is where a caller
        re-judges it at the authoritative point. Default None: the
        /publish route and every pre-4D-A caller pass nothing and keep
        byte-identical behavior.
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

        # SLICE 4D-B: the OPERATIONAL closed-day judgment - the single
        # authoritative creation gate for normal publish, the 4D-A one-off,
        # and any recurring-Apply materialization that reaches this owner.
        # Judged FIRST (authoritative state outranks caller revalidations),
        # UNDER the lock (a creator that waited here while a Close Day
        # committed must see that closure - the anti-TOCTOU shape), and
        # from a FRESH row read (never the request-time settings snapshot,
        # which can be stale). Consults closed_days ONLY - the recurring
        # closures list stays Apply-time configuration per the frozen P4-B
        # contract.
        fresh_settings = closure_authority.read_settings_fresh(db, client_id)
        if closure_authority.is_date_closed(fresh_settings, local_day):
            db.rollback()  # zero inserts; releases the advisory lock
            return PublishResult(False, PUBLISH_CLOSED_DAY,
                                 detail=CLOSED_DAY_DETAIL)

        # SLICE 4D-A v1.0.1 (audit F1): the under-lock revalidation runs
        # HERE - the lock is held, nothing has been read or written for
        # this operation yet, so a refusal provably inserts nothing and
        # the rollback releases the lock for the next waiter.
        if under_lock_check is not None:
            under_lock_refusal = under_lock_check()
            if under_lock_refusal is not None:
                db.rollback()  # zero inserts; releases the advisory lock
                return under_lock_refusal

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
        result = _block_all_open_locked(db, client_id, settings, local_day)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


def _block_all_open_locked(
    db: Session,
    client_id: uuid.UUID,
    settings: CalendarSettings,
    local_day: date,
) -> BulkBlockResult:
    """
    Purpose (Slice 4D-B extraction - approved GO shape): the ONE bulk
        block-transition body, shared VERBATIM by the public block_all_open
        above and by close_day below, so exactly one row-transition engine
        exists. Byte-equivalent to the pre-4D-B block_all_open interior: row
        locks via get_slots_for_update_between, available/held -> blocked
        through the ONE shared apply_block rule (bulk block outranks holds -
        D4), booked rows collected untouched (times only), blocked/cancelled
        rows byte-untouched.
    Locking contract: the caller ALREADY HOLDS the tenant/day advisory lock
        and OWNS the transaction - this helper acquires no advisory lock and
        never commits or rolls back, which is exactly what lets close_day be
        one atomic transaction with no nested advisory acquisition.
    Returns: BulkBlockResult(blocked_count, booked_remaining) with the
        booked windows sorted soonest-first (Rule 16: staff SEES that
        appointments still stand).
    Database effects: SELECT ... FOR UPDATE on the day's slot rows plus the
        in-place status transitions. Commit/rollback belong to the caller.
    """
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
    booked_remaining.sort(key=lambda pair: pair[0])
    return BulkBlockResult(blocked_count, booked_remaining)


# ---------------------------------------------------------------------------
# PHASE 3A SLICE 4D-A - Calendar-native ONE-OFF availability
# ---------------------------------------------------------------------------
# One narrow addition, NO second engine: create_one_off_slot below is a thin
# composition over the P4-A owners this module already contains. It
# prevalidates the CALLER-FACING fields (start_time / duration_minutes) with
# the SAME shared helpers and named limits publish uses, applies the two
# rules that are new in 4D-A (the strictly-future rule and the same-local-day
# rule), and then delegates WHOLESALE to publish_day_slots - the same
# advisory day lock, the same SS3 DST classification and refusal wording,
# the same overlap universe and OVERLAP_DETAIL, the same repository insert
# owner (create_slot), and the same single commit. A one-off slot is simply
# a published window whose span equals its slot_minutes, so exactly one row
# is generated by construction.
#
# STRICTLY-FUTURE RULE (4D-A contract): availability may never be created in
# the past, and start == now is refused too (start must be STRICTLY greater
# than the authoritative clock). The effective "now" is SERVER-authoritative
# only: calendar_settings_service.client_now(settings) - the server clock in
# the office timezone - resolved through the module attribute at call time
# (the frozen seam). No browser- or request-supplied current time is read,
# accepted, or trusted anywhere on this path. This mirrors the frozen
# finalize_staff_booking boundary ("start not strictly in the future" is
# refused as slot_started), so creation and booking speak ONE time rule.
# The comparison happens ONLY for a start classify_local_wall_time judges
# WALL_VALID; nonexistent/ambiguous DST starts fall through to
# publish_day_slots so the SS3 refusal vocabulary keeps its single owner.
#
# TWO-PHASE ENFORCEMENT (v1.0.1, audit F1): the pre-lock check above is a
# FAST rejection only - a request can WAIT on the tenant/day advisory lock
# and cross its requested start while waiting, so eligibility judged before
# the lock can be stale by the time the lock is held. The rule is therefore
# re-judged at the AUTHORITATIVE point through publish_day_slots'
# under_lock_check seam: a fresh client_now read AFTER the lock is acquired
# and BEFORE any overlap read or INSERT. requested_start <= fresh now there
# refuses with the SAME ONE_OFF_PAST_DETAIL, rollback, zero inserts. Same
# classification, same clock authority, no second availability engine.
#
# SAME-LOCAL-DAY RULE (4D-A scope decision): the one-off window must END on
# the SAME office-local day. A window that crosses local midnight - or ends
# exactly AT midnight, which the publish vocabulary cannot express (close
# times are capped at 23:59 by _parse_hh_mm) - is refused loudly rather than
# broadening the frozen publish behavior. Real dental one-offs do not
# straddle midnight; widening this is a future contract change, not a silent
# expansion here.
#
# TENANCY: client_id arrives ALREADY RESOLVED from the verified
# PortalIdentity (the route layer) - this function accepts no tenant
# selector from any request body, and every DB touch below happens inside
# publish_day_slots' tenant-scoped advisory lock + tenant-filtered overlap
# read + tenant-owned insert.

# Named 4D-A refusal wording, each in one reviewable place (Rule 4).
ONE_OFF_PAST_DETAIL = "The availability start time must be in the future."
ONE_OFF_SAME_DAY_DETAIL = (
    "The availability window must end on the same day; choose an earlier "
    "start time or a shorter duration."
)


def create_one_off_slot(
    db: Session,
    client_id: uuid.UUID,
    settings: CalendarSettings,
    local_day: date,
    start_time: str,
    duration_minutes: int,
) -> PublishResult:
    """
    Purpose: Create ONE one-off available slot on an office-local day (the
        Slice 4D-A Calendar affordance) by composing the existing P4-A
        publish machinery - never by inserting an appointment row into free
        calendar space and never through a second availability engine.
    Inputs:  session; AUTHENTICATED tenant id (resolved by the route from
        the verified portal identity - never from a request body); the
        request-level settings snapshot (timezone + clock source); the
        office-local day; the raw "HH:MM" start; the slot length in minutes.
    Returns: PublishResult - ok with EXACTLY ONE created row, or the 422/409
        refusal with zero inserts. Reuses the closed PUBLISH_* vocabulary.
    Database effects: none on any prevalidation refusal (pure checks only);
        none on the under-lock strictly-future refusal (the lock is taken
        and rolled back - zero inserts); otherwise exactly
        publish_day_slots' effects - advisory day lock, one tenant-scoped
        overlap read, at most one INSERT, one commit, and rollback on any
        refusal or error.
    Possible failures (caller-safe 422 wording in the CALLER'S field
        vocabulary): malformed start_time; duration_minutes outside
        [SLOT_MINUTES_MIN, SLOT_MINUTES_MAX] or not divisible by
        SLOT_MINUTES_STEP; window not ending on the same local day; start
        not STRICTLY in the future (start == now is refused); the SS3
        nonexistent/ambiguous DST refusals (delegated, publish wording);
        PUBLISH_OVERLAP (409, zero inserts) exactly per the frozen overlap
        rule. Database errors roll back and propagate (Rule 16).
    """
    # --- Caller-facing prevalidation: SAME helpers, SAME named limits, the
    # --- caller's OWN field names in every refusal (recon decision).
    start_minutes = _parse_hh_mm(start_time, "start_time")
    if start_minutes is None:
        return PublishResult(False, PUBLISH_INVALID,
                             detail="start_time must be HH:MM (00:00-23:59).")
    if not isinstance(duration_minutes, int) or isinstance(duration_minutes,
                                                           bool):
        return PublishResult(False, PUBLISH_INVALID,
                             detail="duration_minutes must be an integer.")
    if (duration_minutes < SLOT_MINUTES_MIN
            or duration_minutes > SLOT_MINUTES_MAX):
        return PublishResult(
            False, PUBLISH_INVALID,
            detail=f"duration_minutes must be between {SLOT_MINUTES_MIN} "
                   f"and {SLOT_MINUTES_MAX}.")
    if duration_minutes % SLOT_MINUTES_STEP != 0:
        return PublishResult(
            False, PUBLISH_INVALID,
            detail=f"duration_minutes must be divisible by "
                   f"{SLOT_MINUTES_STEP}.")

    # --- Same-local-day rule: the window must end at 23:59 or earlier on
    # --- the SAME day (see the block comment above for why exactly-at-
    # --- midnight is refused too).
    end_minutes = start_minutes + duration_minutes
    if end_minutes >= 24 * 60:
        return PublishResult(False, PUBLISH_INVALID,
                             detail=ONE_OFF_SAME_DAY_DETAIL)

    # --- Strictly-future rule, PHASE 1 (fast pre-lock rejection): classify
    # --- the START through the ONE SS3 owner (never a naive local
    # --- comparison), then compare the resulting UTC instant with the
    # --- SERVER-authoritative office clock. A nonexistent or ambiguous
    # --- start is NOT judged here - publish_day_slots refuses it with the
    # --- frozen SS3 wording (single refusal vocabulary). This phase alone
    # --- is NOT sufficient (audit F1): the request may still WAIT on the
    # --- advisory lock past its own start time - phase 2 below closes that.
    naive_start = datetime.combine(
        local_day, time(hour=start_minutes // 60, minute=start_minutes % 60))
    start_outcome = classify_local_wall_time(naive_start,
                                             settings.timezone_name)
    under_lock_check = None
    if start_outcome.status == WALL_VALID:
        now_utc = ensure_utc(calendar_settings_service.client_now(settings))
        if start_outcome.utc_instant <= now_utc:
            return PublishResult(False, PUBLISH_INVALID,
                                 detail=ONE_OFF_PAST_DETAIL)

        # --- Strictly-future rule, PHASE 2 (the AUTHORITATIVE judgment):
        # --- re-read the server clock through the SAME module seam AFTER
        # --- publish_day_slots holds the tenant/day advisory lock and
        # --- BEFORE its overlap read or INSERT. The classified start
        # --- instant is captured here; only "now" is fresh - DST handling
        # --- is unchanged. start <= fresh now refuses with the SAME named
        # --- detail; publish rolls back (zero inserts, lock released).
        start_utc = start_outcome.utc_instant

        def _one_off_still_future_under_lock():
            """Under-lock revalidation (v1.0.1, audit F1): the strictly-
            future rule judged at the point that actually matters - the
            lock is held, so no more waiting can stale this answer before
            the INSERT. Returns the refusal PublishResult, or None to
            proceed. Reads ONLY the server-authoritative office clock."""
            fresh_now_utc = ensure_utc(
                calendar_settings_service.client_now(settings))
            if start_utc <= fresh_now_utc:
                return PublishResult(False, PUBLISH_INVALID,
                                     detail=ONE_OFF_PAST_DETAIL)
            return None

        under_lock_check = _one_off_still_future_under_lock

    # --- Wholesale delegation: a window whose span equals slot_minutes
    # --- expands to EXACTLY ONE slot. Times are re-serialized canonically
    # --- ("HH:MM") so publish re-parses the same instants this function
    # --- validated. The under-lock revalidation travels WITH the request
    # --- into the one locked transaction (None for a non-WALL_VALID start,
    # --- which publish refuses on its own wording before any DB read).
    canonical_start = f"{start_minutes // 60:02d}:{start_minutes % 60:02d}"
    canonical_close = f"{end_minutes // 60:02d}:{end_minutes % 60:02d}"
    return publish_day_slots(
        db, client_id, settings, local_day,
        canonical_start, canonical_close, duration_minutes,
        under_lock_check=under_lock_check,
    )


# ---------------------------------------------------------------------------
# PHASE 3A SLICE 4D-B - Calendar-native CLOSE DAY / REOPEN DAY
# ---------------------------------------------------------------------------
# The receptionist marks an office-local date operationally closed WITHOUT a
# fake appointment: close_day persists the date into the LIVE authority
# (settings.calendar.closed_days, owned by closure_authority) AND blocks the
# date's current open/held inventory through the ONE shared bulk transition
# (_block_all_open_locked) - in ONE transaction, ONE commit, under the SAME
# tenant/day advisory lock every inventory mutation uses. Booked appointments
# are never touched: they are collected and returned so staff SEES them
# (Rule 16). reopen_day removes the live restriction ONLY - existing blocked
# rows carry no provenance (manual block vs closure block is
# indistinguishable by design of the frozen slot model), so reopening never
# unblocks anything; recovery is explicit (per-slot Unblock, one-off
# availability, or publish - all of which the reopened date now accepts).
#
# LOCK ORDER (documented once, obeyed everywhere): tenant/day ADVISORY lock
# FIRST, clients ROW lock SECOND. publish_day_slots' closed-day gate takes
# only the advisory lock plus a plain fresh read (same-day serialization
# comes from the advisory lock both sides hold), so no path anywhere takes
# the row lock before the advisory lock - no deadlock ordering exists.
#
# CLOSE-DAY DATE RULE: today or a future office-local date; a genuinely past
# date refuses server-side (browser min= is assistance only). "Today" is the
# SERVER-authoritative office clock (calendar_settings_service.client_now
# through the module seam), judged under the lock. Closing today blocks
# whatever open/held inventory still exists; booked rows remain booked.
#
# DISTINCT FROM RECURRING CLOSURES (approved ruling): the P4-B list
# settings.calendar.recurring.closures stays desired CONFIGURATION,
# materialized only by Preview/Apply. close_day/reopen_day never read or
# write it - except reopen_day's read-only informational flag telling staff
# a future Apply may close the date again.

# Named 4D-B refusal wording (Rule 4).
CLOSE_DAY_PAST_DETAIL = "Only today or a future day can be closed."

# Closed close/reopen outcome vocabulary (the PublishResult convention).
CLOSE_OK = "ok"
CLOSE_INVALID = "invalid"           # -> HTTP 422; detail says which rule


@dataclass
class CloseDayResult:
    """The close_day outcome: ok/refusal plus - on success - whether the day
    was ALREADY closed (idempotent path), how many open/held rows this call
    blocked, and the day's still-booked windows soonest-first (times only,
    never patient data)."""
    ok: bool
    reason: str
    detail: Optional[str] = None
    already_closed: bool = False
    blocked_count: int = 0
    booked_remaining: List[Tuple[datetime, datetime]] = field(
        default_factory=list)


@dataclass
class ReopenDayResult:
    """The reopen_day outcome: whether the date was actually closed
    (idempotent remove), and whether the SAME date is also configured in the
    recurring closures list - informational only, so the office learns a
    future Recurring Apply may close the date again."""
    ok: bool
    reason: str
    detail: Optional[str] = None
    was_closed: bool = False
    recurring_configured: bool = False


def close_day(
    db: Session,
    client_id: uuid.UUID,
    settings: CalendarSettings,
    local_day: date,
) -> CloseDayResult:
    """
    Purpose: Mark an office-local date operationally closed - the durable
        closed_days entry AND the bulk block of its current open/held
        inventory - ATOMICALLY (one transaction, one commit; the 4D-B GO's
        mandatory shape). Idempotent: closing an already-closed day re-runs
        only the block core (which heals any open rows) and reports
        already_closed=True.
    Inputs:  session; AUTHENTICATED tenant id (route-resolved from the
        verified portal identity - never a request body); the request-level
        settings snapshot (timezone/clock source); the office-local date.
    Returns: CloseDayResult. Refusals carry CLOSE_INVALID + caller-safe
        wording and mutate NOTHING (rollback releases both locks).
    Database effects: advisory day lock; clients row lock (FOR UPDATE);
        at most one surgical closed_days jsonb write; the day's slot-row
        locks + available/held->blocked transitions; ONE commit. Any error
        rolls back the COMBINED mutation - no partially-closed state exists.
    Possible failures: past office-local date (CLOSE_DAY_PAST_DETAIL);
        closed-days cap (closure_authority.CLOSED_DAYS_CAP_DETAIL);
        vanished tenant row (loud RuntimeError); database errors propagate
        after rollback (Rule 16).
    """
    try:
        # Documented lock order: ADVISORY FIRST...
        acquire_schedule_day_lock(db, client_id, local_day)
        # ...clients ROW lock SECOND - and the settings read is FRESH under
        # that lock (never the request-time snapshot). A legacy NULL
        # settings value is legal here; a VANISHED row raises loudly inside
        # the owner (never guessed).
        fresh_settings = closure_authority.lock_settings_for_update(
            db, client_id)

        # Server-authoritative office-local date rule, judged UNDER the
        # lock: today may be closed; a genuinely past date may not.
        today_local = calendar_settings_service.client_now(settings).date()
        if local_day < today_local:
            db.rollback()  # releases both locks; zero mutation
            return CloseDayResult(False, CLOSE_INVALID,
                                  detail=CLOSE_DAY_PAST_DETAIL)

        closed = closure_authority.read_closed_days(fresh_settings)
        try:
            new_closed, already_closed = closure_authority.add_closed_day(
                closed, local_day)
        except closure_authority.ClosedDaysCapError as cap:
            db.rollback()  # zero mutation
            return CloseDayResult(False, CLOSE_INVALID, detail=str(cap))

        if not already_closed:
            closure_authority.write_closed_days_locked(
                db, client_id, new_closed)

        # The ONE shared bulk transition (no second engine): open/held ->
        # blocked, booked collected untouched. Runs even on the idempotent
        # path so a retried Close heals any rows a failed attempt left open.
        block = _block_all_open_locked(db, client_id, settings, local_day)

        db.commit()  # the SINGLE commit - closure + inventory together
        return CloseDayResult(True, CLOSE_OK,
                              already_closed=already_closed,
                              blocked_count=block.blocked_count,
                              booked_remaining=block.booked_remaining)
    except Exception:
        db.rollback()  # the COMBINED mutation rolls back as one
        raise


def reopen_day(
    db: Session,
    client_id: uuid.UUID,
    settings: CalendarSettings,
    local_day: date,
) -> ReopenDayResult:
    """
    Purpose: Remove the operational closed_days restriction for an
        office-local date - and NOTHING else. No slot row is read, locked,
        or mutated: blocked rows have no provenance, so unblocking is never
        inferred (the approved Reopen ruling). The date becomes eligible
        for creation again (publish / one-off / recurring Apply); existing
        blocked windows remain blocked until staff explicitly unblocks or
        adds non-conflicting availability.
    Returns: ReopenDayResult with was_closed (idempotent remove) and
        recurring_configured - True when the SAME date is also configured
        in the recurring closures list, so the caller can warn that a
        future Recurring Apply may close it again (informational only; the
        recurring configuration is never modified).
    Database effects: advisory day lock; clients row lock; at most one
        surgical closed_days write; ONE commit. ZERO appointment_slots
        statements of any kind.
    Possible failures: vanished tenant row (loud RuntimeError); database
        errors propagate after rollback (Rule 16).
    """
    try:
        acquire_schedule_day_lock(db, client_id, local_day)   # advisory FIRST
        fresh_settings = closure_authority.lock_settings_for_update(
            db, client_id)                                    # row SECOND
        closed = closure_authority.read_closed_days(fresh_settings)
        new_closed, was_closed = closure_authority.remove_closed_day(
            closed, local_day)
        recurring_configured = closure_authority.date_in_recurring_closures(
            fresh_settings, local_day)
        if was_closed:
            closure_authority.write_closed_days_locked(
                db, client_id, new_closed)
        db.commit()
        return ReopenDayResult(True, CLOSE_OK, was_closed=was_closed,
                               recurring_configured=recurring_configured)
    except Exception:
        db.rollback()
        raise
