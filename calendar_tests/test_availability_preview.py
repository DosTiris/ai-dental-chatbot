# calendar_tests/test_availability_preview.py
#
# Prototype B B1 tests:
#   1. The pure UNCAPPED bookable-slot rule (list_bookable_slots) and its
#      equivalence contract with the existing capped filter_bookable_slots.
#   2. The AvailabilityPreviewRequest rules and the ENFORCED response
#      contract (locked day states, derived selectable, aware-UTC fields,
#      end-after-start) — B1 revision, Correction 2.
#   3. The read-only preview service: day-state classification, one-range-
#      query behavior, DST bucketing, contract locks, and read-only proof.
#   4. The strongest possible DATABASE-backed read-only proof (skips without
#      TEST_DATABASE_URL, like every other DB test — see conftest.py).
#
# Run: pytest calendar_tests/test_availability_preview.py -v

import sys
import uuid as uuid_module
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import pydantic

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.calendar_models import SlotStatus  # noqa: E402
from app.repositories import appointment_repository  # noqa: E402
from app.schemas import (  # noqa: E402
    PREVIEW_MAX_RANGE_DAYS,
    AvailabilityPreviewRequest,
    AvailabilityPreviewResponse,
    PreviewDay,
    PreviewSlot,
)
from app.services import availability_preview_service as preview  # noqa: E402
from app.services import appointment_hold_service  # noqa: E402
from app.services import booking_service  # noqa: E402
from app.services import notification_service  # noqa: E402
from app.services.availability_preview_service import (  # noqa: E402
    ALL_DAY_STATES,
    DAY_STATE_FULL,
    DAY_STATE_OPEN,
    DAY_STATE_PAST,
    DAY_STATE_UNAVAILABLE,
    build_availability_preview,
)
from app.services.availability_rules import (  # noqa: E402
    filter_bookable_slots,
    list_bookable_slots,
)
from app.services.calendar_settings_service import (  # noqa: E402
    CalendarSettings,
    ensure_utc,
    local_day_utc_window,
)
from calendar_tests.conftest import requires_db  # noqa: E402

UTC = ZoneInfo("UTC")
NY = ZoneInfo("America/New_York")

# Fixed "now": Saturday July 11, 2026, 9:00 AM New York = 13:00 UTC —
# identical to test_availability_rules.py so both rule suites share one
# mental model.
NOW_UTC = datetime(2026, 7, 11, 13, 0, tzinfo=UTC)
TODAY_LOCAL = date(2026, 7, 11)

SETTINGS = CalendarSettings(
    booking_enabled=True,
    hold_minutes=5,
    minimum_notice_minutes=60,
    max_offered_slots=3,
    max_booking_days=30,
    require_staff_confirmation=True,
    timezone_name="America/New_York",
)

# An EXISTING Calendar-policy vocabulary value (the raw values carried by
# slot rows and compared by evaluate_slot_policy). B1 treats service_key as
# OPAQUE and owns no vocabulary validation or master-key translation — see
# SERVICE KEY OWNERSHIP in availability_preview_service.py.
SERVICE_KEY = "cleaning/checkup"
OTHER_POLICY_SERVICE_KEY = "extraction/implant"


def slot(hours_from_now=24.0, status=SlotStatus.AVAILABLE, held_until=None,
         service_key=None, start=None, duration_minutes=45):
    """Stub slot row; mirrors the helper in test_availability_rules.py."""
    start = start or (NOW_UTC + timedelta(hours=hours_from_now))
    return SimpleNamespace(
        id=f"slot-{start.isoformat()}-{status}",
        status=status,
        start_datetime=start,
        end_datetime=start + timedelta(minutes=duration_minutes),
        held_until=held_until,
        service_key=service_key,
    )


def fake_client(booking_enabled=True, timezone="America/New_York"):
    return SimpleNamespace(
        id=uuid_module.uuid4(),
        practice_name="Test Dental",
        timezone=None,
        settings={
            "timezone": timezone,
            "calendar": {
                "booking_enabled": booking_enabled,
                "hold_minutes": 5,
                "minimum_notice_minutes": 60,
                "max_offered_slots": 3,
                "max_booking_days": 30,
                "require_staff_confirmation": True,
            },
        },
    )


class ExplodingDb:
    """A session stand-in that fails on ANY attribute access.

    The preview service may hand db ONLY to the (monkeypatched) repository
    function; if the service itself ever touches the session — query, add,
    commit, flush, delete, anything — this object makes the test fail loudly.
    """

    def __getattr__(self, name):
        raise AssertionError(
            f"read-only preview touched the db session directly: .{name}"
        )


