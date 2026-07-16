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
from app.services.availability_rules import filter_bookable_slots
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


def find_days_with_availability(
    db,
    client_id: uuid.UUID,
    settings: CalendarSettings,
    start_day: date,
    now_utc: datetime,
    days_to_scan: int = 7,
    max_days_to_return: int = 3,
) -> List[date]:
    """
    Purpose: When the requested day has nothing, suggest nearby days that do.
    Returns: up to max_days_to_return dates (client-timezone) with >=1
             bookable slot, scanning start_day .. start_day + days_to_scan.
    Database effects: SELECT only.
    """
    found: List[date] = []
    for offset in range(days_to_scan + 1):
        candidate = start_day + timedelta(days=offset)
        slots = get_available_slots(
            db, client_id, settings, candidate, time_preference="any", now_utc=now_utc
        )
        if slots:
            found.append(candidate)
        if len(found) >= max_days_to_return:
            break
    return found
