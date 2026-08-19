# app/services/closure_authority.py
#
# OWNER OF: the OPERATIONAL one-date office closure state (Slice 4D-B).
#
# Persisted state: clients.settings["calendar"]["closed_days"] - a sorted,
# duplicate-free JSON list of ISO office-local calendar dates. Its meaning
# is unambiguous BY CONSTRUCTION: a date present in this list is
# operationally closed RIGHT NOW - Calendar renders "Office closed" from it,
# and the ONE publication gate (portal_schedule_service.publish_day_slots)
# refuses to create inventory on it, judged fresh under the tenant/day
# advisory lock.
#
# DISTINCT CONCEPT (approved 4D-B ruling - never conflate): the P4-B list
# settings.calendar.recurring.closures is DESIRED recurring CONFIGURATION
# whose effect is materialized only by Preview/Apply under the frozen P4-B
# contract ("PUT saves config ONLY", "appointment_slots remains the SOLE
# bookability authority" for existing inventory). Saving a recurring closure
# has NO operational effect here; this module never reads it except through
# the one explicit read-only helper date_in_recurring_closures, which exists
# solely so Reopen can WARN that a future Recurring Apply may close the date
# again. This module never mutates the recurring block, the CAS token, or
# any other settings sibling.
#
# SCOPE (Rule 3, deliberately narrow): tolerant parsing of closed_days,
# office-local date membership, bounded idempotent add, remove, and the
# SURGICAL serialization back into settings. Nothing else. It imports NO
# other service module (acyclic by design: portal_schedule_service imports
# THIS module; the recurring service is untouched by it).
#
# READ TOLERANCE / WRITE STRICTNESS (the settings-read convention): reads
# skip malformed entries silently - a garbage entry can neither close nor
# open a day it does not validly name - while every WRITE re-serializes the
# whole list normalized (sorted, duplicate-free, ISO strings only), so a
# malformed stored list heals on the first mutation instead of propagating.
#
# LOCKING CONTRACT (documented order, 4D-B GO): callers that mutate hold the
# tenant/day advisory lock FIRST, then take the clients row lock via
# lock_settings_for_update SECOND. This module acquires NO advisory lock and
# performs NO commit/rollback - transaction ownership stays with the calling
# service (portal_schedule_service.close_day / reopen_day), which is what
# makes Close Day a single atomic transaction.

import json
import re
from datetime import date, timedelta
from typing import Any, List, Tuple

from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

# --- Named bounds and wording (Rule 4) -------------------------------------

# The closed-days list length cap. Mirrors the P4-B MAX_CLOSURES bound in
# spirit but is a SEPARATE named constant: the two lists are different
# concepts with independent limits.
MAX_CLOSED_DAYS = 100


class ClosedDaysCapError(Exception):
    """Raised by add_closed_day when the bounded list is full. Carries the
    caller-safe wording; the service maps it to a 422-style refusal with
    ZERO mutation (the transaction rolls back)."""


