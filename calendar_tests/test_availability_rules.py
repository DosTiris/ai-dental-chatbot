# calendar_tests/test_availability_rules.py
#
# Pure tests for the availability business rules and settings loader.
# No database, no external packages. Run: pytest calendar_tests/ -v
# (also runnable directly: python3 calendar_tests/test_availability_rules.py)

import sys
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# calendar_models needs SQLAlchemy; on machines without it the literal-sync
# test is skipped and a stub with the canonical strings is used instead.
try:
    from app.calendar_models import SlotStatus  # noqa: E402
    HAVE_MODELS = True
except ModuleNotFoundError:
    HAVE_MODELS = False

    class SlotStatus:  # matches calendar_models; sync verified when available
        AVAILABLE = "available"
        HELD = "held"
        BOOKED = "booked"
        BLOCKED = "blocked"
        CANCELLED = "cancelled"
from app.services.availability_rules import (  # noqa: E402
    STATUS_AVAILABLE,
    STATUS_HELD,
    SlotPolicyResult,
    evaluate_slot_policy,
    filter_bookable_slots,
    hold_is_active,
)
from app.services.calendar_settings_service import (  # noqa: E402
    CalendarSettings,
    ensure_utc,
    load_calendar_settings,
    local_day_utc_window,
)

UTC = ZoneInfo("UTC")
NY = ZoneInfo("America/New_York")

# Fixed "now": Saturday July 11, 2026, 9:00 AM New York = 13:00 UTC.
NOW_UTC = datetime(2026, 7, 11, 13, 0, tzinfo=UTC)

SETTINGS = CalendarSettings(
    booking_enabled=True,
    hold_minutes=5,
    minimum_notice_minutes=60,
    max_offered_slots=3,
    max_booking_days=30,
    require_staff_confirmation=True,
    timezone_name="America/New_York",
)


def slot(hours_from_now=24.0, status=SlotStatus.AVAILABLE, held_until=None,
         service_key=None):
    start = NOW_UTC + timedelta(hours=hours_from_now)
    return SimpleNamespace(
        id=f"slot-{hours_from_now}-{status}",
        status=status,
        start_datetime=start,
        end_datetime=start + timedelta(minutes=45),
        held_until=held_until,
        service_key=service_key,
    )


def test_status_literals_stay_in_sync_with_models():
    # availability_rules keeps import-light literals; they must match the
    # single source of truth in calendar_models (guarded here, Rule 3).
    if not HAVE_MODELS:
        print("  (SQLAlchemy not installed: sync check ran against stub)")
    assert STATUS_AVAILABLE == SlotStatus.AVAILABLE
    assert STATUS_HELD == SlotStatus.HELD


def test_only_available_and_expired_held_are_offered():
    active_hold = slot(status=SlotStatus.HELD,
                       held_until=NOW_UTC + timedelta(minutes=3))
    expired_hold = slot(hours_from_now=25, status=SlotStatus.HELD,
                        held_until=NOW_UTC - timedelta(minutes=1))
    rows = [
        slot(hours_from_now=26),                       # available -> offered
        active_hold,                                    # held (active) -> hidden
        expired_hold,                                   # held (expired) -> offered
        slot(hours_from_now=27, status=SlotStatus.BOOKED),
        slot(hours_from_now=28, status=SlotStatus.BLOCKED),
        slot(hours_from_now=29, status=SlotStatus.CANCELLED),
    ]
    result = filter_bookable_slots(rows, NOW_UTC, SETTINGS, "any")
    ids = {s.id for s in result}
    assert expired_hold.id in ids and len(result) == 2
    assert active_hold.id not in ids


def test_minimum_notice_and_horizon():
    too_soon = slot(hours_from_now=0.5)            # 30 min < 60 min notice
    fine = slot(hours_from_now=2)
    too_far = slot(hours_from_now=24 * 31)         # beyond 30-day horizon
    result = filter_bookable_slots([too_soon, fine, too_far], NOW_UTC, SETTINGS, "any")
    assert [s.id for s in result] == [fine.id]


def test_time_preference_uses_client_local_hour():
    # 18:00 UTC on July 16 is 2:00 PM in New York -> afternoon, NOT evening.
    afternoon_ny = SimpleNamespace(
        id="ny-afternoon", status=SlotStatus.AVAILABLE, held_until=None,
        service_key=None,
        start_datetime=datetime(2026, 7, 16, 18, 0, tzinfo=UTC),
        end_datetime=datetime(2026, 7, 16, 18, 45, tzinfo=UTC),
    )
    assert filter_bookable_slots([afternoon_ny], NOW_UTC, SETTINGS, "afternoon")
    assert not filter_bookable_slots([afternoon_ny], NOW_UTC, SETTINGS, "evening")
    assert not filter_bookable_slots([afternoon_ny], NOW_UTC, SETTINGS, "morning")


