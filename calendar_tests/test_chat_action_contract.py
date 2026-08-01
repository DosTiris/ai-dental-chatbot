# calendar_tests/test_chat_action_contract.py
#
# C1-B structured /chat action-transport contract.
#
# Scope: request validation, tenant/conversation fail-closed routing, and
# proof that transport-only actions do not create messages, conversations,
# Calendar state, holds, appointments, or notifications.

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import app.routes.chat as chat_module
from app.models import Client, Conversation, Message
from app.schemas import (
    CHAT_ACTION_CHOICE_ID_MAX_CHARS,
    ChatAction,
    ChatRequest,
)


class _FakeAddr:
    host = "127.0.0.1"


class _FakeRequest:
    client = _FakeAddr()


def _client(db, *, practice_name="C1-B Test Dental"):
    row = Client(
        id=uuid.uuid4(),
        practice_name=practice_name,
        api_key=f"key-{uuid.uuid4()}",
        active=True,
        settings={
            "timezone": "America/New_York",
            "booking_mode": "capture_first",
            "calendar": {
                "booking_enabled": False,
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
        "visitor_id": "c1b-visitor",
        "is_lead": False,
        "lead_status": "new",
    }
    values.update(overrides)
    row = Conversation(**values)
    db.add(row)
    db.commit()
    return row


def _action_request(
    client,
    conversation_id,
    *,
    message="Saturday, August 1",
):
    return ChatRequest(
        message=message,
        client_key=client.api_key,
        visitor_id="c1b-visitor",
        conversation_id=str(conversation_id),
        action={
            "type": "calendar_choice",
            "choice_id": "opaque-choice-123",
        },
    )


def test_existing_message_only_request_remains_valid():
    request = ChatRequest(
        message="I need an appointment",
        client_key="office-key",
        visitor_id="visitor-1",
        conversation_id=None,
    )

    assert request.action is None
    assert request.message == "I need an appointment"
    assert request.client_key == "office-key"


def test_valid_calendar_action_is_strict_and_trimmed():
    action = ChatAction(
        type="calendar_choice",
        choice_id="  opaque-choice-123  ",
    )

    assert action.type == "calendar_choice"
    assert action.choice_id == "opaque-choice-123"
    assert action.model_dump() == {
        "type": "calendar_choice",
        "choice_id": "opaque-choice-123",
    }


@pytest.mark.parametrize(
    "action",
    [
        {"type": "book_now", "choice_id": "opaque"},
        {"type": "calendar_choice", "choice_id": "   "},
        {
            "type": "calendar_choice",
            "choice_id": "opaque",
            "slot_id": "raw-slot-id",
        },
        {
            "type": "calendar_choice",
            "choice_id": "x" * (CHAT_ACTION_CHOICE_ID_MAX_CHARS + 1),
        },
    ],
)
def test_invalid_or_overbroad_action_is_rejected(action):
    with pytest.raises(ValidationError):
        ChatAction.model_validate(action)


def test_action_requires_existing_conversation_id_in_request():
    with pytest.raises(ValidationError):
        ChatRequest(
            message="Saturday, August 1",
            client_key="office-key",
            conversation_id=None,
            action={
                "type": "calendar_choice",
                "choice_id": "opaque-choice-123",
            },
        )


def test_action_transport_rejects_without_mutating_existing_conversation(db):
    client = _client(db)
    conversation = _conversation(
        db,
        client,
        booking_state="waiting_for_date",
        booking_preferred_date="2026-08-01",
        booking_offered_slot_ids=["legacy-slot-id"],
    )

    before_messages = db.query(Message).count()
    before_conversations = db.query(Conversation).count()
    before_state = {
        "booking_state": conversation.booking_state,
        "booking_preferred_date": conversation.booking_preferred_date,
        "booking_offered_slot_ids": list(
            conversation.booking_offered_slot_ids or []
        ),
        "booking_selected_slot_id": conversation.booking_selected_slot_id,
    }

    with pytest.raises(HTTPException) as exc:
        chat_module.chat(
            _action_request(client, conversation.id),
            _FakeRequest(),
            db,
        )

    assert exc.value.status_code == 409
    assert exc.value.detail == chat_module.STRUCTURED_ACTION_NOT_ACTIVE_DETAIL

    db.refresh(conversation)
    assert db.query(Message).count() == before_messages
    assert db.query(Conversation).count() == before_conversations
    assert conversation.booking_state == before_state["booking_state"]
    assert (
        conversation.booking_preferred_date
        == before_state["booking_preferred_date"]
    )
    assert (
        conversation.booking_offered_slot_ids
        == before_state["booking_offered_slot_ids"]
    )
    assert (
        conversation.booking_selected_slot_id
        == before_state["booking_selected_slot_id"]
    )


def test_action_for_other_tenant_does_not_create_replacement_conversation(db):
    client_a = _client(db, practice_name="Tenant A")
    client_b = _client(db, practice_name="Tenant B")
    conversation_b = _conversation(db, client_b)

    before_messages = db.query(Message).count()
    before_conversations = db.query(Conversation).count()

    with pytest.raises(HTTPException) as exc:
        chat_module.chat(
            _action_request(client_a, conversation_b.id),
            _FakeRequest(),
            db,
        )

    assert exc.value.status_code == 409
    assert exc.value.detail == chat_module.STRUCTURED_ACTION_STALE_DETAIL
    assert db.query(Message).count() == before_messages
    assert db.query(Conversation).count() == before_conversations


def test_action_for_unknown_conversation_does_not_create_conversation(db):
    client = _client(db)
    before_messages = db.query(Message).count()
    before_conversations = db.query(Conversation).count()

    with pytest.raises(HTTPException) as exc:
        chat_module.chat(
            _action_request(client, uuid.uuid4()),
            _FakeRequest(),
            db,
        )

    assert exc.value.status_code == 409
    assert exc.value.detail == chat_module.STRUCTURED_ACTION_STALE_DETAIL
    assert db.query(Message).count() == before_messages
    assert db.query(Conversation).count() == before_conversations


def test_final_closed_contract_precedes_transport_rejection(db):
    client = _client(db)
    conversation = _conversation(db, client, final_closed=True)
    before_messages = db.query(Message).count()

    response = chat_module.chat(
        _action_request(client, conversation.id),
        _FakeRequest(),
        db,
    )

    assert response.meta["mode"] == "final_closed"
    assert response.meta["show_start_over"] is True
    assert "conversation has ended" in response.reply.lower()
    assert response.conversation_id == str(conversation.id)
    assert db.query(Message).count() == before_messages

def test_action_with_malformed_conversation_id_is_stale_without_mutation(db):
    client = _client(db)
    before_messages = db.query(Message).count()
    before_conversations = db.query(Conversation).count()

    with pytest.raises(HTTPException) as exc:
        chat_module.chat(
            _action_request(client, "not-a-valid-uuid"),
            _FakeRequest(),
            db,
        )

    assert exc.value.status_code == 409
    assert exc.value.detail == chat_module.STRUCTURED_ACTION_STALE_DETAIL
    assert db.query(Message).count() == before_messages
    assert db.query(Conversation).count() == before_conversations


def test_locked_conversation_action_pins_c1b_fail_closed_precedence(db):
    client = _client(db)
    locked_until = datetime.now(timezone.utc) + timedelta(days=1)
    conversation = _conversation(
        db,
        client,
        abuse_strikes=3,
        abuse_locked_until=locked_until,
    )
    before_messages = db.query(Message).count()
    before_conversations = db.query(Conversation).count()

    # C1-B transports but never executes actions. This test records the
    # deliberate transport-only ordering: an action fails closed before the
    # normal locked-conversation reply. C1-C must resolve guard precedence
    # before any structured action is allowed to execute.
    with pytest.raises(HTTPException) as exc:
        chat_module.chat(
            _action_request(client, conversation.id),
            _FakeRequest(),
            db,
        )

    assert exc.value.status_code == 409
    assert exc.value.detail == chat_module.STRUCTURED_ACTION_NOT_ACTIVE_DETAIL

    db.refresh(conversation)
    assert db.query(Message).count() == before_messages
    assert db.query(Conversation).count() == before_conversations
    assert conversation.abuse_strikes == 3
    assert chat_module.conversation_is_locked(conversation) is True


def test_emergency_text_with_action_pins_c1b_deferred_safety_ordering(db):
    client = _client(db)
    conversation = _conversation(db, client)
    before_messages = db.query(Message).count()
    before_conversations = db.query(Conversation).count()

    # This is a recorded C1-B deferral, not the future execution contract.
    # C1-C must resolve final-closed, locked, misconduct, obscenity, and
    # emergency boundaries before executing any structured Calendar action.
    with pytest.raises(HTTPException) as exc:
        chat_module.chat(
            _action_request(
                client,
                conversation.id,
                message="I can't breathe",
            ),
            _FakeRequest(),
            db,
        )

    assert exc.value.status_code == 409
    assert exc.value.detail == chat_module.STRUCTURED_ACTION_NOT_ACTIVE_DETAIL

    db.refresh(conversation)
    assert db.query(Message).count() == before_messages
    assert db.query(Conversation).count() == before_conversations
    assert conversation.final_closed is False
    assert conversation.booking_state in (None, "none")
