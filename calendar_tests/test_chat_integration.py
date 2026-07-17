# calendar_tests/test_chat_integration.py
#
# PATCH 3 (Senior Audit Critical #5) — Mia <-> Calendar integration tests.
#
# These tests drive the REAL chat() endpoint function against PostgreSQL:
# completion routing (all five call sites), the booking-ownership contract
# (external > internal > lead-capture-only), guard gating during an active
# dialog, information interruptions, conversation-ending behavior at the
# confirmation step, emergency mid-booking cleanup, ownership transition,
# and the honest failure fallbacks.
#
# Determinism: every network boundary is replaced with recording fakes —
#   chat.extract_lead_fields_with_ai / chat.classify_message_guard_with_ai
#     -> return {} (regex/keyword extraction still runs and is what these
#        tests exercise)
#   chat.send_office_lead_sms / chat.send_office_lead_email
#     -> recorders (completed-lead office notifications)
#   notification_service._send_sms / _send_email
#     -> recorders (booking notifications)
# No OpenAI, Twilio, or Resend call can occur.
#
# Prerequisites are the same as the rest of calendar_tests/ (disposable
# PostgreSQL via TEST_DATABASE_URL; see conftest.py). The conftest `engine`
# fixture creates ALL tables (app.models + app.calendar_models) so the full
# chat flow (clients, conversations, messages, client_faqs) works.

import uuid
from datetime import datetime, timedelta, timezone as dt_timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

# conftest.py prepared OPENAI_API_KEY / ADMIN_API_KEY / DATABASE_URL before
# any app import; importing app modules is safe from here on.
import app.routes.chat as chat_module
from app.calendar_models import AppointmentSlot, Appointment, BookingState, SlotStatus
from app.models import Client, Conversation, Message
from app.schemas import ChatRequest
from app.services import booking_conversation, notification_service
from app.services.booking_conversation import (
    begin_booking_after_intake,
    handle_booking_message,
)

NY = ZoneInfo("America/New_York")

MEDICAL_SAFETY_REPLY = (
    "I can’t provide medical advice in chat. "
    "If you’re in pain, the safest next step is to call the office so a clinician can guide you. "
    "If symptoms are severe (swelling, fever, trouble breathing/swallowing), please seek urgent care."
)

FALLBACK_NOTIFIED = (
    "I\u2019m sorry, I couldn\u2019t open the booking calendar right now. "
    "The office has your request and will follow up."
)
REMINDER_TEXT = "The online booking link is still available below."

# A production-realistic office-hours struct (Client.office_hours JSONB).
# All seven days open so day-relative fixtures ("tomorrow morning") are
# valid on ANY run date — with office_hours unset, chat.py's pre-existing
# build_time_window_issue_reply treats every day as CLOSED and bounces the
# time window before it can be stored.
OPEN_ALL_WEEK_HOURS = {
    day: {"open": True, "start": "09:00", "end": "17:00"}
    for day in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
}


# ---------------------------------------------------------------------------
# Fakes (autouse): no network boundary is ever exercised.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    lead_sms, lead_email = [], []
    booking_sms, booking_email = [], []

    monkeypatch.setattr(chat_module, "extract_lead_fields_with_ai", lambda user_text: {})
    monkeypatch.setattr(chat_module, "classify_message_guard_with_ai", lambda user_text: {})
    # Belt-and-suspenders: even the final OpenAI fallback (already wrapped in
    # try/except in chat.py) must never attempt a network call from a test.
    monkeypatch.setattr(
        chat_module, "ai",
        SimpleNamespace(responses=SimpleNamespace(
            create=lambda **kwargs: SimpleNamespace(
                output_text="(ai fallback disabled in tests)"
            )
        )),
    )
    monkeypatch.setattr(
        chat_module, "send_office_lead_sms",
        lambda to_phone, body: lead_sms.append((to_phone, body)),
    )
    monkeypatch.setattr(
        chat_module, "send_office_lead_email",
        lambda to_email, subject, body_text: lead_email.append((to_email, subject, body_text)),
    )
    monkeypatch.setattr(
        notification_service, "_send_sms",
        lambda to_phone, body: booking_sms.append((to_phone, body)),
    )
    monkeypatch.setattr(
        notification_service, "_send_email",
        lambda to_email, subject, body_text: booking_email.append((to_email, subject, body_text)),
    )
    return SimpleNamespace(
        lead_sms=lead_sms, lead_email=lead_email,
        booking_sms=booking_sms, booking_email=booking_email,
    )


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def make_client(db, *, calendar_enabled=None, booking_url=None,
                notification_channels=True, office_hours=None):
    """One dental office. calendar_enabled: None = no calendar settings at
    all; True/False = explicit strict flag. booking_url turns on external
    booking. office_hours: Client.office_hours struct (default None — tests
    asserting the no-hours fallback replies depend on it staying unset).
    Fresh api_key per test keeps tests client-isolated (Rule 15)."""
    settings = {"timezone": "America/New_York"}
    if calendar_enabled is not None:
        settings["calendar"] = {
            "booking_enabled": bool(calendar_enabled),
            "hold_minutes": 5,
            "minimum_notice_minutes": 60,
            "max_offered_slots": 3,
            "max_booking_days": 30,
            "require_staff_confirmation": True,
        }
    if booking_url:
        settings["booking_url"] = booking_url
    client = Client(
        id=uuid.uuid4(),
        practice_name="Integration Test Dental",
        api_key=f"key-{uuid.uuid4()}",
        active=True,
        settings=settings,
        office_hours=office_hours,
        notification_phone="+15550001111" if notification_channels else None,
        notification_email="office@example.com" if notification_channels else None,
    )
    db.add(client)
    db.commit()
    return client


def make_conversation(db, client, **overrides):
    """A conversation one answer away from completion unless overridden:
    reason + name + phone + complete time window + email opt-out are set;
    new/returning (lead_is_new_patient) is the missing field."""
    fields = dict(
        id=uuid.uuid4(),
        client_id=client.id,
        visitor_id="test-visitor",
        is_lead=True,
        lead_status="new",
        lead_reason="cleaning/checkup",
        lead_name="Kevin Alvarado",
        lead_phone="516-555-1234",
        lead_time_window="Tuesday morning",
        lead_email_opt_out=True,
        lead_is_new_patient=None,
    )
    fields.update(overrides)
    conversation = Conversation(**fields)
    db.add(conversation)
    db.commit()
    return conversation


def make_slot(db, client, *, days_ahead=3, hour=10, status=SlotStatus.AVAILABLE,
              held_by=None, held_minutes=5):
    """One staff-published slot at a deterministic future local time."""
    now_local = datetime.now(NY)
    start_local = (now_local + timedelta(days=days_ahead)).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )
    start_utc = start_local.astimezone(dt_timezone.utc)
    slot = AppointmentSlot(
        id=uuid.uuid4(),
        client_id=client.id,
        provider_name="Dr. Test",
        service_key=None,
        start_datetime=start_utc,
        end_datetime=start_utc + timedelta(minutes=30),
        status=status,
        held_until=(
            datetime.now(dt_timezone.utc) + timedelta(minutes=held_minutes)
            if status == SlotStatus.HELD else None
        ),
        held_by_conversation_id=held_by,
    )
    db.add(slot)
    db.commit()
    return slot


