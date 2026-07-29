# calendar_tests/test_availability_day_suggestions.py
#
# Focused tests for find_days_with_availability() — the owner of the
# "nearest days with availability" list.
#
# Staging defect these pin (2026-07-26, Mia Staging Dental):
#     "I don't see openings on Monday, July 27.
#      The nearest days with availability are: Monday, July 27."
#
# Supabase confirmed July 27 held three available slots with
# service_key="cleaning/checkup", while the patient's request was severe
# tooth pain. The offer query filtered by service and correctly found
# nothing; the suggestion scan omitted service_key, hardcoded
# time_preference="any", and started at offset 0 — so it re-offered the day
# it had just rejected.
#
# The slot POLICY rules were never wrong and are not touched here. These
# tests prove the scan now supplies those rules with the same inputs the
# offer query uses.
#
# No database: appointment_repository.list_slots_between is monkeypatched, so
# the real get_available_slots -> filter_bookable_slots -> evaluate_slot_policy
# chain still executes.

import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# availability_service reaches the repository layer, which needs SQLAlchemy.
# Machines without it skip this module rather than erroring at collection —
# the same defensive pattern test_availability_rules.py uses.
try:
    from app.services import availability_service  # noqa: E402
    from app.services.appointment_intent import PREF_ANY, PREF_MORNING  # noqa: E402
    from app.services.calendar_settings_service import CalendarSettings  # noqa: E402
    HAVE_SERVICE = True
except ModuleNotFoundError:  # pragma: no cover - environment guard
    HAVE_SERVICE = False

pytestmark = pytest.mark.skipif(
    not HAVE_SERVICE, reason="availability_service requires SQLAlchemy"
)

UTC = ZoneInfo("UTC")
NY = ZoneInfo("America/New_York")

# Fixed "now": Sunday July 26, 2026, 9:00 AM New York = 13:00 UTC. This is the
# staging date, so REQUESTED_DAY below is the real July 27.
NOW_UTC = datetime(2026, 7, 26, 13, 0, tzinfo=UTC)
REQUESTED_DAY = date(2026, 7, 27)

SETTINGS = CalendarSettings(
    booking_enabled=True,
    hold_minutes=5,
    minimum_notice_minutes=60,
    max_offered_slots=3,
    max_booking_days=30,
    require_staff_confirmation=True,
    timezone_name="America/New_York",
)

CLIENT_ID = "11111111-1111-1111-1111-111111111111"

TOOTH_PAIN = "tooth pain"
CLEANING = "cleaning/checkup"


def slot_at(day: date, local_hour: int, service_key=None, status="available"):
    """One staff-published slot at a LOCAL hour on a LOCAL day."""
    start = datetime(day.year, day.month, day.day, local_hour, 0, tzinfo=NY).astimezone(UTC)
    return SimpleNamespace(
        id=f"slot-{day.isoformat()}-{local_hour}-{service_key}",
        status=status,
        start_datetime=start,
        end_datetime=start + timedelta(minutes=45),
        held_until=None,
        service_key=service_key,
    )


@pytest.fixture()
def fake_slots(monkeypatch):
    """Replace only the repository fetch. Every real rule still runs."""
    store = []

    def list_slots_between(db, client_id, start_utc, end_utc):
        return [s for s in store if start_utc <= s.start_datetime < end_utc]

    monkeypatch.setattr(
        availability_service.appointment_repository,
        "list_slots_between",
        list_slots_between,
    )
    return store


def scan(fake_slots, **kwargs):
    return availability_service.find_days_with_availability(
        None, CLIENT_ID, SETTINGS, REQUESTED_DAY, NOW_UTC, **kwargs
    )


# ---------------------------------------------------------------------------
# 1. The requested day is excluded from its own suggestion list
# ---------------------------------------------------------------------------