@pytest.fixture()
def repo_spy(monkeypatch):
    """Replace list_slots_between with a spy; also arm tripwires proving no
    mutation-service or notification function is ever called."""
    calls = []
    rows_to_return = []

    def spy(db, client_id, start_utc, end_utc):
        calls.append(SimpleNamespace(
            client_id=client_id, start_utc=start_utc, end_utc=end_utc,
        ))
        return list(rows_to_return)

    monkeypatch.setattr(appointment_repository, "list_slots_between", spy)

    def tripwire(name):
        def _fail(*args, **kwargs):
            raise AssertionError(f"read-only preview called {name}")
        return _fail

    # Mutation owners (spec: "no mutation-service function is called").
    monkeypatch.setattr(appointment_hold_service, "place_hold",
                        tripwire("appointment_hold_service.place_hold"))
    monkeypatch.setattr(appointment_hold_service, "release_hold",
                        tripwire("appointment_hold_service.release_hold"))
    monkeypatch.setattr(booking_service, "finalize_booking",
                        tripwire("booking_service.finalize_booking"))
    monkeypatch.setattr(booking_service, "confirm_appointment",
                        tripwire("booking_service.confirm_appointment"))
    monkeypatch.setattr(booking_service, "cancel_appointment",
                        tripwire("booking_service.cancel_appointment"))
    # Notification owner (spec: "no notification function is called").
    monkeypatch.setattr(notification_service, "send_booking_notifications",
                        tripwire("notification_service.send_booking_notifications"))

    return SimpleNamespace(calls=calls, rows=rows_to_return)


def preview_request(start, end, selected=None, service_key=SERVICE_KEY):
    return AvailabilityPreviewRequest(
        start_day=start, end_day=end, selected_day=selected,
        service_key=service_key,
    )


def run_preview(repo_spy, rows, request, client=None, now_utc=NOW_UTC):
    repo_spy.rows.clear()
    repo_spy.rows.extend(rows)
    return build_availability_preview(
        ExplodingDb(), client or fake_client(), request, now_utc,
    )


def day_state(response, local_date):
    matches = [d for d in response.days if d.local_date == local_date]
    assert len(matches) == 1, f"expected exactly one entry for {local_date}"
    return matches[0].state


# ===========================================================================
# 1. PURE UNCAPPED RULE
# ===========================================================================

def test_uncapped_returns_all_eligible_slots():
    # Six eligible slots — twice the max_offered_slots cap of 3.
    rows = [slot(hours_from_now=h) for h in (26, 30, 40, 50, 60, 70)]
    result = list_bookable_slots(rows, NOW_UTC, SETTINGS, "any")
    assert len(result) == 6  # UNCAPPED: every eligible slot comes back.


def test_uncapped_excludes_booked_and_blocked_slots():
    keep = slot(hours_from_now=26)
    rows = [
        keep,
        slot(hours_from_now=27, status=SlotStatus.BOOKED),
        slot(hours_from_now=28, status=SlotStatus.BLOCKED),
        slot(hours_from_now=29, status=SlotStatus.CANCELLED),
    ]
    result = list_bookable_slots(rows, NOW_UTC, SETTINGS, "any")
    assert [s.id for s in result] == [keep.id]


def test_uncapped_excludes_actively_held_includes_expired_unchanged():
    active = slot(hours_from_now=26, status=SlotStatus.HELD,
                  held_until=NOW_UTC + timedelta(minutes=3))
    expired = slot(hours_from_now=27, status=SlotStatus.HELD,
                   held_until=NOW_UTC - timedelta(minutes=1))
    result = list_bookable_slots([active, expired], NOW_UTC, SETTINGS, "any")
    assert [s.id for s in result] == [expired.id]
    # Read-only interpretation: the expired-held ROW is untouched.
    assert expired.status == SlotStatus.HELD
    assert expired.held_until == NOW_UTC - timedelta(minutes=1)


def test_uncapped_applies_minimum_notice_and_horizon_policies():
    too_soon = slot(hours_from_now=0.5)     # 30 min < 60 min notice
    fine = slot(hours_from_now=2)
    too_far = slot(hours_from_now=24 * 31)  # beyond the 30-day horizon
    result = list_bookable_slots([too_soon, fine, too_far], NOW_UTC,
                                 SETTINGS, "any")
    assert [s.id for s in result] == [fine.id]


def test_uncapped_applies_service_compatibility():
    generic = slot(hours_from_now=26)
    other_service = slot(hours_from_now=27, service_key="extraction/implant")
    result = list_bookable_slots([generic, other_service], NOW_UTC, SETTINGS,
                                 "any", service_key="cleaning/checkup")
    assert [s.id for s in result] == [generic.id]


def test_uncapped_ordering_is_deterministic_soonest_first():
    rows = [slot(hours_from_now=h) for h in (50, 26, 40, 30, 35, 60, 45)]
    result = list_bookable_slots(rows, NOW_UTC, SETTINGS, "any")
    starts = [ensure_utc(s.start_datetime) for s in result]
    assert starts == sorted(starts) and len(result) == 7


