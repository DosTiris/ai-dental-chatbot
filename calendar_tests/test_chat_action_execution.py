# calendar_tests/test_chat_action_execution.py
#
# C1-C structured Calendar action EXECUTION contract.
#
# Scope: the /chat action lane's five 409 envelope codes, the pinned
# final_closed 200, the shared booking_boundary_state helper, choice
# resolution against persisted state only, replay/recovery idempotency
# (T-19a / T-19b), the C-3 hold-loss restatement, duplicate-confirm and
# concurrent-confirm defenses, no-mutation-on-rejection, transcript
# ordering, and flag-gated calendar_actions emission.
#
# V2 (C1-C audit): confirm-yes response-loss retry restatement (item 1),
# confirmation-stage hold validity (item 2), boundary-before-availability
# precedence (item 3), per-conversation concurrency reconciliation with
# separate PostgreSQL sessions and deterministic seams (item 4), and the
# explicit ACTION_EXECUTED outcome-drift fail-closed route check (item 5).
#
# V4 (audit item 1): the service performs a REAL tenant-scoped database
# reload before its boundary re-check; the route-window test commits the
# boundary from a genuinely separate PostgreSQL session inside the
# dispatch window (no pre-seeding on the route object, no faked service
# result) and proves the boundary wins over OFF feature flags with no
# action transition service invoked.
#
# Provenance (Rule 19): authored against verified branch evidence at
# f06930e; every pass/fail claim is owner-authoritative on local
# PostgreSQL 16 only. Tests that depend on PostgreSQL-specific behavior
# (row locks under concurrency, the notification-attempt ledger) skip on
# any other dialect instead of pretending to cover it.

import json
import threading
import types
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import sessionmaker

import app.routes.chat as chat_module
import app.services.booking_conversation as bc
from app.calendar_models import (
    AppointmentSlot,
    BookingState,
    NotificationAttempt,
    SlotStatus,
)
from app.models import Client, Conversation, Message
from app.schemas import ChatAction, ChatRequest
from app.services.appointment_intent import PREF_ANY
from app.services.booking_service import BookingResult
from app.services.calendar_settings_service import (
    DEFAULT_CALENDAR_ACTIONS_ENABLED,
    load_calendar_settings,
)


class _FakeAddr:
    host = "127.0.0.1"


class _FakeRequest:
    client = _FakeAddr()


# ---------------------------------------------------------------------------
# Builders. Calendar actions require BOTH booking_enabled and
# calendar_actions_enabled; every seeded conversation gets intake identity
# because Intake owns identity (Rule 3) and the dialog will not run without it.
# ---------------------------------------------------------------------------

def _client(db, *, booking=True, actions=True, practice_name="C1-C Exec Dental"):
    row = Client(
        id=uuid.uuid4(),
        practice_name=practice_name,
        api_key=f"key-{uuid.uuid4()}",
        active=True,
        settings={
            "timezone": "America/New_York",
            "booking_mode": "capture_first",
            "calendar": {
                "booking_enabled": booking,
                "calendar_actions_enabled": actions,
            },
        },
    )
    db.add(row)
    db.commit()
    return row


def _conversation(db, client, **overrides):
    values = {
        "id": uuid.uuid4(),
        "client_id": client.id,
        "visitor_id": "c1c-visitor",
        "is_lead": False,
        "lead_status": "new",
        "lead_name": "Casey Patient",
        "lead_phone": "5165551234",
    }
    values.update(overrides)
    row = Conversation(**values)
    db.add(row)
    db.commit()
    return row


def _slot(db, client, *, days_ahead=2, hour=14, status=SlotStatus.AVAILABLE,
          held_by=None, held_until=None):
    start = (datetime.now(timezone.utc) + timedelta(days=days_ahead)).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )
    row = AppointmentSlot(
        id=uuid.uuid4(),
        client_id=client.id,
        start_datetime=start,
        end_datetime=start + timedelta(minutes=30),
        status=status,
        held_until=held_until,
        held_by_conversation_id=held_by,
    )
    db.add(row)
    db.commit()
    return row


def _seed_selection(db, conversation, slots):
    """WAITING_FOR_SLOT_SELECTION with a live (unexpired) persisted offer."""
    conversation.booking_state = BookingState.WAITING_FOR_SLOT_SELECTION
    conversation.booking_offered_slot_ids = [str(s.id) for s in slots]
    conversation.booking_offer_expires_at = (
        datetime.now(timezone.utc) + timedelta(minutes=20)
    )
    conversation.booking_effective_time_preference = PREF_ANY
    db.add(conversation)
    db.commit()


def _seed_confirmation(db, conversation, slot, *, hold_minutes=5):
    """WAITING_FOR_CONFIRMATION with the slot HELD by this conversation
    (exactly the state _hold_offered_slot leaves behind)."""
    slot.status = SlotStatus.HELD
    slot.held_by_conversation_id = conversation.id
    slot.held_until = datetime.now(timezone.utc) + timedelta(minutes=hold_minutes)
    conversation.booking_state = BookingState.WAITING_FOR_CONFIRMATION
    conversation.booking_selected_slot_id = slot.id
    conversation.booking_offered_slot_ids = None
    conversation.booking_offer_expires_at = None
    conversation.booking_effective_time_preference = PREF_ANY
    db.add(slot)
    db.add(conversation)
    db.commit()


def _action_request(client, conversation_id, choice_id, *, message="2:00 PM"):
    return ChatRequest(
        message=message,
        client_key=client.api_key,
        visitor_id="c1c-visitor",
        conversation_id=str(conversation_id),
        action={"type": "calendar_choice", "choice_id": choice_id},
    )


def _call(db, client, conversation_id, choice_id, *, message="2:00 PM"):
    return chat_module.chat(
        _action_request(client, conversation_id, choice_id, message=message),
        _FakeRequest(),
        db,
    )


def _raises_409(db, client, conversation_id, choice_id, *, message="2:00 PM"):
    with pytest.raises(HTTPException) as exc:
        _call(db, client, conversation_id, choice_id, message=message)
    assert exc.value.status_code == 409
    assert isinstance(exc.value.detail, dict)
    return exc.value.detail


def _transcript(db, conversation):
    return (
        db.query(Message)
        .filter(Message.conversation_id == conversation.id)
        .order_by(Message.created_at, Message.id)
        .all()
    )


def _record_message_adds(monkeypatch, db):
    """V4.2 (owner-run evidence, audit item B): deterministic transcript-
    ORDER instrumentation. Both transcript rows of one request may share a
    single transaction timestamp, and random UUIDv4 ids carry no insertion
    order, so ORDER BY created_at, id cannot prove sequence. This spy
    records the exact order Message objects are handed to db.add — the
    real insertion sequence — while delegating to the real db.add
    unchanged. No sleeps, no timestamp ties, no UUID assumptions, no
    extra commits."""
    added = []
    real_add = db.add

    def recording_add(obj):
        if isinstance(obj, Message):
            added.append(obj)
        return real_add(obj)

    monkeypatch.setattr(db, "add", recording_add)
    return added


def _assert_transcript_pair(db, conversation, added, *, user_content=None,
                            user_prefix=None, assistant_content=None):
    """Locked decisions 13–14, asserted deterministically: the ADD ORDER
    proves the server-derived user row was inserted BEFORE the assistant
    row; role-keyed persisted queries prove exactly one of each with the
    exact content. Nothing here depends on created_at ties or UUID order,
    and the actual contract is not weakened."""
    pair = [m for m in added if m.conversation_id == conversation.id]
    assert [m.role for m in pair] == ["user", "assistant"]
    persisted_user = (
        db.query(Message)
        .filter(Message.conversation_id == conversation.id,
                Message.role == "user")
        .all()
    )
    persisted_assistant = (
        db.query(Message)
        .filter(Message.conversation_id == conversation.id,
                Message.role == "assistant")
        .all()
    )
    assert len(persisted_user) == 1
    assert len(persisted_assistant) == 1
    if user_content is not None:
        assert pair[0].content == user_content
        assert persisted_user[0].content == user_content
    if user_prefix is not None:
        assert pair[0].content.startswith(user_prefix)
        assert persisted_user[0].content.startswith(user_prefix)
    if assistant_content is not None:
        assert pair[1].content == assistant_content
        assert persisted_assistant[0].content == assistant_content


# ---------------------------------------------------------------------------
# Vocabulary sync + boundary helper (pure unit tests).
# ---------------------------------------------------------------------------

def test_calendar_choice_action_type_matches_schema_literal():
    # The service-owned constant must satisfy the route schema's Literal —
    # the same one-owner sync pattern the PreviewDay day-state test uses.
    ChatAction(type=bc.CALENDAR_CHOICE_ACTION_TYPE, choice_id="opaque")


