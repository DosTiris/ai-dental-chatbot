# calendar_tests/test_symptom_name_continuation.py
#
# S10 — Defect 1: ordinary dental symptom continuation.
#
# Reuses the real-endpoint harness from test_chat_integration.py (builders,
# recording fakes, send helper) so every test drives the actual chat() flow
# against PostgreSQL with no network boundary.
#
# Staging regression these tests pin:
#   "I have severe tooth pain and swelling"
#     -> symptom safety guidance + "What's your first name?"
#   "Kyle"
#     -> (was) "Just to confirm, would you like to schedule an appointment
#        for swelling / possible infection?" with the name DISCARDED
#     -> (now) "Thanks Kyle! What's the best phone number to reach you?"
#
# Root cause: the service-offer clarification owner claimed a message that
# ANSWERED a pending first-name question, and returned without persisting
# anything. The approved repair (S10 decision: Option A) is an owner-ordering
# fix — when the previous assistant response asked for a name, the existing
# name-capture owner takes the message instead.
#
# Approved flow (supersedes the STAGING_FINDINGS.md wording that expected an
# intermediate "yes" confirmation turn):
#     symptom -> name -> phone
#
# These tests assert externally observable behavior only. They deliberately do
# NOT pin the known last_assistant_offered_scheduling_service() over-matching,
# which is recorded as deferred drift and may be repaired later.

import pytest

from app.models import Message

# Importing these registers the autouse `fakes` fixture in this module too.
from calendar_tests.test_chat_integration import (  # noqa: F401
    fakes,
    make_client,
    make_conversation,
    send,
)

# The exact staging opener and the exact staging name reply.
SYMPTOM_OPENER = "I have severe tooth pain and swelling"
NAME_REPLY = "Kyle"

# The safety sentence carried by build_symptom_appointment_start_reply() for
# swelling / bleeding / infection symptoms. Repeating it was the visible
# defect, so its occurrence count is the regression signal.
SAFETY_SENTENCE = "seek urgent care right away"

# A GENUINE service offer: it names a service and offers scheduling, and it
# asks NO name question. This is the case that must keep today's behavior.
GENUINE_SERVICE_OFFER = (
    "Yes \u2014 we offer dental implants. Would you like to schedule a consultation?"
)


def empty_lead_conversation(db, client):
    """A brand-new conversation with no captured lead fields at all.

    make_conversation() otherwise seeds a lead that is one answer away from
    completion, which would skip the reason and name stages these tests need.
    """
    return make_conversation(
        db,
        client,
        lead_reason=None,
        lead_name=None,
        lead_phone=None,
        lead_time_window=None,
        lead_email_opt_out=False,
        lead_is_new_patient=None,
    )


def assistant_messages(db, conversation):
    return [
        m.content or ""
        for m in db.query(Message)
        .filter(
            Message.conversation_id == conversation.id,
            Message.role == "assistant",
        )
        .order_by(Message.created_at.asc())
        .all()
    ]


def seed_assistant_message(db, conversation, content):
    db.add(
        Message(
            conversation_id=conversation.id,
            role="assistant",
            content=content,
        )
    )
    db.commit()


# ---------------------------------------------------------------------------
# 1-3: the exact staging transcript now advances to the phone step
# ---------------------------------------------------------------------------

def test_exact_staging_transcript_symptom_then_name_then_phone(db, fakes):
    """The literal two-message staging transcript. Turn 1 asks for the first
    name; turn 2 answers it and must advance straight to the phone step."""
    client = make_client(db)
    conversation = empty_lead_conversation(db, client)

    first = send(db, client, conversation, SYMPTOM_OPENER)

    assert "first name" in first.reply.lower()
    assert SAFETY_SENTENCE in first.reply

    second = send(db, client, conversation, NAME_REPLY)

    # The name-capture owner produced this turn, not the clarification owner.
    assert second.meta.get("mode") == "bypass"
    assert "best phone number" in second.reply


def test_lead_name_persisted_immediately_after_name_turn(db, fakes):
    """The name must be persisted on the turn it is typed. Previously the
    clarification owner returned early and stored nothing at all."""
    client = make_client(db)
    conversation = empty_lead_conversation(db, client)

    send(db, client, conversation, SYMPTOM_OPENER)
    assert (conversation.lead_name or "") == ""

    send(db, client, conversation, NAME_REPLY)

    assert conversation.lead_name == "Kyle"
    assert NAME_REPLY in (
        getattr(conversation, "lead_name_source_text", "") or ""
    )