def seed_active_confirmation(db, client, conversation):
    """Put the conversation at WAITING_FOR_CONFIRMATION with a real owned
    hold — the exact shape _handle_slot_selection leaves behind."""
    slot = make_slot(db, client, status=SlotStatus.HELD, held_by=conversation.id)
    conversation.booking_state = BookingState.WAITING_FOR_CONFIRMATION
    conversation.booking_preferred_date = (
        slot.start_datetime.astimezone(NY).date().isoformat()
    )
    conversation.booking_offered_slot_ids = [str(slot.id)]
    conversation.booking_selected_slot_id = slot.id
    conversation.booking_effective_time_preference = "any"
    db.add(conversation)
    db.commit()
    return slot


class _FakeAddr:
    host = "127.0.0.1"


class _FakeRequest:
    client = _FakeAddr()


def send(db, client, conversation, text):
    """Call the real endpoint function and refresh the conversation row."""
    req = ChatRequest(
        client_key=client.api_key,
        message=text,
        conversation_id=str(conversation.id) if conversation is not None else None,
        visitor_id="test-visitor",
    )
    resp = chat_module.chat(req, _FakeRequest(), db)
    if conversation is not None:
        db.refresh(conversation)
    return resp


def refreshed_slot(db, slot_id):
    return db.query(AppointmentSlot).filter(AppointmentSlot.id == slot_id).one()


# ===========================================================================
# 1. Completion-routing matrix — ownership contract at every eligible site
# ===========================================================================

def test_completion_with_no_booking_flags_keeps_manual_reply(db, fakes):
    client = make_client(db)  # no calendar settings, no booking_url
    conversation = make_conversation(db, client)

    resp = send(db, client, conversation, "returning")

    assert resp.meta.get("mode") == "lead_complete_after_patient_type"
    # Byte-identical to today's manual-callback reply for THIS conversation.
    assert resp.reply == chat_module.build_normal_lead_complete_reply(conversation)
    assert (conversation.booking_state or "none") == BookingState.NONE
    assert (conversation.lead_status or "").lower() == "completed"
    assert len(fakes.lead_sms) == 1 and len(fakes.lead_email) == 1


def test_internal_patient_type_completion_starts_booking(db, fakes):
    client = make_client(db, calendar_enabled=True)
    conversation = make_conversation(db, client)

    resp = send(db, client, conversation, "returning")

    assert resp.meta.get("mode") == "booking"
    assert resp.meta.get("state") == BookingState.WAITING_FOR_DATE
    assert resp.reply == "What day would work best for your appointment?"
    assert conversation.booking_state == BookingState.WAITING_FOR_DATE
    assert (conversation.lead_status or "").lower() == "completed"
    # The completed-lead office notification ran FIRST (approved temporary
    # MVP behavior) and exactly once per channel.
    assert len(fakes.lead_sms) == 1 and len(fakes.lead_email) == 1
    assert resp.meta.get("lead_email_sent") is True
    assert resp.meta.get("lead_sms_sent") is True


def test_internal_lead_complete_branch_starts_booking(db, fakes):
    client = make_client(db, calendar_enabled=True)
    # Patient type already known; the email skip is the completing answer,
    # which lands in the LEAD COMPLETION branch (not the patient-type one).
    conversation = make_conversation(
        db, client, lead_is_new_patient=True, lead_email_opt_out=False
    )

    resp = send(db, client, conversation, "skip")

    assert resp.meta.get("mode") == "booking"
    assert conversation.booking_state == BookingState.WAITING_FOR_DATE
    assert (conversation.lead_status or "").lower() == "completed"
    assert len(fakes.lead_sms) == 1 and len(fakes.lead_email) == 1


def test_internal_short_symptom_completion_starts_booking(db, fakes):
    # office_hours must be configured (as in production): without it,
    # chat.py's pre-existing time-window issue check reads every day as
    # closed and bounces "tomorrow morning" before it is stored, so the
    # completion site is never reached (2026-07-12 failed-run root cause).
    client = make_client(db, calendar_enabled=True,
                         office_hours=OPEN_ALL_WEEK_HOURS)
    # Short-symptom flow: symptom reason, priority, no time window yet.
    conversation = make_conversation(
        db, client,
        lead_reason="tooth pain",
        lead_is_priority=True,
        lead_time_window=None,
        lead_email_opt_out=False,
    )

    resp = send(db, client, conversation, "tomorrow morning")

    assert resp.meta.get("mode") == "booking"
    # The completing message named a day, so the Calendar start parsed it
    # and moved straight to the time-preference question.
    assert conversation.booking_state == BookingState.WAITING_FOR_TIME_PREFERENCE
    assert chat_module.time_window_is_complete(conversation.lead_time_window)
    assert (conversation.lead_status or "").lower() == "completed"
    assert len(fakes.lead_sms) == 1 and len(fakes.lead_email) == 1


def test_internal_priority_time_window_completion_starts_booking(db, fakes):
    client = make_client(db, calendar_enabled=True)
    conversation = make_conversation(
        db, client, lead_time_window=None, lead_is_new_patient=None
    )

    resp = send(db, client, conversation, "as soon as possible please")

    # handle_time_window_capture set ASAP + priority; the priority
    # time-window completion site notified the office and routed to the
    # Calendar (priority NON-emergency leads may book).
    assert resp.meta.get("mode") == "booking"
    assert (conversation.lead_time_window or "").strip().upper() == "ASAP"
    assert conversation.lead_is_priority is True
    assert conversation.booking_state == BookingState.WAITING_FOR_DATE
    assert (conversation.lead_status or "").lower() == "completed"
    assert len(fakes.lead_sms) == 1 and len(fakes.lead_email) == 1


def test_internal_bypass_priority_completion_starts_booking(db, fakes):
    client = make_client(db, calendar_enabled=True)
    # Priority intake already complete BEFORE the message; a neutral message
    # falls through to the receptionist bypass at its "complete" stage.
    conversation = make_conversation(
        db, client,
        lead_is_priority=True,
        lead_time_window="ASAP",
        lead_email_opt_out=False,
    )

    resp = send(db, client, conversation, "ok")

    assert resp.meta.get("mode") == "booking"
    assert conversation.booking_state == BookingState.WAITING_FOR_DATE
    assert (conversation.lead_status or "").lower() == "completed"
    assert len(fakes.lead_sms) == 1 and len(fakes.lead_email) == 1


def test_external_completion_sends_link_via_shared_owner(db, fakes):
    client = make_client(db, booking_url="https://book.example.com")
    conversation = make_conversation(db, client)

    resp = send(db, client, conversation, "returning")

    assert resp.meta.get("mode") == "external_booking_handoff"
    assert conversation.booking_link_sent is True
    assert (conversation.booking_state or "none") == BookingState.NONE
    assert (conversation.lead_status or "").lower() == "completed"
    assert len(fakes.lead_sms) == 1 and len(fakes.lead_email) == 1


