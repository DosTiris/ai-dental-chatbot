# calendar_tests/test_appointment_intent.py
#
# Pure tests for date parsing, time preference, slot selection, yes/no.
# No database, no external packages. Run: pytest calendar_tests/ -v
# (also runnable as a plain script: python3 calendar_tests/test_appointment_intent.py)

import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.appointment_intent import (  # noqa: E402
    PREF_AFTERNOON,
    PREF_ANY,
    PREF_EVENING,
    PREF_MORNING,
    match_slot_selection,
    parse_preferred_date,
    parse_time_preference,
    parse_yes_no,
)

# Saturday, July 11, 2026 — a fixed "today" so results are deterministic.
TODAY = date(2026, 7, 11)


def test_relative_days():
    assert parse_preferred_date("can I come in today?", TODAY) == TODAY
    assert parse_preferred_date("tomorrow works", TODAY) == date(2026, 7, 12)
    assert parse_preferred_date("day after tomorrow", TODAY) == date(2026, 7, 13)


def test_bare_weekday_is_next_occurrence_including_today():
    # Today is Saturday; "thursday" -> the coming Thursday (July 16).
    assert parse_preferred_date("thursday afternoon", TODAY) == date(2026, 7, 16)
    # "saturday" said on a Saturday -> today.
    assert parse_preferred_date("saturday", TODAY) == TODAY


def test_next_weekday_is_strictly_after_today():
    # "next saturday" said ON Saturday -> a week out, never today.
    assert parse_preferred_date("next saturday", TODAY) == date(2026, 7, 18)
    assert parse_preferred_date("next thursday", TODAY) == date(2026, 7, 16)


def test_explicit_dates_roll_forward_when_past():
    assert parse_preferred_date("july 16", TODAY) == date(2026, 7, 16)
    assert parse_preferred_date("7/16", TODAY) == date(2026, 7, 16)
    # July 2 already passed this year -> assume next year.
    assert parse_preferred_date("july 2", TODAY) == date(2027, 7, 2)
    # Explicit past year is a mistake, not a rollover.
    assert parse_preferred_date("7/16/2020", TODAY) is None
    # Impossible date never crashes.
    assert parse_preferred_date("2/30", TODAY) is None


def test_no_date_returns_none():
    assert parse_preferred_date("my tooth hurts", TODAY) is None
    assert parse_preferred_date("", TODAY) is None


def test_time_preferences():
    assert parse_time_preference("morning please") == PREF_MORNING
    assert parse_time_preference("Afternoon") == PREF_AFTERNOON
    assert parse_time_preference("after work") == PREF_EVENING
    assert parse_time_preference("any time is fine") == PREF_ANY
    assert parse_time_preference("no preference") == PREF_ANY
    # No preference expressed -> None so the flow re-asks (never assumes).
    assert parse_time_preference("thursday") is None


def _offered():
    # 10:00 AM, 1:30 PM, 3:45 PM — order as displayed to the patient.
    return [
        ("slot-a", datetime(2026, 7, 16, 10, 0)),
        ("slot-b", datetime(2026, 7, 16, 13, 30)),
        ("slot-c", datetime(2026, 7, 16, 15, 45)),
    ]


def test_slot_selection_by_ordinal_and_index():
    assert match_slot_selection("the second one", _offered()) == "slot-b"
    assert match_slot_selection("1", _offered()) == "slot-a"
    assert match_slot_selection("option 3", _offered()) == "slot-c"


def test_slot_selection_by_time():
    assert match_slot_selection("1:30 works", _offered()) == "slot-b"
    assert match_slot_selection("10am", _offered()) == "slot-a"
    assert match_slot_selection("3:45 pm please", _offered()) == "slot-c"
    # "1" matches 1:00 PM by time only if such a slot exists; here it is an
    # index and picks the first option.
    assert match_slot_selection("1", _offered()) == "slot-a"


def test_slot_selection_rejects_unoffered_and_ambiguous():
    # 2:00 PM was never offered -> no match, Mia re-asks.
    assert match_slot_selection("2:00", _offered()) is None
    assert match_slot_selection("whatever is fine", _offered()) is None
    assert match_slot_selection("9", _offered()) is None  # index out of range
    # Ambiguous "10" with both 10 AM and 10 PM offered -> re-ask.
    ambiguous = [("a", datetime(2026, 7, 16, 10, 0)), ("b", datetime(2026, 7, 16, 22, 0))]
    assert match_slot_selection("10", ambiguous) is None


def test_yes_no():
    assert parse_yes_no("yes please") is True
    assert parse_yes_no("that works, book it") is True
    assert parse_yes_no("no, that's wrong") is False
    assert parse_yes_no("can we change it") is False
    # Mixed signals must never book (found in the Rule-10 mental test).
    assert parse_yes_no("ok but can we do 2pm instead") is None
    assert parse_yes_no("hmm") is None


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    sys.exit(1 if failures else 0)