def test_capped_function_still_caps_identically():
    rows = [slot(hours_from_now=h) for h in (50, 26, 40, 30, 35, 60, 45)]
    uncapped = list_bookable_slots(rows, NOW_UTC, SETTINGS, "any")
    capped = filter_bookable_slots(rows, NOW_UTC, SETTINGS, "any")
    # The old public contract, exactly: the first max_offered_slots of the
    # uncapped result, same objects, same order.
    assert capped == uncapped[: SETTINGS.max_offered_slots]
    assert len(capped) == 3


@pytest.mark.parametrize("preference", ["any", "morning", "afternoon"])
@pytest.mark.parametrize("svc", [None, "cleaning/checkup"])
def test_capped_equals_uncapped_prefix_across_filters(preference, svc):
    """Regression: for mixed rows and every filter combination, the capped
    function returns exactly the uncapped result truncated at the cap —
    proving the refactor changed nothing about its results or ordering."""
    rows = [
        slot(hours_from_now=26),
        slot(hours_from_now=0.2),                          # too soon
        slot(hours_from_now=27, status=SlotStatus.BOOKED),
        slot(hours_from_now=28, status=SlotStatus.HELD,
             held_until=NOW_UTC + timedelta(minutes=3)),   # active hold
        slot(hours_from_now=29, status=SlotStatus.HELD,
             held_until=NOW_UTC - timedelta(minutes=3)),   # expired hold
        slot(hours_from_now=30, service_key="extraction/implant"),
        slot(hours_from_now=31),
        slot(hours_from_now=32),
        slot(hours_from_now=33, status=SlotStatus.BLOCKED),
        slot(hours_from_now=34),
    ]
    capped = filter_bookable_slots(rows, NOW_UTC, SETTINGS, preference,
                                   service_key=svc)
    uncapped = list_bookable_slots(rows, NOW_UTC, SETTINGS, preference,
                                   service_key=svc)
    assert capped == uncapped[: SETTINGS.max_offered_slots]
    assert len(capped) <= SETTINGS.max_offered_slots


# ===========================================================================
# 2. REQUEST CONTRACT
# ===========================================================================

def test_contract_accepts_valid_seven_day_range():
    req = preview_request(date(2026, 7, 30), date(2026, 8, 5),
                          selected=date(2026, 7, 30))
    assert (req.end_day - req.start_day).days + 1 == 7


def test_contract_accepts_maximum_31_day_range():
    req = preview_request(date(2026, 8, 1), date(2026, 8, 31))
    assert (req.end_day - req.start_day).days + 1 == PREVIEW_MAX_RANGE_DAYS


def test_contract_rejects_range_over_31_days():
    with pytest.raises(pydantic.ValidationError):
        preview_request(date(2026, 8, 1), date(2026, 9, 1))  # 32 inclusive


def test_contract_rejects_end_before_start():
    with pytest.raises(pydantic.ValidationError):
        preview_request(date(2026, 8, 5), date(2026, 8, 4))


def test_contract_rejects_selected_day_outside_range():
    with pytest.raises(pydantic.ValidationError):
        preview_request(date(2026, 8, 1), date(2026, 8, 7),
                        selected=date(2026, 8, 8))
    with pytest.raises(pydantic.ValidationError):
        preview_request(date(2026, 8, 1), date(2026, 8, 7),
                        selected=date(2026, 7, 31))


def test_contract_rejects_invalid_local_date_input():
    with pytest.raises(pydantic.ValidationError):
        AvailabilityPreviewRequest.model_validate({
            "start_day": "2026-13-40",  # not a real date
            "end_day": "2026-08-05",
            "service_key": SERVICE_KEY,
        })


def test_contract_rejects_blank_service_key():
    with pytest.raises(pydantic.ValidationError):
        preview_request(date(2026, 8, 1), date(2026, 8, 7), service_key="  ")


# ---- Enforced response-contract rules (B1 revision, Correction 2) ----

AWARE_START = datetime(2026, 7, 14, 14, 15, tzinfo=UTC)
AWARE_END = datetime(2026, 7, 14, 14, 45, tzinfo=UTC)


def valid_slot_kwargs(**overrides):
    """A fully valid PreviewSlot payload; tests override one field each so
    every rejection below fails for exactly the reason under test."""
    kwargs = dict(
        start_utc=AWARE_START,
        end_utc=AWARE_END,
        local_start_time="10:15 AM",
        local_end_time="10:45 AM",
        accessible_date_label="Tuesday, July 14",
        accessible_time_label="10:15 AM to 10:45 AM",
        time_of_day="morning",
        selectable=True,
    )
    kwargs.update(overrides)
    return kwargs


def test_day_state_vocabulary_stays_in_sync_with_service_owner():
    # The schema Literal and the service's ALL_DAY_STATES are two views of
    # ONE locked vocabulary; drift must fail a test, not ship.
    from typing import get_args
    literal_states = set(get_args(PreviewDay.__annotations__["state"]))
    assert literal_states == ALL_DAY_STATES
    assert "closed" not in literal_states  # impossible by construction