def test_service_specific_slots():
    generic = slot(hours_from_now=26)
    implant_only = slot(hours_from_now=27, service_key="extraction/implant")
    rows = [generic, implant_only]
    cleaning = filter_bookable_slots(rows, NOW_UTC, SETTINGS, "any",
                                     service_key="cleaning/checkup")
    assert [s.id for s in cleaning] == [generic.id]
    implant = filter_bookable_slots(rows, NOW_UTC, SETTINGS, "any",
                                    service_key="extraction/implant")
    assert {s.id for s in implant} == {generic.id, implant_only.id}


def test_results_sorted_and_capped():
    rows = [slot(hours_from_now=h) for h in (50, 26, 40, 30, 35)]
    result = filter_bookable_slots(rows, NOW_UTC, SETTINGS, "any")
    starts = [ensure_utc(s.start_datetime) for s in result]
    assert starts == sorted(starts) and len(result) == 3  # max_offered_slots


def test_hold_is_active_handles_naive_utc_from_sqlite():
    naive = SimpleNamespace(status=SlotStatus.HELD,
                            held_until=(NOW_UTC + timedelta(minutes=2)).replace(tzinfo=None))
    assert hold_is_active(naive, NOW_UTC) is True


def test_settings_defaults_and_bounds():
    class FakeClient:
        settings = {"calendar": {"booking_enabled": True, "hold_minutes": 9999,
                                 "max_offered_slots": 0}}
        timezone = None
    s = load_calendar_settings(FakeClient())
    assert s.booking_enabled is True
    assert s.hold_minutes == 60          # clamped to the documented ceiling
    assert s.max_offered_slots == 1      # clamped to the documented floor
    assert s.timezone_name == "America/New_York"

    class Bare:
        settings = None
    bare = load_calendar_settings(Bare())
    assert bare.booking_enabled is False  # calendar is OPT-IN per office
    assert bare.require_staff_confirmation is True

    class ZeroHorizon:  # Patch 2B: the floor is 0 — JSON 0 must SURVIVE,
        settings = {"calendar": {"max_booking_days": 0}}  # meaning "today only".
        timezone = None
    assert load_calendar_settings(ZeroHorizon()).max_booking_days == 0


# ---------------------------------------------------------------------------
# PATCH 2A — strict boolean settings (Senior Audit Critical #6).
# bool("false") is True, so truthiness parsing could silently enable booking
# for an office that never opted in. Only real JSON booleans may count.
# 17 cases/assertions total: 9 booking_enabled + 7 require_staff_confirmation
# + 1 consumer-contract assertion.
# ---------------------------------------------------------------------------

def _client_with_calendar(**calendar):
    """A fake client whose settings.calendar contains exactly these keys."""
    return SimpleNamespace(settings={"calendar": dict(calendar)}, timezone=None)


def test_booking_enabled_strict_boolean_matrix():
    """booking_enabled: ONLY JSON true enables; everything else disables.
    Asserted with `is` (identity), never truthiness. 9 cases."""
    def enabled(**calendar):
        return load_calendar_settings(_client_with_calendar(**calendar)).booking_enabled

    assert enabled(booking_enabled=True) is True        # JSON true -> enabled
    assert enabled(booking_enabled=False) is False      # JSON false -> disabled
    assert enabled() is False                           # missing -> disabled (opt-in)
    assert enabled(booking_enabled="false") is False    # string never enables...
    assert enabled(booking_enabled="true") is False     # ...not even "true":
    #   bool("true") AND bool("false") are both True — strings are rejected
    #   wholesale rather than interpreted (Critical #6).
    assert enabled(booking_enabled=1) is False          # 1 == True in Python, but
    #   isinstance(1, bool) is False — the integer must NOT enable booking.
    assert enabled(booking_enabled=0) is False          # number -> disabled
    assert enabled(booking_enabled=None) is False       # JSON null -> disabled
    assert enabled(booking_enabled="yes") is False      # the audit's example


def test_require_staff_confirmation_strict_boolean_matrix():
    """require_staff_confirmation: ONLY JSON false disables; malformed or
    missing values keep the safety default ON. 7 cases."""
    def confirmation(**calendar):
        return load_calendar_settings(
            _client_with_calendar(**calendar)
        ).require_staff_confirmation

    assert confirmation(require_staff_confirmation=True) is True     # JSON true
    assert confirmation(require_staff_confirmation=False) is False   # JSON false —
    #   the ONLY way to turn the safety off.
    assert confirmation() is True                                    # missing -> ON
    assert confirmation(require_staff_confirmation="false") is True  # string -> ON
    assert confirmation(require_staff_confirmation="true") is True   # string -> ON
    assert confirmation(require_staff_confirmation=0) is True        # falsy garbage
    #   must NOT switch off the pending-confirmation safety (inverted failure
    #   direction vs booking_enabled: here falsy junk is the danger).
    assert confirmation(require_staff_confirmation=None) is True     # null -> ON