def test_both_flags_external_wins(db, fakes):
    client = make_client(
        db, calendar_enabled=True, booking_url="https://book.example.com"
    )
    conversation = make_conversation(db, client)

    resp = send(db, client, conversation, "returning")

    assert resp.meta.get("mode") == "external_booking_handoff"
    assert conversation.booking_link_sent is True
    # The internal Calendar never started.
    assert (conversation.booking_state or "none") == BookingState.NONE


# ===========================================================================
# 2. External pre-completion behavior — unchanged, plus post-link ownership
# ===========================================================================

def test_precompletion_capture_first_unchanged(db, fakes):
    client = make_client(db, booking_url="https://book.example.com")
    # Fresh visitor, nothing captured yet: the capture-first rule asks for
    # contact details before handing out the link.
    req = ChatRequest(
        client_key=client.api_key,
        message="I want to book a cleaning",
        conversation_id=None,
        visitor_id="test-visitor",
    )
    resp = chat_module.chat(req, _FakeRequest(), db)

    assert resp.meta.get("mode") == "booking_capture_first"
    conversation = (
        db.query(Conversation)
        .filter(Conversation.id == uuid.UUID(resp.conversation_id))
        .one()
    )
    assert bool(conversation.booking_link_sent) is False


def test_precompletion_direct_link_unchanged(db, fakes):
    client = make_client(db, booking_url="https://book.example.com")
    # Name + phone already captured: the link goes out immediately.
    conversation = make_conversation(
        db, client, lead_time_window=None, lead_is_new_patient=None
    )

    resp = send(db, client, conversation, "I'd like to book an appointment")

    assert resp.meta.get("mode") == "external_booking_handoff"
    assert conversation.booking_link_sent is True


def test_post_link_scheduling_gets_reminder(db, fakes):
    client = make_client(db, booking_url="https://book.example.com")
    conversation = make_conversation(
        db, client, lead_time_window=None, lead_is_new_patient=None,
        booking_link_sent=True,
    )

    resp = send(db, client, conversation, "I want to book an appointment")

    assert resp.reply == REMINDER_TEXT
    assert resp.meta.get("mode") == "external_booking_link_reminder"
    # The booking button/meta survive the reminder (correction pass).
    assert resp.meta.get("open_booking_in_new_tab") is True
    assert conversation.booking_link_sent is True
    # External stays the owner: no internal dialog began.
    assert (conversation.booking_state or "none") == BookingState.NONE


def test_post_link_reminder_is_repeatable(db, fakes):
    client = make_client(db, booking_url="https://book.example.com")
    conversation = make_conversation(
        db, client, lead_time_window=None, lead_is_new_patient=None,
        booking_link_sent=True,
    )

    first = send(db, client, conversation, "can I book online?")
    second = send(db, client, conversation, "book appointment")

    assert first.reply == REMINDER_TEXT
    assert second.reply == REMINDER_TEXT
    assert first.meta.get("mode") == "external_booking_link_reminder"
    assert second.meta.get("mode") == "external_booking_link_reminder"


# ===========================================================================
# 3. Start-entry gates
# ===========================================================================

def test_begin_booking_refuses_emergency(db, fakes):
    client = make_client(db, calendar_enabled=True)
    conversation = make_conversation(db, client, lead_is_emergency=True)

    reply = begin_booking_after_intake(db, client, conversation, "returning")

    assert reply.handled is False
    db.refresh(conversation)
    assert (conversation.booking_state or "none") == BookingState.NONE


def test_completion_with_booking_disabled_falls_back_to_manual(db, fakes):
    client = make_client(db, calendar_enabled=False)
    conversation = make_conversation(db, client)

    resp = send(db, client, conversation, "returning")

    assert resp.meta.get("mode") == "lead_complete_after_patient_type"
    assert resp.reply == chat_module.build_normal_lead_complete_reply(conversation)
    assert (conversation.booking_state or "none") == BookingState.NONE


# ===========================================================================
# 4. Mid-dialog behavior: guard gating, interruptions, endings
# ===========================================================================

def test_mid_booking_time_answer_goes_to_state_machine(db, fakes):
    client = make_client(db, calendar_enabled=True)
    conversation = make_conversation(db, client)
    send(db, client, conversation, "returning")           # booking starts
    assert conversation.booking_state == BookingState.WAITING_FOR_DATE

    resp = send(db, client, conversation, "tomorrow")

    assert resp.meta.get("mode") == "booking"
    assert conversation.booking_state == BookingState.WAITING_FOR_TIME_PREFERENCE
    # The intake time-window guard did NOT swallow the booking answer:
    assert conversation.lead_time_window == "Tuesday morning"


def test_information_interruption_pauses_and_resumes(db, fakes):
    # Detector contract first (Condition 3): one positive, all the normal
    # booking answers negative.
    assert chat_module.is_information_interruption("what are your hours?") is True
    for booking_answer in ["tomorrow", "morning", "2", "yes", "no"]:
        assert chat_module.is_information_interruption(booking_answer) is False, booking_answer

    client = make_client(db, calendar_enabled=True)
    conversation = make_conversation(db, client)
    send(db, client, conversation, "returning")           # booking starts
    state_before = conversation.booking_state
    date_before = conversation.booking_preferred_date

    resp = send(db, client, conversation, "what are your hours?")

    # Answered by the existing operational path; dialog state untouched.
    assert resp.meta.get("mode") != "booking"
    assert resp.reply == "Please call the office and our team can confirm our office hours."
    assert conversation.booking_state == state_before
    assert conversation.booking_preferred_date == date_before

    resumed = send(db, client, conversation, "tomorrow")
    assert resumed.meta.get("mode") == "booking"
    assert conversation.booking_state == BookingState.WAITING_FOR_TIME_PREFERENCE


def test_no_and_no_thanks_at_confirmation_reach_state_machine(db, fakes):
    client = make_client(db, calendar_enabled=True)
    for rejection in ["no", "no thanks"]:
        conversation = make_conversation(db, client, lead_status="completed")
        slot = seed_active_confirmation(db, client, conversation)

        resp = send(db, client, conversation, rejection)

        # Calendar rejection path — NOT the conversation-ending guard.
        assert resp.reply == "No problem — what day would work better?", rejection
        assert resp.meta.get("mode") == "booking"
        assert conversation.booking_state == BookingState.WAITING_FOR_DATE
        assert conversation.booking_selected_slot_id is None
        slot_row = refreshed_slot(db, slot.id)
        assert slot_row.status == SlotStatus.AVAILABLE
        assert slot_row.held_by_conversation_id is None


def test_genuine_ending_mid_booking_cancels_and_keeps_ending_reply(db, fakes):
    client = make_client(db, calendar_enabled=True)
    conversation = make_conversation(db, client, lead_status="completed")
    slot = seed_active_confirmation(db, client, conversation)

    resp = send(db, client, conversation, "bye")

    # Mia's existing ending reply and mode are preserved; no Calendar
    # question is re-asked.
    assert resp.meta.get("mode") == "conversation_ending"
    assert "day would work" not in resp.reply
    # The Calendar-owned reset ran: hold released, every field cleared.
    assert (conversation.booking_state or "none") == BookingState.NONE
    assert conversation.booking_selected_slot_id is None
    assert conversation.booking_offered_slot_ids in (None, [])
    slot_row = refreshed_slot(db, slot.id)
    assert slot_row.status == SlotStatus.AVAILABLE
    assert slot_row.held_by_conversation_id is None


