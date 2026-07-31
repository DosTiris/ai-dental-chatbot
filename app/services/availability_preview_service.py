# app/services/availability_preview_service.py
#
# OWNER OF: the Prototype B B1 READ-ONLY availability preview — the day-state
# calendar grid and selected-day slot list the future visual picker will
# render.
#
# HARD BOUNDARIES (Prototype B B1 spec — enforced by tests):
#   - SELECT-only: exactly ONE existing repository range query per preview.
#     Never the one-query-per-day scanning helper, never a per-day loop.
#   - Zero database writes: no commit, no flush, no add, no delete, no status
#     or timestamp mutation, no hold takeover.
#   - Calls NO booking, hold-mutation, notification, or conversation service.
#   - No route, no authentication, no credentials: the caller supplies an
#     ALREADY-LOADED tenant client row. HTTP/tenant-match behavior (the
#     existing require_tenant_match 404 owner) arrives with the B2 route.
#
# DAY-STATE VOCABULARY (LOCKED — B1 architecture decision #6/#7):
# The backend has no authoritative office-hours model, so it CANNOT
# distinguish "office closed" from "no availability published". "closed" is
# therefore RESERVED for a future authoritative office-hours source and must
# never be emitted here. Zero slot rows => "unavailable", not "closed".

import uuid  # noqa: F401  (documents that client.id is a UUID; str()ed below)
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from app.calendar_models import SlotStatus
from app.repositories import appointment_repository
from app.schemas import (
    AvailabilityPreviewRequest,
    AvailabilityPreviewResponse,
    PreviewDay,
    PreviewSlot,
)
from app.services.appointment_intent import PREF_ANY
from app.services.availability_rules import (
    evaluate_slot_policy,
    hold_is_active,
    list_bookable_slots,
)
from app.services.calendar_settings_service import (
    CalendarSettings,
    ensure_utc,
    load_calendar_settings,
    local_day_utc_window,
)


# ---------------------------------------------------------------------------
# Locked day states — the COMPLETE vocabulary this service may emit.
# "closed" is deliberately absent (see module header). Rule 4/16: closed
# vocabulary, named constants, unknown values are impossible by construction.
# ---------------------------------------------------------------------------
DAY_STATE_OPEN = "open"                # at least one bookable slot exists
DAY_STATE_FULL = "full"                # capacity exists; all of it is booked
                                       # or actively held
DAY_STATE_UNAVAILABLE = "unavailable"  # no rows, all blocked, all policy-
                                       # rejected, or beyond booking horizon
DAY_STATE_PAST = "past"                # before the office-local current date
ALL_DAY_STATES = {
    DAY_STATE_OPEN, DAY_STATE_FULL, DAY_STATE_UNAVAILABLE, DAY_STATE_PAST,
}

# Locked time-of-day grouping boundary: local start hour before 12 is
# "morning"; otherwise "afternoon". (This is the B1 CONTRACT grouping — it is
# intentionally coarser than appointment_intent's PREF_* buckets and replaces
# time_preference, which was removed from the Prototype B contract: the
# frontend receives the full selected day and groups times itself.)
MORNING_BOUNDARY_LOCAL_HOUR = 12
TIME_OF_DAY_MORNING = "morning"
TIME_OF_DAY_AFTERNOON = "afternoon"


# ---------------------------------------------------------------------------
# SERVICE KEY OWNERSHIP (B1 revision — owner decision applied)
#
# request.service_key is treated as an OPAQUE, non-blank value in the
# EXISTING Calendar policy vocabulary: the values carried by slot rows and
# compared by evaluate_slot_policy via raw equality (e.g.
# "cleaning/checkup", "extraction/implant"). It is passed through to
# list_bookable_slots UNCHANGED.
#
# B1 deliberately owns NO vocabulary validation and NO public master-key
# translation:
#   - The Calendar policy vocabulary's only definition today lives in
#     app/routes/chat.py (SERVICE_LIBRARY_TO_LEGACY_REASON) — a route
#     module this service must not import (Rule 6), and duplicating that
#     mapping here would violate Rule 3.
#   - Master-library keys (mia_service_library, e.g. "cleaning_checkup")
#     are a DIFFERENT vocabulary; validating them here would produce false
#     service mismatches against policy-vocabulary slot rows.
#   - B2 (the public route) CANNOT BEGIN until a separately approved owner
#     extraction provides ONE service-owned master-key-to-Calendar-policy
#     mapping. /chat is unchanged in this revision.
# ---------------------------------------------------------------------------


