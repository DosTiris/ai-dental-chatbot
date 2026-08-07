# calendar_tests/test_capture_first_date_click_e2e.py
#
# COMPLETE-SEQUENCE proof that a capture-first date selection AND its
# morning/afternoon (or exact-time) detail advance the intake — for weekdays and
# for OPEN Saturdays and Sundays — while CLOSED weekend days stay fail-closed at
# BOTH the date and the detail stage. Open/closed is decided solely by the
# existing office-hours owner is_day_open(client, day_key); no new parser,
# availability owner, route, or state machine is introduced.
#
# RECORDED RED (render-only proposal): the widget submitted the native
# calendar_choice / pick-date:<ISO> action at the capture-first boundary, where
# booking_state is NONE, so the action was ACTION_STALE_CHOICE -> HTTP 409.
#
# Run (PostgreSQL required):
#   python -m pytest calendar_tests/test_capture_first_date_click_e2e.py -v

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException

from app.calendar_models import BookingState, SlotStatus
import app.routes.chat as chat_module
from app.schemas import ChatRequest, AvailabilityPreviewRequest
from app.repositories import appointment_repository
from app.services.availability_preview_service import build_availability_preview
from app.services.booking_conversation import (
    INTAKE_TIME_PREFERENCE_PROMPT,
    _date_stage_meta,
)
from app.services.calendar_settings_service import load_calendar_settings

from calendar_tests.test_chat_integration import (  # noqa: F401
    fakes,
    make_client,
    make_conversation,
    make_slot,
    send,
    _FakeRequest,
)

DATE_SIGNAL = {"stage": "date", "submit": "message"}
TIME_SIGNAL = {"stage": "time_preference"}
GENERIC_REJECT = "please choose another day/time"
MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
            "Saturday", "Sunday"]


def _hours(*, sat_open, sun_open):
    h = {d: {"open": True, "start": "09:00", "end": "17:00"}
         for d in ["mon", "tue", "wed", "thu", "fri"]}
    h["sat"] = {"open": bool(sat_open), "start": "09:00", "end": "17:00"}
    h["sun"] = {"open": bool(sun_open), "start": "09:00", "end": "17:00"}
    return h


ALL_OPEN = _hours(sat_open=True, sun_open=True)


def _gated_client(db, office_hours):
    client = make_client(db, calendar_enabled=True, office_hours=office_hours)
    settings = dict(client.settings or {})
    calendar = dict(settings.get("calendar") or {})
    calendar.update({"booking_enabled": True,
                     "calendar_actions_enabled": True,
                     "calendar_picker_enabled": True})
    settings["calendar"] = calendar
    client.settings = settings
    db.add(client)
    db.commit()
    return client


def _pre_date_question(db, client):
    # Package A asks New/Returning first, so patient type is already captured by
    # the time the date/time-window stage is reached; preseed it so 'skip email'
    # still lands on the capture-first day/time-window question.
    return make_conversation(db, client, lead_time_window=None,
                             lead_email_opt_out=False, lead_is_new_patient=True)


def _today(client):
    return chat_module.get_client_now(client).date()


def _next_dow(client, target_dow, after_days=1):
    d = _today(client) + timedelta(days=after_days)
    while d.weekday() != target_dow:
        d += timedelta(days=1)
    return d


def _next_weekday(client, after_days=1):
    d = _today(client) + timedelta(days=after_days)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def _human_message(d):
    return f"{WEEKDAYS[d.weekday()]}, {MONTHS[d.month - 1]} {d.day}, {d.year}"


def _canonical(d):
    return f"{d.strftime('%a')} {d.isoformat()}"


def _seed_slot_on(db, client, d, hour=10):
    make_slot(db, client, days_ahead=(d - _today(client)).days, hour=hour,
              status=SlotStatus.AVAILABLE)


def _preview_state_for(db, client, d):
    req = AvailabilityPreviewRequest(
        start_day=d.isoformat(), end_day=d.isoformat(),
        selected_day=None, service_key=None)
    preview = build_availability_preview(
        db, client, req, datetime.now(ZoneInfo("UTC")))
    for day in preview.days:
        if str(day.local_date) == d.isoformat():
            return day.state
    return None


def _submit_calendar_choice(db, client, conversation, choice_id, *, message):
    req = ChatRequest(
        message=message, client_key=client.api_key, visitor_id="test-visitor",
        conversation_id=str(conversation.id),
        action={"type": "calendar_choice", "choice_id": choice_id},
    )
    resp = chat_module.chat(req, _FakeRequest(), db)
    db.refresh(conversation)
    return resp