def test_day_contract_rejects_state_closed():
    with pytest.raises(pydantic.ValidationError):
        PreviewDay(local_date=date(2026, 7, 14), weekday="Tuesday",
                   state="closed", selectable=False)


def test_day_contract_rejects_open_not_selectable():
    with pytest.raises(pydantic.ValidationError):
        PreviewDay(local_date=date(2026, 7, 14), weekday="Tuesday",
                   state=DAY_STATE_OPEN, selectable=False)


def test_day_contract_rejects_full_selectable():
    with pytest.raises(pydantic.ValidationError):
        PreviewDay(local_date=date(2026, 7, 14), weekday="Tuesday",
                   state=DAY_STATE_FULL, selectable=True)


def test_slot_contract_rejects_time_of_day_night():
    with pytest.raises(pydantic.ValidationError):
        PreviewSlot(**valid_slot_kwargs(time_of_day="night"))


def test_slot_contract_rejects_selectable_false():
    with pytest.raises(pydantic.ValidationError):
        PreviewSlot(**valid_slot_kwargs(selectable=False))


def test_slot_contract_rejects_naive_start():
    with pytest.raises(pydantic.ValidationError):
        PreviewSlot(**valid_slot_kwargs(
            start_utc=AWARE_START.replace(tzinfo=None)))


def test_slot_contract_rejects_naive_end():
    with pytest.raises(pydantic.ValidationError):
        PreviewSlot(**valid_slot_kwargs(
            end_utc=AWARE_END.replace(tzinfo=None)))


def test_slot_contract_rejects_non_utc_aware_values():
    # Aware but NOT UTC: the contract promises real UTC instants, so a
    # -04:00 New York value must be rejected, never silently converted.
    ny_start = AWARE_START.astimezone(NY)
    with pytest.raises(pydantic.ValidationError):
        PreviewSlot(**valid_slot_kwargs(start_utc=ny_start))
    ny_end = AWARE_END.astimezone(NY)
    with pytest.raises(pydantic.ValidationError):
        PreviewSlot(**valid_slot_kwargs(end_utc=ny_end))


def test_slot_contract_rejects_end_not_after_start():
    with pytest.raises(pydantic.ValidationError):
        PreviewSlot(**valid_slot_kwargs(end_utc=AWARE_START))  # equal
    with pytest.raises(pydantic.ValidationError):
        PreviewSlot(**valid_slot_kwargs(  # reversed
            start_utc=AWARE_END, end_utc=AWARE_START))


def test_response_contract_rejects_naive_generated_at(repo_spy):
    response = run_preview(
        repo_spy, [], preview_request(date(2026, 7, 12), date(2026, 7, 18)))
    with pytest.raises(pydantic.ValidationError):
        AvailabilityPreviewResponse(
            client_id=response.client_id,
            practice_name=response.practice_name,
            timezone_name=response.timezone_name,
            booking_enabled=response.booking_enabled,
            range_start=response.range_start,
            range_end=response.range_end,
            generated_at=NOW_UTC.replace(tzinfo=None),  # naive: rejected
            days=list(response.days),
        )


# ===========================================================================
# 3. PREVIEW SERVICE (unit; repository monkeypatched, db must never be used)
# ===========================================================================

def test_policy_service_key_matches_reserved_slot(repo_spy):
    # "cleaning/checkup" request MUST surface a slot reserved for
    # "cleaning/checkup": the key is passed through OPAQUELY to the policy
    # owner, with no master-library validation in between.
    selected = date(2026, 7, 14)
    reserved = slot(start=datetime(2026, 7, 14, 14, 15, tzinfo=UTC),
                    service_key=SERVICE_KEY)
    response = run_preview(
        repo_spy, [reserved],
        preview_request(date(2026, 7, 12), date(2026, 7, 18),
                        selected=selected),
    )
    day = next(d for d in response.days if d.local_date == selected)
    assert day.state == DAY_STATE_OPEN
    assert len(response.slots) == 1


def test_policy_service_key_rejects_other_reserved_slot(repo_spy):
    # "cleaning/checkup" request must NOT surface a slot reserved for
    # "extraction/implant" (service_mismatch in evaluate_slot_policy).
    selected = date(2026, 7, 14)
    other = slot(start=datetime(2026, 7, 14, 14, 15, tzinfo=UTC),
                 service_key=OTHER_POLICY_SERVICE_KEY)
    response = run_preview(
        repo_spy, [other],
        preview_request(date(2026, 7, 12), date(2026, 7, 18),
                        selected=selected),
    )
    day = next(d for d in response.days if d.local_date == selected)
    assert day.state == DAY_STATE_UNAVAILABLE  # not open, not full:
    assert response.slots == []                # mismatch is policy-reject