def test_boundary_vocabulary_is_closed_and_ordered():
    def conv(**kw):
        base = {"final_closed": False, "abuse_locked_until": None,
                "lead_is_emergency": False}
        base.update(kw)
        return types.SimpleNamespace(**base)

    future = datetime.now(timezone.utc) + timedelta(hours=1)
    assert bc.booking_boundary_state(conv()) == bc.BOUNDARY_NONE
    assert bc.booking_boundary_state(
        conv(lead_is_emergency=True)) == bc.BOUNDARY_SAFETY_BLOCKED
    assert bc.booking_boundary_state(
        conv(abuse_locked_until=future)) == bc.BOUNDARY_LOCKED
    # Precedence mirrors the chat text path: final_closed, then locked,
    # then the durable emergency flag.
    assert bc.booking_boundary_state(
        conv(final_closed=True, abuse_locked_until=future,
             lead_is_emergency=True)) == bc.BOUNDARY_FINAL_CLOSED
    assert bc.booking_boundary_state(
        conv(abuse_locked_until=future,
             lead_is_emergency=True)) == bc.BOUNDARY_LOCKED
    for value in (bc.BOUNDARY_FINAL_CLOSED, bc.BOUNDARY_LOCKED,
                  bc.BOUNDARY_SAFETY_BLOCKED, bc.BOUNDARY_NONE):
        assert value in bc.ALL_BOUNDARY_STATES


def test_locked_reader_never_drifts_from_chat_reader(db):
    # Rule 3 documented duplication (import cycle): both readers must give
    # identical verdicts on the same persisted column, forever.
    client = _client(db)
    cases = [
        None,
        datetime.now(timezone.utc) + timedelta(days=1),
        datetime.now(timezone.utc) - timedelta(days=1),
    ]
    for until in cases:
        conversation = _conversation(db, client, abuse_locked_until=until)
        assert chat_module.conversation_is_locked(conversation) == (
            bc.booking_boundary_state(conversation) == bc.BOUNDARY_LOCKED
        )


def test_calendar_actions_flag_is_strict_bool_default_false():
    assert DEFAULT_CALENDAR_ACTIONS_ENABLED is False

    def stub(calendar):
        return types.SimpleNamespace(settings={"calendar": calendar})

    assert load_calendar_settings(stub({})).calendar_actions_enabled is False
    assert load_calendar_settings(
        stub({"calendar_actions_enabled": "true"})
    ).calendar_actions_enabled is False
    assert load_calendar_settings(
        stub({"calendar_actions_enabled": 1})
    ).calendar_actions_enabled is False
    assert load_calendar_settings(
        stub({"calendar_actions_enabled": True})
    ).calendar_actions_enabled is True


# ---------------------------------------------------------------------------
# Route lane: rejection envelopes persist and mutate NOTHING.
# ---------------------------------------------------------------------------

def test_action_not_active_when_flag_off(db):
    client = _client(db, actions=False)
    conversation = _conversation(db, client)
    slot = _slot(db, client)
    _seed_selection(db, conversation, [slot])
    before_messages = db.query(Message).count()

    detail = _raises_409(db, client, conversation.id, str(slot.id))
    assert detail == {
        "code": chat_module.ACTION_ERROR_CODE_NOT_ACTIVE,
        "message": chat_module.STRUCTURED_ACTION_NOT_ACTIVE_DETAIL,
    }
    db.refresh(slot)
    db.refresh(conversation)
    assert slot.status == SlotStatus.AVAILABLE
    assert conversation.booking_state == BookingState.WAITING_FOR_SLOT_SELECTION
    assert db.query(Message).count() == before_messages


def test_action_not_active_when_booking_disabled(db):
    client = _client(db, booking=False, actions=True)
    conversation = _conversation(db, client)
    detail = _raises_409(db, client, conversation.id, "anything")
    assert detail["code"] == chat_module.ACTION_ERROR_CODE_NOT_ACTIVE


def test_conversation_unavailable_bodies_are_byte_identical(db):
    client_a = _client(db, practice_name="Tenant A")
    client_b = _client(db, practice_name="Tenant B")
    other_tenant_conversation = _conversation(db, client_b)
    before_conversations = db.query(Conversation).count()

    details = [
        _raises_409(db, client_a, other_tenant_conversation.id, "choice"),
        _raises_409(db, client_a, uuid.uuid4(), "choice"),
        _raises_409(db, client_a, "not-a-valid-uuid", "choice"),
    ]
    assert details[0] == details[1] == details[2] == {
        "code": chat_module.ACTION_ERROR_CODE_CONVERSATION_UNAVAILABLE,
        "message": chat_module.STRUCTURED_ACTION_STALE_DETAIL,
    }
    # No echo of any submitted conversation_id, and no replacement rows.
    assert str(other_tenant_conversation.id) not in json.dumps(details)
    assert db.query(Conversation).count() == before_conversations


def test_final_closed_keeps_pinned_200(db):
    client = _client(db)
    conversation = _conversation(db, client, final_closed=True)
    before_messages = db.query(Message).count()

    response = _call(db, client, conversation.id, "any-choice")
    assert response.meta["mode"] == "final_closed"
    assert "conversation has ended" in response.reply.lower()
    assert db.query(Message).count() == before_messages


def test_locked_conversation_envelope_and_no_mutation(db):
    client = _client(db)
    conversation = _conversation(
        db, client,
        abuse_locked_until=datetime.now(timezone.utc) + timedelta(days=1),
    )
    slot = _slot(db, client)
    _seed_selection(db, conversation, [slot])
    before_messages = db.query(Message).count()

    detail = _raises_409(db, client, conversation.id, str(slot.id))
    assert detail["code"] == chat_module.ACTION_ERROR_CODE_CONVERSATION_LOCKED
    assert isinstance(detail["message"], str) and detail["message"]
    assert "calendar_actions" not in detail

    db.refresh(slot)
    db.refresh(conversation)
    assert slot.status == SlotStatus.AVAILABLE
    assert conversation.booking_state == BookingState.WAITING_FOR_SLOT_SELECTION
    assert db.query(Message).count() == before_messages


def test_safety_blocked_envelope_releases_hold_and_clears_state(db):
    client = _client(db)
    conversation = _conversation(db, client, lead_is_emergency=True)
    slot = _slot(db, client)
    _seed_confirmation(db, conversation, slot)
    before_messages = db.query(Message).count()

    detail = _raises_409(
        db, client, conversation.id,
        bc.CONFIRM_YES_CHOICE_PREFIX + str(slot.id),
    )
    assert detail == {
        "code": chat_module.ACTION_ERROR_CODE_SAFETY_BLOCKED,
        "message": chat_module.STRUCTURED_ACTION_SAFETY_BLOCKED_DETAIL,
    }
    db.refresh(slot)
    db.refresh(conversation)
    # The Calendar-owned reset ran: hold released, dialog state cleared.
    assert slot.status == SlotStatus.AVAILABLE
    assert slot.held_by_conversation_id is None
    assert conversation.booking_selected_slot_id is None
    assert conversation.booking_state in (None, BookingState.NONE)
    assert db.query(Message).count() == before_messages


def test_emergency_text_cleanup_then_final_closed_action_pin(db):
    # Pins locked decision 6's requirement that the EXISTING emergency-family
    # guard cleanup (PATCH 3) stays intact: a life-threatening TEXT message
    # mid-dialog cancels the active booking (hold released, dialog cleared)
    # AND sets the PERSISTENT stop (chat.py sets conversation.final_closed).
    #
    # V4.2 correction (owner-run PostgreSQL evidence): the original version
    # of this test expected SAFETY_BLOCKED 409 for the previously issued
    # confirm token — contradicting the pinned C1-C precedence in
    # booking_boundary_state (final_closed OUTRANKS locked/safety, exactly
    # like the chat text path). The later action therefore receives the
    # pinned final_closed HTTP 200 ended-conversation response: no 409, no
    # transcript rows, no booking side effects. The production route is
    # unchanged; the TEST expectation was the defect.
    client = _client(db)
    conversation = _conversation(db, client)
    slot = _slot(db, client)
    _seed_confirmation(db, conversation, slot)

    text_request = ChatRequest(
        message="I can't breathe",
        client_key=client.api_key,
        visitor_id="c1c-visitor",
        conversation_id=str(conversation.id),
    )
    response = chat_module.chat(text_request, _FakeRequest(), db)
    assert "911" in response.reply

    db.refresh(slot)
    db.refresh(conversation)
    assert slot.status == SlotStatus.AVAILABLE
    assert conversation.booking_selected_slot_id is None
    assert bool(conversation.final_closed) is True  # persistent stop set

    before_messages = db.query(Message).count()
    action_response = _call(
        db, client, conversation.id,
        bc.CONFIRM_YES_CHOICE_PREFIX + str(slot.id),
    )
    assert action_response.meta["mode"] == "final_closed"
    assert "conversation has ended" in action_response.reply.lower()
    assert db.query(Message).count() == before_messages  # nothing persisted
    db.refresh(slot)
    assert slot.status == SlotStatus.AVAILABLE  # no side effects behind stop