def test_consumer_contract_malformed_opt_in_is_refused():
    """Consumer-contract assertion (NOT end-to-end — booking_conversation.py
    is outside this patch and is not executed here): the booking dialog's
    actual gate expression is `if not settings.booking_enabled: refuse`
    (booking_conversation.py:154). This proves that expression refuses a
    client whose opt-in is the malformed string "true". 1 assertion."""
    settings = load_calendar_settings(_client_with_calendar(booking_enabled="true"))
    assert (not settings.booking_enabled) is True  # the gate refuses



# ---------------------------------------------------------------------------
# PATCH 2B — DST-safe local-day windows (Critical #7) and local-calendar-date
# booking horizon (Recommended #4).
# ---------------------------------------------------------------------------

def _slot_at(start_utc, slot_id=None):
    """A minimal slot stub starting at an exact aware-UTC instant."""
    return SimpleNamespace(
        id=slot_id or start_utc.isoformat(),
        status=SlotStatus.AVAILABLE,
        held_until=None,
        service_key=None,
        start_datetime=start_utc,
        end_datetime=start_utc + timedelta(minutes=45),
    )


def test_local_day_window_normal_day():
    """A normal NY day is exactly 24h with exact UTC boundaries — and the
    timezone_name argument is honored (Los Angeles differs by 3 hours, so a
    hardcoded New York would fail these assertions)."""
    start, end = local_day_utc_window(date(2026, 7, 16), "America/New_York")
    assert start == datetime(2026, 7, 16, 4, 0, tzinfo=UTC)   # EDT = UTC-4
    assert end == datetime(2026, 7, 17, 4, 0, tzinfo=UTC)
    assert end - start == timedelta(hours=24)

    la_start, la_end = local_day_utc_window(date(2026, 7, 16), "America/Los_Angeles")
    assert la_start == datetime(2026, 7, 16, 7, 0, tzinfo=UTC)  # PDT = UTC-7
    assert la_end == datetime(2026, 7, 17, 7, 0, tzinfo=UTC)
    assert la_end - la_start == timedelta(hours=24)


def test_local_day_window_spring_forward_is_23_hours():
    """2026-03-08 America/New_York: EST midnight start, EDT midnight end."""
    start, end = local_day_utc_window(date(2026, 3, 8), "America/New_York")
    assert start == datetime(2026, 3, 8, 5, 0, tzinfo=UTC)   # EST = UTC-5
    assert end == datetime(2026, 3, 9, 4, 0, tzinfo=UTC)     # EDT = UTC-4
    assert end - start == timedelta(hours=23)                # NOT start+24h


def test_local_day_window_fall_back_is_25_hours():
    """2026-11-01 America/New_York: EDT midnight start, EST midnight end.
    The old start+24h formula lost this day's final hour (11 PM-midnight)."""
    start, end = local_day_utc_window(date(2026, 11, 1), "America/New_York")
    assert start == datetime(2026, 11, 1, 4, 0, tzinfo=UTC)  # EDT = UTC-4
    assert end == datetime(2026, 11, 2, 5, 0, tzinfo=UTC)    # EST = UTC-5
    assert end - start == timedelta(hours=25)


def test_horizon_full_final_local_date_allowed():
    """now = 2026-07-11 9:00 AM NY, horizon 30 -> latest allowed LOCAL date
    is 2026-08-10. The WHOLE of that local date must be bookable, and the
    next local date must not be.

    The old exact-instant rule (now + 30 days = 2026-08-10 09:00 local)
    wrongly rejected the 7:30 PM slot on the final date even though the
    booking conversation had already accepted the date. The next-day 8:00 AM
    case was rejected by the old rule too (08:00 < 09:00) and stays rejected
    — it proves the next local calendar date is out, not a behavior change.
    """
    morning = _slot_at(datetime(2026, 8, 10, 12, 0, tzinfo=UTC), "aug10-8am")    # 8:00 AM NY —
    #   EARLIER than now's 9:00 AM clock time, so it proves the rule is
    #   date-based, not instant-based, in the accepting direction.
    evening = _slot_at(datetime(2026, 8, 10, 23, 30, tzinfo=UTC), "aug10-730pm") # 7:30 PM NY —
    #   AFTER now's clock time on the final date: the case the old rule broke.
    next_day = _slot_at(datetime(2026, 8, 11, 12, 0, tzinfo=UTC), "aug11-8am")   # 8:00 AM NY, Aug 11.

    result = filter_bookable_slots([morning, evening, next_day], NOW_UTC, SETTINGS, "any")
    assert [s.id for s in result] == ["aug10-8am", "aug10-730pm"]


