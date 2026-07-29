# app/services/calendar_settings_service.py
#
# OWNER OF: reading calendar configuration for a client.
#
# Rule 4 (No Hidden Behavior): every tunable number in the calendar system is
# named here, has a documented default, and is overridable per client through
# clients.settings JSONB under the "calendar" key. No magic values are buried
# inside booking or availability functions.
#
# Example clients.settings in Supabase:
# {
#   "timezone": "America/New_York",
#   "calendar": {
#     "booking_enabled": true,
#     "hold_minutes": 5,
#     "minimum_notice_minutes": 60,
#     "max_offered_slots": 3,
#     "max_booking_days": 30,
#     "require_staff_confirmation": true
#   }
# }
#
# BOOLEAN SETTINGS ARE STRICT (Patch 2A — Senior Audit Critical #6):
# booking_enabled and require_staff_confirmation accept ONLY JSON booleans
# (true/false). Strings like "true"/"false"/"yes"/"no", numbers like 1/0,
# and null are all treated as malformed and fall back to the fail-safe
# default (booking stays OFF; staff confirmation stays ON). bool(value)
# must never be used for configuration parsing here: bool("false") is True.

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Tuple
from zoneinfo import ZoneInfo


# Defaults — named constants, never inlined elsewhere.
DEFAULT_BOOKING_ENABLED = False          # Calendar is OPT-IN per office. A new
                                         # office must not silently gain booking.
DEFAULT_HOLD_MINUTES = 5                 # How long a selected slot stays reserved
                                         # while the patient confirms.
DEFAULT_MINIMUM_NOTICE_MINUTES = 60      # "No booking 10 minutes from now."
DEFAULT_MAX_OFFERED_SLOTS = 3            # Mia shows at most this many options.
DEFAULT_MAX_BOOKING_DAYS = 30            # How far ahead patients may book.
DEFAULT_REQUIRE_STAFF_CONFIRMATION = True  # Early-rollout safety: appointments
                                           # save as "pending" and patient wording
                                           # says "request received" until the
                                           # office trusts the system.
DEFAULT_TIMEZONE = "America/New_York"


@dataclass(frozen=True)
class CalendarSettings:
    """Typed, validated view of one client's calendar configuration."""
    booking_enabled: bool
    hold_minutes: int
    minimum_notice_minutes: int
    max_offered_slots: int
    max_booking_days: int
    require_staff_confirmation: bool
    timezone_name: str


def _read_int(raw: dict, key: str, default: int, minimum: int, maximum: int) -> int:
    """
    Purpose: Read one integer setting with bounds enforcement.
    Inputs:  raw settings dict, key, default, allowed [minimum, maximum].
    Returns: the clamped integer.
    Failures: none — invalid values fall back to the default (and the fallback
              is deliberate + bounded, not a silent behavior change: the bounds
              themselves are the documented contract).
    """
    value = raw.get(key, default)
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


# Sentinel distinguishing "key absent" from an explicit JSON null (both fall
# back to the default, but the distinction is documented, not accidental).
_MISSING = object()


def _read_strict_bool(raw: dict, key: str, default: bool) -> bool:
    """
    Purpose: Read one boolean setting accepting ONLY real JSON booleans
             (Patch 2A — Senior Audit Critical #6: the opt-in setting must
             never be enabled by a truthy string).
    Inputs:  raw settings dict, key, and the flag's fail-safe default.
    Returns: the value only if it is exactly True or False; the default for
             a missing key or ANY other type — "true", "false", "yes", "no",
             "1", "0", 1, 0, None, lists, dicts, floats are all rejected.
    Database effects: none (pure read).
    Failures: never raises; malformed values fall back to the default. The
              fallback direction is each flag's SAFE direction by design:
              booking_enabled defaults False (garbage cannot open booking),
              require_staff_confirmation defaults True (falsy garbage like
              0 or "" cannot switch off the pending-confirmation safety).

    Why isinstance(value, bool) and nothing looser: in Python 1 == True and
    isinstance(True, int) is also true, so equality or membership checks
    (value in (True, False)) would wrongly accept the integers 1 and 0.
    isinstance(1, bool) is False, which is exactly the strictness required.
    bool(value) is forbidden for configuration parsing: bool("false") is True.
    """
    value = raw.get(key, _MISSING)
    if isinstance(value, bool):
        return value
    return default


def resolve_client_timezone(client) -> str:
    """
    Purpose: Return the practice timezone name.
    Inputs:  client ORM object (clients row).
    Returns: IANA timezone string, defaulting to America/New_York.
    Failures: never raises; invalid names are caught by callers using ZoneInfo.

    NOTE (Rule 3): chat.py has an equivalent get_client_timezone_name(). It is
    intentionally mirrored here rather than imported, because services must not
    import the route module (circular import). Consolidating both into a shared
    helper is a future refactor, kept out of this patch per Rule 12.
    """
    tz = (getattr(client, "timezone", None) or "").strip()
    if not tz:
        settings = getattr(client, "settings", None) or {}
        if isinstance(settings, dict):
            tz = str(settings.get("timezone") or "").strip()
    return tz or DEFAULT_TIMEZONE