# ---------------------------------------------------------------------------
# Execution: selection, confirmation, transcript ordering, meta emission.
# ---------------------------------------------------------------------------

def test_select_action_holds_slot_and_issues_confirm_choices(db, monkeypatch):
    client = _client(db)
    conversation = _conversation(db, client)
    slot = _slot(db, client)
    other = _slot(db, client, hour=15)
    _seed_selection(db, conversation, [slot, other])
    before_messages = db.query(Message).count()
    added = _record_message_adds(monkeypatch, db)

    response = _call(db, client, conversation.id, str(slot.id))

    db.refresh(slot)
    db.refresh(conversation)
    assert slot.status == SlotStatus.HELD
    assert slot.held_by_conversation_id == conversation.id
    assert conversation.booking_state == BookingState.WAITING_FOR_CONFIRMATION
    assert conversation.booking_selected_slot_id == slot.id
    assert conversation.booking_offered_slot_ids is None
    assert "To confirm:" in response.reply
    assert response.meta["state"] == BookingState.WAITING_FOR_CONFIRMATION

    actions = response.meta["calendar_actions"]
    assert [a["action"]["choice_id"] for a in actions] == [
        bc.CONFIRM_YES_CHOICE_PREFIX + str(slot.id),
        bc.CONFIRM_NO_CHOICE_PREFIX + str(slot.id),
    ]
    for entry in actions:
        assert entry["action"]["type"] == bc.CALENDAR_CHOICE_ACTION_TYPE

    # Transcript ordering (locked decisions 13-14): SERVER-derived user
    # label first, assistant reply second; nothing else. V4.2: proven via
    # deterministic add-order instrumentation + role-keyed persistence.
    assert db.query(Message).count() == before_messages + 2
    _assert_transcript_pair(
        db, conversation, added,
        user_prefix="Selected ", assistant_content=response.reply,
    )


def test_confirm_yes_books_and_restates_on_duplicate(db, monkeypatch):
    client = _client(db)
    conversation = _conversation(db, client)
    slot = _slot(db, client)
    _seed_confirmation(db, conversation, slot)
    yes_choice = bc.CONFIRM_YES_CHOICE_PREFIX + str(slot.id)
    added = _record_message_adds(monkeypatch, db)

    response = _call(db, client, conversation.id, yes_choice)

    from app.repositories import appointment_repository
    appointment = appointment_repository.get_appointment_by_conversation(
        db, client.id, conversation.id
    )
    assert appointment is not None
    db.refresh(slot)
    db.refresh(conversation)
    assert slot.status == SlotStatus.BOOKED
    assert conversation.booking_state in (None, BookingState.NONE)
    assert conversation.booking_selected_slot_id is None
    # V4.2: deterministic add-order proof + role-keyed persistence checks
    # (asserted BEFORE the retry below adds a second pair).
    _assert_transcript_pair(
        db, conversation, added,
        user_content="Yes \u2014 book it", assistant_content=response.reply,
    )

    # V2 audit item 1: a sequential duplicate of the SAME confirm token is
    # a response-loss retry of an already-successful booking. It must
    # RESTATE the existing appointment over HTTP 200 — never re-finalize,
    # never notify, never touch the notification-attempt ledger — and it
    # must leave exactly one active appointment.
    ledger_before = db.query(NotificationAttempt).count()
    retry = _call(db, client, conversation.id, yes_choice)
    assert "already have an appointment" in retry.reply
    assert retry.meta.get("existing_appointment_id") == str(appointment.id)
    from app.calendar_models import Appointment
    active = (
        db.query(Appointment)
        .filter(Appointment.conversation_id == conversation.id)
        .filter(Appointment.status != "cancelled")
        .count()
    )
    assert active == 1
    assert db.query(NotificationAttempt).count() == ledger_before


def test_confirm_no_releases_hold_and_restarts(db, monkeypatch):
    client = _client(db)
    conversation = _conversation(db, client)
    slot = _slot(db, client)
    _seed_confirmation(db, conversation, slot)
    added = _record_message_adds(monkeypatch, db)

    response = _call(
        db, client, conversation.id, bc.CONFIRM_NO_CHOICE_PREFIX + str(slot.id)
    )
    db.refresh(slot)
    db.refresh(conversation)
    assert slot.status == SlotStatus.AVAILABLE
    assert slot.held_by_conversation_id is None
    assert conversation.booking_state == BookingState.WAITING_FOR_DATE
    assert conversation.booking_selected_slot_id is None
    assert "what day" in response.reply.lower()
    # V4.2: deterministic add-order proof + role-keyed persistence checks.
    _assert_transcript_pair(
        db, conversation, added,
        user_content="No \u2014 pick another time",
        assistant_content=response.reply,
    )


def test_selection_replay_after_advance_is_pure_restatement(db):
    # T-19a (fail-first for held_until): replaying the SELECTION choice after
    # the transition already advanced to WAITING_FOR_CONFIRMATION must
    # short-circuit before place_hold and leave held_until byte-identical.
    client = _client(db)
    conversation = _conversation(db, client)
    slot = _slot(db, client)
    _seed_confirmation(db, conversation, slot)
    held_until_before = slot.held_until

    response = _call(db, client, conversation.id, str(slot.id))

    db.refresh(slot)
    db.refresh(conversation)
    assert "To confirm:" in response.reply
    assert conversation.booking_state == BookingState.WAITING_FOR_CONFIRMATION
    assert slot.status == SlotStatus.HELD
    assert slot.held_until == held_until_before
    assert slot.held_by_conversation_id == conversation.id
    # The replay re-issues the SAME two confirm choices.
    assert [a["action"]["choice_id"] for a in response.meta["calendar_actions"]] == [
        bc.CONFIRM_YES_CHOICE_PREFIX + str(slot.id),
        bc.CONFIRM_NO_CHOICE_PREFIX + str(slot.id),
    ]


def test_interrupted_selection_recovery_refreshes_hold_once(db):
    # T-19b (approved C-5): the transition was interrupted AFTER place_hold
    # but BEFORE state advanced. Re-submitting the same live offered choice
    # recovers by re-running the hold; the owner re-take refreshes held_until.
    client = _client(db)
    conversation = _conversation(db, client)
    slot = _slot(db, client)
    _seed_selection(db, conversation, [slot])
    slot.status = SlotStatus.HELD
    slot.held_by_conversation_id = conversation.id
    slot.held_until = datetime.now(timezone.utc) + timedelta(minutes=1)
    db.add(slot)
    db.commit()
    stale_hold_expiry = slot.held_until

    response = _call(db, client, conversation.id, str(slot.id))

    db.refresh(slot)
    db.refresh(conversation)
    assert "To confirm:" in response.reply
    assert conversation.booking_state == BookingState.WAITING_FOR_CONFIRMATION
    assert slot.status == SlotStatus.HELD
    assert slot.held_by_conversation_id == conversation.id
    assert slot.held_until is not None
    assert slot.held_until > stale_hold_expiry


def test_forged_and_cross_tenant_choices_are_identically_stale(db):
    client = _client(db)
    other_client = _client(db, practice_name="Other Tenant")
    conversation = _conversation(db, client)
    slot = _slot(db, client)
    _seed_selection(db, conversation, [slot])
    foreign_slot = _slot(db, other_client)
    before_messages = db.query(Message).count()

    forged = _raises_409(db, client, conversation.id, str(uuid.uuid4()))
    cross = _raises_409(db, client, conversation.id, str(foreign_slot.id))

    # Indistinguishable by design, and each carries the still-live offer as
    # its replacement set (issued WITHOUT mutating any state).
    assert forged == cross
    assert forged["code"] == chat_module.ACTION_ERROR_CODE_STALE_CHOICE
    assert [a["action"]["choice_id"] for a in forged["calendar_actions"]] == [
        str(slot.id)
    ]
    db.refresh(conversation)
    assert conversation.booking_state == BookingState.WAITING_FOR_SLOT_SELECTION
    assert conversation.booking_offered_slot_ids == [str(slot.id)]
    assert db.query(Message).count() == before_messages


def test_expired_offer_is_stale_with_no_replacement(db):
    client = _client(db)
    conversation = _conversation(db, client)
    slot = _slot(db, client)
    _seed_selection(db, conversation, [slot])
    conversation.booking_offer_expires_at = (
        datetime.now(timezone.utc) - timedelta(minutes=1)
    )
    db.add(conversation)
    db.commit()

    detail = _raises_409(db, client, conversation.id, str(slot.id))
    assert detail["code"] == chat_module.ACTION_ERROR_CODE_STALE_CHOICE
    # No replacement set exists without mutating state (re-offering writes
    # new offer rows); the widget falls back to typed input.
    assert "calendar_actions" not in detail
    db.refresh(conversation)
    # The rejection changed nothing — the expired offer fields are intact
    # for the TEXT path to refresh on the next typed message.
    assert conversation.booking_offered_slot_ids == [str(slot.id)]