def test_stale_state_never_resurrects_after_ending(db, fakes):
    # Fresh-ownership contract (correction pass): after a genuine ending, a
    # NEUTRAL message resurrects nothing — the OLD dialog (state, offers,
    # selected slot) is gone for good. A fresh scheduling/date message MAY
    # start a clean NEW dialog under the same contract; that path is proven
    # by test_url_removed_fresh_internal_dialog_starts.
    client = make_client(db, calendar_enabled=True)
    conversation = make_conversation(db, client, lead_status="completed")
    slot = seed_active_confirmation(db, client, conversation)
    send(db, client, conversation, "bye")
    assert (conversation.booking_state or "none") == BookingState.NONE

    resp = send(db, client, conversation, "thanks")

    assert resp.meta.get("mode") != "booking"
    assert (conversation.booking_state or "none") == BookingState.NONE
    assert conversation.booking_selected_slot_id is None
    assert conversation.booking_offered_slot_ids in (None, [])
    slot_row = refreshed_slot(db, slot.id)
    assert slot_row.status == SlotStatus.AVAILABLE


# ===========================================================================
# 5. Emergency mid-booking
# ===========================================================================

def test_emergency_mid_booking_cleans_up_same_request(db, fakes):
    client = make_client(db, calendar_enabled=True)
    conversation = make_conversation(db, client, lead_status="completed")
    slot = seed_active_confirmation(db, client, conversation)

    resp = send(db, client, conversation, "I have uncontrolled bleeding from my mouth")

    # The emergency reply is the patient-facing response — never a booking
    # prompt — and the cleanup happened in THIS request.
    assert resp.meta.get("mode") != "booking"
    assert "day would work" not in resp.reply
    assert (conversation.booking_state or "none") == BookingState.NONE
    assert conversation.booking_selected_slot_id is None
    slot_row = refreshed_slot(db, slot.id)
    assert slot_row.status == SlotStatus.AVAILABLE
    assert slot_row.held_by_conversation_id is None


def test_cleanup_failure_never_suppresses_emergency_reply(db, fakes, monkeypatch):
    client = make_client(db, calendar_enabled=True)
    conversation = make_conversation(db, client, lead_status="completed")
    seed_active_confirmation(db, client, conversation)

    def boom(*args, **kwargs):
        raise RuntimeError("simulated cleanup failure")

    monkeypatch.setattr(chat_module, "cancel_active_booking", boom)

    # Must NOT raise (no 500) and must still return a non-booking reply.
    resp = send(db, client, conversation, "I have uncontrolled bleeding from my mouth")

    assert resp is not None
    assert resp.meta.get("mode") != "booking"
    assert "day would work" not in resp.reply


# ===========================================================================
# 6. Ownership transition (internal -> external) and no resurrection
# ===========================================================================

def test_internal_to_external_transition_mid_booking(db, fakes):
    client = make_client(db, calendar_enabled=True)
    conversation = make_conversation(db, client, lead_status="completed")
    slot = seed_active_confirmation(db, client, conversation)

    # The office turns on external booking mid-dialog.
    client.settings = {**client.settings, "booking_url": "https://book.example.com"}
    db.add(client)
    db.commit()

    # "2" would never satisfy the external trigger's intent conditions —
    # the continuation hook must hand off in THIS request anyway.
    resp = send(db, client, conversation, "2")

    assert resp.meta.get("mode") == "external_booking_handoff"
    assert conversation.booking_link_sent is True
    assert (conversation.booking_state or "none") == BookingState.NONE
    slot_row = refreshed_slot(db, slot.id)
    assert slot_row.status == SlotStatus.AVAILABLE
    assert slot_row.held_by_conversation_id is None


def test_url_removed_fresh_internal_dialog_starts(db, fakes):
    # Fresh-ownership contract (correction pass): after internal -> external
    # transition and later URL removal, ownership resolves fresh — a NEW
    # internal dialog starts cleanly on a scheduling/date message; stale
    # state does NOT resurrect and booking_link_sent does not block.
    client = make_client(db, calendar_enabled=True)
    conversation = make_conversation(db, client, lead_status="completed")
    old_slot = seed_active_confirmation(db, client, conversation)
    client.settings = {**client.settings, "booking_url": "https://book.example.com"}
    db.add(client)
    db.commit()
    send(db, client, conversation, "2")                   # transition happened
    assert (conversation.booking_state or "none") == BookingState.NONE
    assert conversation.booking_link_sent is True

    new_settings = {**client.settings}
    new_settings.pop("booking_url", None)
    client.settings = new_settings
    db.add(client)
    db.commit()

    resp = send(db, client, conversation, "can we book tomorrow?")

    assert resp.meta.get("mode") == "booking"
    # A clean NEW dialog: the completing message seeded tomorrow's date;
    # nothing stale (selected slot / offers) came back with it.
    assert conversation.booking_state == BookingState.WAITING_FOR_TIME_PREFERENCE
    assert conversation.booking_selected_slot_id is None
    assert conversation.booking_offered_slot_ids in (None, [])
    assert conversation.booking_link_sent is True          # did not block
    old_row = refreshed_slot(db, old_slot.id)
    assert old_row.status == SlotStatus.AVAILABLE          # not silently re-held


# ===========================================================================
# 6b. Correction pass: medical safety, hold-safe emergency defense,
#     single-owner transition failure, post-link hijack, location yield
# ===========================================================================

def test_medical_advice_mid_booking_yields_and_resumes(db, fakes):
    client = make_client(db, calendar_enabled=True)
    conversation = make_conversation(db, client, lead_status="completed")
    slot = seed_active_confirmation(db, client, conversation)

    resp = send(db, client, conversation, "What medicine can I take for this tooth pain?")

    # The existing medical-advice safety response wins; no Calendar question.
    assert resp.reply == MEDICAL_SAFETY_REPLY
    assert resp.meta.get("mode") == "safety_guard"
    # Not necessarily an emergency: state AND the held slot stay unchanged.
    assert conversation.booking_state == BookingState.WAITING_FOR_CONFIRMATION
    assert conversation.booking_selected_slot_id == slot.id
    slot_row = refreshed_slot(db, slot.id)
    assert slot_row.status == SlotStatus.HELD
    assert slot_row.held_by_conversation_id == conversation.id

    # The next valid scheduling answer resumes the SAME dialog step.
    resumed = send(db, client, conversation, "no")
    assert resumed.reply == "No problem — what day would work better?"
    assert conversation.booking_state == BookingState.WAITING_FOR_DATE
    assert refreshed_slot(db, slot.id).status == SlotStatus.AVAILABLE