def test_null_service_generic_slot_stays_compatible(repo_spy):
    # A generic slot (service_key NULL) is compatible with ANY requested
    # service — unchanged existing policy behavior, proven at preview level.
    selected = date(2026, 7, 14)
    generic = slot(start=datetime(2026, 7, 14, 14, 15, tzinfo=UTC),
                   service_key=None)
    response = run_preview(
        repo_spy, [generic],
        preview_request(date(2026, 7, 12), date(2026, 7, 18),
                        selected=selected),
    )
    day = next(d for d in response.days if d.local_date == selected)
    assert day.state == DAY_STATE_OPEN
    assert len(response.slots) == 1


def test_one_range_query_for_seven_day_preview(repo_spy):
    response = run_preview(
        repo_spy, [], preview_request(date(2026, 7, 12), date(2026, 7, 18)),
    )
    assert len(repo_spy.calls) == 1  # ONE query — never a per-day loop
    assert len(response.days) == 7


def test_one_range_query_for_31_day_preview(repo_spy):
    response = run_preview(
        repo_spy, [], preview_request(date(2026, 8, 1), date(2026, 8, 31)),
    )
    assert len(repo_spy.calls) == 1
    assert len(response.days) == 31


def test_query_window_uses_local_day_utc_window_owner(repo_spy):
    start, end = date(2026, 7, 12), date(2026, 7, 18)
    run_preview(repo_spy, [], preview_request(start, end))
    call = repo_spy.calls[0]
    expected_start, _ = local_day_utc_window(start, "America/New_York")
    _, expected_end = local_day_utc_window(end, "America/New_York")
    assert call.start_utc == expected_start
    assert call.end_utc == expected_end


def test_rows_bucket_by_office_local_date(repo_spy):
    # 03:00 UTC July 15 is 11:00 PM July 14 in New York: the row must land
    # on LOCAL July 14, not UTC July 15.
    late_local = slot(start=datetime(2026, 7, 15, 3, 0, tzinfo=UTC))
    response = run_preview(
        repo_spy, [late_local],
        preview_request(date(2026, 7, 12), date(2026, 7, 18)),
    )
    assert day_state(response, date(2026, 7, 14)) == DAY_STATE_OPEN
    assert day_state(response, date(2026, 7, 15)) == DAY_STATE_UNAVAILABLE


def test_dst_fall_back_boundary_buckets_correctly(repo_spy):
    # 2026-11-01 America/New_York is a 25-hour local day. 04:30 UTC Nov 2 is
    # 11:30 PM EST Nov 1 — still LOCAL Nov 1 and must be inside the window.
    row = slot(start=datetime(2026, 11, 2, 4, 30, tzinfo=UTC))
    now = datetime(2026, 10, 28, 13, 0, tzinfo=UTC)
    response = run_preview(
        repo_spy, [row],
        preview_request(date(2026, 10, 28), date(2026, 11, 3)),
        now_utc=now,
    )
    assert day_state(response, date(2026, 11, 1)) == DAY_STATE_OPEN
    assert day_state(response, date(2026, 11, 2)) == DAY_STATE_UNAVAILABLE
    # The queried window must cover the WHOLE 25-hour local day.
    call = repo_spy.calls[0]
    _, expected_end = local_day_utc_window(date(2026, 11, 3),
                                           "America/New_York")
    assert call.end_utc == expected_end


def test_dst_spring_forward_boundary_buckets_correctly(repo_spy):
    # 2026-03-08 America/New_York is a 23-hour local day. 03:30 UTC Mar 9 is
    # 11:30 PM EDT Mar 8 — LOCAL Mar 8.
    row = slot(start=datetime(2026, 3, 9, 3, 30, tzinfo=UTC))
    now = datetime(2026, 3, 5, 13, 0, tzinfo=UTC)
    response = run_preview(
        repo_spy, [row],
        preview_request(date(2026, 3, 5), date(2026, 3, 10)),
        now_utc=now,
    )
    assert day_state(response, date(2026, 3, 8)) == DAY_STATE_OPEN
    assert day_state(response, date(2026, 3, 9)) == DAY_STATE_UNAVAILABLE


def test_day_state_past(repo_spy):
    # July 10 is before the office-local current date (July 11) — past even
    # though a slot row exists there.
    old_row = slot(start=datetime(2026, 7, 10, 15, 0, tzinfo=UTC))
    response = run_preview(
        repo_spy, [old_row], preview_request(date(2026, 7, 9), date(2026, 7, 15)),
    )
    assert day_state(response, date(2026, 7, 9)) == DAY_STATE_PAST
    assert day_state(response, date(2026, 7, 10)) == DAY_STATE_PAST
    past_days = [d for d in response.days if d.state == DAY_STATE_PAST]
    assert all(d.selectable is False for d in past_days)