def test_confirm_token_in_wrong_state_is_stale(db):
    client = _client(db)
    conversation = _conversation(
        db, client, booking_state=BookingState.WAITING_FOR_DATE
    )
    detail = _raises_409(
        db, client, conversation.id,
        bc.CONFIRM_YES_CHOICE_PREFIX + str(uuid.uuid4()),
    )
    assert detail["code"] == chat_module.ACTION_ERROR_CODE_STALE_CHOICE
    assert "calendar_actions" not in detail


def test_mismatched_confirm_token_reissues_confirm_choices(db):
    client = _client(db)
    conversation = _conversation(db, client)
    slot = _slot(db, client)
    _seed_confirmation(db, conversation, slot)

    detail = _raises_409(
        db, client, conversation.id,
        bc.CONFIRM_YES_CHOICE_PREFIX + str(uuid.uuid4()),
    )
    assert detail["code"] == chat_module.ACTION_ERROR_CODE_STALE_CHOICE
    assert [a["action"]["choice_id"] for a in detail["calendar_actions"]] == [
        bc.CONFIRM_YES_CHOICE_PREFIX + str(slot.id),
        bc.CONFIRM_NO_CHOICE_PREFIX + str(slot.id),
    ]
    db.refresh(slot)
    assert slot.status == SlotStatus.HELD


def test_action_message_echo_is_never_classified_or_persisted(db):
    # Locked decision 1: the message on an action request is an untrusted
    # display echo. Even emergency wording must not be classified, must not
    # set the emergency flag, and must never appear in the transcript.
    client = _client(db)
    conversation = _conversation(db, client)
    slot = _slot(db, client)
    _seed_selection(db, conversation, [slot])

    response = _call(
        db, client, conversation.id, str(slot.id), message="I can't breathe"
    )
    assert "To confirm:" in response.reply
    db.refresh(conversation)
    assert bool(conversation.lead_is_emergency) is False
    contents = [m.content for m in _transcript(db, conversation)]
    assert "I can't breathe" not in contents


def test_flag_gates_calendar_actions_on_text_replies(db):
    # The text path's replies emit calendar_actions ONLY when the flag is on
    # (additive rendering of the same persisted state).
    for actions_enabled in (False, True):
        client = _client(db, actions=actions_enabled)
        conversation = _conversation(db, client)
        slot = _slot(db, client)
        _seed_confirmation(db, conversation, slot)
        settings = load_calendar_settings(client)
        reply = bc.handle_booking_message(
            db, client, conversation, "hmm let me think"
        )
        assert reply.handled is True
        assert ("calendar_actions" in reply.meta) is actions_enabled
        if actions_enabled:
            assert [a["action"]["choice_id"] for a in reply.meta["calendar_actions"]] == [
                bc.CONFIRM_YES_CHOICE_PREFIX + str(slot.id),
                bc.CONFIRM_NO_CHOICE_PREFIX + str(slot.id),
            ]
        assert settings.calendar_actions_enabled is actions_enabled


def test_rejected_actions_never_touch_the_transcript(db):
    # Aggregate pin for locked decision 12 across every rejection class that
    # reaches the route with a real conversation.
    client_off = _client(db, actions=False)
    conversation_off = _conversation(db, client_off)

    client_on = _client(db)
    stale_conversation = _conversation(db, client_on)
    locked_conversation = _conversation(
        db, client_on,
        abuse_locked_until=datetime.now(timezone.utc) + timedelta(days=1),
    )
    safety_conversation = _conversation(db, client_on, lead_is_emergency=True)

    before = db.query(Message).count()
    _raises_409(db, client_off, conversation_off.id, "x")
    _raises_409(db, client_on, stale_conversation.id, "x")
    _raises_409(db, client_on, locked_conversation.id, "x")
    _raises_409(db, client_on, safety_conversation.id, "x")
    _raises_409(db, client_on, uuid.uuid4(), "x")
    assert db.query(Message).count() == before


# ---------------------------------------------------------------------------
# C-3 restatement and PostgreSQL-only concurrency.
# ---------------------------------------------------------------------------

def test_finalize_restates_existing_appointment_after_hold_loss(db, monkeypatch):
    # Approved C-3, unit-tested at the exact branch: when finalize reports
    # hold_lost/hold_expired on the ACTION path and this conversation
    # already owns an appointment (the concurrent same-slot loser wrinkle),
    # the reply RESTATES it — never a re-offer, never a second booking.
    client = _client(db)
    conversation = _conversation(db, client)
    slot = _slot(db, client)
    _seed_confirmation(db, conversation, slot)

    from app.calendar_models import Appointment
    appointment = Appointment(
        id=uuid.uuid4(),
        client_id=client.id,
        slot_id=slot.id,
        conversation_id=conversation.id,
        patient_name="Casey Patient",
        patient_phone="5165551234",
        start_datetime=slot.start_datetime,
        end_datetime=slot.end_datetime,
        status="pending",
    )
    db.add(appointment)
    db.commit()

    monkeypatch.setattr(
        bc.booking_service,
        "finalize_booking",
        lambda *args, **kwargs: BookingResult(success=False, reason="hold_lost"),
    )
    settings = load_calendar_settings(client)
    now_utc = datetime.now(timezone.utc)
    reply = bc._finalize_and_reply(
        db, client, conversation, settings, slot.id, now_utc,
        restate_after_hold_loss=True,
    )
    assert "already have an appointment" in reply.text
    assert reply.meta.get("existing_appointment_id") == str(appointment.id)
    db.refresh(conversation)
    assert conversation.booking_state in (None, BookingState.NONE)
    assert conversation.booking_selected_slot_id is None


def test_text_path_hold_loss_behavior_unchanged(db, monkeypatch):
    # The C-3 branch is keyword-gated: the TEXT path (default False) keeps
    # its existing re-offer behavior byte-for-byte even when an appointment
    # exists (recorded deferred finding — no silent text-path change).
    client = _client(db)
    conversation = _conversation(db, client)
    slot = _slot(db, client)
    _seed_confirmation(db, conversation, slot)

    monkeypatch.setattr(
        bc.booking_service,
        "finalize_booking",
        lambda *args, **kwargs: BookingResult(success=False, reason="hold_lost"),
    )
    settings = load_calendar_settings(client)
    reply = bc._finalize_and_reply(
        db, client, conversation, settings, slot.id,
        datetime.now(timezone.utc),
    )
    assert "already have an appointment" not in reply.text


def test_concurrent_confirm_yes_books_exactly_once(db):
    # T-21: two independent sessions race the SAME confirm token. PostgreSQL
    # row locks + the finalize duplicate defenses must yield exactly one
    # active appointment; the loser gets a truthful non-booking reply.
    if db.get_bind().dialect.name != "postgresql":
        pytest.skip("PostgreSQL-only concurrency semantics")

    client = _client(db)
    conversation = _conversation(db, client)
    slot = _slot(db, client)
    _seed_confirmation(db, conversation, slot)
    db.commit()

    Session = sessionmaker(bind=db.get_bind())
    request = _action_request(
        client, conversation.id, bc.CONFIRM_YES_CHOICE_PREFIX + str(slot.id)
    )
    barrier = threading.Barrier(2)
    outcomes = [None, None]

    def worker(index):
        session = Session()
        try:
            barrier.wait(timeout=10)
            try:
                response = chat_module.chat(request, _FakeRequest(), session)
                outcomes[index] = ("ok", response.reply)
            except HTTPException as exc:
                outcomes[index] = ("409", exc.detail)
        except Exception as exc:  # pragma: no cover - surfaced by assert below
            outcomes[index] = ("error", repr(exc))
        finally:
            session.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert all(o is not None and o[0] != "error" for o in outcomes), outcomes

    from app.calendar_models import Appointment
    active = (
        db.query(Appointment)
        .filter(Appointment.conversation_id == conversation.id)
        .filter(Appointment.status != "cancelled")
        .count()
    )
    assert active == 1

    # V3 (audit item 3): the complete post-race invariant set.
    db.expire_all()
    db.refresh(slot)
    db.refresh(conversation)
    assert slot.status == SlotStatus.BOOKED
    assert _held_slots_for(db, conversation) == []          # no active hold
    assert conversation.booking_state in (None, BookingState.NONE)
    assert conversation.booking_selected_slot_id is None
    # Both requests return HTTP 200 with truthful content: exactly one
    # booking-success reply and exactly one existing-appointment
    # restatement; neither re-offers slots nor asks for a new day.
    assert [o[0] for o in outcomes] == ["ok", "ok"], outcomes
    replies = [o[1] for o in outcomes]
    assert sum("All set" in r for r in replies) == 1, replies
    assert sum("already have an appointment" in r for r in replies) == 1, replies
    for r in replies:
        assert "Which works best?" not in r
        assert "What day would work" not in r
        assert "what day would work better" not in r
    # V4.3 (owner full-suite evidence): the previous global-count
    # comparison read three unrelated rows left by earlier notification
    # tests in the session-shared database. Ownership is now proven PER
    # APPOINTMENT: every ledger row owned by the raced conversation must
    # belong to its single raced appointment, and that appointment's
    # footprint must EQUAL a solo control booking's footprint under the
    # same environment configuration — any duplicate row per appointment/
    # channel, or any claim from the losing request, breaks the equality.
    # No global table count; no dependency on suite ordering.
    from app.repositories import appointment_repository
    raced_appointment = appointment_repository.get_appointment_by_conversation(
        db, client.id, conversation.id
    )
    assert raced_appointment is not None
    raced_rows = _notification_rows_for_conversation(db, client, conversation)
    assert all(r.appointment_id == raced_appointment.id for r in raced_rows)

    control_conversation = _conversation(db, client, visitor_id="c1c-control")
    control_slot = _slot(db, client, days_ahead=3, hour=11)
    _seed_confirmation(db, control_conversation, control_slot)
    chat_module.chat(
        _action_request(client, control_conversation.id,
                        bc.CONFIRM_YES_CHOICE_PREFIX + str(control_slot.id)),
        _FakeRequest(), db,
    )
    control_active = (
        db.query(Appointment)
        .filter(Appointment.conversation_id == control_conversation.id)
        .filter(Appointment.status != "cancelled")
        .count()
    )
    assert control_active == 1
    control_rows = _notification_rows_for_conversation(
        db, client, control_conversation
    )
    assert len(raced_rows) == len(control_rows)