def test_medical_advice_defers_external_transition(db, fakes):
    client = make_client(db, calendar_enabled=True)
    conversation = make_conversation(db, client, lead_status="completed")
    slot = seed_active_confirmation(db, client, conversation)
    client.settings = {**client.settings, "booking_url": "https://book.example.com"}
    db.add(client)
    db.commit()

    resp = send(db, client, conversation, "What medicine can I take for this tooth pain?")

    # Safety still wins even though an external URL appeared on this
    # message; the ownership transition waits for the next appropriate one.
    assert resp.reply == MEDICAL_SAFETY_REPLY
    assert resp.meta.get("mode") == "safety_guard"
    assert conversation.booking_link_sent is False
    assert conversation.booking_state == BookingState.WAITING_FOR_CONFIRMATION
    assert refreshed_slot(db, slot.id).status == SlotStatus.HELD

    follow_up = send(db, client, conversation, "2")
    assert follow_up.meta.get("mode") == "external_booking_handoff"
    assert conversation.booking_link_sent is True
    assert (conversation.booking_state or "none") == BookingState.NONE
    assert refreshed_slot(db, slot.id).status == SlotStatus.AVAILABLE


def test_emergency_gate_releases_active_hold(db, fakes):
    # Correction pass: the Calendar's OWN emergency defense (not chat.py's
    # text detectors) must release a live hold, not orphan it. The message
    # here ("tomorrow") never triggers chat.py's emergency guards.
    client = make_client(db, calendar_enabled=True)

    # Via the continuation entry.
    conversation = make_conversation(db, client, lead_status="completed")
    slot = seed_active_confirmation(db, client, conversation)
    conversation.lead_is_emergency = True
    db.add(conversation)
    db.commit()

    reply = handle_booking_message(db, client, conversation, "tomorrow")

    assert reply.handled is False
    db.refresh(conversation)
    assert (conversation.booking_state or "none") == BookingState.NONE
    assert conversation.booking_selected_slot_id is None
    slot_row = refreshed_slot(db, slot.id)
    assert slot_row.status == SlotStatus.AVAILABLE
    assert slot_row.held_by_conversation_id is None

    # Via the start entry (same two-layer rule, same reset pathway).
    conversation2 = make_conversation(db, client, lead_status="completed")
    slot2 = seed_active_confirmation(db, client, conversation2)
    conversation2.lead_is_emergency = True
    db.add(conversation2)
    db.commit()

    reply2 = begin_booking_after_intake(db, client, conversation2, "tomorrow")

    assert reply2.handled is False
    db.refresh(conversation2)
    assert (conversation2.booking_state or "none") == BookingState.NONE
    assert refreshed_slot(db, slot2.id).status == SlotStatus.AVAILABLE


def test_transition_cleanup_failure_keeps_single_owner(db, fakes, monkeypatch):
    client = make_client(db, calendar_enabled=True)
    # Office already notified: both per-channel flags persisted True.
    conversation = make_conversation(
        db, client, lead_status="completed",
        lead_email_sent=True, lead_sms_sent=True,
    )
    slot = seed_active_confirmation(db, client, conversation)
    client.settings = {**client.settings, "booking_url": "https://book.example.com"}
    db.add(client)
    db.commit()

    calls = {"n": 0}
    real_cancel = booking_conversation.cancel_active_booking

    def flaky_cancel(db_arg, client_arg, conversation_arg):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated cancellation failure")
        return real_cancel(db_arg, client_arg, conversation_arg)

    monkeypatch.setattr(chat_module, "cancel_active_booking", flaky_cancel)

    resp = send(db, client, conversation, "2")

    # No external handoff, no second owner, no false "cleared" claim.
    assert resp.meta.get("mode") == "booking_error"
    assert resp.reply == FALLBACK_NOTIFIED
    assert conversation.booking_link_sent is False
    # The internal dialog (and its hold) is intact — retryable, one owner.
    assert conversation.booking_state == BookingState.WAITING_FOR_CONFIRMATION
    slot_row = refreshed_slot(db, slot.id)
    assert slot_row.status == SlotStatus.HELD
    assert slot_row.held_by_conversation_id == conversation.id
    # Persisted flags respected: zero duplicate lead sends.
    assert len(fakes.lead_sms) == 0 and len(fakes.lead_email) == 0

    # The very next message retries the transition and succeeds.
    retry = send(db, client, conversation, "2")
    assert retry.meta.get("mode") == "external_booking_handoff"
    assert conversation.booking_link_sent is True
    assert (conversation.booking_state or "none") == BookingState.NONE
    assert refreshed_slot(db, slot.id).status == SlotStatus.AVAILABLE
    assert len(fakes.lead_sms) == 0 and len(fakes.lead_email) == 0


def test_post_link_unrelated_message_not_hijacked(db, fakes):
    client = make_client(db, booking_url="https://book.example.com")
    # lead_reason is set (factory default) — exactly the stored value that
    # used to make the trigger fire for every message after the link.
    # Intake is deliberately UNFINISHED (lead_is_new_patient=None), so the
    # pre-Patch-3 operational behavior appends the next intake prompt to
    # the hours answer — the combined reply below is the exact existing
    # Patch 2D wording, preserved on purpose.
    conversation = make_conversation(db, client, booking_link_sent=True)

    resp = send(db, client, conversation, "what are your hours?")

    assert resp.meta.get("mode") != "external_booking_link_reminder"
    assert resp.meta.get("mode") == "faq_operational_no_match"
    assert resp.reply == (
        "Please call the office and our team can confirm our office hours."
        "\n\n"
        "One quick question — Kevin Alvarado, are you a new or returning patient?"
    )
    assert conversation.booking_link_sent is True
    assert (conversation.booking_state or "none") == BookingState.NONE
    # No internal Calendar owner started and no completed-lead notification
    # was sent for this message.
    assert len(fakes.lead_sms) == 0 and len(fakes.lead_email) == 0


def test_location_interruption_pauses_and_resumes(db, fakes):
    # Detector contract: one owner, positive case, and the normal booking
    # answers stay negative.
    assert chat_module.looks_like_location_request("where are you located?") is True
    assert chat_module.is_information_interruption("where are you located?") is True
    for booking_answer in ["tomorrow", "morning", "2", "yes", "no"]:
        assert chat_module.looks_like_location_request(booking_answer) is False, booking_answer

    client = make_client(db, calendar_enabled=True)
    conversation = make_conversation(db, client, lead_status="completed")
    conversation.booking_state = BookingState.WAITING_FOR_DATE
    db.add(conversation)
    db.commit()

    resp = send(db, client, conversation, "where are you located?")

    # The existing location path answers; the dialog is untouched.
    assert resp.reply == "Please call the office and our team can share our address and directions."
    assert resp.meta.get("mode") == "faq_operational_no_match"
    db.refresh(conversation)
    assert conversation.booking_state == BookingState.WAITING_FOR_DATE

    resumed = send(db, client, conversation, "tomorrow")
    assert resumed.meta.get("mode") == "booking"
    assert conversation.booking_state == BookingState.WAITING_FOR_TIME_PREFERENCE


# ===========================================================================
# 7. Honest failure fallbacks (Rule 16) — no double-send, no false claims
# ===========================================================================