def test_horizon_zero_days_allows_today_only():
    """max_booking_days=0 means today's LOCAL date: later today accepted,
    tomorrow rejected. (The settings loader's floor is 0 so this is a real,
    configurable production value — asserted in the loader test.)"""
    today_only = replace(SETTINGS, max_booking_days=0)
    later_today = _slot_at(datetime(2026, 7, 11, 19, 0, tzinfo=UTC), "today-3pm")  # 3 PM NY today
    tomorrow = _slot_at(datetime(2026, 7, 12, 14, 0, tzinfo=UTC), "tmrw-10am")     # 10 AM NY tomorrow
    result = filter_bookable_slots([later_today, tomorrow], NOW_UTC, today_only, "any")
    assert [s.id for s in result] == ["today-3pm"]


def test_minimum_notice_is_exact_elapsed_minutes():
    """The notice rule stays an EXACT elapsed-time rule (never calendar-
    based): with 60 minutes notice from now=13:00 UTC — 59 min out rejected,
    exactly 60 accepted, 61 accepted, past rejected."""
    at_59 = _slot_at(NOW_UTC + timedelta(minutes=59), "m59")
    at_60 = _slot_at(NOW_UTC + timedelta(minutes=60), "m60")
    at_61 = _slot_at(NOW_UTC + timedelta(minutes=61), "m61")
    past = _slot_at(NOW_UTC - timedelta(hours=1), "past")
    result = filter_bookable_slots([at_59, at_60, at_61, past], NOW_UTC, SETTINGS, "any")
    assert [s.id for s in result] == ["m60", "m61"]



# ---------------------------------------------------------------------------
# PATCH 2C — the single pure policy owner (Senior Audit Critical #8).
# ---------------------------------------------------------------------------

def test_policy_owner_reason_matrix():
    """evaluate_slot_policy returns the EXACT deterministic reason for each
    rule violated in isolation, and "ok" for an eligible slot. Semantics are
    unchanged from Patch 2B (the untouched notice/horizon boundary tests
    above still prove the boundaries); this pins the reason vocabulary."""
    def judge(the_slot, preference="any", service=None):
        return evaluate_slot_policy(the_slot, now_utc=NOW_UTC, settings=SETTINGS,
                                    time_preference=preference, service_key=service)

    eligible = slot(hours_from_now=24)
    assert judge(eligible) == SlotPolicyResult(True, "ok")

    too_soon = slot(hours_from_now=0.5)                 # 30 min < 60 min notice
    assert judge(too_soon) == SlotPolicyResult(False, "too_soon")

    past = slot(hours_from_now=-2)                      # past slots are too_soon
    assert judge(past) == SlotPolicyResult(False, "too_soon")

    beyond = _slot_at(datetime(2026, 8, 11, 12, 0, tzinfo=UTC))  # horizon+1 local day
    assert judge(beyond) == SlotPolicyResult(False, "beyond_horizon")

    afternoon_ny = _slot_at(datetime(2026, 7, 16, 18, 0, tzinfo=UTC))  # 2 PM NY
    assert judge(afternoon_ny, preference="morning") == SlotPolicyResult(
        False, "preference_mismatch")

    reserved = slot(hours_from_now=24, service_key="implant consult")
    assert judge(reserved, service="cleaning/checkup") == SlotPolicyResult(
        False, "service_mismatch")
    # NULL slot service stays generic; NULL request matches anything —
    # the same equality rule display filtering has always used.
    assert judge(reserved, service=None).eligible is True
    assert judge(eligible, service="implant consult").eligible is True


def test_display_filter_delegates_to_policy_owner():
    """filter_bookable_slots must DELEGATE its policy rules to
    evaluate_slot_policy (one owner — Rule 3): a reject-all policy empties
    the results even for perfect slots; an accept-all policy leaves the
    status/hold checks, ordering, and max_offered_slots cap intact."""
    import app.services.availability_rules as rules
    original = rules.evaluate_slot_policy
    try:
        rules.evaluate_slot_policy = (
            lambda s, **kw: SlotPolicyResult(False, "too_soon"))
        assert filter_bookable_slots([slot(24), slot(48)], NOW_UTC, SETTINGS, "any") == []

        rules.evaluate_slot_policy = lambda s, **kw: SlotPolicyResult(True, "ok")
        booked = slot(30, status=SlotStatus.BOOKED)        # status check stays local
        held = slot(31, status=SlotStatus.HELD,
                    held_until=NOW_UTC + timedelta(minutes=3))  # active hold stays local
        s4, s3, s2, s1 = slot(96), slot(72), slot(50), slot(26)
        result = filter_bookable_slots([booked, held, s4, s3, s2, s1],
                                       NOW_UTC, SETTINGS, "any")
        assert [x.id for x in result] == [s1.id, s2.id, s3.id]  # sorted + capped at 3
    finally:
        rules.evaluate_slot_policy = original

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