def _appointment(db, client, conversation):
    return appointment_repository.get_appointment_by_conversation(
        db, client.id, conversation.id)


def _counters(fakes):
    return (len(fakes.lead_sms), len(fakes.lead_email),
            len(fakes.booking_sms), len(fakes.booking_email))


def _assert_no_side_effects(db, client, conversation, fakes, before):
    # no appointment, hold, slot booking, office/patient email or SMS, or
    # duplicate request at THIS stage.
    assert _appointment(db, client, conversation) is None
    assert _counters(fakes) == before
    assert (conversation.booking_state or BookingState.NONE) == BookingState.NONE


def _assert_no_booking_side_effect(db, client, conversation, fakes, before):
    # Package A: completing the time window (the last field) routes into Calendar,
    # so booking_state legitimately advances; assert only that NO appointment was
    # booked and NO office/patient notification was sent on this turn.
    assert _appointment(db, client, conversation) is None
    assert _counters(fakes) == before


def _day_turn(db, client, conversation, d, fakes, before):
    """Turn 1: the exact date ordinary message stores the canonical day-only
    value and asks the existing morning/afternoon question."""
    resp = send(db, client, conversation, _human_message(d))
    assert resp.reply == INTAKE_TIME_PREFERENCE_PROMPT
    assert resp.meta.get("calendar_picker") == TIME_SIGNAL
    assert conversation.lead_time_window == _canonical(d)
    _assert_no_side_effects(db, client, conversation, fakes, before)
    return resp


def _assert_completes(resp, conversation, expected_window, fakes, before,
                      db, client):
    """A detail turn that COMPLETES the window. Package A captures patient type
    FIRST, so the time window is the LAST intake field: completing it routes into
    the existing Calendar slot offering (booking_state -> slot selection,
    server-owned slot actions) - never a trailing New/Returning question and
    never a generic 'our team will reach out' handoff. Slot selection is still
    required, so no appointment is booked and no notification is sent yet."""
    assert conversation.lead_time_window == expected_window
    assert GENERIC_REJECT not in resp.reply.lower()
    assert resp.meta.get("calendar_actions"), "completed intake must offer Calendar slots"
    assert conversation.booking_state == BookingState.WAITING_FOR_SLOT_SELECTION
    assert "our team will reach out" not in resp.reply.lower()
    assert "new or returning" not in resp.reply.lower()
    # No booking side effect yet: slot selection still required.
    assert _appointment(db, client, conversation) is None
    assert _counters(fakes) == before


# ---------------------------------------------------------------------------
# RECORDED RED
# ---------------------------------------------------------------------------

def test_render_only_calendar_choice_click_is_rejected_409(db, fakes):
    client = _gated_client(db, ALL_OPEN)
    conversation = _pre_date_question(db, client)
    r_date = send(db, client, conversation, "skip email")
    assert r_date.meta.get("mode") == "bypass"
    assert r_date.meta.get("calendar_picker") == DATE_SIGNAL
    before = _counters(fakes)
    open_date = _next_weekday(client)
    with pytest.raises(HTTPException) as exc:
        _submit_calendar_choice(
            db, client, conversation,
            f"pick-date:{open_date.isoformat()}", message=open_date.isoformat())
    assert exc.value.status_code == 409
    _assert_no_side_effects(db, client, conversation, fakes, before)


# ---------------------------------------------------------------------------
# A. FUTURE WEEKDAY — complete two-turn sequence.
# ---------------------------------------------------------------------------

def test_A_weekday_complete_sequence(db, fakes):
    client = _gated_client(db, ALL_OPEN)
    conversation = _pre_date_question(db, client)
    assert send(db, client, conversation, "skip email").meta.get(
        "calendar_picker") == DATE_SIGNAL
    before = _counters(fakes)

    d = _next_weekday(client)
    _seed_slot_on(db, client, d)
    _day_turn(db, client, conversation, d, fakes, before)
    r2 = send(db, client, conversation, "Morning")
    _assert_completes(r2, conversation, f"{_canonical(d)} morning",
                      fakes, before, db, client)


# ---------------------------------------------------------------------------
# B. SATURDAY OPEN — preview reports open; complete two-turn sequence.
# ---------------------------------------------------------------------------