def test_calendar_failure_after_notification_uses_honest_fallback(db, fakes, monkeypatch):
    client = make_client(db, calendar_enabled=True)
    conversation = make_conversation(db, client)

    def boom(*args, **kwargs):
        raise RuntimeError("simulated calendar failure")

    monkeypatch.setattr(chat_module, "begin_booking_after_intake", boom)

    resp = send(db, client, conversation, "returning")

    assert resp.reply == FALLBACK_NOTIFIED
    assert resp.meta.get("mode") == "booking_error"
    # The completed-lead notification went out exactly once per channel —
    # the fallback consulted the per-channel flags and re-sent nothing.
    assert len(fakes.lead_sms) == 1 and len(fakes.lead_email) == 1
    assert (conversation.booking_state or "none") == BookingState.NONE


def test_calendar_failure_with_no_channels_directs_to_phone(db, fakes, monkeypatch):
    client = make_client(db, calendar_enabled=True, notification_channels=False)
    conversation = make_conversation(db, client)

    def boom(*args, **kwargs):
        raise RuntimeError("simulated calendar failure")

    monkeypatch.setattr(chat_module, "begin_booking_after_intake", boom)

    resp = send(db, client, conversation, "returning")

    # No office channel succeeded: never claim the office has the request.
    assert resp.meta.get("mode") == "booking_error"
    assert "The office has your request" not in resp.reply
    assert "Please call the office at" in resp.reply
    assert len(fakes.lead_sms) == 0 and len(fakes.lead_email) == 0


def test_mid_booking_failure_fallback_does_not_resend(db, fakes, monkeypatch):
    client = make_client(db, calendar_enabled=True)
    # Office already notified in a previous turn: both flags True.
    conversation = make_conversation(
        db, client, lead_status="completed",
        lead_email_sent=True, lead_sms_sent=True,
    )
    conversation.booking_state = BookingState.WAITING_FOR_DATE
    db.add(conversation)
    db.commit()

    def boom(*args, **kwargs):
        raise RuntimeError("simulated calendar failure")

    monkeypatch.setattr(chat_module, "handle_booking_message", boom)

    resp = send(db, client, conversation, "tomorrow")

    assert resp.reply == FALLBACK_NOTIFIED
    assert resp.meta.get("mode") == "booking_error"
    # Per-channel idempotency: zero new sends.
    assert len(fakes.lead_sms) == 0 and len(fakes.lead_email) == 0


# ===========================================================================
# 8. Full internal booking drive-through — priority urgency preserved
# ===========================================================================

def test_priority_lead_booked_end_to_end_with_priority_urgency(db, fakes):
    # Same production-realistic fixture as the short-symptom completion
    # test: the seeding message "tomorrow morning" needs an open day.
    client = make_client(db, calendar_enabled=True,
                         office_hours=OPEN_ALL_WEEK_HOURS)
    conversation = make_conversation(
        db, client,
        lead_reason="tooth pain",
        lead_is_priority=True,
        lead_time_window=None,
        lead_email_opt_out=False,
    )
    # A staff-published slot TOMORROW at 10:00 AM local — always more than
    # 60 minutes of notice away, and it matches the day the completing
    # message will seed ("tomorrow") and the "morning" preference.
    slot = make_slot(db, client, days_ahead=1, hour=10)

    # Completion (short-symptom site) starts the booking with the date seeded.
    send(db, client, conversation, "tomorrow morning")
    assert conversation.booking_state == BookingState.WAITING_FOR_TIME_PREFERENCE

    resp = send(db, client, conversation, "morning")
    assert conversation.booking_state == BookingState.WAITING_FOR_SLOT_SELECTION, resp.reply

    resp = send(db, client, conversation, "1")
    assert conversation.booking_state == BookingState.WAITING_FOR_CONFIRMATION, resp.reply

    resp = send(db, client, conversation, "yes")
    assert resp.meta.get("mode") == "booking", resp.reply
    assert resp.meta.get("booked") is True, resp.reply
    assert (conversation.booking_state or "none") == BookingState.NONE

    appointment = (
        db.query(Appointment)
        .filter(
            Appointment.client_id == client.id,
            Appointment.conversation_id == conversation.id,
        )
        .one()
    )
    assert appointment.urgency == "priority"
    slot_row = refreshed_slot(db, slot.id)
    assert slot_row.status == SlotStatus.BOOKED
    # The separate booking notification went out (approved temporary MVP:
    # completed-lead notification earlier + booking notification now).
    assert len(fakes.booking_sms) + len(fakes.booking_email) >= 1


# ===========================================================================
# PATCH 6 (Senior Audit Recommended #7) — lead-notification output
# boundaries: HTML escaping, subject/body normalization, fixed error codes
# in ChatResponse meta, and PII-free server logging.
# ===========================================================================

# Captured at MODULE IMPORT time — before the autouse `fakes` fixture
# replaces the module attribute — so the REAL output-boundary function
# stays testable.
_REAL_SEND_OFFICE_LEAD_EMAIL = chat_module.send_office_lead_email

LEAD_EMAIL_PRE_OPEN = "<pre style='font-family:Arial,sans-serif;white-space:pre-wrap'>"


def _stub_resend(monkeypatch):
    """Replace chat.py's resend module object with a recorder and provide
    the two env values send_office_lead_email reads. Returns the recording
    list of provider params dicts."""
    sent = []
    stub = SimpleNamespace(
        api_key=None,
        Emails=SimpleNamespace(send=lambda params: sent.append(params)),
    )
    monkeypatch.setattr(chat_module, "resend", stub)
    monkeypatch.setenv("RESEND_API_KEY", "test-key")
    monkeypatch.setenv("RESEND_FROM_EMAIL", "Mia <mia@test.example>")
    return sent


def test_lead_email_html_escaped_at_boundary(monkeypatch):
    """The lead office email escapes untrusted text exactly once inside the
    byte-identical fixed <pre> wrapper — markup in patient-typed values is
    inert, ordinary text stays readable."""
    sent = _stub_resend(monkeypatch)
    _REAL_SEND_OFFICE_LEAD_EMAIL(
        "office@example.com",
        "Appointment request - Test Dental",
        'Name: <script>alert(1)</script>\nReason: a & b <img src=x onerror=y>',
    )
    assert len(sent) == 1
    doc = sent[0]["html"]
    assert doc.startswith(LEAD_EMAIL_PRE_OPEN) and doc.endswith("</pre>")
    assert doc.count("<pre") == 1
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in doc
    assert "<script" not in doc and "<img" not in doc
    assert "a &amp; b" in doc
    assert "Name: " in doc  # structural newline template survives escaping
    assert sent[0]["to"] == ["office@example.com"]  # recipient unchanged