CLOSED_DAYS_CAP_DETAIL = (
    f"At most {MAX_CLOSED_DAYS} days may be marked closed at once; "
    "reopen a day before closing another."
)

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_iso_date(value: Any):
    """One strict ISO-calendar-date parser for THIS list: exactly
    YYYY-MM-DD and a real calendar date, else None (tolerant-read rule)."""
    if not isinstance(value, str) or not _ISO_DATE_RE.match(value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Pure parsing / membership (no session, no side effects)
# ---------------------------------------------------------------------------

def read_closed_days(settings: Any) -> List[date]:
    """
    Purpose: The tolerant read of settings.calendar.closed_days.
    Inputs:  the settings JSON value exactly as persisted (any shape).
    Returns: sorted, duplicate-free list of date objects. A missing key, a
        non-list, or any malformed entry contributes NOTHING (fail-safe: a
        garbage entry can neither close nor open a validly named day).
    Database effects: none (pure).
    """
    if not isinstance(settings, dict):
        return []
    calendar = settings.get("calendar")
    if not isinstance(calendar, dict):
        return []
    raw = calendar.get("closed_days")
    if not isinstance(raw, list):
        return []
    parsed = set()
    for entry in raw:
        parsed_date = _parse_iso_date(entry)
        if parsed_date is not None:
            parsed.add(parsed_date)
    return sorted(parsed)


def is_date_closed(settings: Any, day: date) -> bool:
    """Membership: is the office-local date operationally closed?"""
    return day in read_closed_days(settings)


def closed_days_in_range(settings: Any, start_day: date,
                         end_day: date) -> List[date]:
    """The operationally closed dates inside [start_day, end_day] inclusive,
    sorted - the Calendar read-model slice (never derived from slot rows,
    never from recurring configuration)."""
    return [d for d in read_closed_days(settings)
            if start_day <= d <= end_day]


def add_closed_day(closed: List[date], day: date) -> Tuple[List[date], bool]:
    """
    Purpose: Bounded, idempotent add.
    Returns: (new sorted list, already_closed). Idempotent: adding a present
        day returns the same membership with already_closed=True.
    Possible failures: ClosedDaysCapError when the list is full and the day
        is new (the caller refuses loudly with CLOSED_DAYS_CAP_DETAIL and
        rolls back - Rule 4/16, no silent truncation).
    Database effects: none (pure).
    """
    if day in closed:
        return sorted(closed), True
    if len(closed) >= MAX_CLOSED_DAYS:
        raise ClosedDaysCapError(CLOSED_DAYS_CAP_DETAIL)
    return sorted(list(closed) + [day]), False


def remove_closed_day(closed: List[date],
                      day: date) -> Tuple[List[date], bool]:
    """Idempotent remove. Returns (new sorted list, was_closed)."""
    if day not in closed:
        return sorted(closed), False
    return sorted(d for d in closed if d != day), True


def date_in_recurring_closures(settings: Any, day: date) -> bool:
    """
    Purpose: READ-ONLY informational helper for Reopen (approved scope):
        is the date ALSO configured in the P4-B recurring closures list -
        as a single {date} entry or inside an inclusive {start,end} range?
        Used ONLY to warn that a future Recurring Apply may close the date
        again. Tolerant read; NEVER treats configured closures as live
        state, NEVER mutates them (the frozen Save/Preview/Apply contract).
    Database effects: none (pure).
    """
    if not isinstance(settings, dict):
        return False
    calendar = settings.get("calendar")
    if not isinstance(calendar, dict):
        return False
    recurring = calendar.get("recurring")
    if not isinstance(recurring, dict):
        return False
    closures = recurring.get("closures")
    if not isinstance(closures, list):
        return False
    for entry in closures:
        if not isinstance(entry, dict):
            continue
        if set(entry.keys()) == {"date"}:
            single = _parse_iso_date(entry.get("date"))
            if single is not None and single == day:
                return True
        elif set(entry.keys()) == {"start", "end"}:
            start = _parse_iso_date(entry.get("start"))
            end = _parse_iso_date(entry.get("end"))
            if start is not None and end is not None and start <= day <= end:
                return True
    return False


# ---------------------------------------------------------------------------
# Persistence primitives (no advisory lock, no commit - the caller owns both)
# ---------------------------------------------------------------------------

_SELECT_SETTINGS_SQL = sql_text(
    "SELECT settings FROM clients WHERE id = :client_id"
)

_SELECT_SETTINGS_FOR_UPDATE_SQL = sql_text(
    "SELECT settings FROM clients WHERE id = :client_id FOR UPDATE"
)

# The SURGICAL write (the proven P4-B _CAS_UPDATE_SQL jsonb_set shape, minus
# the token - by design: closed_days has NO CAS interplay with the recurring
# config). Only {calendar,closed_days} is replaced; calendar.recurring,
# calendar.booking_enabled, notification configuration, and every other
# sibling are preserved byte-for-byte - pinned by PostgreSQL tests.
_CLOSED_DAYS_UPDATE_SQL = sql_text(
    """
    UPDATE clients
    SET settings = jsonb_set(
                     jsonb_set(coalesce(settings, '{}'::jsonb),
                               '{calendar}',
                               CASE
                                 WHEN settings -> 'calendar' IS NULL THEN '{}'::jsonb
                                 WHEN jsonb_typeof(settings -> 'calendar') = 'null' THEN '{}'::jsonb
                                 ELSE settings -> 'calendar'
                               END,
                               true),
                     '{calendar,closed_days}',
                     CAST(:closed_days_json AS jsonb),
                     true)
    WHERE id = :client_id
      AND (settings IS NULL OR jsonb_typeof(settings) = 'object')
    """
)


def read_settings_fresh(db: Session, client_id) -> Any:
    """
    Purpose: The FRESH persisted settings value, straight from the row -
        never a request-time ORM snapshot (the 4D-B GO's staleness rule for
        the under-lock closed-day judgment).
    Database effects: one SELECT. No lock (the caller's advisory day lock
        is the serialization for same-day judgments).
    """
    row = db.execute(_SELECT_SETTINGS_SQL,
                     {"client_id": str(client_id)}).fetchone()
    return row[0] if row is not None else None


def lock_settings_for_update(db: Session, client_id) -> Any:
    """
    Purpose: Fresh settings under the clients ROW lock - the SECOND lock in
        the documented order (tenant/day advisory lock FIRST, clients row
        lock SECOND; never the reverse anywhere).
    Database effects: SELECT ... FOR UPDATE; held until the caller's single
        commit/rollback.
    Returns: the settings value, which MAY legally be None (a legacy NULL
        settings row - the surgical write coalesces it to an object).
    Possible failures: RuntimeError when the clients ROW itself is missing
        (vanished tenant) - fail loud, never guess (Rule 16).
    """
    row = db.execute(_SELECT_SETTINGS_FOR_UPDATE_SQL,
                     {"client_id": str(client_id)}).fetchone()
    if row is None:
        raise RuntimeError(
            "clients row vanished while locking settings - refusing to "
            "guess.")
    return row[0]


def write_closed_days_locked(db: Session, client_id,
                             closed: List[date]) -> None:
    """
    Purpose: Serialize the normalized list back into settings with the ONE
        surgical jsonb_set write above. STRICT on mutation: always writes
        the sorted, duplicate-free ISO form (a malformed stored list heals
        here). No commit - the calling transaction owns atomicity.
    Possible failures: RuntimeError when the guarded UPDATE matched no row
        (vanished tenant or non-object settings) - fail loud, caller rolls
        back.
    """
    normalized = sorted({d.isoformat() for d in closed})
    result = db.execute(_CLOSED_DAYS_UPDATE_SQL, {
        "closed_days_json": json.dumps(normalized),
        "client_id": str(client_id),
    })
    if result.rowcount != 1:
        raise RuntimeError(
            "closed_days write matched no clients row (vanished tenant or "
            "non-object settings) - refusing to guess.")