# ---------------------------------------------------------------------------
# V2 audit item 1 — confirm-yes response-loss retry after a successful
# booking commit. The retry restates; it never re-finalizes or notifies.
# ---------------------------------------------------------------------------

def test_lost_response_retry_of_identical_request_restates(db):
    # The HTTP response of a successful confirm-yes is lost; the browser
    # resends the IDENTICAL request object. The retry must land on the
    # restatement path: HTTP 200, existing appointment restated, exactly
    # one active appointment, notification-attempt ledger untouched.
    client = _client(db)
    conversation = _conversation(db, client)
    slot = _slot(db, client)
    _seed_confirmation(db, conversation, slot)
    request = _action_request(
        client, conversation.id, bc.CONFIRM_YES_CHOICE_PREFIX + str(slot.id)
    )

    first = chat_module.chat(request, _FakeRequest(), db)
    assert first.meta.get("booked") is True
    ledger_before = db.query(NotificationAttempt).count()

    second = chat_module.chat(request, _FakeRequest(), db)

    assert "already have an appointment" in second.reply
    from app.calendar_models import Appointment
    active = (
        db.query(Appointment)
        .filter(Appointment.conversation_id == conversation.id)
        .filter(Appointment.status != "cancelled")
        .count()
    )
    assert active == 1
    assert db.query(NotificationAttempt).count() == ledger_before
    db.refresh(conversation)
    assert conversation.booking_state in (None, BookingState.NONE)


def test_confirm_yes_retry_with_mismatched_or_forged_token_stays_stale(db):
    # Item 1 is deliberately narrow: only the confirm-yes token bound to
    # THIS conversation's appointment's consumed slot restates. A yes-token
    # for a DIFFERENT slot, the confirm-no token, and a forged token all
    # remain fail-closed STALE_CHOICE with no replacement set.
    client = _client(db)
    conversation = _conversation(db, client)
    slot = _slot(db, client)
    _seed_confirmation(db, conversation, slot)
    yes_choice = bc.CONFIRM_YES_CHOICE_PREFIX + str(slot.id)
    chat_module.chat(
        _action_request(client, conversation.id, yes_choice), _FakeRequest(), db
    )
    other_slot = _slot(db, client, hour=16)
    ledger_before = db.query(NotificationAttempt).count()

    for stale_choice in (
        bc.CONFIRM_YES_CHOICE_PREFIX + str(other_slot.id),
        bc.CONFIRM_NO_CHOICE_PREFIX + str(slot.id),
        "forged-choice-token",
    ):
        detail = _raises_409(db, client, conversation.id, stale_choice)
        assert detail["code"] == chat_module.ACTION_ERROR_CODE_STALE_CHOICE
        assert "calendar_actions" not in detail

    from app.calendar_models import Appointment
    active = (
        db.query(Appointment)
        .filter(Appointment.conversation_id == conversation.id)
        .filter(Appointment.status != "cancelled")
        .count()
    )
    assert active == 1
    assert db.query(NotificationAttempt).count() == ledger_before


# ---------------------------------------------------------------------------
# V2 audit item 2 — confirmation-stage hold validity. Replay restatements
# and mismatched-token replacement choices are issued only over a LIVE hold
# owned by this conversation; held_until is never refreshed by the check.
# ---------------------------------------------------------------------------

def test_selection_replay_after_hold_expiry_is_stale_without_replacement(db):
    client = _client(db)
    conversation = _conversation(db, client)
    slot = _slot(db, client)
    _seed_confirmation(db, conversation, slot, hold_minutes=-1)  # expired
    held_until_before = slot.held_until

    detail = _raises_409(db, client, conversation.id, str(slot.id))

    assert detail["code"] == chat_module.ACTION_ERROR_CODE_STALE_CHOICE
    assert "calendar_actions" not in detail
    db.refresh(slot)
    db.refresh(conversation)
    assert slot.held_until == held_until_before  # never refreshed/extended
    assert conversation.booking_state == BookingState.WAITING_FOR_CONFIRMATION


def test_mismatched_token_after_hold_expiry_is_stale_without_replacement(db):
    # Pre-V2 this branch re-issued confirmation buttons over the DEAD hold;
    # those buttons advertised a confirmation that could no longer succeed.
    client = _client(db)
    conversation = _conversation(db, client)
    slot = _slot(db, client)
    _seed_confirmation(db, conversation, slot, hold_minutes=-1)  # expired
    held_until_before = slot.held_until

    detail = _raises_409(db, client, conversation.id, "forged-choice-token")

    assert detail["code"] == chat_module.ACTION_ERROR_CODE_STALE_CHOICE
    assert "calendar_actions" not in detail
    db.refresh(slot)
    assert slot.held_until == held_until_before


@pytest.mark.parametrize("scenario", ["released", "foreign", "booked", "missing"])
def test_dead_selected_hold_variants_are_stale_without_replacement(db, scenario):
    # Released, foreign-owned, booked, and missing selected slots are all
    # not-live: replay AND mismatched-token submissions fail closed with no
    # replacement confirmation choices and no hold mutation.
    client = _client(db)
    conversation = _conversation(db, client)
    slot = _slot(db, client)
    _seed_confirmation(db, conversation, slot)
    if scenario == "released":
        slot.status = SlotStatus.AVAILABLE
        slot.held_by_conversation_id = None
        slot.held_until = None
        db.add(slot)
    elif scenario == "foreign":
        slot.held_by_conversation_id = uuid.uuid4()
        db.add(slot)
    elif scenario == "booked":
        slot.status = SlotStatus.BOOKED
        db.add(slot)
    elif scenario == "missing":
        db.delete(slot)
    db.commit()

    for choice in (str(slot.id), "forged-choice-token"):
        detail = _raises_409(db, client, conversation.id, choice)
        assert detail["code"] == chat_module.ACTION_ERROR_CODE_STALE_CHOICE
        assert "calendar_actions" not in detail

    if scenario == "foreign":
        db.refresh(slot)
        assert slot.status == SlotStatus.HELD  # the foreign hold is untouched


def test_mismatched_token_with_live_hold_still_reissues_choices(db):
    # The item-2 gate must NOT change the live-hold behavior: over a live
    # owned hold, a mismatched token still gets the SAME two replacement
    # confirmation choices, and held_until stays byte-identical.
    client = _client(db)
    conversation = _conversation(db, client)
    slot = _slot(db, client)
    _seed_confirmation(db, conversation, slot)
    held_until_before = slot.held_until

    detail = _raises_409(db, client, conversation.id, "forged-choice-token")

    assert detail["code"] == chat_module.ACTION_ERROR_CODE_STALE_CHOICE
    assert [a["action"]["choice_id"] for a in detail["calendar_actions"]] == [
        bc.CONFIRM_YES_CHOICE_PREFIX + str(slot.id),
        bc.CONFIRM_NO_CHOICE_PREFIX + str(slot.id),
    ]
    db.refresh(slot)
    assert slot.held_until == held_until_before