def _preview_ordering_key(slot):
    """
    Purpose: deterministic PREVIEW-INTERNAL ordering for rendered slots.
    Inputs:  slot — a slot row already accepted by list_bookable_slots.
    Returns: (aware-UTC start, aware-UTC end, str(internal id)) — a total
             order, so equal-start slots render identically regardless of
             repository input order. The id participates ONLY in this
             internal key; it is never emitted (contract lock: PreviewSlot
             carries no slot_id).
    Why here and not in availability_rules: filter_bookable_slots ordering
    is EXISTING Mia chat behavior and must stay untouched (Rule 12); the
    stronger total order is a B1 preview presentation rule, so this module
    owns it (Rule 3).
    """
    return (
        ensure_utc(slot.start_datetime),
        ensure_utc(slot.end_datetime),
        str(slot.id),
    )


def _format_local_time(local_dt: datetime) -> str:
    """
    Purpose: Render "10:15 AM" style office-local times.
    Why not strftime("%-I"): the platform-dependent no-pad flags differ
    between Linux (%-I) and Windows (%#I) — the owner develops on Windows and
    deploys on Linux, so the hour is computed portably instead (Rule 4: no
    hidden platform behavior).
    """
    hour_12 = local_dt.hour % 12 or 12
    suffix = "AM" if local_dt.hour < 12 else "PM"
    return f"{hour_12}:{local_dt.minute:02d} {suffix}"


def _slot_view(slot, tz: ZoneInfo) -> PreviewSlot:
    """
    Purpose: Convert one bookable slot row into the B1 contract shape.
    Inputs:  slot — a row already judged bookable; tz — office timezone.
    Returns: PreviewSlot. Deliberately carries NO slot_id (contract lock).
    Database effects: none — reads attributes only, never mutates the row.
    """
    start_utc = ensure_utc(slot.start_datetime)
    end_utc = ensure_utc(slot.end_datetime)
    local_start = start_utc.astimezone(tz)
    local_end = end_utc.astimezone(tz)
    local_start_time = _format_local_time(local_start)
    local_end_time = _format_local_time(local_end)
    return PreviewSlot(
        start_utc=start_utc,
        end_utc=end_utc,
        local_start_time=local_start_time,
        local_end_time=local_end_time,
        # Manual "Thursday, July 30" assembly for the same Windows/Linux
        # strftime-padding reason documented in _format_local_time.
        accessible_date_label=(
            f"{local_start.strftime('%A')}, "
            f"{local_start.strftime('%B')} {local_start.day}"
        ),
        accessible_time_label=f"{local_start_time} to {local_end_time}",
        time_of_day=(
            TIME_OF_DAY_MORNING
            if local_start.hour < MORNING_BOUNDARY_LOCAL_HOUR
            else TIME_OF_DAY_AFTERNOON
        ),
        # Every slot in the list is bookable by construction, so it is
        # selectable; non-bookable slots are simply never emitted.
        selectable=True,
    )


def _classify_day(
    day: date,
    day_rows: Sequence,
    today_local: date,
    now_utc: datetime,
    settings: CalendarSettings,
    service_key: str,
) -> Tuple[str, List]:
    """
    Purpose: Apply the LOCKED B1 day-state rules to one office-local day.
    Inputs:
        day:         the office-local calendar date being classified.
        day_rows:    raw slot rows whose LOCAL date is `day` (any status).
        today_local: the office-local current date (derived once from the
                     injected now_utc — never from the wall clock here).
        now_utc:     aware-UTC "now" used by the pure hold/policy owners.
        settings:    the client's CalendarSettings snapshot.
        service_key: the validated requested service.
    Returns: (state, bookable_slots). bookable_slots is non-empty only for
             DAY_STATE_OPEN and is already in deterministic soonest-first
             order (it feeds the selected-day slot list directly, so the two
             can never disagree).
    Database effects: none — delegates to the pure owners only.

    Rule precedence (locked):
      past        — local_date before the office-local current date; wins
                    over everything else.
      open        — at least one bookable slot (the pure UNCAPPED owner,
                    list_bookable_slots, with PREF_ANY: the preview has no
                    time_preference by contract).
      full        — no bookable slot, but at least one OTHERWISE-ELIGIBLE
                    slot (passes evaluate_slot_policy) is booked or actively
                    held. An expired hold is NOT "actively held" — lazy
                    reclaim makes that slot bookable, hence "open", while the
                    ROW is left untouched (read-only interpretation only).
      unavailable — everything else: zero rows, all rows blocked/cancelled,
                    or every row rejected by booking policy (which includes
                    every day beyond the max_booking_days horizon).
    """
    if day < today_local:
        return DAY_STATE_PAST, []

    bookable = list_bookable_slots(
        day_rows, now_utc, settings, PREF_ANY, service_key
    )
    if bookable:
        return DAY_STATE_OPEN, bookable

    for row in day_rows:
        occupied = (
            getattr(row, "status", None) == SlotStatus.BOOKED
            or hold_is_active(row, now_utc)
        )
        if not occupied:
            continue  # blocked / cancelled / expired-held: never "capacity".
        policy = evaluate_slot_policy(
            row,
            now_utc=now_utc,
            settings=settings,
            time_preference=PREF_ANY,
            service_key=service_key,
        )
        if policy.eligible:
            # Real capacity exists and someone else has it => "full".
            return DAY_STATE_FULL, []

    return DAY_STATE_UNAVAILABLE, []


