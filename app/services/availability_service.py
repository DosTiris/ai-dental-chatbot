# app/services/availability_service.py
#
# OWNER OF: answering "what appointment times are actually available?"
#
# MVP model ("Model B" — controlled slots): staff publishes explicit slot rows;
# this service fetches them and applies the pure rules in availability_rules.
# It does not generate candidate times from office hours — that computed
# engine belongs to a later approved phase (Rule 17).

import uuid
from datetime import date, datetime, timedelta
from typing import List, Optional

from app.repositories import appointment_repository
from app.services.appointment_intent import PREF_ANY
from app.services.availability_rules import filter_bookable_slots, list_bookable_slots
from app.services.calendar_settings_service import CalendarSettings, local_day_utc_window


def get_available_slots(
    db,
    client_id: uuid.UUID,
    settings: CalendarSettings,
    day: date,
    time_preference: str,
    now_utc: datetime,
    service_key: Optional[str] = None,
) -> List:
    """
    Purpose: Fetch + filter bookable slots for one LOCAL calendar day.
    Inputs:  `day` is a date in the CLIENT's timezone. It is converted to a
             UTC window here so a 9 AM New York slot stored as 14:00 UTC is
             found on the right day (Rule 9: timezone boundaries).
    Returns: bookable slot rows, soonest first, capped at max_offered_slots.
    Database effects: SELECT only (via repository).
    Possible failures: database errors propagate to the caller (Rule 4 — no
        broad exception handling that hides failures).

    The UTC window comes from local_day_utc_window (Patch 2B): both local
    midnights are converted independently, so local dates containing an
    offset transition (23h/25h days) query their true boundaries instead of
    a hardcoded start+24h.
    """
    day_start_utc, day_end_utc = local_day_utc_window(day, settings.timezone_name)

    rows = appointment_repository.list_slots_between(db, client_id, day_start_utc, day_end_utc)
    return filter_bookable_slots(rows, now_utc, settings, time_preference, service_key)


def get_bookable_slots_for_day(
    db,
    client_id: uuid.UUID,
    settings: CalendarSettings,
    day: date,
    time_preference: str,
    now_utc: datetime,
    service_key: Optional[str] = None,
) -> List:
    """
    Purpose: Fetch + filter EVERY bookable slot for one LOCAL calendar day -
             the UNCAPPED sibling of get_available_slots (UX-A slot
             pagination). Same UTC-window fetch, same single pure rule owner
             (list_bookable_slots); the ONLY difference is that
             max_offered_slots is not applied here, so the offer owner in
             booking_conversation can page through the full chronological
             day without a second slot engine (Rule 3).
    Inputs:  identical to get_available_slots.
    Returns: EVERY bookable slot row for the day, soonest first, uncapped.
             By construction get_available_slots(...) equals
             get_bookable_slots_for_day(...)[: settings.max_offered_slots]
             (filter_bookable_slots is documented as exactly that thin cap
             over list_bookable_slots); a UX-A acceptance test pins the
             equivalence so the two callers can never drift apart.
    Database effects: SELECT only (via repository).
    Possible failures: database errors propagate to the caller (Rule 4 - no
        broad exception handling that hides failures).
    """
    day_start_utc, day_end_utc = local_day_utc_window(day, settings.timezone_name)

    rows = appointment_repository.list_slots_between(db, client_id, day_start_utc, day_end_utc)
    return list_bookable_slots(rows, now_utc, settings, time_preference, service_key)


def find_days_with_availability(
    db,
    client_id: uuid.UUID,
    settings: CalendarSettings,
    start_day: date,
    now_utc: datetime,
    days_to_scan: int = 7,
    max_days_to_return: int = 3,
    time_preference: str = PREF_ANY,
    service_key: Optional[str] = None,
    skip_start_day: bool = False,
) -> List[date]:
    """
    Purpose: When the requested day has nothing, suggest nearby days that do.
    Inputs:
        time_preference: the SAME preference bucket the rejected day was
            filtered with. Defaults to PREF_ANY, which is the pre-existing
            behavior, so no existing caller changes meaning by accident.
        service_key: the SAME service filter the rejected day was filtered
            with. Defaults to None (no service filtering) - also the
            pre-existing behavior.
        skip_start_day: when True the scan begins at start_day + 1. A caller
            that has just told the patient start_day is unavailable MUST set
            this, otherwise the rejected day is offered straight back.
    Returns: up to max_days_to_return dates (client-timezone) with >=1
             bookable slot, scanning start_day .. start_day + days_to_scan,
             or start_day + 1 .. start_day + days_to_scan when
             skip_start_day is set.
    Database effects: SELECT only.
    Possible failures: database errors propagate to the caller (Rule 4 - no
        broad exception handling that hides failures).

    Both filters are passed straight through to get_available_slots, so a day
    is suggested only when it would really produce a menu for THIS patient.
    Filtering the suggestion scan differently from the offer query is what
    produced the staging contradiction on 2026-07-27: a tooth-pain request
    was told July 27 had no openings and was then offered July 27, because
    three cleaning/checkup slots matched the unfiltered scan. The policy
    rules themselves were correct and are unchanged; only this caller was
    under-supplying them.
    """
    found: List[date] = []
    first_offset = 1 if skip_start_day else 0
    for offset in range(first_offset, days_to_scan + 1):
        candidate = start_day + timedelta(days=offset)
        slots = get_available_slots(
            db, client_id, settings, candidate, time_preference, now_utc,
            service_key=service_key,
        )
        if slots:
            found.append(candidate)
        if len(found) >= max_days_to_return:
            break
    return found