# ---------------------------------------------------------------------------
# V2 audit item 3 — boundary precedence over feature availability.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("boundary_setup,expected_boundary", [
    ({"abuse_locked_until": datetime.now(timezone.utc) + timedelta(hours=1)},
     "locked"),
    ({"lead_is_emergency": True}, "safety_blocked"),
])
def test_service_boundary_precedes_not_active(db, boundary_setup, expected_boundary):
    # Flags OFF + boundary state: the service re-check must report the
    # BOUNDARY, never mask it behind ACTION_NOT_ACTIVE.
    client = _client(db, booking=False, actions=False)
    conversation = _conversation(db, client, **boundary_setup)

    outcome = bc.handle_booking_action(db, client, conversation, "any-choice")

    assert outcome.status == bc.ACTION_BOUNDARY
    assert outcome.boundary == expected_boundary


@pytest.mark.parametrize("boundary_kind",
                         ["locked", "safety_blocked", "final_closed"])
def test_route_boundary_window_real_db_recheck_wins(db, monkeypatch,
                                                    boundary_kind):
    # V4 (audit item 1): the REAL database window. The conversation row is
    # clean at request start, so the route's first boundary check — its
    # own real reader, nothing monkeypatched, nothing pre-seeded on the
    # route session's object — genuinely observes NONE. A SECOND
    # PostgreSQL session then COMMITS the boundary inside the dispatch
    # window; the service's tenant-scoped reload must see it, the boundary
    # must win over the OFF feature flags, and no action transition
    # service may execute. SAFETY_BLOCKED additionally performs the
    # existing Calendar-owned cleanup (hold released, dialog cleared).
    _postgres_only(db)
    client = _client(db, booking=False, actions=False)
    conversation = _conversation(db, client)
    slot = _slot(db, client)
    if boundary_kind == "safety_blocked":
        _seed_confirmation(db, conversation, slot)
    choice = str(slot.id)

    Session = sessionmaker(bind=db.get_bind())
    calls = {"place_hold": 0, "release_hold": 0, "finalize_booking": 0}
    real_place = bc.appointment_hold_service.place_hold
    real_release = bc.appointment_hold_service.release_hold
    real_finalize = bc.booking_service.finalize_booking

    def spy(name, real):
        def wrapper(*args, **kwargs):
            calls[name] += 1
            return real(*args, **kwargs)
        return wrapper

    monkeypatch.setattr(bc.appointment_hold_service, "place_hold",
                        spy("place_hold", real_place))
    monkeypatch.setattr(bc.appointment_hold_service, "release_hold",
                        spy("release_hold", real_release))
    monkeypatch.setattr(bc.booking_service, "finalize_booking",
                        spy("finalize_booking", real_finalize))

    real_handle = bc.handle_booking_action

    def dispatch_seam(*args, **kwargs):
        # The route's first boundary check has ALREADY run and saw NONE.
        # Commit the boundary from a genuinely separate session, THEN run
        # the REAL service (its result is never faked or replaced).
        other = Session()
        try:
            row = other.get(Conversation, conversation.id)
            if boundary_kind == "locked":
                row.abuse_locked_until = (
                    datetime.now(timezone.utc) + timedelta(hours=1)
                )
            elif boundary_kind == "safety_blocked":
                row.lead_is_emergency = True
            else:
                row.final_closed = True
            other.add(row)
            other.commit()
        finally:
            other.close()
        return real_handle(*args, **kwargs)

    monkeypatch.setattr(chat_module, "handle_booking_action", dispatch_seam)

    if boundary_kind == "final_closed":
        response = _call(db, client, conversation.id, choice)
        assert response.meta["mode"] == "final_closed"
        assert "conversation has ended" in response.reply.lower()
    else:
        detail = _raises_409(db, client, conversation.id, choice)
        expected = ("CONVERSATION_LOCKED" if boundary_kind == "locked"
                    else "SAFETY_BLOCKED")
        assert detail["code"] == getattr(
            chat_module, "ACTION_ERROR_CODE_" + expected
        )

    # No action transition executed behind the boundary.
    assert calls["place_hold"] == 0
    assert calls["finalize_booking"] == 0
    if boundary_kind != "safety_blocked":
        assert calls["release_hold"] == 0
    else:
        # The Calendar-owned safety cleanup ran: hold released, cleared.
        db.expire_all()
        db.refresh(slot)
        db.refresh(conversation)
        assert slot.status == SlotStatus.AVAILABLE
        assert slot.held_by_conversation_id is None
        assert conversation.booking_state in (None, BookingState.NONE)
        assert conversation.booking_selected_slot_id is None


# ---------------------------------------------------------------------------
# V2 audit item 5 — explicit ACTION_EXECUTED outcome validation at the route.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("drifted_outcome", [
    types.SimpleNamespace(status="mystery_status", reply=None, user_label=None,
                          boundary=None, calendar_actions=None),
    types.SimpleNamespace(status=bc.ACTION_EXECUTED, reply=None,
                          user_label="Yes \u2014 book it",
                          boundary=None, calendar_actions=None),
    types.SimpleNamespace(status=bc.ACTION_EXECUTED,
                          reply=bc.BookingReply(True, "text", {}),
                          user_label="   ",
                          boundary=None, calendar_actions=None),
])
def test_outcome_drift_fails_closed_through_booking_error(db, monkeypatch,
                                                          drifted_outcome):
    # An unknown status, a missing reply, or an empty server-derived user
    # label must fail CLOSED through the visible booking-error boundary:
    # no false success, no executed-action transcript user row.
    client = _client(db)
    conversation = _conversation(db, client)
    slot = _slot(db, client)
    _seed_selection(db, conversation, [slot])
    monkeypatch.setattr(
        chat_module, "handle_booking_action",
        lambda *args, **kwargs: drifted_outcome,
    )

    response = _call(db, client, conversation.id, str(slot.id))

    assert response.meta.get("mode") == "booking_error"
    rows = _transcript(db, conversation)
    assert rows, "the visible failure reply must be persisted"
    assert rows[-1].role == "assistant"
    assert all(r.role != "user" for r in rows)  # no executed-action label row


# ---------------------------------------------------------------------------
# V2 audit item 4 — per-conversation concurrency reconciliation. Separate
# PostgreSQL sessions; deterministic single-threaded interleaving through
# seams placed exactly at the reconciliation windows (after the internally
# committing service call, before the conversation-row reacquisition).
# ---------------------------------------------------------------------------

def _postgres_only(db):
    if db.get_bind().dialect.name != "postgresql":
        pytest.skip("PostgreSQL-only concurrency semantics")


def _held_slots_for(db, conversation):
    return (
        db.query(AppointmentSlot)
        .filter(AppointmentSlot.held_by_conversation_id == conversation.id)
        .filter(AppointmentSlot.status == SlotStatus.HELD)
        .all()
    )


def _notification_rows_for_conversation(db, client, conversation):
    """V4.3 (owner full-suite evidence — test-isolation correction): the
    disposable database is session-scoped for the WHOLE pytest run, so
    NotificationAttempt is a SHARED ledger and any global table count sees
    legitimate rows left by earlier, unrelated notification tests.
    Ownership is therefore established through the appointments THIS
    conversation created (tenant-scoped, cancelled included): only ledger
    rows attached to those appointment ids belong to the current test.
    Reads only; never truncates or mutates the shared ledger."""
    from app.calendar_models import Appointment
    appointment_ids = [
        row.id
        for row in db.query(Appointment)
        .filter(
            Appointment.client_id == client.id,
            Appointment.conversation_id == conversation.id,
        )
        .all()
    ]
    if not appointment_ids:
        return []
    return (
        db.query(NotificationAttempt)
        .filter(NotificationAttempt.appointment_id.in_(appointment_ids))
        .all()
    )