def test_lead_email_subject_and_body_values_normalized_bounded(db, fakes, monkeypatch):
    """Builder VALUES are flattened and bounded (structure/labels intact);
    the practice name obeys its 120-char field limit BEFORE subject
    assembly; the complete subject is normalized at the send boundary so
    CR/LF can never reach an email header and length stays within 160;
    stored office data is never modified."""
    client = make_client(db)
    client.practice_name = "P" * 200  # over the 120-char field limit
    db.add(client)
    db.commit()
    conversation = make_conversation(db, client, lead_name="Kevin\r\nAlvarado")
    conversation.lead_is_outside_hours = True
    conversation.lead_outside_hours_note = "n" * 400

    # Subject ASSEMBLY proof: the recorder fake receives the subject exactly
    # as notify_office_of_completed_lead assembled it (before the final
    # send-boundary pass) — the practice name inside it is exactly 119
    # characters + U+2026.
    chat_module.notify_office_of_completed_lead(db, client, conversation)
    assert len(fakes.lead_email) == 1
    _to, assembled_subject, _body = fakes.lead_email[0]
    assert assembled_subject == "Appointment request - " + "P" * 119 + "\u2026"
    assert len(assembled_subject) <= 160
    db.refresh(client)
    assert client.practice_name == "P" * 200  # storage untouched

    summary = chat_module.build_staff_lead_summary(client, conversation)
    lines = summary.split("\n")
    assert "Name: Kevin Alvarado" in lines           # CR/LF flattened to one space
    note_lines = [l for l in lines if l.startswith("Outside-hours note: ")]
    assert note_lines and note_lines[0] == "Outside-hours note: " + "n" * 299 + "\u2026"
    assert "\r" not in summary

    sent = _stub_resend(monkeypatch)
    _REAL_SEND_OFFICE_LEAD_EMAIL(
        "office@example.com",
        "Appointment request\r\nBcc: attacker@example.com - " + "p" * 200,
        summary,
    )
    subject = sent[0]["subject"]
    assert "\r" not in subject and "\n" not in subject
    assert len(subject) <= 160 and subject.endswith("\u2026")
    assert subject.startswith("Appointment request Bcc: attacker@example.com")


def test_lead_error_meta_carries_fixed_code_never_raw_message(db, fakes, monkeypatch):
    """Both lead channels failing surfaces EXACTLY send_failed in the
    ChatResponse meta — the raw provider message never enters the response.
    The completion reply and flags stay honest (nothing marked sent)."""
    client = make_client(db)  # no calendar, no booking_url -> manual path
    conversation = make_conversation(db, client)
    secret = ("Authorization: Bearer sk_live_SECRET123 at "
              "https://api.resend.example/emails")

    def boom(*args, **kwargs):
        raise RuntimeError(secret)

    # Override the autouse recorders with failing providers.
    monkeypatch.setattr(chat_module, "send_office_lead_email", boom)
    monkeypatch.setattr(chat_module, "send_office_lead_sms", boom)

    resp = send(db, client, conversation, "returning")

    assert resp.meta.get("mode") == "lead_complete_after_patient_type"
    assert resp.meta.get("lead_email_error") == "send_failed"
    assert resp.meta.get("lead_sms_error") == "send_failed"
    assert resp.meta.get("lead_email_sent") is False
    assert resp.meta.get("lead_sms_sent") is False
    blob = str(resp.meta) + resp.reply
    for fragment in ("Authorization", "Bearer", "sk_live", "https://", "RuntimeError"):
        assert fragment not in blob