def test_day_state_unavailable_when_no_rows(repo_spy):
    response = run_preview(
        repo_spy, [], preview_request(date(2026, 7, 12), date(2026, 7, 18)),
    )
    assert all(d.state == DAY_STATE_UNAVAILABLE for d in response.days)
    # LOCKED decision #6: zero slot rows must NOT be labeled "closed".
    assert all(d.state != "closed" for d in response.days)
    assert all(d.state in ALL_DAY_STATES for d in response.days)


def test_day_state_unavailable_when_all_rows_blocked(repo_spy):
    day = date(2026, 7, 14)
    rows = [
        slot(start=datetime(2026, 7, 14, 14, 0, tzinfo=UTC),
             status=SlotStatus.BLOCKED),
        slot(start=datetime(2026, 7, 14, 15, 0, tzinfo=UTC),
             status=SlotStatus.BLOCKED),
    ]
    response = run_preview(
        repo_spy, rows, preview_request(date(2026, 7, 12), date(2026, 7, 18)),
    )
    assert day_state(response, day) == DAY_STATE_UNAVAILABLE


def test_day_state_unavailable_when_all_rows_fail_policy(repo_spy):
    # Today's only slot is 30 minutes away — inside minimum notice, so it is
    # policy-rejected. AVAILABLE capacity that fails policy is NOT "full".
    rows = [slot(hours_from_now=0.5)]
    response = run_preview(
        repo_spy, rows, preview_request(date(2026, 7, 11), date(2026, 7, 12)),
    )
    assert day_state(response, TODAY_LOCAL) == DAY_STATE_UNAVAILABLE


def test_day_state_unavailable_beyond_booking_horizon(repo_spy):
    # max_booking_days=30 from July 11 => horizon ends Aug 10; Aug 11 rows
    # are policy-rejected (beyond_horizon) => unavailable.
    rows = [slot(start=datetime(2026, 8, 11, 15, 0, tzinfo=UTC))]
    response = run_preview(
        repo_spy, rows, preview_request(date(2026, 8, 9), date(2026, 8, 12)),
    )
    assert day_state(response, date(2026, 8, 11)) == DAY_STATE_UNAVAILABLE


def test_day_state_full_when_all_capacity_booked(repo_spy):
    day = date(2026, 7, 14)
    rows = [
        slot(start=datetime(2026, 7, 14, 14, 0, tzinfo=UTC),
             status=SlotStatus.BOOKED),
        slot(start=datetime(2026, 7, 14, 15, 0, tzinfo=UTC),
             status=SlotStatus.BOOKED),
    ]
    response = run_preview(
        repo_spy, rows, preview_request(date(2026, 7, 12), date(2026, 7, 18)),
    )
    assert day_state(response, day) == DAY_STATE_FULL


def test_day_state_full_when_all_capacity_actively_held(repo_spy):
    day = date(2026, 7, 14)
    rows = [
        slot(start=datetime(2026, 7, 14, 14, 0, tzinfo=UTC),
             status=SlotStatus.HELD,
             held_until=NOW_UTC + timedelta(minutes=4)),
    ]
    response = run_preview(
        repo_spy, rows, preview_request(date(2026, 7, 12), date(2026, 7, 18)),
    )
    assert day_state(response, day) == DAY_STATE_FULL


def test_day_state_open_when_any_slot_bookable(repo_spy):
    day = date(2026, 7, 14)
    rows = [
        slot(start=datetime(2026, 7, 14, 14, 0, tzinfo=UTC),
             status=SlotStatus.BOOKED),
        slot(start=datetime(2026, 7, 14, 15, 0, tzinfo=UTC)),  # bookable
    ]
    response = run_preview(
        repo_spy, rows, preview_request(date(2026, 7, 12), date(2026, 7, 18)),
    )
    entry = [d for d in response.days if d.local_date == day][0]
    assert entry.state == DAY_STATE_OPEN
    assert entry.selectable is True
    # And only "open" days are selectable anywhere in the grid.
    assert all(d.selectable == (d.state == DAY_STATE_OPEN)
               for d in response.days)


def test_expired_hold_opens_day_without_mutating_row(repo_spy):
    day = date(2026, 7, 14)
    expired = slot(start=datetime(2026, 7, 14, 14, 0, tzinfo=UTC),
                   status=SlotStatus.HELD,
                   held_until=NOW_UTC - timedelta(minutes=2))
    response = run_preview(
        repo_spy, [expired],
        preview_request(date(2026, 7, 12), date(2026, 7, 18), selected=day),
    )
    assert day_state(response, day) == DAY_STATE_OPEN
    assert len(response.slots) == 1
    # The MODEL ROW still says held — interpretation only, zero mutation.
    assert expired.status == SlotStatus.HELD
    assert expired.held_until == NOW_UTC - timedelta(minutes=2)


