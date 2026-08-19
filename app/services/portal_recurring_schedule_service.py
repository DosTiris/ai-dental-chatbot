# app/services/portal_recurring_schedule_service.py
#
# OWNER OF: Office Portal RECURRING SCHEDULE management rules (P4-B).
#
# Single owner (Rule 3) of every rule that decides whether an authenticated
# office may VIEW, SAVE (CAS), PREVIEW, or APPLY its recurring weekly schedule
# (weekly office_hours + settings.calendar.recurring{slot_minutes,closures}),
# and HOW each of those is performed safely. app/routes/portal_recurring_schedule.py
# is transport-only wiring (the portal.py / portal_auth.py split, mirrored by
# every prior portal phase). ALL slot mutation is DELEGATED to the FROZEN P4-A
# primitives in portal_schedule_service.py; this module edits none of them.
#
# SCOPE (Contract v1.1): CONFIG + bounded on-demand materialization only.
#   * clients.office_hours          -> application-owned canonical weekly hours
#                                      in the EXISTING chat-compatible shape
#                                      ({"open":true,"start","end"} / {"open":false})
#   * settings.calendar.recurring   -> ONLY {slot_minutes, closures}
#   * schedule_config_updated_at    -> the P4-B CAS token (migration 010),
#                                      SEPARATE from the P6-A notification token
# appointment_slots remains the SOLE bookability authority. No patient
# notifications, no rolling scheduler/background task, no chat.py change.
#
# TENANT ISOLATION (Rule 15): every read/write derives the tenant SOLELY from
# PortalIdentity.client; nothing here reads a tenant id from request input; the
# CAS WHERE binds the verified client id.
#
# CONCURRENCY (Contract v1.1 B/C3/C4/F7): the Save write is ONE atomic partial
# JSONB compare-and-set on schedule_config_updated_at (NULL-safe), never a
# select-compare-then-write. The token STRICTLY advances (see _advance_token,
# the P6-A/C6 LOCAL equivalent, not a shared-helper refactor).
#
# BINDING ADDENDUM (Option A / G1-G4): every open weekday window must be a
# positive EXACT multiple of slot_minutes and yield <= MAX_GENERATED_SLOTS
# slots. Enforced on Save (G1), on pre-first-Save Preview (G2), and as canonical
# stored-config validation once the token is non-NULL (G3). With G1-G3 in force,
# the only date-specific PUBLISH_INVALID cause reaching expansion is DST, so the
# approved outcome vocabulary (dst_invalid / dst_skipped) is unchanged (G4).

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
import re
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from app.models import Client
from app.portal_models import OfficeUserRole
from app.services.portal_auth import PortalIdentity

# Frozen owners used through the MODULE object so a test seam that substitutes
# e.g. calendar_settings_service.client_now is honored at call time (the P3-C
# seam rule the P4-A route already relies on). No P4-A source is edited.
from app.services import calendar_settings_service
from app.services import portal_schedule_service
from app.repositories import appointment_repository

# Frozen constants reused (Rule 4: no duplicated magic values). The geometry
# bound MAX_GENERATED_SLOTS and the slot-minute bounds are the SAME frozen
# values the P4-A publish path enforces, so a config the portal accepts is a
# config the frozen expander can materialize.
from app.services.portal_schedule_service import (
    SLOT_MINUTES_MIN,
    SLOT_MINUTES_MAX,
    SLOT_MINUTES_STEP,
    MAX_GENERATED_SLOTS,
    PORTAL_DEFAULT_SLOT_MINUTES,
    PUBLISH_INVALID,
    PUBLISH_OVERLAP,
    PUBLISH_OK,
    PUBLISH_CLOSED_DAY,   # 4D-B: operational closed-day refusal vocabulary
)
from app.calendar_models import SlotStatus

# --- Named bounds (Rule 4) ---------------------------------------------------
WEEKDAYS: Tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
MAX_CLOSURES = 100                 # closures list length cap (Contract v1.1 s3)
MAX_CLOSURE_SPAN_DAYS = 366        # inclusive span cap for a {start,end} range
DEFAULT_SLOT_MINUTES = PORTAL_DEFAULT_SLOT_MINUTES  # surfaced/default (== 30)

# --- Controlled failure details (Rule 16: caller-safe; never echoes stored
#     values, tokens internals, tenant, or DB text) ---------------------------
FORBIDDEN_DETAIL = "This action requires an office administrator."
STALE_CONFIG_DETAIL = (
    "Recurring schedule settings were updated elsewhere. Refresh to load the "
    "latest state."
)
CONFIG_NOT_SAVED_DETAIL = "Save recurring schedule settings before applying them."
MALFORMED_STORED_CONFIG_DETAIL = (
    "The stored recurring schedule configuration is not in a usable shape and "
    "must be re-saved from the portal before it can be viewed, previewed, or "
    "applied."
)
SETTINGS_UNAVAILABLE_DETAIL = (
    "Recurring schedule settings are temporarily unavailable."
)
INVALID_TOKEN_DETAIL = (
    "expected_schedule_config_updated_at must be the exact config version "
    "string previously returned by the server, or null."
)