def test_race_a_different_slot_selections_single_surviving_hold(db, monkeypatch):
    # Race A: request A (slot 1) commits its hold, then request B (slot 2)
    # runs to completion inside A's reconciliation window. Invariants:
    # exactly one surviving active hold; booking_selected_slot_id equals
    # that surviving hold; the loser's hold is released; both responses
    # are truthful; the winner's state is not overwritten.
    _postgres_only(db)
    client = _client(db)
    conversation = _conversation(db, client)
    slot_one = _slot(db, client, hour=10)
    slot_two = _slot(db, client, hour=15)
    _seed_selection(db, conversation, [slot_one, slot_two])

    Session = sessionmaker(bind=db.get_bind())
    real_lock = bc._lock_conversation_row
    seam_state = {"fired": False, "b_reply": None}

    def seam(db_arg, client_arg, conversation_arg):
        if not seam_state["fired"] and db_arg is db:
            seam_state["fired"] = True
            other = Session()
            try:
                response_b = chat_module.chat(
                    _action_request(client, conversation.id, str(slot_two.id),
                                    message="3:00 PM"),
                    _FakeRequest(), other,
                )
                seam_state["b_reply"] = response_b.reply
            finally:
                other.close()
        return real_lock(db_arg, client_arg, conversation_arg)

    monkeypatch.setattr(bc, "_lock_conversation_row", seam)

    response_a = _call(db, client, conversation.id, str(slot_one.id),
                       message="10:00 AM")

    assert seam_state["fired"], "the interleaving seam must have run"
    db.expire_all()
    db.refresh(conversation)
    db.refresh(slot_one)
    db.refresh(slot_two)
    # Invariant: exactly one surviving active hold, and it is the winner's.
    held = _held_slots_for(db, conversation)
    assert [h.id for h in held] == [slot_two.id]
    assert conversation.booking_selected_slot_id == slot_two.id
    assert conversation.booking_state == BookingState.WAITING_FOR_CONFIRMATION
    assert slot_one.status == SlotStatus.AVAILABLE  # loser hold released
    assert slot_one.held_by_conversation_id is None
    # Both responses truthful: B won and was asked to confirm slot two; A
    # lost and was shown the SURVIVING pending confirmation (same slot-two
    # time), never a confirmation of slot one.
    two_time = bc._fmt_time(slot_two.start_datetime, "America/New_York")
    one_time = bc._fmt_time(slot_one.start_datetime, "America/New_York")
    assert "To confirm:" in seam_state["b_reply"]
    assert two_time in seam_state["b_reply"]
    assert "To confirm:" in response_a.reply
    assert two_time in response_a.reply
    assert one_time not in response_a.reply
    # V4.3: ownership-scoped — no ledger rows may belong to THIS test's
    # conversation (the shared table may hold earlier tests' rows).
    assert _notification_rows_for_conversation(db, client, conversation) == []


def test_race_b_confirm_no_wins_before_confirm_yes_finalizes(db, monkeypatch):
    # Race B, forced order 1: confirm-no completes inside confirm-yes's
    # window (before finalize_booking runs). Invariants: no appointment;
    # the confirm-no restart (WAITING_FOR_DATE) is preserved, never
    # overwritten by the yes-loser; no stale held slot; no notification
    # claims; the yes-loser's reply is truthful for the surviving state.
    _postgres_only(db)
    client = _client(db)
    conversation = _conversation(db, client)
    slot = _slot(db, client)
    _seed_confirmation(db, conversation, slot)

    Session = sessionmaker(bind=db.get_bind())
    real_finalize = bc.booking_service.finalize_booking
    seam_state = {"fired": False, "no_reply": None}

    def seam(*args, **kwargs):
        if not seam_state["fired"]:
            seam_state["fired"] = True
            other = Session()
            try:
                response_no = chat_module.chat(
                    _action_request(client, conversation.id,
                                    bc.CONFIRM_NO_CHOICE_PREFIX + str(slot.id),
                                    message="No"),
                    _FakeRequest(), other,
                )
                seam_state["no_reply"] = response_no.reply
            finally:
                other.close()
        return real_finalize(*args, **kwargs)

    monkeypatch.setattr(bc.booking_service, "finalize_booking", seam)

    response_yes = _call(
        db, client, conversation.id,
        bc.CONFIRM_YES_CHOICE_PREFIX + str(slot.id), message="Yes",
    )

    assert seam_state["fired"]
    db.expire_all()
    db.refresh(conversation)
    db.refresh(slot)
    from app.calendar_models import Appointment
    active = (
        db.query(Appointment)
        .filter(Appointment.conversation_id == conversation.id)
        .filter(Appointment.status != "cancelled")
        .count()
    )
    assert active == 0
    # The confirm-no winner's state survives; the yes-loser preserved it.
    assert conversation.booking_state == BookingState.WAITING_FOR_DATE
    assert conversation.booking_selected_slot_id is None
    assert _held_slots_for(db, conversation) == []  # no stale hold
    assert slot.status == SlotStatus.AVAILABLE
    assert "what day would work better" in seam_state["no_reply"]
    assert "What day would work" in response_yes.reply  # truthful, no booking
    assert "All set" not in response_yes.reply
    assert response_yes.meta.get("booked") is not True
    # V4.3: ownership-scoped — no ledger rows may belong to THIS test's
    # conversation (the shared table may hold earlier tests' rows).
    assert _notification_rows_for_conversation(db, client, conversation) == []


def test_race_b_confirm_yes_wins_before_confirm_no_releases(db, monkeypatch):
    # Race B, forced order 2: confirm-yes books inside confirm-no's window
    # (before release_hold runs). Invariants: exactly one appointment; the
    # no-loser restates the EXISTING appointment (never "what day would
    # work better?" next to a live appointment); terminal cleared state;
    # no duplicate notification claims beyond the single booking's own.
    _postgres_only(db)
    client = _client(db)
    conversation = _conversation(db, client)
    slot = _slot(db, client)
    _seed_confirmation(db, conversation, slot)

    Session = sessionmaker(bind=db.get_bind())
    real_release = bc.appointment_hold_service.release_hold
    seam_state = {"fired": False, "yes_reply": None, "ledger_after_yes": None}

    def seam(db_arg, *args, **kwargs):
        if not seam_state["fired"] and db_arg is db:
            seam_state["fired"] = True
            other = Session()
            try:
                response_yes = chat_module.chat(
                    _action_request(client, conversation.id,
                                    bc.CONFIRM_YES_CHOICE_PREFIX + str(slot.id),
                                    message="Yes"),
                    _FakeRequest(), other,
                )
                seam_state["yes_reply"] = response_yes.reply
                seam_state["ledger_after_yes"] = (
                    other.query(NotificationAttempt).count()
                )
            finally:
                other.close()
        return real_release(db_arg, *args, **kwargs)

    monkeypatch.setattr(bc.appointment_hold_service, "release_hold", seam)

    response_no = _call(
        db, client, conversation.id,
        bc.CONFIRM_NO_CHOICE_PREFIX + str(slot.id), message="No",
    )

    assert seam_state["fired"]
    db.expire_all()
    db.refresh(conversation)
    db.refresh(slot)
    from app.calendar_models import Appointment
    active = (
        db.query(Appointment)
        .filter(Appointment.conversation_id == conversation.id)
        .filter(Appointment.status != "cancelled")
        .count()
    )
    assert active == 1
    assert slot.status == SlotStatus.BOOKED
    assert conversation.booking_state in (None, BookingState.NONE)
    assert conversation.booking_selected_slot_id is None
    assert "already have an appointment" in response_no.reply
    assert "what day would work better" not in response_no.reply
    # No notification claims from the losing confirm-no: the ledger is
    # exactly what the single successful booking wrote.
    assert db.query(NotificationAttempt).count() == seam_state["ledger_after_yes"]


def test_text_path_sequential_confirm_no_unchanged(db):
    # Shared-owner guarantee, non-race direction: the ordinary sequential
    # TEXT "no" in confirmation keeps its exact pre-V2 behavior.
    client = _client(db)
    conversation = _conversation(db, client)
    slot = _slot(db, client)
    _seed_confirmation(db, conversation, slot)

    reply = bc.handle_booking_message(db, client, conversation, "no")

    assert reply.handled is True
    assert reply.text == "No problem \u2014 what day would work better?"
    db.refresh(conversation)
    db.refresh(slot)
    assert conversation.booking_state == BookingState.WAITING_FOR_DATE
    assert conversation.booking_selected_slot_id is None
    assert slot.status == SlotStatus.AVAILABLE


def test_text_path_confirm_no_after_booking_restates(db):
    # Shared-owner guarantee, race direction: the SAME reconciliation
    # protects the TEXT transition owner. With this conversation's
    # appointment already booked (a concurrent confirm-yes won), a text
    # "no" must restate the appointment — never restart at the day
    # question next to a live appointment.
    client = _client(db)
    conversation = _conversation(db, client)
    slot = _slot(db, client)
    _seed_confirmation(db, conversation, slot)

    from app.calendar_models import Appointment
    appointment = Appointment(
        id=uuid.uuid4(),
        client_id=client.id,
        slot_id=slot.id,
        conversation_id=conversation.id,
        patient_name="Casey Patient",
        patient_phone="5165551234",
        start_datetime=slot.start_datetime,
        end_datetime=slot.end_datetime,
        status="pending",
    )
    slot.status = SlotStatus.BOOKED
    slot.held_until = None
    db.add(appointment)
    db.add(slot)
    db.commit()

    reply = bc.handle_booking_message(db, client, conversation, "no")

    assert reply.handled is True
    assert "already have an appointment" in reply.text
    assert "what day would work better" not in reply.text
    db.refresh(conversation)
    assert conversation.booking_state in (None, BookingState.NONE)
    assert conversation.booking_selected_slot_id is None