def test_selected_day_omitted_produces_no_slot_list(repo_spy):
    rows = [slot(start=datetime(2026, 7, 14, 15, 0, tzinfo=UTC))]
    response = run_preview(
        repo_spy, rows, preview_request(date(2026, 7, 12), date(2026, 7, 18)),
    )
    assert response.selected_day is None
    assert response.slots == []


def test_selected_day_returns_only_that_days_slots_in_order(repo_spy):
    selected = date(2026, 7, 14)
    on_day_late = slot(start=datetime(2026, 7, 14, 19, 0, tzinfo=UTC))
    on_day_early = slot(start=datetime(2026, 7, 14, 14, 15, tzinfo=UTC),
                        duration_minutes=30)
    other_day = slot(start=datetime(2026, 7, 15, 15, 0, tzinfo=UTC))
    response = run_preview(
        repo_spy, [on_day_late, other_day, on_day_early],
        preview_request(date(2026, 7, 12), date(2026, 7, 18),
                        selected=selected),
    )
    assert response.selected_day == selected
    assert len(response.slots) == 2  # ONLY the selected day's slots
    starts = [s.start_utc for s in response.slots]
    assert starts == sorted(starts)  # deterministic, soonest first
    first = response.slots[0]
    # 14:15 UTC = 10:15 AM New York — the spec's own worked example.
    assert first.start_utc == datetime(2026, 7, 14, 14, 15, tzinfo=UTC)
    assert first.start_utc.tzinfo is not None  # aware (contract lock)
    assert first.end_utc.tzinfo is not None
    assert first.local_start_time == "10:15 AM"
    assert first.local_end_time == "10:45 AM"
    assert first.accessible_date_label == "Tuesday, July 14"
    assert first.accessible_time_label == "10:15 AM to 10:45 AM"
    assert first.selectable is True


def test_equal_start_slots_order_deterministically(repo_spy):
    # Three slots share ONE start; ties break by end, then internal id —
    # so output is identical for EVERY repository input order, even though
    # slot_id itself is never emitted (contract lock).
    import itertools
    selected = date(2026, 7, 14)
    same_start = datetime(2026, 7, 14, 14, 15, tzinfo=UTC)
    short_a = slot(start=same_start, duration_minutes=30)
    short_b = slot(start=same_start, duration_minutes=30)
    long_c = slot(start=same_start, duration_minutes=60)
    short_a.id = "slot-aaa"  # equal start AND end: id breaks the tie
    short_b.id = "slot-bbb"
    long_c.id = "slot-ccc"
    expected = None
    for ordering in itertools.permutations([short_a, short_b, long_c]):
        response = run_preview(
            repo_spy, list(ordering),
            preview_request(date(2026, 7, 12), date(2026, 7, 18),
                            selected=selected),
        )
        rendered = [(s.start_utc, s.end_utc, s.local_end_time)
                    for s in response.slots]
        if expected is None:
            expected = rendered
            # Both 30-minute slots precede the 60-minute slot (end sorts
            # before id in the internal key).
            assert [r[2] for r in rendered] == \
                ["10:45 AM", "10:45 AM", "11:15 AM"]
        else:
            assert rendered == expected  # every input permutation agrees


def test_morning_afternoon_grouping_boundary(repo_spy):
    selected = date(2026, 7, 14)
    # 15:59 UTC = 11:59 AM NY (morning); 16:00 UTC = 12:00 PM NY (afternoon).
    morning_edge = slot(start=datetime(2026, 7, 14, 15, 59, tzinfo=UTC))
    afternoon_edge = slot(start=datetime(2026, 7, 14, 16, 0, tzinfo=UTC))
    response = run_preview(
        repo_spy, [morning_edge, afternoon_edge],
        preview_request(date(2026, 7, 12), date(2026, 7, 18),
                        selected=selected),
    )
    assert [s.time_of_day for s in response.slots] == ["morning", "afternoon"]
    assert response.slots[0].local_start_time == "11:59 AM"
    assert response.slots[1].local_start_time == "12:00 PM"


def test_booking_enabled_false_is_informational_only(repo_spy):
    rows = [slot(start=datetime(2026, 7, 14, 15, 0, tzinfo=UTC))]
    response = run_preview(
        repo_spy, rows,
        preview_request(date(2026, 7, 12), date(2026, 7, 18),
                        selected=date(2026, 7, 14)),
        client=fake_client(booking_enabled=False),
    )
    assert response.booking_enabled is False
    # ...and the read-only preview still fully renders.
    assert day_state(response, date(2026, 7, 14)) == DAY_STATE_OPEN
    assert len(response.slots) == 1


