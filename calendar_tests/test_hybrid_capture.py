# calendar_tests/test_hybrid_capture.py
#
# S9 REVISION 2 — hybrid pre-handoff capture, post-handoff ownership, and the
# bypass Other-reason classifier delegation.
#
# These tests drive the REAL chat() endpoint function against PostgreSQL,
# following the same conventions as test_chat_integration.py. Every network
# boundary is replaced with recording fakes, so no OpenAI, Twilio or Resend
# call can occur.
#
# REVISION 2 CHANGES
#   - S9-7 tests removed: the bare-string correction was reverted and
#     deferred (see S9_7_DEFERRAL.md in the review bundle).
#   - A real multi-turn sequential ordinary-hybrid flow was added.
#   - Post-handoff interruption tests now assert the EXACT existing owner
#     mode and reply contract, derived from the bundled baseline.
#   - The S9-5 section was rebuilt around the corrected reachability record:
#     natural real-flow tests prove what the current routing actually does,
#     and separately-labeled TARGETED CONSUMER-BRANCH COVERAGE tests force
#     ONLY the consumer-entry precondition while leaving the real
#     classifier, persistence, response construction and chat() body active.
#
# HONEST REACHABILITY RECORD for the bypass Other-reason consumer
# (corrects the revision-1 record; verified against the bundled baseline):
#   A message that selects Other ("other", "something else", "not sure",
#   "none of these", "none") sets service_reason_now == "other" and is
#   returned EARLIER by the other_service_prompt owner
#   (build_other_reason_prompt, mode "other_service_prompt"). Its follow-up
#   free text matches last_assistant_asked_for_other_reason() and is claimed
#   by the PRIMARY S7 Other-capture block. The legacy bypass consumer's
#   reason_detail sub-block is therefore statically unreachable under the
#   current detector graph; its three-way contract was completed as an
#   instructed defensive cleanup, and its branches are exercised below by
#   forcing only the entry precondition.

import uuid
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

# conftest.py prepared OPENAI_API_KEY / ADMIN_API_KEY / DATABASE_URL before
# any app import; importing app modules is safe from here on.
import app.routes.chat as chat_module
from app.calendar_models import AppointmentSlot, BookingState, SlotStatus
from app.models import Client, Conversation, Message
from app.schemas import ChatRequest
from app.services import notification_service

NY = ZoneInfo("America/New_York")

REMINDER_TEXT = "The online booking link is still available below."
OTHER_SERVICE_PROMPT = "No problem — please briefly tell me what you’re coming in for."

HYBRID_NAME_PROMPT = "Before I send you to online booking, what’s your first name?"
HYBRID_PHONE_PROMPT = (
    "Before I send you to online booking, what’s the best phone number to reach you?"
)
COMBINED_PROMPT = "Before I send you to online booking, what’s your name and phone number?"

# Exact post-handoff owner replies, derived from the bundled baseline for a
# client with no FAQ rows and no office_hours struct:
HOURS_NO_MATCH_REPLY = "Please call the office and our team can confirm our office hours."
LOCATION_NO_MATCH_REPLY = "Please call the office and our team can share our address and directions."
DEFAULT_OFFICE_PHONE = "(555) 123-4567"  # chat.py fallback when the client row has no office phone

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
# Builders — mirror test_chat_integration.py, plus explicit booking_mode.
# ---------------------------------------------------------------------------