def test_lead_sms_values_normalized_and_server_logs_pii_free(db, fakes, monkeypatch, capsys):
    """End to end through the real chat() endpoint with a hostile lead name,
    a failing lead-email provider, and a failing Calendar delegation:
    - the staff SMS body has flattened, UNescaped plain-text values with the
      structural ' | ' separators intact,
    - the lead-email failure and the Calendar error boundary log ONLY
      controlled fields (fixed event/channel/code, exception class, UUIDs),
    - no patient name/phone, provider URL, header, token, exception message,
      or traceback appears in captured server output."""
    client = make_client(db, calendar_enabled=True)
    conversation = make_conversation(
        db, client, lead_name="Kevin\r\n<b>Alvarado</b>",
    )
    secret = ("Authorization: Bearer sk_live_SECRET123 at "
              "https://api.twilio.example/messages")

    def boom(*args, **kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr(chat_module, "send_office_lead_email", boom)
    monkeypatch.setattr(chat_module, "begin_booking_after_intake", boom)

    resp = send(db, client, conversation, "returning")
    out = capsys.readouterr().out

    # Honest fallback: SMS succeeded, so the office HAS the request.
    assert resp.meta.get("mode") == "booking_error"
    assert resp.reply == FALLBACK_NOTIFIED
    assert len(fakes.lead_sms) == 1
    sms_body = fakes.lead_sms[0][1]
    assert "Name: Kevin <b>Alvarado</b>" in sms_body   # flattened, NOT HTML-escaped
    assert "&lt;" not in sms_body
    assert "\r" not in sms_body and "\n" not in sms_body
    assert " | " in sms_body                            # structural separators intact

    # Controlled log fields present:
    assert "[LEAD_NOTIFY] event=begin" in out
    assert "event=send_failed channel=office_email" in out
    assert "code=send_failed" in out
    assert "exc_class=RuntimeError" in out
    assert f"CALENDAR ERROR: exc_class=RuntimeError conversation={conversation.id}" in out
    # PATCH 6 correction pass: the GATE diagnostic logs derived fields only —
    # the raw patient message field is gone.
    assert "[GATE]" in out
    assert "text_present=" in out and "text_length=" in out
    assert "text=" not in out

    # Forbidden content absent from server logs:
    for forbidden in ("Kevin", "516-555-1234", "Authorization", "Bearer",
                      "sk_live", "https://api.twilio.example", "Traceback"):
        assert forbidden not in out


def test_emergency_followup_logs_controlled_fields_only(db, fakes, capsys):
    """PATCH 6 correction pass: the emergency FOLLOW-UP intake turn (the
    only path that logged the actual lead name and phone) now emits a
    controlled event log — booleans + conversation UUID — while the
    emergency handoff behavior itself is unchanged."""
    client = make_client(db)
    conversation = make_conversation(db, client)  # name + phone already set

    # Deterministically arm the follow-up path: the previous assistant turn
    # was the emergency contact prompt (exact wording the production matcher
    # recognizes).
    db.add(Message(
        conversation_id=conversation.id,
        role="assistant",
        content="To help quickly, what's your first name?",
    ))
    db.commit()

    resp = send(db, client, conversation, "yes")
    out = capsys.readouterr().out

    # The follow-up handler ran and handed off (name + phone were present).
    assert resp.meta.get("mode") == "emergency_handoff"
    assert len(fakes.lead_sms) == 1 and len(fakes.lead_email) == 1

    # Controlled event log present:
    assert "[EMERGENCY_FOLLOWUP]" in out
    assert "emergency= True" in out
    assert "has_name= True" in out and "has_phone= True" in out
    assert str(conversation.id) in out

    # The actual lead values never appear in server output:
    assert "Kevin" not in out
    assert "516-555-1234" not in out


# ===========================================================================
# 12. EMERGENCY INTERRUPTION PATCH — life-threatening symptoms end the reply
#
# Staging regression: mid-intake (waiting for the phone number), the patient
# reported facial swelling + trouble breathing. Mia gave the correct 911/ER
# instruction but appended the emergency-contact phone question to the SAME
# message. Life-threatening symptoms (trouble breathing/swallowing,
# uncontrolled bleeding, rapidly worsening swelling) must now interrupt all
# intake with a standalone safety instruction — no question of any kind in
# that response. Dental emergencies WITHOUT a life-threatening symptom keep
# the existing contact prompt (distinct tier preserved).
# ===========================================================================

LIFE_THREATENING_MESSAGE = "My face is swelling and I'm having trouble breathing."

# Any of these fragments in a life-threatening reply means an intake or
# contact question leaked into the same message.
INTAKE_QUESTION_FRAGMENTS = [
    "first name",
    "your name",
    "phone number",
    "email",
    "day would work",
    "day/time works",
    "morning or afternoon",
    "new or returning",
    "what's going on",
    "what\u2019s going on",
]


def _assert_standalone_emergency_reply(resp):
    """The 911/ER instruction must be the entire patient-facing reply:
    no question mark, no intake-field or contact prompt, emergency meta
    intact. (The default emergency wording contains no '?', so any '?'
    proves an appended question.)"""
    assert "call 911" in resp.reply
    assert "?" not in resp.reply
    low = resp.reply.lower()
    for fragment in INTAKE_QUESTION_FRAGMENTS:
        assert fragment not in low, f"intake question leaked into emergency reply: {fragment!r}"
    assert resp.meta.get("emergency_mode") is True


# One override set per normal-intake stage: each seeds a conversation whose
# NEXT expected intake answer is that stage's field.
INTAKE_STAGE_OVERRIDES = {
    # Waiting for the appointment reason (nothing captured yet).
    "reason": dict(lead_reason=None, lead_name=None, lead_phone=None,
                   lead_time_window=None, lead_email_opt_out=False),
    # Waiting for the patient's name.
    "name": dict(lead_name=None, lead_phone=None,
                 lead_time_window=None, lead_email_opt_out=False),
    # Waiting for the phone number — the observed staging regression stage.
    "phone": dict(lead_phone=None, lead_time_window=None,
                  lead_email_opt_out=False),
    # Waiting for the email (opt-out not yet chosen).
    "email": dict(lead_time_window=None, lead_email_opt_out=False),
    # Waiting for the preferred day/time window.
    "time_window": dict(lead_time_window=None),
    # Waiting for new-vs-returning (make_conversation default shape).
    "patient_type": dict(),
}


@pytest.mark.parametrize("stage", sorted(INTAKE_STAGE_OVERRIDES))
def test_life_threatening_interrupts_every_intake_stage(db, fakes, stage):
    """At EVERY normal intake stage, a life-threatening message ends the
    reply at the safety instruction: no question, no appointment."""
    client = make_client(db, office_hours=OPEN_ALL_WEEK_HOURS)
    conversation = make_conversation(db, client, **INTAKE_STAGE_OVERRIDES[stage])

    resp = send(db, client, conversation, LIFE_THREATENING_MESSAGE)

    _assert_standalone_emergency_reply(resp)
    # An emergency turn must never create an appointment.
    assert (
            db.query(Appointment)
            .filter(Appointment.conversation_id == conversation.id)
            .count()
            == 0
        )


LIFE_THREATENING_VARIANTS = [
    "I'm having trouble breathing",
    "I can't swallow and my mouth hurts",
    "I have uncontrolled bleeding from my mouth",
    "I have rapidly worsening swelling in my face",
    # Correction pass: normalized uncontrolled-bleeding phrasings added to
    # BOTH trigger lists. Each message below normalizes to contain exactly
    # one of the six new phrases.
    "My mouth can't stop bleeding",
    "I cant stop bleeding from my gums",
    "I cannot stop bleeding after my extraction",
    "The bleeding won't stop",
    "my bleeding wont stop",
    "The bleeding will not stop",
]


@pytest.mark.parametrize("message", LIFE_THREATENING_VARIANTS)
def test_life_threatening_variants_get_standalone_reply(db, fakes, message):
    """Each of the four life-threatening categories (breathing, swallowing,
    uncontrolled bleeding, rapidly worsening swelling) suppresses the
    contact question regardless of which emergency guard handles it."""
    client = make_client(db)
    conversation = make_conversation(db, client, lead_phone=None,
                                     lead_time_window=None,
                                     lead_email_opt_out=False)

    resp = send(db, client, conversation, message)

    _assert_standalone_emergency_reply(resp)


def test_observed_phone_stage_regression_fixed(db, fakes):
    """The exact staging flow: the previous assistant turn was the normal
    intake phone question; the patient then reported facial swelling +
    trouble breathing. The reply must be the standalone 911 instruction —
    previously the emergency-contact phone question was appended."""
    client = make_client(db)
    conversation = make_conversation(db, client, lead_phone=None,
                                     lead_time_window=None,
                                     lead_email_opt_out=False)
    db.add(Message(
        conversation_id=conversation.id,
        role="assistant",
        content="Thanks \u2014 what\u2019s the best phone number to reach you?",
    ))
    db.commit()

    resp = send(db, client, conversation, LIFE_THREATENING_MESSAGE)

    assert resp.meta.get("mode") == "emergency_booking_mode"
    _assert_standalone_emergency_reply(resp)


def test_life_threatening_mid_booking_interrupts_without_question(db, fakes):
    """Mid-Calendar-dialog: the existing same-request cleanup (hold released,
    state reset) is preserved AND the reply now carries no question."""
    client = make_client(db, calendar_enabled=True)
    conversation = make_conversation(db, client, lead_status="completed")
    slot = seed_active_confirmation(db, client, conversation)

    resp = send(db, client, conversation, LIFE_THREATENING_MESSAGE)

    _assert_standalone_emergency_reply(resp)
    assert (conversation.booking_state or "none") == BookingState.NONE
    assert conversation.booking_selected_slot_id is None
    slot_row = refreshed_slot(db, slot.id)
    assert slot_row.status == SlotStatus.AVAILABLE
    assert slot_row.held_by_conversation_id is None
    assert (
            db.query(Appointment)
            .filter(Appointment.conversation_id == conversation.id)
            .count()
            == 0
        )


def test_dental_emergency_without_life_threat_keeps_contact_prompt(db, fakes):
    """Distinct-tier confirmation: 'severe pain' reaches the emergency
    routing tier, but with no life-threatening symptom the emergency
    contact question is still asked (behavior intentionally unchanged)."""
    client = make_client(db)
    conversation = make_conversation(db, client, lead_reason=None,
                                     lead_name=None, lead_phone=None,
                                     lead_time_window=None,
                                     lead_email_opt_out=False)

    resp = send(db, client, conversation, "I'm in severe pain, it's a dental emergency")

    assert resp.meta.get("emergency_mode") is True
    assert "call 911" in resp.reply
    # Name is missing, so the emergency contact chain asks for it.
    assert "first name" in resp.reply.lower()


def test_affirmative_after_standalone_emergency_still_offers_contact_capture(db, fakes):
    """Intentionally unchanged: on the NEXT turn, an explicit patient
    affirmative after the emergency instruction resumes the emergency
    contact capture. Only same-message resumption was removed."""
    client = make_client(db)
    conversation = make_conversation(db, client, lead_phone=None,
                                     lead_time_window=None,
                                     lead_email_opt_out=False)

    first = send(db, client, conversation, LIFE_THREATENING_MESSAGE)
    assert "?" not in first.reply  # standalone instruction, per this patch

    resp = send(db, client, conversation, "ok")

    assert resp.meta.get("mode") == "emergency_intake_continue"
    assert "phone number" in resp.reply.lower()