def test_phone_prompt_includes_captured_name(db, fakes):
    """The phone question addresses the patient by the name just captured."""
    client = make_client(db)
    conversation = empty_lead_conversation(db, client)

    send(db, client, conversation, SYMPTOM_OPENER)
    second = send(db, client, conversation, NAME_REPLY)

    assert "Kyle" in second.reply
    assert "phone number" in second.reply.lower()


# ---------------------------------------------------------------------------
# 4: the symptom safety introduction is never repeated
# ---------------------------------------------------------------------------

def test_symptom_safety_introduction_appears_exactly_once(db, fakes):
    """The visible staging defect was the safety guidance being re-emitted on
    a later turn. Across the whole conversation it must appear exactly once."""
    client = make_client(db)
    conversation = empty_lead_conversation(db, client)

    send(db, client, conversation, SYMPTOM_OPENER)
    send(db, client, conversation, NAME_REPLY)
    send(db, client, conversation, "516-555-0143")

    occurrences = sum(
        1 for content in assistant_messages(db, conversation)
        if SAFETY_SENTENCE in content
    )
    assert occurrences == 1, (
        f"symptom safety introduction emitted {occurrences} times; expected 1"
    )


# ---------------------------------------------------------------------------
# 5: urgent classification and the original reason source survive
# ---------------------------------------------------------------------------

def test_priority_and_reason_source_survive_continuation(db, fakes):
    """Capturing the name must not reclassify the lead or overwrite the
    original symptom text the office needs to see."""
    client = make_client(db)
    conversation = empty_lead_conversation(db, client)

    send(db, client, conversation, SYMPTOM_OPENER)
    reason_after_opener = conversation.lead_reason
    source_after_opener = getattr(conversation, "lead_reason_source_text", None)

    assert bool(getattr(conversation, "lead_is_priority", False)) is True
    assert bool(getattr(conversation, "lead_is_emergency", False)) is False
    assert (reason_after_opener or "").strip() != ""
    assert SYMPTOM_OPENER in (source_after_opener or "")

    send(db, client, conversation, NAME_REPLY)

    # Unchanged by the name turn.
    assert conversation.lead_reason == reason_after_opener
    assert getattr(conversation, "lead_reason_source_text", None) == source_after_opener
    assert bool(getattr(conversation, "lead_is_priority", False)) is True
    assert bool(getattr(conversation, "lead_is_emergency", False)) is False


# ---------------------------------------------------------------------------
# 6: the spurious clarification turn is gone from this flow
# ---------------------------------------------------------------------------

def test_no_spurious_just_to_confirm_turn(db, fakes):
    """No service-offer clarification may appear anywhere in the symptom
    flow — the previous turn asked for a name, not for a service."""
    client = make_client(db)
    conversation = empty_lead_conversation(db, client)

    send(db, client, conversation, SYMPTOM_OPENER)
    send(db, client, conversation, NAME_REPLY)

    for content in assistant_messages(db, conversation):
        assert "Just to confirm" not in content


# ---------------------------------------------------------------------------
# 7: a GENUINE service offer keeps today's clarification behavior
# ---------------------------------------------------------------------------

def test_genuine_service_offer_clarification_unchanged(db, fakes):
    """The narrow S10 condition must not disable the clarification owner for
    a real service offer, which contains no name question."""
    client = make_client(db)
    conversation = empty_lead_conversation(db, client)
    seed_assistant_message(db, conversation, GENUINE_SERVICE_OFFER)

    resp = send(db, client, conversation, "Bakri")

    assert resp.meta.get("mode") == "service_offer_clarification"
    assert "Just to confirm" in resp.reply
    # Unchanged contract: the clarification owner persists nothing.
    assert (conversation.lead_name or "") == ""


# ---------------------------------------------------------------------------
# 8: the ordinary EMERGENCY tier also advances instead of clarifying
# ---------------------------------------------------------------------------

def test_ordinary_emergency_name_capture_advances(db, fakes):
    """The emergency reply also carries service aliases (it lists "swelling",
    "broken tooth", "knocked-out tooth"), so it hit the same defect. Its name
    turn must reach the existing emergency follow-up owner."""
    client = make_client(db)
    conversation = empty_lead_conversation(db, client)

    first = send(db, client, conversation, "I knocked out my tooth")
    assert first.meta.get("mode") == "emergency_booking_mode"
    assert "first name" in first.reply.lower()
    assert conversation.final_closed is not True

    second = send(db, client, conversation, "Kevin")

    assert second.meta.get("mode") == "emergency_followup_intake"
    assert conversation.lead_name == "Kevin"
    assert "phone number" in second.reply.lower()
    assert "Just to confirm" not in second.reply