def test_B_saturday_open_complete_sequence(db, fakes):
    client = _gated_client(db, _hours(sat_open=True, sun_open=False))
    conversation = _pre_date_question(db, client)
    assert send(db, client, conversation, "skip email").meta.get(
        "calendar_picker") == DATE_SIGNAL
    before = _counters(fakes)

    sat = _next_dow(client, 5)
    _seed_slot_on(db, client, sat)
    assert _preview_state_for(db, client, sat) == "open"
    _day_turn(db, client, conversation, sat, fakes, before)
    r2 = send(db, client, conversation, "Morning")
    _assert_completes(r2, conversation, f"{_canonical(sat)} morning",
                      fakes, before, db, client)


# ---------------------------------------------------------------------------
# C. SUNDAY OPEN — preview reports open; complete two-turn sequence.
# ---------------------------------------------------------------------------

def test_C_sunday_open_complete_sequence(db, fakes):
    client = _gated_client(db, _hours(sat_open=False, sun_open=True))
    conversation = _pre_date_question(db, client)
    assert send(db, client, conversation, "skip email").meta.get(
        "calendar_picker") == DATE_SIGNAL
    before = _counters(fakes)

    sun = _next_dow(client, 6)
    _seed_slot_on(db, client, sun)
    assert _preview_state_for(db, client, sun) == "open"
    _day_turn(db, client, conversation, sun, fakes, before)
    r2 = send(db, client, conversation, "Afternoon")
    _assert_completes(r2, conversation, f"{_canonical(sun)} afternoon",
                      fakes, before, db, client)


# ---------------------------------------------------------------------------
# D. OPEN SUNDAY — exact-time detail is accepted, not rejected.
# ---------------------------------------------------------------------------

def test_D_open_sunday_exact_time(db, fakes):
    client = _gated_client(db, _hours(sat_open=False, sun_open=True))
    conversation = _pre_date_question(db, client)
    assert send(db, client, conversation, "skip email").meta.get(
        "calendar_picker") == DATE_SIGNAL
    before = _counters(fakes)

    sun = _next_dow(client, 6)
    _day_turn(db, client, conversation, sun, fakes, before)
    detail = chat_module.canonicalize_time_window_for_storage(client, "2pm")
    r2 = send(db, client, conversation, "2pm")
    assert GENERIC_REJECT not in r2.reply.lower()
    assert conversation.lead_time_window == f"{_canonical(sun)} {detail}"
    # The detail is accepted and completes intake, which routes into Calendar
    # (no appointment/notification side effect on this turn).
    _assert_no_booking_side_effect(db, client, conversation, fakes, before)


# ---------------------------------------------------------------------------
# E. CLOSED WEEKEND — a later detail answer cannot complete a CLOSED weekend
#    day-only value (including a legacy/previously stored one).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("target_dow", [5, 6])   # Saturday, Sunday
@pytest.mark.parametrize("detail_msg", ["Morning", "Afternoon", "2pm"])
def test_E_closed_weekend_detail_stays_failclosed(db, fakes, target_dow, detail_msg):
    client = _gated_client(db, _hours(sat_open=False, sun_open=False))
    d = _next_dow(client, target_dow)
    legacy = _canonical(d)   # a stored day-only value for a CLOSED weekend day
    conversation = make_conversation(
        db, client, lead_time_window=legacy, lead_email_opt_out=True,
        lead_is_new_patient=None)
    before = _counters(fakes)

    resp = send(db, client, conversation, detail_msg)
    # Not completed: the closed day-only value is unchanged and the detail is
    # refused (no " morning"/time appended).
    assert conversation.lead_time_window == legacy
    assert GENERIC_REJECT in resp.reply.lower()
    _assert_no_side_effects(db, client, conversation, fakes, before)


# ---------------------------------------------------------------------------
# F. SAME-DAY OPEN SUNDAY — clock pinned to an open Sunday; the today-specific
#    follow-up consumes its detail without the generic rejection.
# ---------------------------------------------------------------------------