def test_requested_day_is_excluded_from_its_own_suggestions(fake_slots):
    """The exact contradiction: the day just declared unavailable must never
    come back as the nearest day with availability."""
    fake_slots.append(slot_at(REQUESTED_DAY, 10))

    days = scan(fake_slots, skip_start_day=True)

    assert REQUESTED_DAY not in days
    assert days == []


# ---------------------------------------------------------------------------
# 2-3. The scan respects service_key
# ---------------------------------------------------------------------------

def test_scan_respects_service_key(fake_slots):
    """A day whose only slots are reserved for another service is not a day
    with availability for THIS patient."""
    other_day = REQUESTED_DAY + timedelta(days=1)
    fake_slots.append(slot_at(other_day, 10, service_key=CLEANING))

    assert scan(fake_slots, service_key=TOOTH_PAIN, skip_start_day=True) == []
    # The parameter is what changes the outcome — nothing else moved.
    assert scan(fake_slots, service_key=None, skip_start_day=True) == [other_day]


def test_cleaning_checkup_slots_are_not_suggested_for_tooth_pain(fake_slots):
    """The exact staging data: three available cleaning/checkup slots on
    July 27 against a severe-tooth-pain request."""
    for hour in (9, 11, 14):
        fake_slots.append(slot_at(REQUESTED_DAY, hour, service_key=CLEANING))

    days = scan(fake_slots, time_preference=PREF_MORNING,
                service_key=TOOTH_PAIN, skip_start_day=True)

    assert REQUESTED_DAY not in days
    assert days == []


# ---------------------------------------------------------------------------
# 4. The scan respects morning/afternoon preference
# ---------------------------------------------------------------------------

def test_scan_respects_time_preference(fake_slots):
    """A morning request must not be offered an afternoon-only day."""
    other_day = REQUESTED_DAY + timedelta(days=1)
    fake_slots.append(slot_at(other_day, 15))  # afternoon only

    assert scan(fake_slots, time_preference=PREF_MORNING, skip_start_day=True) == []
    assert scan(fake_slots, time_preference=PREF_ANY, skip_start_day=True) == [other_day]


# ---------------------------------------------------------------------------
# 5. No matching days reaches the honest office-help fallback
# ---------------------------------------------------------------------------

def test_no_matching_days_returns_empty_list(fake_slots):
    """An empty list is what makes _suggest_other_days use the honest
    office-help wording instead of inventing an option."""
    for hour in (9, 11, 14):
        fake_slots.append(slot_at(REQUESTED_DAY, hour, service_key=CLEANING))
    fake_slots.append(slot_at(REQUESTED_DAY + timedelta(days=2), 15, service_key=CLEANING))

    days = scan(fake_slots, time_preference=PREF_MORNING,
                service_key=TOOTH_PAIN, skip_start_day=True)

    assert days == []


# ---------------------------------------------------------------------------
# 6. Scan window and maximum returned days stay enforced
# ---------------------------------------------------------------------------

def test_scan_window_and_max_days_are_enforced(fake_slots):
    for offset in range(1, 11):
        fake_slots.append(slot_at(REQUESTED_DAY + timedelta(days=offset), 10))

    days = scan(fake_slots, skip_start_day=True)

    assert len(days) == 3, "max_days_to_return default must still cap the list"
    assert days == [REQUESTED_DAY + timedelta(days=n) for n in (1, 2, 3)]

    # Nothing beyond start_day + days_to_scan is ever considered.
    far = scan(fake_slots, skip_start_day=True, days_to_scan=2, max_days_to_return=10)
    assert far == [REQUESTED_DAY + timedelta(days=n) for n in (1, 2)]


# ---------------------------------------------------------------------------
# Compatibility: the new parameters default to the previous behavior
# ---------------------------------------------------------------------------

def test_defaults_preserve_previous_behavior(fake_slots):
    """Called with no new arguments the function behaves exactly as before:
    it starts at offset 0 and applies no preference or service filter."""
    fake_slots.append(slot_at(REQUESTED_DAY, 15, service_key=CLEANING))

    assert scan(fake_slots) == [REQUESTED_DAY]