def client_now(settings: CalendarSettings) -> datetime:
    """
    Purpose: Current time in the practice's timezone (tz-aware).
    Inputs:  loaded CalendarSettings.
    Returns: aware datetime.
    Failures: falls back to the default timezone if the configured name is
              invalid (logged by callers when it matters; the fallback keeps
              booking usable instead of crashing every chat message).
    """
    try:
        return datetime.now(ZoneInfo(settings.timezone_name))
    except Exception:
        return datetime.now(ZoneInfo(DEFAULT_TIMEZONE))


def ensure_utc(value: datetime) -> datetime:
    """
    Purpose: Normalize a stored datetime to aware-UTC before any comparison.
    Why it exists (documented, not hidden — Rule 4): PostgreSQL timestamptz
    columns come back timezone-aware, but SQLite (used by the local test
    suite) returns naive datetimes for the same model. All calendar values
    are WRITTEN as UTC, so a naive value read back is, by construction, UTC.
    This function makes that single assumption explicit in exactly one place.
    Inputs:  aware or naive datetime.
    Returns: aware datetime in UTC.
    Failures: raises TypeError on non-datetime input (a bug upstream).
    """
    if not isinstance(value, datetime):
        raise TypeError(f"ensure_utc expected datetime, got {type(value)!r}")
    if value.tzinfo is None:
        return value.replace(tzinfo=ZoneInfo("UTC"))
    return value.astimezone(ZoneInfo("UTC"))


def local_day_utc_window(day: date, timezone_name: str) -> Tuple[datetime, datetime]:
    """
    Purpose: THE single owner of "one LOCAL calendar day" expressed as a UTC
             query window (Patch 2B — Senior Audit Critical #7). Every
             local-day database query (availability + admin routes) must get
             its boundaries here and nowhere else (Rule 3).
    Inputs:  day — a calendar date in the client's timezone;
             timezone_name — an IANA name, e.g. "America/New_York".
    Returns: (start_utc, end_utc) aware-UTC datetimes for the HALF-OPEN
             range start_utc <= t < end_utc. A record exactly at end_utc
             belongs to the NEXT local day.
    Database effects: none (pure).
    Possible failures: an unknown timezone name raises (via ZoneInfo) — a
             configuration bug that must surface, not be hidden (Rule 16).

    A local calendar day is NOT always 24 hours: with an offset transition
    it can be 23 hours (spring forward) or 25 hours (fall back) — for
    America/New_York in 2026: 2026-03-08 and 2026-11-01. The contract is
    therefore: BOTH local midnights are constructed independently and
    converted to UTC independently. end_utc is never derived by adding
    24 hours to start_utc.
    """
    tz = ZoneInfo(timezone_name)
    local_start = datetime.combine(day, time.min, tzinfo=tz)
    local_end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz)
    return local_start.astimezone(ZoneInfo("UTC")), local_end.astimezone(ZoneInfo("UTC"))


def load_calendar_settings(client) -> CalendarSettings:
    """
    Purpose: Build the typed CalendarSettings for one client.
    Inputs:  client ORM object; reads client.settings["calendar"] (JSONB).
    Returns: CalendarSettings with defaults applied and bounds enforced.
    Database effects: none (pure read of the already-loaded client row).
    Failures: never raises; malformed JSON shapes degrade to defaults.
    """
    settings = getattr(client, "settings", None)
    raw = {}
    if isinstance(settings, dict):
        candidate = settings.get("calendar")
        if isinstance(candidate, dict):
            raw = candidate

    return CalendarSettings(
        # Strict JSON booleans only (Critical #6): a string "true" or number 1
        # must NOT enable booking; falsy garbage must NOT disable staff
        # confirmation. Each flag falls back to its fail-safe default.
        booking_enabled=_read_strict_bool(
            raw, "booking_enabled", DEFAULT_BOOKING_ENABLED
        ),
        hold_minutes=_read_int(raw, "hold_minutes", DEFAULT_HOLD_MINUTES, 1, 60),
        minimum_notice_minutes=_read_int(
            raw, "minimum_notice_minutes", DEFAULT_MINIMUM_NOTICE_MINUTES, 0, 60 * 24 * 7
        ),
        max_offered_slots=_read_int(raw, "max_offered_slots", DEFAULT_MAX_OFFERED_SLOTS, 1, 10),
        # Floor is 0, not 1 (Patch 2B): max_booking_days=0 means "today's
        # LOCAL date only" under the local-calendar-date horizon rule.
        max_booking_days=_read_int(raw, "max_booking_days", DEFAULT_MAX_BOOKING_DAYS, 0, 365),
        require_staff_confirmation=_read_strict_bool(
            raw, "require_staff_confirmation", DEFAULT_REQUIRE_STAFF_CONFIRMATION
        ),
        timezone_name=resolve_client_timezone(client),
    )