def make_client(db, *, booking_mode=None, booking_url=None, calendar_enabled=None,
                notification_channels=True, office_hours=None):
    """One dental office. booking_mode is written to settings ONLY when given,
    so the "missing mode" default can be proven honestly. Fresh api_key per
    test keeps tests client-isolated (Rule 15)."""
    settings = {"timezone": "America/New_York"}
    if booking_mode is not None:
        settings["booking_mode"] = booking_mode
    if booking_url:
        settings["booking_url"] = booking_url
    if calendar_enabled is not None:
        settings["calendar"] = {
            "booking_enabled": bool(calendar_enabled),
            "hold_minutes": 5,
            "minimum_notice_minutes": 60,
            "max_offered_slots": 3,
            "max_booking_days": 30,
            "require_staff_confirmation": True,
        }
    client = Client(
        id=uuid.uuid4(),
        practice_name="Hybrid Test Dental",
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
    """A hybrid-capture conversation. Defaults deliberately hold NOTHING but a
    service reason, so the hybrid capture sequence starts from the top."""
    fields = dict(
        id=uuid.uuid4(),
        client_id=client.id,
        visitor_id="test-visitor",
        is_lead=False,
        lead_status="new",
        lead_reason="cleaning/checkup",
        lead_name=None,
        lead_phone=None,
        lead_email=None,
        lead_email_opt_out=False,
        lead_time_window=None,
        lead_is_new_patient=None,
    )
    fields.update(overrides)
    conversation = Conversation(**fields)
    db.add(conversation)
    db.commit()
    return conversation


def captured_conversation(db, client, **overrides):
    """Name + phone already captured — the shape that earns the handoff."""
    fields = dict(lead_name="Kevin", lead_phone="516-555-1234", is_lead=True)
    fields.update(overrides)
    return make_conversation(db, client, **fields)


def post_handoff_conversation(db, client, **overrides):
    """A hybrid conversation that already received the external link."""
    fields = dict(booking_link_sent=True)
    fields.update(overrides)
    return captured_conversation(db, client, **fields)


def seed_assistant_message(db, conversation, text):
    """Give the conversation a last-assistant message, which several owners
    read (last_assistant_text / last_assistant_asked_* detectors)."""
    db.add(Message(conversation_id=conversation.id, role="assistant", content=text))
    db.commit()


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


def snapshot_lead(conversation):
    """Every captured lead field, for no-mutation assertions."""
    return (
        conversation.lead_reason,
        conversation.lead_name,
        conversation.lead_phone,
        conversation.lead_email,
        conversation.lead_email_opt_out,
        conversation.lead_time_window,
        conversation.lead_is_new_patient,
        conversation.lead_status,
        conversation.booking_link_sent,
    )


# ===========================================================================
# 1. Booking-mode resolution
# ===========================================================================

@pytest.mark.parametrize("configured,expected", [
    ("direct", "direct"),
    ("capture_first", "capture_first"),
    ("hybrid", "hybrid"),
    ("zocdoc_mode", "hybrid"),   # invalid falls back
    ("", "hybrid"),              # blank falls back
])
def test_booking_mode_resolves(db, configured, expected):
    client = make_client(db, booking_mode=configured)
    assert chat_module.get_booking_mode(client) == expected


def test_missing_booking_mode_defaults_to_hybrid(db):
    client = make_client(db)
    assert "booking_mode" not in (client.settings or {})
    assert chat_module.get_booking_mode(client) == "hybrid"


# ===========================================================================
# 2. Direct-mode preservation
# ===========================================================================

def test_direct_mode_sends_link_without_capture(db, fakes):
    client = make_client(db, booking_mode="direct", booking_url="https://book.example.com")
    conversation = make_conversation(db, client)  # no name, no phone

    resp = send(db, client, conversation, "I want to book an appointment")

    assert resp.meta.get("mode") == "external_booking_handoff"
    assert conversation.booking_link_sent is True
    assert HYBRID_NAME_PROMPT not in resp.reply
    assert HYBRID_PHONE_PROMPT not in resp.reply
    assert conversation.lead_name is None
    assert conversation.lead_phone is None


def test_direct_mode_repeat_scheduling_keeps_reminder(db, fakes):
    client = make_client(db, booking_mode="direct", booking_url="https://book.example.com")
    conversation = make_conversation(db, client, booking_link_sent=True)

    resp = send(db, client, conversation, "I want to book an appointment")

    assert resp.meta.get("mode") == "external_booking_link_reminder"
    assert resp.reply == REMINDER_TEXT


# ===========================================================================
# 3. capture_first preservation
# ===========================================================================

def test_capture_first_keeps_its_own_prompt(db, fakes):
    """capture_first must NOT take the hybrid name-only branch. A high-value
    reason with nothing captured still receives the pre-existing combined
    prompt, which hybrid never uses."""
    client = make_client(db, booking_mode="capture_first",
                         booking_url="https://book.example.com")
    conversation = make_conversation(db, client, lead_reason="crown")

    resp = send(db, client, conversation, "I want to book an appointment")

    assert resp.meta.get("mode") == "booking_capture_first"
    assert resp.reply == COMBINED_PROMPT
    assert resp.reply != HYBRID_NAME_PROMPT


def test_capture_first_does_not_hand_off_prematurely(db, fakes):
    client = make_client(db, booking_mode="capture_first",
                         booking_url="https://book.example.com")
    conversation = make_conversation(db, client, lead_reason="crown")

    resp = send(db, client, conversation, "I want to book an appointment")

    assert resp.meta.get("mode") != "external_booking_handoff"
    assert conversation.booking_link_sent is False


# ===========================================================================
# 4. Ordinary hybrid capture — single-turn owner checks
# ===========================================================================

def test_hybrid_missing_name_asks_only_first_name(db, fakes):
    client = make_client(db, booking_mode="hybrid", booking_url="https://book.example.com")
    conversation = make_conversation(db, client)

    resp = send(db, client, conversation, "I want to book an appointment")

    assert resp.meta.get("mode") == "booking_capture_first"
    assert resp.reply == HYBRID_NAME_PROMPT
    assert resp.reply.count("?") == 1
    assert COMBINED_PROMPT not in resp.reply
    assert conversation.booking_link_sent is False


def test_hybrid_with_name_asks_only_phone(db, fakes):
    client = make_client(db, booking_mode="hybrid", booking_url="https://book.example.com")
    conversation = make_conversation(db, client, lead_name="Kevin")

    resp = send(db, client, conversation, "I want to book an appointment")

    assert resp.meta.get("mode") == "booking_capture_first"
    assert resp.reply == HYBRID_PHONE_PROMPT
    assert resp.reply.count("?") == 1
    assert COMBINED_PROMPT not in resp.reply


def test_hybrid_with_name_and_phone_hands_off(db, fakes):
    client = make_client(db, booking_mode="hybrid", booking_url="https://book.example.com")
    conversation = captured_conversation(db, client)

    resp = send(db, client, conversation, "I want to book an appointment")

    assert resp.meta.get("mode") == "external_booking_handoff"
    assert conversation.booking_link_sent is True
    lowered = resp.reply.lower()
    assert "email" not in lowered
    assert "day/time window" not in lowered
    assert "new or returning" not in lowered
    assert conversation.lead_email is None
    assert conversation.lead_time_window is None
    assert conversation.lead_is_new_patient is None


@pytest.mark.parametrize("reason", ["other", "appointment request", "some-unmapped-reason"])
def test_hybrid_generic_or_unmapped_reason_still_captures(db, fakes, reason):
    """Decision D-1: the unconditional hybrid policy captures for generic,
    Other and unmapped reasons instead of releasing the link."""
    client = make_client(db, booking_mode="hybrid", booking_url="https://book.example.com")
    conversation = make_conversation(db, client, lead_reason=reason)

    resp = send(db, client, conversation, "I want to book an appointment")

    assert resp.meta.get("mode") == "booking_capture_first"
    assert resp.reply == HYBRID_NAME_PROMPT
    assert conversation.booking_link_sent is False


def test_hybrid_capture_policy_is_unconditional(db):
    """Decision D-1 at the owner: hybrid always captures, whatever the
    reason, and the removed is_after_hours conditional cannot resurface."""
    client = make_client(db, booking_mode="hybrid", booking_url="https://book.example.com")
    conversation = make_conversation(db, client)
    for reason in [None, "other", "cleaning/checkup", "crown", "unmapped"]:
        assert chat_module.should_capture_before_booking_link(
            client=client, conversation=conversation,
            user_text="I want to book", service_reason=reason,
        ) is True


def test_direct_and_capture_first_policies_unchanged(db):
    direct = make_client(db, booking_mode="direct", booking_url="https://book.example.com")
    capture = make_client(db, booking_mode="capture_first", booking_url="https://book.example.com")
    conv = make_conversation(db, direct)
    assert chat_module.should_capture_before_booking_link(
        client=direct, conversation=conv, user_text="book", service_reason="crown") is False
    assert chat_module.should_capture_before_booking_link(
        client=capture, conversation=conv, user_text="book", service_reason="crown") is True


# ===========================================================================
# 5. Ordinary hybrid capture — REAL SEQUENTIAL MULTI-TURN FLOW (revision 2)
# ===========================================================================

def test_hybrid_sequential_real_flow(db, fakes):
    """One conversation, four real chat() turns, no state pre-seeding beyond
    the initial service reason:

      scheduling request -> exact name prompt -> name persists + exact phone
      prompt -> phone persists + external handoff, with the never-asked
      fields, notification silence, and native-booking silence all proven on
      the same conversation row."""
    client = make_client(db, booking_mode="hybrid", booking_url="https://book.example.com",
                         calendar_enabled=True)
    conversation = make_conversation(db, client)  # reason only; no name/phone

    # Turn 1 — scheduling request: exactly the first-name prompt, once.
    turn1 = send(db, client, conversation, "I want to book an appointment")
    assert turn1.meta.get("mode") == "booking_capture_first"
    assert turn1.reply == HYBRID_NAME_PROMPT
    assert turn1.reply.count("?") == 1
    assert (conversation.lead_name or "") == ""
    assert conversation.booking_link_sent is False

    # Turn 2 — the name: persisted, then exactly the phone prompt, once.
    turn2 = send(db, client, conversation, "Kevin")
    assert conversation.lead_name == "Kevin"
    assert turn2.meta.get("mode") == "booking_capture_first"
    assert turn2.reply == HYBRID_PHONE_PROMPT
    assert turn2.reply.count("?") == 1
    assert conversation.booking_link_sent is False

    # Turn 3 — the phone: persisted, then the external handoff.
    turn3 = send(db, client, conversation, "516-555-1234")
    assert (conversation.lead_phone or "").strip() != ""
    assert turn3.meta.get("mode") == "external_booking_handoff"
    assert turn3.meta.get("booking_url") == "https://book.example.com"
    assert conversation.booking_link_sent is True

    # Never requested and never populated: email, time window, new/returning.
    all_replies = " ".join([turn1.reply, turn2.reply, turn3.reply]).lower()
    assert "email" not in all_replies
    assert "day/time window" not in all_replies
    assert "new or returning" not in all_replies
    assert conversation.lead_email is None
    assert conversation.lead_time_window is None
    assert conversation.lead_is_new_patient is None

    # No office or booking notification was introduced (decision D-3).
    assert fakes.lead_sms == [] and fakes.lead_email == []
    assert fakes.booking_sms == [] and fakes.booking_email == []

    # No native booking state or hold was created.
    assert (conversation.booking_state or "none") == BookingState.NONE
    assert conversation.booking_selected_slot_id is None
    held = (
        db.query(AppointmentSlot)
        .filter(AppointmentSlot.client_id == client.id,
                AppointmentSlot.status == SlotStatus.HELD)
        .count()
    )
    assert held == 0


# ===========================================================================
# 6. Priority / ASAP hybrid preservation — S3 condition
# ===========================================================================

def test_priority_hybrid_missing_name_asks_name(db, fakes):
    client = make_client(db, booking_mode="hybrid", booking_url="https://book.example.com")
    conversation = make_conversation(db, client, lead_is_priority=True)

    resp = send(db, client, conversation, "I want to book an appointment")

    assert resp.meta.get("mode") == "booking_capture_first"
    assert "first name" in resp.reply.lower()
    assert resp.reply != HYBRID_NAME_PROMPT
    assert conversation.booking_link_sent is False


def test_priority_hybrid_with_name_asks_phone(db, fakes):
    client = make_client(db, booking_mode="hybrid", booking_url="https://book.example.com")
    conversation = make_conversation(db, client, lead_is_priority=True, lead_name="Kevin")

    resp = send(db, client, conversation, "I want to book an appointment")

    assert "for the office to call you back" in resp.reply
    assert conversation.booking_link_sent is False


def test_priority_hybrid_still_requires_email_or_skip(db, fakes):
    client = make_client(db, booking_mode="hybrid", booking_url="https://book.example.com")
    conversation = captured_conversation(db, client, lead_is_priority=True)

    resp = send(db, client, conversation, "I want to book an appointment")

    assert "email" in resp.reply.lower()
    assert resp.meta.get("mode") != "external_booking_handoff"
    assert conversation.booking_link_sent is False


def test_priority_hybrid_still_requires_time_window(db, fakes):
    client = make_client(db, booking_mode="hybrid", booking_url="https://book.example.com")
    conversation = captured_conversation(
        db, client, lead_is_priority=True, lead_email_opt_out=True)

    resp = send(db, client, conversation, "I want to book an appointment")

    assert "day/time window" in resp.reply.lower()
    assert conversation.booking_link_sent is False


def test_priority_hybrid_still_requires_new_or_returning(db, fakes):
    client = make_client(db, booking_mode="hybrid", booking_url="https://book.example.com",
                         office_hours=OPEN_ALL_WEEK_HOURS)
    conversation = captured_conversation(
        db, client, lead_is_priority=True, lead_email_opt_out=True,
        lead_time_window="Tuesday morning")

    resp = send(db, client, conversation, "I want to book an appointment")

    assert "new or returning" in resp.reply.lower()
    assert conversation.booking_link_sent is False


def test_priority_hybrid_hands_off_only_when_s3_complete(db, fakes):
    client = make_client(db, booking_mode="hybrid", booking_url="https://book.example.com",
                         office_hours=OPEN_ALL_WEEK_HOURS)
    conversation = captured_conversation(
        db, client, lead_is_priority=True, lead_email_opt_out=True,
        lead_time_window="Tuesday morning", lead_is_new_patient=True)

    assert chat_module.priority_intake_is_complete(conversation) is True
    resp = send(db, client, conversation, "I want to book an appointment")

    assert resp.meta.get("mode") == "external_booking_handoff"
    assert conversation.booking_link_sent is True


def test_ordinary_vs_priority_split_owner(db):
    """The single split owner, including the ASAP time-window form, so the
    two prompt owners can never disagree."""
    client = make_client(db, booking_mode="hybrid", booking_url="https://book.example.com")
    ordinary = make_conversation(db, client)
    assert chat_module.conversation_is_ordinary_hybrid_lead(ordinary) is True
    for overrides in [
        dict(lead_is_priority=True),
        dict(lead_time_window="ASAP"),
        dict(lead_time_window="ASAP / tomorrow ok"),
        dict(lead_is_emergency=True),
    ]:
        conv = make_conversation(db, client, **overrides)
        assert chat_module.conversation_is_ordinary_hybrid_lead(conv) is False


# ===========================================================================
# 7. External-vs-native precedence
# ===========================================================================

def test_external_wins_when_both_configured(db, fakes):
    client = make_client(db, booking_mode="hybrid", booking_url="https://book.example.com",
                         calendar_enabled=True)
    conversation = captured_conversation(db, client)

    resp = send(db, client, conversation, "I want to book an appointment")

    assert resp.meta.get("mode") == "external_booking_handoff"
    assert (conversation.booking_state or "none") == BookingState.NONE
    assert conversation.booking_selected_slot_id is None
    held = (
        db.query(AppointmentSlot)
        .filter(AppointmentSlot.client_id == client.id,
                AppointmentSlot.status == SlotStatus.HELD)
        .count()
    )
    assert held == 0


def test_route_completed_lead_precedence_unchanged(db, fakes):
    client = make_client(db, booking_mode="hybrid", booking_url="https://book.example.com",
                         calendar_enabled=True)
    conversation = captured_conversation(db, client)

    routed = chat_module.route_completed_lead(
        db, client, conversation, "I want to book an appointment", DEFAULT_OFFICE_PHONE)

    assert routed is not None
    _reply, meta = routed
    assert meta.get("mode") in {"external_booking_handoff", "external_booking_link_reminder"}


# ===========================================================================
# 8. Post-handoff scheduling — decision D-5
# ===========================================================================

def test_post_handoff_scheduling_returns_reminder(db, fakes):
    client = make_client(db, booking_mode="hybrid", booking_url="https://book.example.com")
    conversation = post_handoff_conversation(db, client)
    before = snapshot_lead(conversation)

    resp = send(db, client, conversation, "I want to book an appointment")

    assert resp.meta.get("mode") == "external_booking_link_reminder"
    assert resp.reply == REMINDER_TEXT
    assert resp.meta.get("open_booking_in_new_tab") is True
    assert resp.meta.get("booking_url")
    assert snapshot_lead(conversation) == before
    assert (conversation.booking_state or "none") == BookingState.NONE


def test_post_handoff_scheduling_is_repeatable(db, fakes):
    client = make_client(db, booking_mode="hybrid", booking_url="https://book.example.com")
    conversation = post_handoff_conversation(db, client)

    first = send(db, client, conversation, "can I book online?")
    second = send(db, client, conversation, "book appointment")

    assert first.meta.get("mode") == "external_booking_link_reminder"
    assert second.meta.get("mode") == "external_booking_link_reminder"


def test_post_handoff_service_selection_uses_external_owner_exactly(db, fakes):
    """REVISION 2 strengthening: a recognized service after handoff must
    follow the existing external scheduling owner EXACTLY — the repeatable
    reminder — never the hybrid residue guard."""
    client = make_client(db, booking_mode="hybrid", booking_url="https://book.example.com")
    conversation = post_handoff_conversation(db, client)
    before = snapshot_lead(conversation)

    resp = send(db, client, conversation, "crown")

    assert resp.meta.get("mode") == "external_booking_link_reminder"
    assert resp.reply == REMINDER_TEXT
    assert resp.meta.get("open_booking_in_new_tab") is True
    assert "new or returning" not in resp.reply.lower()
    assert "first name" not in resp.reply.lower()
    assert snapshot_lead(conversation) == before
    assert (conversation.booking_state or "none") == BookingState.NONE


# ===========================================================================
# 9. Post-handoff non-scheduling residue
# ===========================================================================

RESIDUE_MESSAGES = [
    "I don't see any times",
    "the link isn't working",
    "please have the office call me",
]


@pytest.mark.parametrize("text", RESIDUE_MESSAGES)
def test_post_handoff_residue_returns_followup(db, fakes, text):
    """The residue guard answers with the exact ported reply, repeats no
    link, reopens no intake, mutates no field and retriggers no
    notification."""
    client = make_client(db, booking_mode="hybrid", booking_url="https://book.example.com")
    conversation = post_handoff_conversation(db, client)
    before = snapshot_lead(conversation)

    resp = send(db, client, conversation, text)

    assert resp.meta.get("mode") == "hybrid_post_handoff_followup"
    assert resp.reply == chat_module.build_hybrid_post_handoff_reply(
        conversation, DEFAULT_OFFICE_PHONE)
    assert resp.meta.get("booking_url") is None
    assert resp.meta.get("show_booking_button") is None
    assert REMINDER_TEXT not in resp.reply
    assert "?" not in resp.reply
    assert snapshot_lead(conversation) == before
    assert fakes.lead_sms == [] and fakes.lead_email == []
    assert fakes.booking_sms == [] and fakes.booking_email == []
    assert (conversation.booking_state or "none") == BookingState.NONE
    assert conversation.final_closed is False


def test_hybrid_post_handoff_predicate_owner(db):
    """The predicate is read-only and false outside the hybrid post-handoff
    state (wrong mode, no URL, no link, missing capture, completed, closed)."""
    client = make_client(db, booking_mode="hybrid", booking_url="https://book.example.com")
    conv = post_handoff_conversation(db, client)
    assert chat_module.conversation_is_hybrid_post_handoff(client, conv) is True

    direct = make_client(db, booking_mode="direct", booking_url="https://book.example.com")
    assert chat_module.conversation_is_hybrid_post_handoff(direct, conv) is False

    no_url = make_client(db, booking_mode="hybrid")
    assert chat_module.conversation_is_hybrid_post_handoff(no_url, conv) is False

    for overrides in [
        dict(booking_link_sent=False),
        dict(lead_name=None),
        dict(lead_phone=None),
        dict(lead_status="completed"),
        dict(final_closed=True),
    ]:
        other = post_handoff_conversation(db, client, **overrides)
        assert chat_module.conversation_is_hybrid_post_handoff(client, other) is False


def test_hybrid_post_handoff_reply_never_contains_a_link(db):
    client = make_client(db, booking_mode="hybrid", booking_url="https://book.example.com")
    conv = post_handoff_conversation(db, client)

    reply = chat_module.build_hybrid_post_handoff_reply(conv, DEFAULT_OFFICE_PHONE)

    assert "Kevin" in reply
    assert "https://" not in reply
    assert "?" not in reply
    assert DEFAULT_OFFICE_PHONE in reply


# ===========================================================================
# 10. Post-handoff interruption preservation — EXACT owner modes (revision 2)
#
# Exact modes and replies derived from the bundled baseline for a client
# with no FAQ rows and no office_hours struct.
# ===========================================================================

def test_post_handoff_operational_hours_exact_owner(db, fakes):
    """Hours question: the operational-FAQ owner answers with its exact
    no-match reply — the ENTIRE reply (S8 contract) — and no resume."""
    client = make_client(db, booking_mode="hybrid", booking_url="https://book.example.com")
    conversation = post_handoff_conversation(db, client)
    before = snapshot_lead(conversation)

    resp = send(db, client, conversation, "what are your hours?")

    assert resp.meta.get("mode") == "faq_operational_no_match"
    assert resp.meta.get("faq_match") is False
    assert resp.reply == HOURS_NO_MATCH_REPLY
    assert "new or returning" not in resp.reply.lower()
    assert snapshot_lead(conversation) == before


def test_post_handoff_insurance_exact_owner(db, fakes):
    """Insurance question: the pre-existing insurance guard owns it with its
    exact builder reply and mode."""
    client = make_client(db, booking_mode="hybrid", booking_url="https://book.example.com")
    conversation = post_handoff_conversation(db, client)
    text = "do you take Delta Dental insurance?"

    resp = send(db, client, conversation, text)

    assert resp.meta.get("mode") == "insurance_info"
    assert resp.reply == chat_module.build_insurance_reply(text)
    assert "new or returning" not in resp.reply.lower()


def test_post_handoff_office_phone_exact_owner(db, fakes):
    """Office-phone request: the office_phone owner answers with its exact
    builder reply — S8's booking_link_sent suppression means no resumed
    intake question is appended."""
    client = make_client(db, booking_mode="hybrid", booking_url="https://book.example.com")
    conversation = post_handoff_conversation(db, client)

    resp = send(db, client, conversation, "what is your phone number?")

    assert resp.meta.get("mode") == "office_phone"
    assert resp.reply == chat_module.build_office_phone_reply(
        client, conversation, DEFAULT_OFFICE_PHONE)


def test_post_handoff_location_exact_owner(db, fakes):
    """Location request: the operational-FAQ owner (single location owner via
    looks_like_location_request) answers with its exact no-match reply."""
    client = make_client(db, booking_mode="hybrid", booking_url="https://book.example.com")
    conversation = post_handoff_conversation(db, client)

    resp = send(db, client, conversation, "where are you located?")

    assert resp.meta.get("mode") == "faq_operational_no_match"
    assert resp.reply == LOCATION_NO_MATCH_REPLY


def test_post_handoff_emergency_exact_owner(db, fakes):
    """Dental emergency: the main emergency routing owner answers with its
    exact mode; the residue guard never sees it."""
    client = make_client(db, booking_mode="hybrid", booking_url="https://book.example.com")
    conversation = post_handoff_conversation(db, client)

    resp = send(db, client, conversation, "my tooth just got knocked out")

    assert resp.meta.get("mode") == "emergency_booking_mode"
    assert resp.meta.get("emergency_mode") is True
    assert conversation.final_closed is False


def test_post_handoff_life_threatening_exact_owner_and_final_closed(db, fakes):
    """Life-threatening emergency: same emergency owner, S4 persistence —
    final_closed set, the 911 instruction stands alone (no question)."""
    client = make_client(db, booking_mode="hybrid", booking_url="https://book.example.com")
    conversation = post_handoff_conversation(db, client)

    resp = send(db, client, conversation, "I'm having trouble breathing")

    assert resp.meta.get("mode") == "emergency_booking_mode"
    assert conversation.final_closed is True
    assert "911" in resp.reply
    assert "?" not in resp.reply


def test_post_handoff_ending_exact_owner(db, fakes):
    """Genuine ending: the conversation-ending owner answers with its exact
    mode; the residue guard never sees it."""
    client = make_client(db, booking_mode="hybrid", booking_url="https://book.example.com")
    conversation = post_handoff_conversation(db, client)

    resp = send(db, client, conversation, "no thanks, that's all")

    assert resp.meta.get("mode") == "conversation_ending"


def test_completed_conversation_is_not_reopened(db, fakes):
    """A completed lead keeps its existing dedicated handling — the
    post-handoff predicate is False and the residue guard stays silent."""
    client = make_client(db, booking_mode="hybrid", booking_url="https://book.example.com")
    conversation = post_handoff_conversation(db, client, lead_status="completed")

    assert chat_module.conversation_is_hybrid_post_handoff(client, conversation) is False
    resp = send(db, client, conversation, "I don't see any times")
    assert resp.meta.get("mode") != "hybrid_post_handoff_followup"


# ===========================================================================
# 11. Settings transitions
# ===========================================================================

def test_url_removed_before_handoff_prevents_external(db, fakes):
    client = make_client(db, booking_mode="hybrid")  # no booking_url
    conversation = captured_conversation(db, client)

    resp = send(db, client, conversation, "I want to book an appointment")

    assert resp.meta.get("mode") not in {
        "external_booking_handoff", "external_booking_link_reminder", "booking_capture_first"}
    assert conversation.booking_link_sent is False


def test_url_removed_after_handoff_disables_post_handoff_guard(db, fakes):
    client = make_client(db, booking_mode="hybrid")  # URL removed
    conversation = post_handoff_conversation(db, client)

    assert chat_module.conversation_is_hybrid_post_handoff(client, conversation) is False
    resp = send(db, client, conversation, "I don't see any times")
    assert resp.meta.get("mode") != "hybrid_post_handoff_followup"


def test_url_added_during_internal_dialog_uses_transition_owner(db, fakes):
    """The pre-existing Patch 3 ownership-transition hook still cancels the
    internal dialog and hands off — S9 added no second owner."""
    client = make_client(db, booking_mode="hybrid", booking_url="https://book.example.com",
                         calendar_enabled=True)
    conversation = captured_conversation(db, client)
    conversation.booking_state = BookingState.WAITING_FOR_SLOT_SELECTION
    db.add(conversation)
    db.commit()

    resp = send(db, client, conversation, "2")

    assert resp.meta.get("mode") in {
        "external_booking_handoff", "external_booking_link_reminder"}
    assert (conversation.booking_state or "none") == BookingState.NONE


def test_booking_mode_change_while_incomplete_uses_current_settings(db, fakes):
    client = make_client(db, booking_mode="hybrid", booking_url="https://book.example.com")
    conversation = make_conversation(db, client)

    first = send(db, client, conversation, "I want to book an appointment")
    assert first.reply == HYBRID_NAME_PROMPT

    client.settings = dict(client.settings or {}, booking_mode="direct")
    db.add(client)
    db.commit()

    second = send(db, client, conversation, "I want to book an appointment")
    assert second.meta.get("mode") == "external_booking_handoff"


def test_booking_mode_change_after_handoff_creates_no_second_owner(db, fakes):
    client = make_client(db, booking_mode="hybrid", booking_url="https://book.example.com",
                         calendar_enabled=True)
    conversation = post_handoff_conversation(db, client)

    client.settings = dict(client.settings or {}, booking_mode="direct")
    db.add(client)
    db.commit()

    resp = send(db, client, conversation, "I want to book an appointment")

    assert resp.meta.get("mode") == "external_booking_link_reminder"
    assert (conversation.booking_state or "none") == BookingState.NONE


# ===========================================================================
# 12. Other-reason routing — NATURAL REAL FLOWS (revision 2)
#
# These prove what the current public routing actually does, per the
# corrected reachability record at the top of this file.
# ===========================================================================

def test_natural_other_selection_gets_other_service_prompt(db, fakes):
    """Selecting Other is claimed by the other_service_prompt owner — this,
    not the legacy bypass consumer, is the natural first turn."""
    client = make_client(db, booking_mode="hybrid")
    conversation = make_conversation(db, client, lead_reason=None)

    resp = send(db, client, conversation, "other")

    assert resp.meta.get("mode") == "other_service_prompt"
    assert resp.reply == OTHER_SERVICE_PROMPT
    assert (conversation.lead_reason or "") == ""


def test_natural_non_dental_after_other_stays_pending_then_retry_succeeds(db, fakes):
    """The natural three-turn flow: Other -> non-dental free text (exact
    rejection wording, S7 mode, nothing persisted, step pending) -> a valid
    dental retry accepted by the existing S7 owner."""
    client = make_client(db, booking_mode="hybrid")
    conversation = make_conversation(db, client, lead_reason=None)

    turn1 = send(db, client, conversation, "other")
    assert turn1.meta.get("mode") == "other_service_prompt"

    turn2 = send(db, client, conversation, "my knee hurts")
    assert turn2.meta.get("mode") == "non_dental_other_reason_detail"
    assert turn2.reply == chat_module.build_non_dental_reason_detail_reply()
    assert (conversation.lead_reason or "") == ""
    assert (conversation.lead_reason_source_text or "") == ""

    # Reason step remained pending: a valid dental retry is accepted by the
    # existing primary S7 owner.
    turn3 = send(db, client, conversation, "I need a root canal")
    assert turn3.meta.get("mode") == "other_reason_detail_captured"
    assert (conversation.lead_reason or "").strip() != ""
    assert conversation.lead_reason_source_text == "I need a root canal"


def test_natural_unclear_after_other_stays_pending(db, fakes):
    """Natural unclear flow: negated dental wording gets the exact S7
    clarification and persists nothing."""
    client = make_client(db, booking_mode="hybrid")
    conversation = make_conversation(db, client, lead_reason=None)

    send(db, client, conversation, "other")
    resp = send(db, client, conversation, "not a root canal")

    assert resp.meta.get("mode") == "unclear_other_reason_detail"
    assert resp.reply == chat_module.build_unclear_dental_reason_reply()
    assert (conversation.lead_reason or "") == ""


def test_primary_s7_other_block_unchanged(db, fakes):
    """The primary S7 Other-capture site still owns its pending-retry turns
    with its exact modes."""
    client = make_client(db, booking_mode="hybrid")
    conversation = make_conversation(db, client, lead_reason=None)
    seed_assistant_message(
        db, conversation, "Please briefly describe the dental reason for your visit.")

    resp = send(db, client, conversation, "my knee hurts")

    assert resp.meta.get("mode") == "non_dental_other_reason_detail"
    assert (conversation.lead_reason or "") == ""


# ===========================================================================
# 13. Bypass Other-reason consumer — TARGETED CONSUMER-BRANCH COVERAGE
#
# NOT NATURAL PUBLIC ROUTING. Per the corrected reachability record, the
# legacy bypass consumer's reason_detail sub-block is statically unreachable
# under the current detector graph. These tests force ONLY the
# consumer-entry precondition: the first receptionist_bypass_reply() call of
# the request is replaced so it yields the "reason_detail" stage (the value
# the dead seed branch would have produced), and every later call delegates
# to the REAL function. The real classifier, real persistence, real reply
# builders, real response construction and the real chat() body all stay
# active. Fixture texts are chosen so no upstream extractor consumes them
# (detect_appointment_reason maps none of them), keeping the entry
# precondition (empty lead_reason) genuinely satisfied rather than forced.
# ===========================================================================

BYPASS_ENTRY_PROMPT = "Got it — can you briefly tell me what you need help with?"


@pytest.fixture
def force_bypass_reason_detail(monkeypatch):
    """Entry forcer: first receptionist_bypass_reply() call of the request
    returns the reason_detail stage; subsequent calls (the consumer's own
    re-entry) hit the real S3 owner."""
    real = chat_module.receptionist_bypass_reply
    state = {"first": True}

    def entry_forcer(conversation, client=None):
        if state["first"]:
            state["first"] = False
            return ("(forced consumer entry)", "reason_detail")
        return real(conversation, client)

    monkeypatch.setattr(chat_module, "receptionist_bypass_reply", entry_forcer)
    return state


def _bypass_consumer_conversation(db, client):
    """Empty reason; lead_email_opt_out=True is real DB state that keeps
    in_intake_mode active without touching name/phone/reason; the seeded
    last-assistant message satisfies the consumer's own text precondition."""
    conversation = make_conversation(
        db, client, lead_reason=None, lead_email_opt_out=True)
    seed_assistant_message(db, conversation, BYPASS_ENTRY_PROMPT)
    return conversation


def test_bypass_consumer_dental_non_enum_branch(db, fakes, force_bypass_reason_detail):
    """TARGETED CONSUMER-BRANCH COVERAGE — dental verdict, no enum maps:
    persists lead_reason == "appointment request", persists the exact
    accepted detail, advances through the consumer's existing intake owner,
    and uses no second classifier."""
    client = make_client(db, booking_mode="hybrid")
    conversation = _bypass_consumer_conversation(db, client)
    text = "There is a metallic taste in my mouth"
    # Fixture sanity, executed here rather than assumed: the real classifier
    # accepts it, the mapper's own generic fallback (not a service enum)
    # answers it, and no upstream extractor consumes it.
    assert chat_module.classify_other_reason_detail(text) == "dental"
    assert chat_module.map_reason_detail_to_enum(text) == "appointment request"
    assert chat_module.detect_appointment_reason(text) is None

    resp = send(db, client, conversation, text)

    assert conversation.lead_reason == "appointment request"
    assert conversation.lead_reason_source_text == text
    # Advanced through the real re-entered intake owner: the next required
    # field (the name) is asked, not the reason again.
    assert "first name" in resp.reply.lower()
    assert resp.meta.get("mode") not in {
        "unclear_other_reason_detail", "non_dental_other_reason_detail"}
    # No second classifier exists.
    assert not hasattr(chat_module, "looks_like_dental_reason_detail")


def test_bypass_consumer_dental_enum_branch(db, fakes, force_bypass_reason_detail):
    """TARGETED CONSUMER-BRANCH COVERAGE — dental verdict with a real
    mapped enum: persists the enum through the existing lead_reason
    contract and advances. "my teeth are crooked" maps to "orthodontics"
    inside map_reason_detail_to_enum only — detect_appointment_reason maps
    nothing for it, so no upstream extractor consumes it and the entry
    precondition stays genuine."""
    client = make_client(db, booking_mode="hybrid")
    conversation = _bypass_consumer_conversation(db, client)
    text = "my teeth are crooked"
    assert chat_module.classify_other_reason_detail(text) == "dental"
    assert chat_module.map_reason_detail_to_enum(text) == "orthodontics"
    assert chat_module.detect_appointment_reason(text) is None

    resp = send(db, client, conversation, text)

    assert conversation.lead_reason == "orthodontics"
    assert conversation.lead_reason_source_text == text
    assert resp.meta.get("mode") not in {
        "unclear_other_reason_detail", "non_dental_other_reason_detail"}


def test_bypass_consumer_unclear_branch(db, fakes, force_bypass_reason_detail):
    """TARGETED CONSUMER-BRANCH COVERAGE — unclear verdict: exact
    clarification wording, exact mode, no persistence, reason pending.
    "no gum irritation" is negated dental wording built from D2 deviation
    vocabulary, which detect_appointment_reason does not map."""
    client = make_client(db, booking_mode="hybrid")
    conversation = _bypass_consumer_conversation(db, client)
    text = "no gum irritation"
    assert chat_module.classify_other_reason_detail(text) == "unclear"
    assert chat_module.detect_appointment_reason(text) is None

    resp = send(db, client, conversation, text)

    assert resp.meta.get("mode") == "unclear_other_reason_detail"
    assert resp.reply == chat_module.build_unclear_dental_reason_reply()
    assert (conversation.lead_reason or "") == ""
    assert (conversation.lead_reason_source_text or "") == ""


def test_bypass_consumer_non_dental_branch(db, fakes, force_bypass_reason_detail):
    """TARGETED CONSUMER-BRANCH COVERAGE — non_dental verdict: exact
    rejection wording, exact mode (never flattened to "bypass"), no
    persistence, reason pending."""
    client = make_client(db, booking_mode="hybrid")
    conversation = _bypass_consumer_conversation(db, client)
    text = "pizza delivery"
    assert chat_module.classify_other_reason_detail(text) == "non_dental"
    assert chat_module.detect_appointment_reason(text) is None

    resp = send(db, client, conversation, text)

    assert resp.meta.get("mode") == "non_dental_other_reason_detail"
    assert resp.reply == chat_module.build_non_dental_reason_detail_reply()
    assert (conversation.lead_reason or "") == ""
    assert (conversation.lead_reason_source_text or "") == ""


D1_D5_ACCEPTED = [
    "I have a sore spot in my mouth",
    "sore spot since my last visit",
    "I have gum irritation",
    "There is a metallic taste in my mouth",
    "my tooth hurts and I want to book a visit",
]

D1_D5_GUARDRAILS = [
    "sore spot on my arm",
    "knee irritation",
    "skin irritation",
    "metallic taste in music",
    "book club last week",
    "last minute meeting",
]


@pytest.mark.parametrize("text", D1_D5_ACCEPTED)
def test_d1_d5_deviations_remain_accepted(db, text):
    """The approved S7 deviations survive S9 untouched, at the single
    classifier both consumers now delegate to."""
    assert chat_module.classify_other_reason_detail(text) == "dental"


@pytest.mark.parametrize("text", D1_D5_GUARDRAILS)
def test_d1_d5_guardrails_remain_rejected(db, text):
    assert chat_module.classify_other_reason_detail(text) != "dental"


def test_meaningful_source_text_not_overwritten(db, fakes, force_bypass_reason_detail):
    """The consumer's existing no-overwrite rule is preserved: an already
    meaningful source text survives a later accepted detail."""
    client = make_client(db, booking_mode="hybrid")
    conversation = make_conversation(
        db, client, lead_reason=None, lead_email_opt_out=True,
        lead_reason_source_text="sore spot since my last visit")
    seed_assistant_message(db, conversation, BYPASS_ENTRY_PROMPT)

    send(db, client, conversation, "There is a metallic taste in my mouth")

    assert conversation.lead_reason_source_text == "sore spot since my last visit"


# ===========================================================================
# 14. Notification deferral — decision D-3
# ===========================================================================

def test_hybrid_handoff_sends_no_office_notification(db, fakes):
    client = make_client(db, booking_mode="hybrid", booking_url="https://book.example.com")
    conversation = captured_conversation(db, client)

    resp = send(db, client, conversation, "I want to book an appointment")

    assert resp.meta.get("mode") == "external_booking_handoff"
    assert fakes.lead_sms == []
    assert fakes.lead_email == []
    assert fakes.booking_sms == []
    assert fakes.booking_email == []
    assert "sent to the office" not in resp.reply
    assert "saved for the office" not in resp.reply


def test_hybrid_capture_turn_sends_no_notification(db, fakes):
    client = make_client(db, booking_mode="hybrid", booking_url="https://book.example.com")
    conversation = make_conversation(db, client)

    send(db, client, conversation, "I want to book an appointment")

    assert fakes.lead_sms == [] and fakes.lead_email == []


def test_post_handoff_residue_sends_no_notification(db, fakes):
    client = make_client(db, booking_mode="hybrid", booking_url="https://book.example.com")
    conversation = post_handoff_conversation(db, client)

    send(db, client, conversation, "the link isn't working")

    assert fakes.lead_sms == [] and fakes.lead_email == []
    assert fakes.booking_sms == [] and fakes.booking_email == []


def test_existing_s5_honest_wording_untouched(db, fakes):
    """Where a notification DOES occur, the S5 per-channel wording owner
    still reports this turn's real results."""
    client = make_client(db, booking_mode="hybrid", office_hours=OPEN_ALL_WEEK_HOURS)
    conversation = make_conversation(
        db, client, lead_name="Kevin", lead_phone="516-555-1234",
        lead_email_opt_out=True, lead_time_window="Tuesday morning",
        lead_is_new_patient=None, is_lead=True)

    resp = send(db, client, conversation, "returning")

    assert resp.meta.get("lead_sms_sent") is True
    assert resp.meta.get("lead_email_sent") is True
    assert resp.meta.get("lead_sms_error") is None
    assert resp.meta.get("lead_email_error") is None