def test_reacquisition_observes_change_during_place_hold(db, monkeypatch):
    # Reacquisition proof at the selection owner: a concurrent session
    # changes the conversation DURING the internally committing place_hold.
    # The reconciliation must observe the newer row before any mutation:
    # the request becomes a loser, releases its own hold, and the stale
    # ORM snapshot never overwrites the newer state.
    _postgres_only(db)
    client = _client(db)
    conversation = _conversation(db, client)
    slot = _slot(db, client)
    _seed_selection(db, conversation, [slot])

    Session = sessionmaker(bind=db.get_bind())
    real_place_hold = bc.appointment_hold_service.place_hold
    seam_state = {"fired": False}

    def seam(*args, **kwargs):
        result = real_place_hold(*args, **kwargs)
        if not seam_state["fired"]:
            seam_state["fired"] = True
            other = Session()
            try:
                row = other.get(Conversation, conversation.id)
                row.booking_state = BookingState.WAITING_FOR_DATE
                row.booking_offered_slot_ids = None
                row.booking_offer_expires_at = None
                row.booking_effective_time_preference = None
                other.add(row)
                other.commit()
            finally:
                other.close()
        return result

    monkeypatch.setattr(bc.appointment_hold_service, "place_hold", seam)

    response = _call(db, client, conversation.id, str(slot.id))

    assert seam_state["fired"]
    assert "What day would work" in response.reply  # truthful for new state
    db.expire_all()
    db.refresh(conversation)
    db.refresh(slot)
    # The newer row survived; the stale snapshot did not win.
    assert conversation.booking_state == BookingState.WAITING_FOR_DATE
    assert conversation.booking_selected_slot_id is None
    assert conversation.booking_offered_slot_ids in (None, [])
    # The request's own transient hold was released again.
    assert slot.status == SlotStatus.AVAILABLE
    assert slot.held_by_conversation_id is None


def test_reacquisition_observes_change_during_finalize(db, monkeypatch):
    # Reacquisition proof at the finalize owner: the conversation changes
    # DURING the internally committing finalize_booking (which reports
    # hold_lost). The failure recovery must observe the newer row and
    # preserve it — no blind re-offer over the concurrent state, no stale
    # overwrite.
    _postgres_only(db)
    client = _client(db)
    conversation = _conversation(db, client)
    slot = _slot(db, client)
    _seed_confirmation(db, conversation, slot)

    Session = sessionmaker(bind=db.get_bind())

    def seam(*args, **kwargs):
        other = Session()
        try:
            row = other.get(Conversation, conversation.id)
            row.booking_state = BookingState.WAITING_FOR_DATE
            row.booking_selected_slot_id = None
            row.booking_offered_slot_ids = None
            row.booking_offer_expires_at = None
            row.booking_effective_time_preference = None
            other.add(row)
            other.commit()
        finally:
            other.close()
        return BookingResult(success=False, reason="hold_lost")

    monkeypatch.setattr(bc.booking_service, "finalize_booking", seam)

    response = _call(
        db, client, conversation.id,
        bc.CONFIRM_YES_CHOICE_PREFIX + str(slot.id), message="Yes",
    )

    assert "What day would work" in response.reply
    db.expire_all()
    db.refresh(conversation)
    assert conversation.booking_state == BookingState.WAITING_FOR_DATE
    assert conversation.booking_selected_slot_id is None
    # V4.3: ownership-scoped — no ledger rows may belong to THIS test's
    # conversation (the shared table may hold earlier tests' rows).
    assert _notification_rows_for_conversation(db, client, conversation) == []


# ---------------------------------------------------------------------------
# V3 audit item 1 — FAILED place_hold reconciliation. A failed hold must
# never blindly re-offer over a concurrent request's newer state; the
# sequential failed hold keeps its exact pre-V3 re-offer behavior.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("failure_kind", ["taken", "ineligible"])
def test_race_a_failed_hold_preserves_survivor_confirmation(db, monkeypatch,
                                                            failure_kind):
    # Request B enters from a stale WAITING_FOR_SLOT_SELECTION snapshot;
    # request A advances to a live WAITING_FOR_CONFIRMATION inside B's
    # window; B's different slot then FAILS to hold ("taken" = live
    # foreign hold; "ineligible" = current policy rejects the past start
    # — both traverse the same failed-hold branch). Invariants: A's
    # selected slot and live hold survive; B rewrites nothing; B's reply
    # truthfully restates A's confirmation; no orphaned hold exists.
    _postgres_only(db)
    client = _client(db)
    conversation = _conversation(db, client)
    slot_one = _slot(db, client, hour=10)
    slot_two = _slot(db, client, hour=15)
    _seed_selection(db, conversation, [slot_one, slot_two])

    Session = sessionmaker(bind=db.get_bind())
    real_place_hold = bc.appointment_hold_service.place_hold
    seam_state = {"fired": False, "a_reply": None, "b_hold_success": None}

    def seam(db_arg, *args, **kwargs):
        first_b_call = (not seam_state["fired"]) and db_arg is db
        if first_b_call:
            seam_state["fired"] = True
            other = Session()
            try:
                response_a = chat_module.chat(
                    _action_request(client, conversation.id, str(slot_one.id),
                                    message="6:00 AM"),
                    _FakeRequest(), other,
                )
                seam_state["a_reply"] = response_a.reply
            finally:
                other.close()
            mutator = Session()
            try:
                row = mutator.get(AppointmentSlot, slot_two.id)
                if failure_kind == "taken":
                    row.status = SlotStatus.HELD
                    row.held_by_conversation_id = uuid.uuid4()
                    row.held_until = (
                        datetime.now(timezone.utc) + timedelta(minutes=5)
                    )
                else:
                    past = datetime.now(timezone.utc) - timedelta(days=2)
                    row.start_datetime = past
                    row.end_datetime = past + timedelta(minutes=30)
                mutator.add(row)
                mutator.commit()
            finally:
                mutator.close()
        result = real_place_hold(db_arg, *args, **kwargs)
        if first_b_call:
            seam_state["b_hold_success"] = result.success
        return result

    monkeypatch.setattr(bc.appointment_hold_service, "place_hold", seam)

    response_b = _call(db, client, conversation.id, str(slot_two.id),
                       message="11:00 AM")

    assert seam_state["fired"]
    # The premise must actually hold: B's place_hold FAILED (either kind).
    assert seam_state["b_hold_success"] is False
    db.expire_all()
    db.refresh(conversation)
    db.refresh(slot_one)
    db.refresh(slot_two)
    # A's confirmation survives untouched; B rewrote nothing.
    assert conversation.booking_state == BookingState.WAITING_FOR_CONFIRMATION
    assert conversation.booking_selected_slot_id == slot_one.id
    assert conversation.booking_offered_slot_ids in (None, [])
    held = _held_slots_for(db, conversation)
    assert [h.id for h in held] == [slot_one.id]  # survivor; no orphan
    assert slot_one.held_by_conversation_id == conversation.id
    if failure_kind == "taken":
        assert slot_two.status == SlotStatus.HELD  # foreign hold untouched
        assert slot_two.held_by_conversation_id != conversation.id
    # Both replies truthful: A was asked to confirm slot one; B was shown
    # the SURVIVING confirmation — never an apology-plus-re-offer, never a
    # rewritten offer.
    one_time = bc._fmt_time(slot_one.start_datetime, "America/New_York")
    assert "To confirm:" in seam_state["a_reply"]
    assert one_time in seam_state["a_reply"]
    assert "To confirm:" in response_b.reply
    assert one_time in response_b.reply
    assert "just taken" not in response_b.reply
    assert "no longer available" not in response_b.reply
    assert "Which works best?" not in response_b.reply
    # V4.3: ownership-scoped — no ledger rows may belong to THIS test's
    # conversation (the shared table may hold earlier tests' rows).
    assert _notification_rows_for_conversation(db, client, conversation) == []


def test_sequential_failed_hold_still_reoffers(db, monkeypatch):
    # V3 regression guard: WITHOUT any concurrent state change, a failed
    # hold keeps its exact pre-V3 behavior — the truthful "just taken"
    # apology plus a fresh same-day re-offer, still awaiting a selection.
    client = _client(db)
    conversation = _conversation(db, client)
    slot = _slot(db, client, hour=14)
    replacement = _slot(db, client, hour=17)
    _seed_selection(db, conversation, [slot])
    conversation.booking_preferred_date = (
        (datetime.now(timezone.utc) + timedelta(days=2)).date().isoformat()
    )
    db.add(conversation)
    # The chosen slot is snatched by a FOREIGN conversation before the tap.
    slot.status = SlotStatus.HELD
    slot.held_by_conversation_id = uuid.uuid4()
    slot.held_until = datetime.now(timezone.utc) + timedelta(minutes=5)
    db.add(slot)
    db.commit()
    monkeypatch.setattr(
        bc.availability_service, "get_available_slots",
        lambda *args, **kwargs: [replacement],
    )

    response = _call(db, client, conversation.id, str(slot.id))

    assert "just taken" in response.reply
    assert "Which works best?" in response.reply
    db.refresh(conversation)
    assert conversation.booking_state == BookingState.WAITING_FOR_SLOT_SELECTION
    assert conversation.booking_offered_slot_ids == [str(replacement.id)]
    assert conversation.booking_selected_slot_id is None