# A1 strict wire grammar for the opaque CAS token: UTC 'Z' designator only,
# whole or fractional (1-6 digit) seconds, no numeric offset, no date-only.
# The browser echoes the server string VERBATIM; the server validates the wire
# syntax before any SQL and mints/serializes tokens in exactly this form.
_TOKEN_WIRE_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$"
)
_HH_MM_RE = re.compile(r"^\d{2}:\d{2}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _forbidden() -> HTTPException:
    return HTTPException(status_code=403, detail=FORBIDDEN_DETAIL)


def _invalid_config(detail: str) -> HTTPException:
    # 422 INVALID_CONFIG family (PUT validation + malformed-legacy on write).
    return HTTPException(status_code=422, detail=detail)


def _malformed_stored() -> HTTPException:
    return HTTPException(status_code=422, detail=MALFORMED_STORED_CONFIG_DETAIL)


def _stale_config() -> HTTPException:
    return HTTPException(status_code=409, detail=STALE_CONFIG_DETAIL)


def _config_not_saved() -> HTTPException:
    return HTTPException(status_code=409, detail=CONFIG_NOT_SAVED_DETAIL)


def _settings_unavailable() -> HTTPException:
    return HTTPException(status_code=500, detail=SETTINGS_UNAVAILABLE_DETAIL)


def require_office_admin(identity: PortalIdentity) -> None:
    """
    Purpose: Pin all four recurring-schedule endpoints to office_admin
        (Contract s10) with a LOCAL check that reads only the existing
        OfficeUserRole.OFFICE_ADMIN constant - a deliberate local equivalent of
        the P6-A guard, not a P6-A import and not an auth-owner change.
    Failures: HTTPException 403 when the role is not office_admin.
    Database effects: none.
    """
    if identity.office_user.role != OfficeUserRole.OFFICE_ADMIN:
        raise _forbidden()


def _now_utc() -> datetime:
    """One clock source for token minting in this module."""
    return datetime.now(timezone.utc)


def _advance_token(expected_token: Optional[datetime]) -> datetime:
    """
    Purpose: Mint the config CAS token for one ACCEPTED write so it is STRICTLY
        newer than the token it replaces, even under an equal/backward clock
        (P6-A/C6 semantics; a LOCAL equivalent, not a shared-helper refactor).
    Returns: aware UTC datetime, strictly greater than expected_token when one
        exists; the plain current clock for the first-ever write (expected None).
    Database effects: none (pure).
    """
    now = _now_utc()
    if expected_token is None:
        return now
    expected = (expected_token if expected_token.tzinfo is not None
                else expected_token.replace(tzinfo=timezone.utc))
    if now > expected:
        return now
    return expected + timedelta(microseconds=1)


def _token_to_wire(value: Optional[datetime]) -> Optional[str]:
    """
    Purpose: Serialize a stored/minted token to the EXACT A1 wire form
        (UTC 'Z', 1-6 fractional digits when present). The browser echoes this
        verbatim; Pydantic's default '+00:00' rendering would FAIL A1 on the
        return trip (the v1.0.2 wire-form lesson), so serialization is explicit.
    Returns: the wire string, or None.
    """
    if value is None:
        return None
    aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    iso = aware.astimezone(timezone.utc).isoformat()
    # isoformat yields '...+00:00'; A1 requires the 'Z' designator.
    return iso.replace("+00:00", "Z")


def parse_expected_token(raw: Optional[str]) -> Optional[datetime]:
    """
    Purpose: A1 STRICT validation of the opaque wire token BEFORE any SQL.
        null is permitted; a non-null value must match the exact server wire
        grammar (UTC 'Z', whole/fractional seconds, a REAL calendar instant).
    Rejects (HTTPException 422 INVALID_CONFIG, zero write/mutation): missing
        designator, date-only, numeric offsets such as +00:00, impossible dates
        or times, malformed/junk strings, any other non-server wire form. Does
        NOT rely on PostgreSQL's permissive timestamp parsing.
    Returns: an aware UTC datetime for a valid non-null token, else None.
    """
    if raw is None:
        return None
    if not isinstance(raw, str) or not _TOKEN_WIRE_RE.match(raw):
        raise _invalid_config(INVALID_TOKEN_DETAIL)
    try:
        # Real-instant check: reject impossible dates/times (e.g. month 13,
        # 25:00, day 32). fromisoformat with the offset form validates the
        # calendar instant; the regex already fixed the wire syntax.
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise _invalid_config(INVALID_TOKEN_DETAIL)
    return parsed.astimezone(timezone.utc)


# ============================================================================
# Config shape validation (Contract s7 + Option A/G1 geometry)
# ============================================================================

def _validate_slot_minutes(value: Any) -> int:
    """slot_minutes: strict int (reject bool), in [MIN, MAX], divisible by STEP."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise _invalid_config("slot_minutes must be an integer.")
    if value < SLOT_MINUTES_MIN or value > SLOT_MINUTES_MAX:
        raise _invalid_config(
            f"slot_minutes must be between {SLOT_MINUTES_MIN} and "
            f"{SLOT_MINUTES_MAX}.")
    if value % SLOT_MINUTES_STEP != 0:
        raise _invalid_config(
            f"slot_minutes must be divisible by {SLOT_MINUTES_STEP}.")
    return value


def _parse_hh_mm(value: Any) -> int:
    """Return minutes-since-midnight for a strict 'HH:MM' 00:00-23:59, else raise."""
    if not isinstance(value, str) or not _HH_MM_RE.match(value):
        raise _invalid_config("Times must be HH:MM (00:00-23:59).")
    hh, mm = int(value[:2]), int(value[3:])
    if hh > 23 or mm > 59:
        raise _invalid_config("Times must be HH:MM (00:00-23:59).")
    return hh * 60 + mm


def _validate_weekly_day_wire(wd: str, value: Any, slot_minutes: int) -> Dict[str, Any]:
    """
    Validate one weekday's fixed wire value {open,start,end} (Contract s4.4)
    and, for an open day, the Option A/G1 geometry against slot_minutes.
    Returns the validated wire dict (unchanged shape).
    """
    if not isinstance(value, dict):
        raise _invalid_config(f"{wd} must be an object with open/start/end.")
    if set(value.keys()) != {"open", "start", "end"}:
        raise _invalid_config(
            f"{wd} must have exactly the keys open, start, end.")
    is_open = value["open"]
    if not isinstance(is_open, bool):
        raise _invalid_config(f"{wd}.open must be true or false.")
    if not is_open:
        # Closed wire day: start/end MUST both be null (any non-null -> 422).
        if value["start"] is not None or value["end"] is not None:
            raise _invalid_config(
                f"{wd} is closed; start and end must both be null.")
        return {"open": False, "start": None, "end": None}
    start_m = _parse_hh_mm(value["start"])
    end_m = _parse_hh_mm(value["end"])
    if end_m <= start_m:
        raise _invalid_config(f"{wd}: end must be after start.")
    # --- Option A / G1 geometry (binding addendum) ---
    span = end_m - start_m
    if span % slot_minutes != 0:
        raise _invalid_config(
            f"{wd}: the open window length must be an exact multiple of "
            f"slot_minutes ({slot_minutes} minutes).")
    if span // slot_minutes > MAX_GENERATED_SLOTS:
        raise _invalid_config(
            f"{wd}: the open window would create more than "
            f"{MAX_GENERATED_SLOTS} slots; shorten the window or increase "
            f"slot_minutes.")
    return {"open": True, "start": value["start"], "end": value["end"]}


def _validate_weekly_hours_wire(weekly: Any, slot_minutes: int) -> Dict[str, Dict[str, Any]]:
    """weekly_hours: exactly the 7 keys mon..sun, each a valid wire day (+G1)."""
    if not isinstance(weekly, dict):
        raise _invalid_config("weekly_hours must be an object with 7 weekdays.")
    if set(weekly.keys()) != set(WEEKDAYS):
        raise _invalid_config(
            "weekly_hours must have exactly the keys mon,tue,wed,thu,fri,sat,sun.")
    return {wd: _validate_weekly_day_wire(wd, weekly[wd], slot_minutes) for wd in WEEKDAYS}


def _validate_closures(closures: Any) -> List[Dict[str, str]]:
    """closures: <=100 entries, each exactly {date} or {start,end} (strict
    YYYY-MM-DD, end>=start, inclusive span <= 366)."""
    if not isinstance(closures, list):
        raise _invalid_config("closures must be a list.")
    if len(closures) > MAX_CLOSURES:
        raise _invalid_config(f"closures may contain at most {MAX_CLOSURES} entries.")
    out: List[Dict[str, str]] = []
    for entry in closures:
        if not isinstance(entry, dict):
            raise _invalid_config("Each closure must be an object.")
        keys = set(entry.keys())
        if keys == {"date"}:
            _parse_iso_date(entry["date"])
            out.append({"date": entry["date"]})
        elif keys == {"start", "end"}:
            s = _parse_iso_date(entry["start"])
            e = _parse_iso_date(entry["end"])
            if e < s:
                raise _invalid_config("A closure range end must be on or after its start.")
            if (e - s).days + 1 > MAX_CLOSURE_SPAN_DAYS:
                raise _invalid_config(
                    f"A closure range may span at most {MAX_CLOSURE_SPAN_DAYS} days.")
            out.append({"start": entry["start"], "end": entry["end"]})
        else:
            raise _invalid_config('Each closure must be exactly {"date"} or {"start","end"}.')
    return out


def _parse_iso_date(value: Any) -> date:
    if not isinstance(value, str) or not _DATE_RE.match(value):
        raise _invalid_config("Dates must be in YYYY-MM-DD form.")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise _invalid_config("Dates must be a real calendar date in YYYY-MM-DD form.")


# ============================================================================
# Stored-config reading (tolerant for GET/Preview surfacing; A2/C4/G3 fail-closed)
# ============================================================================

def _stored_settings_is_incompatible(settings: Any) -> bool:
    """C4: settings that is a scalar/array (not object/None) cannot be surfaced."""
    if settings is None:
        return False
    if not isinstance(settings, dict):
        return True
    calendar = settings.get("calendar")
    if calendar is None:
        return False
    if not isinstance(calendar, dict):
        return True
    recurring = calendar.get("recurring")
    if recurring is None:
        return False
    return not isinstance(recurring, dict)


def _read_recurring_block(client: Client) -> Tuple[int, List[Dict[str, str]]]:
    """
    Read slot_minutes + closures from settings.calendar.recurring, defaulting
    (30 / []) when absent. Raises MALFORMED_STORED_CONFIG (422) when the stored
    shape is an incompatible scalar/array (C4) - never silently substitutes.
    """
    if _stored_settings_is_incompatible(getattr(client, "settings", None)):
        raise _malformed_stored()
    settings = getattr(client, "settings", None)
    recurring: Dict[str, Any] = {}
    if isinstance(settings, dict):
        cal = settings.get("calendar")
        if isinstance(cal, dict) and isinstance(cal.get("recurring"), dict):
            recurring = cal["recurring"]
    slot_minutes = recurring.get("slot_minutes", DEFAULT_SLOT_MINUTES)
    closures = recurring.get("closures", [])
    return slot_minutes, closures


def _read_weekly_hours_wire(client: Client) -> Dict[str, Dict[str, Any]]:
    """
    Surface clients.office_hours in the fixed wire shape (every day open/start/end).
    Read TOLERANTLY: a missing/partial/legacy weekday is surfaced as closed. This
    is the first-writer safety read (Contract s11) and never raises on hours.
    """
    raw = getattr(client, "office_hours", None)
    hours = raw if isinstance(raw, dict) else {}
    wire: Dict[str, Dict[str, Any]] = {}
    for wd in WEEKDAYS:
        row = hours.get(wd)
        if isinstance(row, dict) and bool(row.get("open", False)):
            start = row.get("start")
            end = row.get("end")
            if isinstance(start, str) and isinstance(end, str):
                wire[wd] = {"open": True, "start": start, "end": end}
            else:
                wire[wd] = {"open": False, "start": None, "end": None}
        else:
            wire[wd] = {"open": False, "start": None, "end": None}
    return wire


def _read_canonical_weekly_hours(client: Client) -> Dict[str, Dict[str, Any]]:
    """
    STRICT reader used ONLY once P4-B owns the config (token non-NULL). Unlike
    the tolerant reader, this NEVER normalizes: office_hours must be an object
    with EXACTLY the 7 weekdays, each in canonical DB form - an open day is
    EXACTLY {open:true,start:"HH:MM",end:"HH:MM"} and a closed day is EXACTLY
    {open:false}. A missing weekday, an extra key, a wrong type, or a non-
    HH:MM time raises MALFORMED_STORED_CONFIG (422) - it is never silently
    surfaced as closed (F1). Returns the fixed wire shape on success.
    """
    raw = getattr(client, "office_hours", None)
    if not isinstance(raw, dict) or set(raw.keys()) != set(WEEKDAYS):
        raise _malformed_stored()
    wire: Dict[str, Dict[str, Any]] = {}
    for wd in WEEKDAYS:
        row = raw[wd]
        if not isinstance(row, dict):
            raise _malformed_stored()
        keys = set(row.keys())
        open_val = row.get("open")
        if open_val is True:
            if keys != {"open", "start", "end"}:
                raise _malformed_stored()
            start, end = row["start"], row["end"]
            if not (isinstance(start, str) and _HH_MM_RE.match(start)
                    and isinstance(end, str) and _HH_MM_RE.match(end)):
                raise _malformed_stored()
            wire[wd] = {"open": True, "start": start, "end": end}
        elif open_val is False:
            if keys != {"open"}:
                raise _malformed_stored()
            wire[wd] = {"open": False, "start": None, "end": None}
        else:
            raise _malformed_stored()
    return wire


def _read_canonical_recurring_block(client: Client) -> Tuple[Any, Any]:
    """
    STRICT reader (token non-NULL): settings.calendar.recurring MUST exist as
    an object that carries BOTH slot_minutes and closures - nothing is
    defaulted. Any missing level, wrong type, or missing key raises
    MALFORMED_STORED_CONFIG (422). Returns the RAW slot_minutes/closures for
    downstream value validation (F1).
    """
    settings = getattr(client, "settings", None)
    if not isinstance(settings, dict):
        raise _malformed_stored()
    cal = settings.get("calendar")
    if not isinstance(cal, dict):
        raise _malformed_stored()
    recurring = cal.get("recurring")
    if not isinstance(recurring, dict):
        raise _malformed_stored()
    # T1: once P4-B owns the config, settings.calendar.recurring must be
    # EXACTLY {slot_minutes, closures} - any missing OR extra key is a
    # malformed application-owned shape (422), never tolerated/normalized.
    if set(recurring.keys()) != {"slot_minutes", "closures"}:
        raise _malformed_stored()
    return recurring["slot_minutes"], recurring["closures"]


# ============================================================================
# View slice (dataclass)
# ============================================================================

@dataclass(frozen=True)
class RecurringConfigView:
    weekly_hours: Dict[str, Dict[str, Any]]
    slot_minutes: int
    closures: List[Dict[str, str]]
    schedule_config_updated_at: Optional[str]   # A1 wire form (str|null)


def _view(client: Client) -> RecurringConfigView:
    slot_minutes, closures = _read_recurring_block(client)
    return RecurringConfigView(
        weekly_hours=_read_weekly_hours_wire(client),
        slot_minutes=slot_minutes,
        closures=closures,
        schedule_config_updated_at=_token_to_wire(client.schedule_config_updated_at),
    )


def get_recurring_config(identity: PortalIdentity) -> RecurringConfigView:
    """
    Purpose: Surface the office's current recurring config (first-writer safety).
    Authorization: office_admin.
    Failures: 403; 422 MALFORMED_STORED_CONFIG when settings/calendar/recurring
        is an incompatible scalar/array (C4). No write.
    """
    require_office_admin(identity)
    client = identity.client
    if client.schedule_config_updated_at is None:
        return _view(client)                    # legacy first-writer tolerance
    # Token non-NULL: P4-B owns the config; it must be canonical or 422 (F1).
    weekly_wire, slot_minutes, closures = _validate_canonical_config(
        client, malformed_exc_factory=_malformed_stored, strict=True)
    return RecurringConfigView(
        weekly_hours=weekly_wire, slot_minutes=slot_minutes, closures=closures,
        schedule_config_updated_at=_token_to_wire(
            client.schedule_config_updated_at))


# ============================================================================
# Canonical stored-config validation (G3) / pre-Save geometry (G2)
# ============================================================================

def _validate_canonical_config(client: Client, malformed_exc_factory, strict: bool = False):
    """
    Validate the STORED (application-owned or legacy) config as a canonical
    RecurringConfig: slot_minutes bounds, weekly wire structure + Option A/G1
    geometry, and closures bounds. On any violation raise the HTTPException the
    caller supplies (INVALID_CONFIG for pre-Save Preview/G2; MALFORMED_STORED_
    CONFIG for the post-Save canonical contract/G3). C4 scalar/array shapes are
    caught earlier by _read_recurring_block -> 422 MALFORMED_STORED_CONFIG.
    Returns (weekly_wire, slot_minutes, closures) on success.
    """
    if strict:
        # Token non-NULL: application-owned state must be canonical (F1). The
        # strict readers raise MALFORMED_STORED_CONFIG on ANY deviation - no
        # missing weekday, missing recurring key, or wrong type is normalized.
        weekly_wire = _read_canonical_weekly_hours(client)
        slot_minutes_raw, closures_raw = _read_canonical_recurring_block(client)
    else:
        # Token NULL: approved legacy first-writer tolerance (G2 geometry only).
        slot_minutes_raw, closures_raw = _read_recurring_block(client)
        weekly_wire = _read_weekly_hours_wire(client)
    try:
        slot_minutes = _validate_slot_minutes(slot_minutes_raw)
        _validate_weekly_hours_wire(weekly_wire, slot_minutes)
        closures = _validate_closures(closures_raw)
    except HTTPException:
        # Re-map the value/geometry refusal to the caller's chosen 422 class.
        raise malformed_exc_factory()
    return weekly_wire, slot_minutes, closures


def _normalize_weekly_for_db(weekly_wire: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """
    F4 DB normalization: an open day keeps {"open":true,"start","end"}; a closed
    wire day {"open":false,"start":null,"end":null} is written as the chat-
    compatible {"open":false} (no start/end), so chat.py stays unchanged.
    """
    out: Dict[str, Any] = {}
    for wd in WEEKDAYS:
        row = weekly_wire[wd]
        if row["open"]:
            out[wd] = {"open": True, "start": row["start"], "end": row["end"]}
        else:
            out[wd] = {"open": False}
    return out


# ============================================================================
# PUT (Save) - atomic partial-JSONB compare-and-set (Contract s6)
# ============================================================================

import json as _json

# ONE atomic server-side UPDATE (no Python select->mutate-dict->replace-whole-
# JSONB). Only settings.calendar.recurring is rewritten; every other settings
# and settings.calendar key is preserved by value (F7). The WHERE carries the
# NULL-safe CAS and the C4 fail-closed guards.
_CAS_UPDATE_SQL = sql_text(
    """
    UPDATE clients
    SET
      office_hours = CAST(:office_hours_json AS jsonb),
      settings = jsonb_set(
                   jsonb_set(coalesce(settings, '{}'::jsonb),
                             '{calendar}',
                             CASE
                               WHEN settings -> 'calendar' IS NULL THEN '{}'::jsonb
                               WHEN jsonb_typeof(settings -> 'calendar') = 'null' THEN '{}'::jsonb
                               ELSE settings -> 'calendar'
                             END,
                             true),
                   '{calendar,recurring}',
                   CAST(:recurring_json AS jsonb),
                   true),
      schedule_config_updated_at = :new_token
    WHERE id = :client_id
      AND schedule_config_updated_at IS NOT DISTINCT FROM :expected_token
      AND (settings IS NULL OR jsonb_typeof(settings) = 'object')
      AND (settings IS NULL
           OR (settings -> 'calendar') IS NULL
           OR jsonb_typeof(settings -> 'calendar') IN ('object','null'))
    """
)


def put_recurring_config(
    db: Session,
    identity: PortalIdentity,
    weekly_hours: Any,
    slot_minutes: Any,
    closures: Any,
    expected_raw: Optional[str],
) -> RecurringConfigView:
    """
    Purpose: Validate and SAVE the recurring config under optimistic concurrency.
        Save updates CONFIG ONLY - it never materializes or blocks slots.
    Authorization: office_admin.
    Validation: A1 strict token (422); weekly_hours/slot_minutes/closures shape
        + Option A/G1 geometry (422 INVALID_CONFIG); malformed-legacy stored
        JSONB fail-closed on write (422).
    Database effects: exactly ONE atomic compare-and-set; commit + authoritative
        re-read on success (never echoes request values).
    Failures: 403; 422 INVALID_CONFIG; 409 STALE_CONFIG; 500 SETTINGS_UNAVAILABLE.
    """
    require_office_admin(identity)
    expected_token = parse_expected_token(expected_raw)   # A1 - before any SQL

    # Validate slot_minutes FIRST (weekly geometry is validated against it).
    minutes = _validate_slot_minutes(slot_minutes)
    weekly_wire = _validate_weekly_hours_wire(weekly_hours, minutes)
    closures_clean = _validate_closures(closures)

    office_hours_db = _normalize_weekly_for_db(weekly_wire)
    recurring_db = {"slot_minutes": minutes, "closures": closures_clean}
    client_id = identity.client.id

    result = db.execute(
        _CAS_UPDATE_SQL,
        {
            "office_hours_json": _json.dumps(office_hours_db),
            "recurring_json": _json.dumps(recurring_db),
            "new_token": _advance_token(expected_token),
            "expected_token": expected_token,
            "client_id": client_id,
        },
    )

    if result.rowcount == 1:
        db.commit()
        db.refresh(identity.client)   # authoritative re-read (never request body)
        return _view(identity.client)

    # Zero rows: nothing written. Roll back and disambiguate.
    db.rollback()
    row = db.execute(
        sql_text("SELECT schedule_config_updated_at, "
                 "jsonb_typeof(settings) AS s_type, "
                 "jsonb_typeof(settings -> 'calendar') AS c_type "
                 "FROM clients WHERE id = :cid"),
        {"cid": client_id},
    ).first()
    if row is None:
        raise _settings_unavailable()              # row vanished -> 500 (never 404)
    persisted_token = row[0]
    persisted_token = (persisted_token.astimezone(timezone.utc)
                       if persisted_token is not None else None)
    if persisted_token != expected_token:
        raise _stale_config()                      # 409 STALE_CONFIG
    # Token matched but the row still did not update => a C4 guard rejected the
    # incompatible stored shape -> malformed-legacy fail-closed (422).
    raise _invalid_config(MALFORMED_STORED_CONFIG_DETAIL)


# ============================================================================
# Preview (Contract s4.3 + G2) and Apply (s4.5 / s8 + G3)
# ============================================================================

@dataclass(frozen=True)
class _Snapshot:
    token: Optional[datetime]
    weekly_wire: Dict[str, Dict[str, Any]]
    slot_minutes: int
    closures: List[Dict[str, str]]
    settings: Any                     # CalendarSettings (timezone/max_booking_days)


def _closure_dates(closures: List[Dict[str, str]], start_day: date, end_day: date) -> set:
    """Union of {date} and each inclusive {start..end} range, intersected with
    the horizon (bounded by the <=366-day span rule)."""
    days = set()
    for c in closures:
        if "date" in c:
            d = date.fromisoformat(c["date"])
            if start_day <= d <= end_day:
                days.add(d)
        else:
            s = date.fromisoformat(c["start"])
            e = date.fromisoformat(c["end"])
            cur = max(s, start_day)
            last = min(e, end_day)
            while cur <= last:
                days.add(cur)
                cur += timedelta(days=1)
    return days


def _horizon(settings) -> Tuple[date, date]:
    today_local = calendar_settings_service.client_now(settings).date()
    end_day = today_local + timedelta(days=settings.max_booking_days)
    return today_local, end_day


def preview_recurring_config(db: Session, identity: PortalIdentity) -> Dict[str, Any]:
    """
    Purpose: Advisory, read-only snapshot over today_local..+max_booking_days,
        identical horizon to Apply (F2). No locks, no writes, no mutation.
    Authorization: office_admin. Body must be exactly {} (enforced at transport).
    Geometry: pre-first-Save (token NULL) a non-exact/over-max geometry -> 422
        INVALID_CONFIG (G2); post-Save (token non-NULL) -> 422 MALFORMED_STORED_
        CONFIG (G3). C4 scalar/array -> 422 MALFORMED_STORED_CONFIG.
    """
    require_office_admin(identity)
    client = identity.client
    token = client.schedule_config_updated_at
    if token is None:
        weekly_wire, slot_minutes, closures = _validate_canonical_config(
            client, malformed_exc_factory=lambda: _invalid_config(
                "The current office hours do not divide evenly into slots; "
                "adjust the hours or slot length, then Save."))
    else:
        weekly_wire, slot_minutes, closures = _validate_canonical_config(
            client, malformed_exc_factory=_malformed_stored, strict=True)

    settings = calendar_settings_service.load_calendar_settings(client)
    start_day, end_day = _horizon(settings)
    closure_days = _closure_dates(closures, start_day, end_day)

    days_out: List[Dict[str, Any]] = []
    cur = start_day
    while cur <= end_day:
        wd = WEEKDAYS[cur.weekday()]
        row = weekly_wire[wd]
        if cur in closure_days:
            start_utc, end_utc = calendar_settings_service.local_day_utc_window(
                cur, settings.timezone_name)
            existing = appointment_repository.list_slots_between(
                db, client.id, start_utc, end_utc)
            blockable = sum(1 for s in existing
                            if s.status in (SlotStatus.AVAILABLE, SlotStatus.HELD))
            # R2: surface the day's BOOKED windows (times only, no patient
            # data) so staff SEES a closure will NOT cancel existing
            # appointments (approved Preview contract; Rule 16).
            booked_windows = [
                {"start_utc": s.start_datetime.isoformat(),
                 "end_utc": s.end_datetime.isoformat()}
                for s in existing if s.status == SlotStatus.BOOKED]
            days_out.append({
                "day": cur.isoformat(), "weekday": wd, "classification": "closure",
                "outcome": "would_block" if blockable else "closure_empty",
                "would_block_available_held": blockable,
                "booked_windows": booked_windows})
        elif row["open"]:
            expansion = portal_schedule_service.expand_publish_slots(
                cur, row["start"], row["end"], slot_minutes, settings.timezone_name)
            if isinstance(expansion, portal_schedule_service.PublishResult):
                # G1-G3 removed every non-DST cause, so this is a DST wall-time.
                days_out.append({
                    "day": cur.isoformat(), "weekday": wd, "classification": "open",
                    "outcome": "dst_invalid"})
            else:
                start_utc, end_utc = calendar_settings_service.local_day_utc_window(
                    cur, settings.timezone_name)
                existing = appointment_repository.list_slots_between(
                    db, client.id, start_utc, end_utc)
                has_inv = any(s.status != SlotStatus.CANCELLED for s in existing)
                if has_inv:
                    days_out.append({
                        "day": cur.isoformat(), "weekday": wd, "classification": "open",
                        "outcome": "existing_inventory", "existing_inventory": True})
                else:
                    days_out.append({
                        "day": cur.isoformat(), "weekday": wd, "classification": "open",
                        "outcome": "would_publish", "would_publish_count": len(expansion)})
        else:
            start_utc, end_utc = calendar_settings_service.local_day_utc_window(
                cur, settings.timezone_name)
            existing = appointment_repository.list_slots_between(
                db, client.id, start_utc, end_utc)
            has_inv = any(s.status != SlotStatus.CANCELLED for s in existing)
            days_out.append({
                "day": cur.isoformat(), "weekday": wd, "classification": "weekly_closed",
                "outcome": "existing_inventory" if has_inv else "weekly_closed_empty",
                "existing_inventory": has_inv})
        cur += timedelta(days=1)

    return {
        "schedule_config_updated_at": _token_to_wire(token),
        "start_day": start_day.isoformat(), "end_day": end_day.isoformat(),
        "days": days_out,
    }


def apply_recurring_config(
    db: Session, identity: PortalIdentity, expected_raw: Optional[str]
) -> Dict[str, Any]:
    """
    Purpose: Materialize the recurring config over today_local..+max_booking_days
        (F2), delegating ALL slot mutation to the frozen P4-A primitives. Per-day
        commits (C5 - NOT one transaction); safe/idempotent on rerun.
    Authorization: office_admin. Body {expected_schedule_config_updated_at}.
    Gates: A1 strict token (422); token NULL -> 409 CONFIG_NOT_SAVED (F3);
        captured token != expected -> 409 STALE_CONFIG; malformed stored config
        (C4 or G3 geometry) -> 422 MALFORMED_STORED_CONFIG; all zero mutation.
    """
    require_office_admin(identity)
    expected_token = parse_expected_token(expected_raw)   # A1 - before any work

    db.refresh(identity.client)                 # one consistent read = linearization
    client = identity.client
    current_token = client.schedule_config_updated_at
    if current_token is None:
        raise _config_not_saved()               # F3 (zero mutation)
    if current_token.astimezone(timezone.utc) != expected_token:
        raise _stale_config()                   # C3 (zero mutation)

    # G3 canonical validation of the captured, application-owned config. The
    # token is non-NULL here (checked above), so strict canonical form is
    # required; any deviation -> 422 MALFORMED_STORED_CONFIG, ZERO mutation (F1).
    weekly_wire, slot_minutes, closures = _validate_canonical_config(
        client, malformed_exc_factory=_malformed_stored, strict=True)
    settings = calendar_settings_service.load_calendar_settings(client)
    snap = _Snapshot(token=current_token, weekly_wire=weekly_wire,
                     slot_minutes=slot_minutes, closures=closures, settings=settings)

    start_day, end_day = _horizon(settings)
    closure_days = _closure_dates(snap.closures, start_day, end_day)

    days_out: List[Dict[str, Any]] = []
    totals = {"published_days": 0, "published_slots": 0, "closure_blocked_days": 0,
              "blocked_slots": 0, "existing_inventory_skipped_days": 0,
              "weekly_closed_days": 0, "dst_skipped_days": 0,
              "operationally_closed_days": 0}

    cur = start_day
    while cur <= end_day:
        wd = WEEKDAYS[cur.weekday()]
        row = snap.weekly_wire[wd]
        if cur in closure_days:
            # CLOSURE day: block available/expired-held, preserve booked. The
            # P4-A primitive owns its own advisory lock + commit.
            block = portal_schedule_service.block_all_open(
                db, client.id, snap.settings, cur)
            if block.blocked_count > 0 or block.booked_remaining:
                days_out.append({"day": cur.isoformat(), "weekday": wd,
                                 "outcome": "closure_blocked",
                                 "blocked_count": block.blocked_count,
                                 "booked_remaining": [
                                     {"start_utc": s.isoformat(), "end_utc": e.isoformat()}
                                     for s, e in block.booked_remaining]})
                totals["closure_blocked_days"] += 1
                totals["blocked_slots"] += block.blocked_count
            else:
                days_out.append({"day": cur.isoformat(), "weekday": wd,
                                 "outcome": "closure_empty"})
        elif row["open"]:
            outcome = _apply_open_day(db, client.id, snap, cur, row)
            days_out.append(dict({"day": cur.isoformat(), "weekday": wd}, **outcome))
            if outcome["outcome"] == "published":
                totals["published_days"] += 1
                totals["published_slots"] += outcome.get("published_count", 0)
            elif outcome["outcome"] == "existing_inventory_skipped":
                totals["existing_inventory_skipped_days"] += 1
            elif outcome["outcome"] == "dst_skipped":
                totals["dst_skipped_days"] += 1
            elif outcome["outcome"] == "operationally_closed_skipped":
                # 4D-B: a live closed_days restriction outranked recurring
                # materialization on this date (zero inserts there).
                totals["operationally_closed_days"] += 1
        else:
            # WEEKLY-CLOSED day: never publishes, never blocks (C1).
            start_utc, end_utc = calendar_settings_service.local_day_utc_window(
                cur, snap.settings.timezone_name)
            existing = appointment_repository.list_slots_between(
                db, client.id, start_utc, end_utc)
            if any(s.status != SlotStatus.CANCELLED for s in existing):
                days_out.append({"day": cur.isoformat(), "weekday": wd,
                                 "outcome": "existing_inventory_skipped"})
                totals["existing_inventory_skipped_days"] += 1
            else:
                days_out.append({"day": cur.isoformat(), "weekday": wd,
                                 "outcome": "weekly_closed"})
                totals["weekly_closed_days"] += 1
        cur += timedelta(days=1)

    return {
        "schedule_config_updated_at": _token_to_wire(snap.token),
        "start_day": start_day.isoformat(), "end_day": end_day.isoformat(),
        "days": days_out, "totals": totals,
    }


def _apply_open_day(db: Session, client_id, snap: "_Snapshot", cur: date,
                    row: Dict[str, Any]) -> Dict[str, Any]:
    """
    Race-safe empty-day publish (C2) + lock-termination invariant (F5): one
    transaction per OPEN day; on EVERY exit db.in_transaction() is False (the
    per-(tenant,local-day) advisory lock is released) before the next day. All
    slot mutation is delegated to the frozen P4-A primitives.
    """
    try:
        portal_schedule_service.acquire_schedule_day_lock(db, client_id, cur)  # SAME P4-A lock
        start_utc, end_utc = calendar_settings_service.local_day_utc_window(
            cur, snap.settings.timezone_name)
        existing = appointment_repository.list_slots_between(db, client_id, start_utc, end_utc)
        if any(s.status != SlotStatus.CANCELLED for s in existing):
            db.rollback()                          # F5: release lock; mutate nothing
            return {"outcome": "existing_inventory_skipped"}
        result = portal_schedule_service.publish_day_slots(
            db, client_id, snap.settings, cur, row["start"], row["end"], snap.slot_minutes)
        if result.reason == PUBLISH_INVALID:
            db.rollback()                          # F5: expander issued NO DB stmt
            return {"outcome": "dst_skipped", "reason": result.detail}
        if result.reason == PUBLISH_OVERLAP:
            return {"outcome": "existing_inventory_skipped"}  # publish already rolled back
        if result.reason == PUBLISH_CLOSED_DAY:
            # 4D-B: the date is OPERATIONALLY closed (settings.calendar.
            # closed_days) - a live restriction that outranks recurring
            # materialization. Publish already rolled back with zero
            # inserts; report it honestly rather than pretending "published"
            # with an empty list. The configured recurring closures list is
            # a DIFFERENT concept and is untouched by this outcome.
            return {"outcome": "operationally_closed_skipped"}
        return {"outcome": "published", "published_count": len(result.slots)}
    except Exception:
        db.rollback()                              # F5: release lock before propagating
        raise