def build_availability_preview(
    db,
    client,
    request: AvailabilityPreviewRequest,
    now_utc: datetime,
) -> AvailabilityPreviewResponse:
    """
    Purpose: Build the complete read-only picker payload for one tenant.
    Inputs:
        db:      an open SQLAlchemy session — passed ONLY to the repository's
                 SELECT-only range query; this service never touches it
                 directly (no commit/flush/add/delete — proven by tests).
        client:  the ALREADY-LOADED tenant client row. Authentication and
                 tenant matching are B2 route concerns, not B1.
        request: a VALIDATED AvailabilityPreviewRequest — constructing it is
                 what enforces the date/range/selected_day rules, so raw
                 unvalidated primitives cannot reach this function.
        now_utc: the current aware-UTC instant, INJECTED for deterministic
                 tests (DST boundaries, expired holds). Naive input is
                 normalized via ensure_utc like every other calendar entry
                 point.
    Returns: AvailabilityPreviewResponse (see app/schemas.py contract locks).
    Database effects: exactly ONE SELECT via
             appointment_repository.list_slots_between. Nothing else.
    External effects: none — no notification, hold, booking, or conversation
             function is called (spec boundary, enforced by tests).
    Possible failures:
        ZoneInfo KeyError-family — invalid configured timezone name surfaces
            (Rule 16), identical to local_day_utc_window's documented
            contract.
        Database errors propagate (Rule 4 — never hidden).

    Range boundaries: EACH boundary is resolved independently through the
    existing local-day UTC-window owner — range start is start_day's window
    start; range end is end_day's window end. The range is NEVER computed as
    start + N * 24 hours, so 23h/25h DST days query their true boundaries.
    """
    # Settings + timezone through the existing owners only: CalendarSettings
    # embeds resolve_client_timezone's result as timezone_name.
    settings = load_calendar_settings(client)
    tz = ZoneInfo(settings.timezone_name)

    normalized_now = ensure_utc(now_utc)
    today_local = normalized_now.astimezone(tz).date()

    range_start_utc, _ = local_day_utc_window(
        request.start_day, settings.timezone_name
    )
    _, range_end_utc = local_day_utc_window(
        request.end_day, settings.timezone_name
    )

    # THE one range query — seven-day, selected-day, and full 31-day month
    # requests all share this single SELECT (B1 spec section D).
    rows = appointment_repository.list_slots_between(
        db, client.id, range_start_utc, range_end_utc
    )

    # Bucket by OFFICE-LOCAL calendar date. Each row's own start instant is
    # converted independently, so rows near a DST transition land on the
    # local day a human would see on the office wall calendar.
    rows_by_local_date: Dict[date, List] = defaultdict(list)
    for row in rows:
        local_date = ensure_utc(row.start_datetime).astimezone(tz).date()
        rows_by_local_date[local_date].append(row)

    days: List[PreviewDay] = []
    selected_slots: List[PreviewSlot] = []
    day = request.start_day
    while day <= request.end_day:  # timedelta on date objects is exact
        state, bookable = _classify_day(   # calendar arithmetic — this loop
            day,                           # is NOT the forbidden start+N*24h
            rows_by_local_date.get(day, []),  # UTC shortcut (dates, not
            today_local,                      # instants).
            normalized_now,
            settings,
            request.service_key,
        )
        days.append(
            PreviewDay(
                local_date=day,
                weekday=day.strftime("%A"),
                state=state,
                # Contract lock: only an "open" day is selectable.
                selectable=(state == DAY_STATE_OPEN),
            )
        )
        # Selected-day slots are emitted ONLY when the request asked for a
        # selected day, and only for that exact day (contract lock).
        if request.selected_day is not None and day == request.selected_day:
            # Preview-internal total order: equal-start slots are further
            # ordered by end then internal id, so output is deterministic
            # for ANY repository input order (see _preview_ordering_key).
            selected_slots = [
                _slot_view(slot, tz)
                for slot in sorted(bookable, key=_preview_ordering_key)
            ]
        day += timedelta(days=1)

    return AvailabilityPreviewResponse(
        client_id=str(client.id),
        practice_name=client.practice_name,
        timezone_name=settings.timezone_name,
        # Informational only: a frozen tenant still gets a rendered preview.
        booking_enabled=settings.booking_enabled,
        range_start=request.start_day,
        range_end=request.end_day,
        generated_at=normalized_now,
        days=days,
        selected_day=request.selected_day,
        slots=selected_slots,
    )