def test_F_same_day_open_sunday(db, fakes, monkeypatch):
    client = _gated_client(db, _hours(sat_open=False, sun_open=True))
    # Deterministically pin "now" to a fixed OPEN Sunday morning.
    base = chat_module.get_client_now(client).date()
    while base.weekday() != 6:
        base += timedelta(days=1)
    pinned = datetime(base.year, base.month, base.day, 10, 0,
                      tzinfo=ZoneInfo("America/New_York"))
    monkeypatch.setattr(chat_module, "get_client_now", lambda c: pinned)

    conversation = _pre_date_question(db, client)
    assert send(db, client, conversation, "skip email").meta.get(
        "calendar_picker") == DATE_SIGNAL
    before = _counters(fakes)

    today = pinned.date()
    r1 = send(db, client, conversation, _human_message(today))
    # Same-day rule owns the wording (today-specific, not the generic prompt).
    assert "today" in r1.reply.lower()
    assert conversation.lead_time_window == _canonical(today)
    _assert_no_side_effects(db, client, conversation, fakes, before)

    r2 = send(db, client, conversation, "Afternoon")
    assert GENERIC_REJECT not in r2.reply.lower()
    assert conversation.lead_time_window == f"{_canonical(today)} afternoon"
    # The detail is accepted and completes intake, which routes into Calendar
    # (no appointment/notification side effect on this turn).
    _assert_no_booking_side_effect(db, client, conversation, fakes, before)


# ---------------------------------------------------------------------------
# Date-selection rejections retained: CLOSED Saturday / Sunday date -> no
# mutation, no signal (the boundary that precedes the detail stage).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("target_dow", [5, 6])
def test_closed_weekend_date_rejected_no_mutation(db, fakes, target_dow):
    client = _gated_client(db, _hours(sat_open=False, sun_open=False))
    conversation = _pre_date_question(db, client)
    assert send(db, client, conversation, "skip email").meta.get(
        "calendar_picker") == DATE_SIGNAL
    before = _counters(fakes)
    stored_before = conversation.lead_time_window

    d = _next_dow(client, target_dow)
    resp = send(db, client, conversation, _human_message(d))
    assert resp.reply != INTAKE_TIME_PREFERENCE_PROMPT
    assert resp.meta.get("calendar_picker") != TIME_SIGNAL
    assert conversation.lead_time_window == stored_before
    _assert_no_side_effects(db, client, conversation, fakes, before)


# ---------------------------------------------------------------------------
# Same-day WEEKDAY preserves the existing today-specific response.
# ---------------------------------------------------------------------------

def test_same_day_weekday_preserves_existing_response(db, fakes):
    client = _gated_client(db, ALL_OPEN)
    conversation = _pre_date_question(db, client)
    assert send(db, client, conversation, "skip email").meta.get(
        "calendar_picker") == DATE_SIGNAL
    before = _counters(fakes)

    today = _today(client)
    resp = send(db, client, conversation, _human_message(today))
    assert resp.reply != INTAKE_TIME_PREFERENCE_PROMPT
    assert "today" in resp.reply.lower()
    assert conversation.lead_time_window == _canonical(today)
    _assert_no_side_effects(db, client, conversation, fakes, before)


# ---------------------------------------------------------------------------
# SIGNAL BOUNDARY — the date signal is emitted ONLY on a genuine transition
# INTO time_window, never when an invalid date re-asks the identical prompt.
# (Recorded regression: an invalid date at time_window re-advertised the stage.)
# ---------------------------------------------------------------------------

def _at_time_window(db, client):
    # reason/name/phone captured, email skipped, time window + patient-type
    # unset: the conversation sits EXACTLY at the capture-first time_window
    # question (pre-turn stage == time_window).
    return make_conversation(db, client, lead_time_window=None,
                             lead_email_opt_out=True, lead_is_new_patient=True)


def test_valid_email_entry_emits_signal(db, fakes):
    # Entering time_window by supplying a VALID email (not "skip") also emits.
    client = _gated_client(db, ALL_OPEN)
    conversation = _pre_date_question(db, client)   # at the email question
    resp = send(db, client, conversation, "someone@example.com")
    assert resp.meta.get("calendar_picker") == DATE_SIGNAL
    assert (conversation.lead_email or "").strip()   # captured this turn


def test_reask_yesterday_at_time_window_no_signal_no_mutation(db, fakes):
    # Reproduces the recorded regression at the route level: already at
    # time_window, "yesterday" re-asks the same prompt with NO transition.
    client = _gated_client(db, ALL_OPEN)
    conversation = _at_time_window(db, client)
    before = _counters(fakes)
    resp = send(db, client, conversation, "yesterday")
    assert "calendar_picker" not in (resp.meta or {})
    assert conversation.lead_time_window is None          # no mutation
    _assert_no_side_effects(db, client, conversation, fakes, before)


def test_reask_past_explicit_date_no_signal(db, fakes):
    client = _gated_client(db, ALL_OPEN)
    conversation = _at_time_window(db, client)
    before = _counters(fakes)
    past = _today(client) - timedelta(days=7)
    resp = send(db, client, conversation, _human_message(past))
    assert "calendar_picker" not in (resp.meta or {})
    _assert_no_side_effects(db, client, conversation, fakes, before)