def test_response_contract_contains_no_forbidden_fields(repo_spy):
    rows = [slot(start=datetime(2026, 7, 14, 15, 0, tzinfo=UTC))]
    response = run_preview(
        repo_spy, rows,
        preview_request(date(2026, 7, 12), date(2026, 7, 18),
                        selected=date(2026, 7, 14)),
    )
    payload = response.model_dump()
    flat_keys = set(payload)
    for d in payload["days"]:
        flat_keys |= set(d)
    for s in payload["slots"]:
        flat_keys |= set(s)
    forbidden = {
        "slot_id", "id",                    # no slot identifiers
        "time_preference",                  # removed from the B contract
        "slot_count", "counts", "total",    # no daily slot counts
        "patient_name", "patient_phone", "patient_email",  # no patient info
        "notification_email", "notification_phone",  # no notification dests
        "api_key", "password", "token",     # no credentials
        "hold_minutes", "minimum_notice_minutes",  # no private settings
    }
    assert flat_keys.isdisjoint(forbidden), flat_keys & forbidden
    assert payload["generated_at"].tzinfo is not None  # aware UTC
    assert payload["generated_at"] == NOW_UTC


def test_full_grid_metadata(repo_spy):
    client = fake_client()
    response = run_preview(
        repo_spy, [], preview_request(date(2026, 7, 12), date(2026, 7, 18)),
        client=client,
    )
    assert response.client_id == str(client.id)
    assert response.practice_name == "Test Dental"
    assert response.timezone_name == "America/New_York"
    assert response.range_start == date(2026, 7, 12)
    assert response.range_end == date(2026, 7, 18)
    assert [d.local_date for d in response.days] == [
        date(2026, 7, 12) + timedelta(days=n) for n in range(7)
    ]
    assert [d.weekday for d in response.days] == [
        "Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
        "Saturday",
    ]


def test_read_only_proof_at_service_boundary(repo_spy):
    """The spy repository is the ONLY interaction; the db object explodes on
    any direct touch; every mutation/notification tripwire stays silent; and
    the stub rows come back byte-identical."""
    rows = [
        slot(start=datetime(2026, 7, 14, 15, 0, tzinfo=UTC)),
        slot(start=datetime(2026, 7, 14, 16, 0, tzinfo=UTC),
             status=SlotStatus.HELD,
             held_until=NOW_UTC + timedelta(minutes=4)),
        slot(start=datetime(2026, 7, 15, 15, 0, tzinfo=UTC),
             status=SlotStatus.BOOKED),
    ]
    snapshot = [
        (r.status, r.start_datetime, r.end_datetime, r.held_until,
         r.service_key)
        for r in rows
    ]
    run_preview(
        repo_spy, rows,
        preview_request(date(2026, 7, 12), date(2026, 7, 18),
                        selected=date(2026, 7, 14)),
    )
    assert len(repo_spy.calls) == 1  # one SELECT, nothing else
    after = [
        (r.status, r.start_datetime, r.end_datetime, r.held_until,
         r.service_key)
        for r in rows
    ]
    assert after == snapshot  # no status/timestamp mutation, no hold takeover


# ===========================================================================
# 4. DATABASE-BACKED READ-ONLY PROOF (skips without TEST_DATABASE_URL)
# ===========================================================================

@requires_db
def test_db_backed_preview_is_select_only_and_classifies_real_rows(
    db, client_row,
):
    """Strongest DB-backed proof possible without a route or migration:
    real rows, real repository, real session — and the preview must leave
    the session with zero pending changes and every row byte-identical."""
    from app.repositories.appointment_repository import create_slot

    tz = ZoneInfo("America/New_York")
    now_utc = ensure_utc(datetime.now(tz)) + timedelta(minutes=0)
    open_day = (now_utc.astimezone(tz) + timedelta(days=3)).date()
    full_day = (now_utc.astimezone(tz) + timedelta(days=4)).date()

    def local_slot_start(day, hour):
        return datetime(day.year, day.month, day.day, hour, 0,
                        tzinfo=tz).astimezone(UTC)

    bookable = create_slot(db, client_row.id, local_slot_start(open_day, 10),
                           local_slot_start(open_day, 10)
                           + timedelta(minutes=45))
    booked = create_slot(db, client_row.id, local_slot_start(full_day, 10),
                         local_slot_start(full_day, 10)
                         + timedelta(minutes=45))
    booked.status = SlotStatus.BOOKED
    db.flush()  # test-fixture setup writes; the PREVIEW itself must not.

    request = preview_request(
        open_day - timedelta(days=1), full_day + timedelta(days=1),
        selected=open_day,
    )
    response = build_availability_preview(db, client_row, request, now_utc)

    assert day_state(response, open_day) == DAY_STATE_OPEN
    assert day_state(response, full_day) == DAY_STATE_FULL
    assert len(response.slots) == 1
    assert response.slots[0].start_utc == ensure_utc(bookable.start_datetime)

    # READ-ONLY PROOF: no add/delete/dirty state pending on the session...
    assert not db.new
    assert not db.deleted
    assert not db.dirty
    # ...and the rows themselves are unchanged in the database.
    db.expire_all()
    assert bookable.status == SlotStatus.AVAILABLE
    assert bookable.held_until is None
    assert booked.status == SlotStatus.BOOKED