def test_reask_unrelated_text_no_signal(db, fakes):
    client = _gated_client(db, ALL_OPEN)
    conversation = _at_time_window(db, client)
    before = _counters(fakes)
    resp = send(db, client, conversation, "not sure yet")
    assert "calendar_picker" not in (resp.meta or {})
    assert conversation.lead_time_window is None
    _assert_no_side_effects(db, client, conversation, fakes, before)


def test_reask_then_valid_date_advances(db, fakes):
    # After an invalid correction (no signal), a later VALID typed date still
    # advances through the existing time_preference owner.
    client = _gated_client(db, ALL_OPEN)
    conversation = _at_time_window(db, client)
    r1 = send(db, client, conversation, "yesterday")
    assert "calendar_picker" not in (r1.meta or {})
    assert conversation.lead_time_window is None
    d = _next_weekday(client)
    r2 = send(db, client, conversation, _human_message(d))
    assert r2.reply == INTAKE_TIME_PREFERENCE_PROMPT
    assert r2.meta.get("calendar_picker") == TIME_SIGNAL
    assert conversation.lead_time_window == _canonical(d)


# ---------------------------------------------------------------------------
# PURE-PREDICATE CONTRACT — the pre-turn stage proof must be a side-effect-free
# predicate, never the active bypass consumer owner. (Recorded regression: a
# pre-turn receptionist_bypass_reply probe consumed the one-shot reason_detail
# consumer before the real route pass.)
# ---------------------------------------------------------------------------

def test_capture_first_time_window_pending_is_pure(db, fakes, monkeypatch):
    client = _gated_client(db, ALL_OPEN)
    conversation = _at_time_window(db, client)   # standard time_window stage

    def boom(*a, **k):
        raise AssertionError("pure predicate invoked an active/consuming owner")

    for name in ("receptionist_bypass_reply", "classify_other_reason_detail",
                 "map_reason_detail_to_enum", "detect_appointment_reason",
                 "extract_lead_fields_with_ai"):
        if hasattr(chat_module, name):
            monkeypatch.setattr(chat_module, name, boom)

    # No classifier / mapper / AI / bypass-owner call, no mutation:
    assert chat_module.capture_first_time_window_pending(conversation, client) is True
    conversation.lead_time_window = "Mon 2026-08-10 morning"   # window complete
    assert chat_module.capture_first_time_window_pending(conversation, client) is False


def test_reason_detail_turn_uses_no_extra_bypass_consumer(db, fakes, monkeypatch):
    # The one-shot reason_detail entry forcer replaces the FIRST
    # receptionist_bypass_reply() call. If a pre-turn probe consumed it, the
    # route's real call would miss the forced entry. With the pure predicate the
    # route's own call is the first one, so the forced entry is honored.
    from calendar_tests.test_hybrid_capture import (
        make_client as make_hybrid_client, seed_assistant_message,
        BYPASS_ENTRY_PROMPT)

    client = make_hybrid_client(db, booking_mode="hybrid")
    conversation = make_conversation(db, client, lead_reason=None,
                                     lead_email_opt_out=True)
    seed_assistant_message(db, conversation, BYPASS_ENTRY_PROMPT)

    real = chat_module.receptionist_bypass_reply
    calls = {"n": 0, "first_forced": False}

    def counting_entry_forcer(conv, cl=None):
        calls["n"] += 1
        if calls["n"] == 1:
            calls["first_forced"] = True
            return ("(forced consumer entry)", "reason_detail")
        return real(conv, cl)

    monkeypatch.setattr(chat_module, "receptionist_bypass_reply",
                        counting_entry_forcer)
    send(db, client, conversation, "There is a metallic taste in my mouth")
    # The route's real call was the FIRST receptionist_bypass_reply invocation
    # (forced entry honored) -> the pure predicate added no preflight call.
    assert calls["first_forced"] is True
    assert conversation.lead_reason == "appointment request"


# ---------------------------------------------------------------------------
# NATIVE CONTRACT — genuine WAITING_FOR_DATE meta stays exactly {"stage":"date"}.
# ---------------------------------------------------------------------------

def test_native_waiting_for_date_meta_has_no_submit_marker(db):
    client = _gated_client(db, ALL_OPEN)
    settings = load_calendar_settings(client)
    meta = _date_stage_meta(settings)
    assert meta["calendar_picker"] == {"stage": "date"}
    assert "submit" not in meta["calendar_picker"]
