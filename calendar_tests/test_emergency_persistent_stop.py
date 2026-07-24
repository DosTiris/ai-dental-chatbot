# calendar_tests/test_emergency_persistent_stop.py
#
# S4 — persistent life-threatening emergency stop.
#
# Reuses the real-endpoint harness from test_chat_integration.py (builders,
# recording fakes, send helper) so every test drives the actual chat() flow
# against PostgreSQL with no network boundary.
#
# What S4 adds (and these tests prove): every life-threatening emergency
# path now persists conversation.final_closed = True, so later messages hit
# the existing top-level final_closed guard and cannot resume intake or
# booking, mutate lead fields, hold slots, or trigger notifications. The
# existing Start Over action (a fresh conversation) remains the only way to
# continue. Ordinary dental emergencies stay open with their contact prompt.

import pytest

import app.routes.chat as chat_module
from app.calendar_models import BookingState, SlotStatus

# Importing these registers the autouse `fakes` fixture in this module too.
from calendar_tests.test_chat_integration import (  # noqa: F401
    OPEN_ALL_WEEK_HOURS,
    fakes,
    make_client,
    make_conversation,
    refreshed_slot,
    seed_active_confirmation,
    send,
)

LIFE_THREATENING_TEXT = "I have uncontrolled bleeding from my mouth"
BLOCKED_REPLY = "This conversation has ended. Please tap Start Over to begin a new request."


# ---------------------------------------------------------------------------
# 1-4: standalone closure, no intake question, guard blocks, no mutation
# ---------------------------------------------------------------------------

def test_life_threatening_message_persists_final_closed(db, fakes):
    client = make_client(db)
    conversation = make_conversation(db, client)

    resp = send(db, client, conversation, LIFE_THREATENING_TEXT)

    assert conversation.final_closed is True
    assert "911" in resp.reply
    # Same-response suppression preserved: no intake/contact question.
    assert "?" not in resp.reply


def test_later_message_is_blocked_and_mutates_nothing(db, fakes):
    client = make_client(db)
    conversation = make_conversation(db, client, lead_name="", lead_phone="")
    send(db, client, conversation, LIFE_THREATENING_TEXT)
    lead_sms_before = len(fakes.lead_sms)
    lead_email_before = len(fakes.lead_email)

    resp = send(db, client, conversation,
                "my name is Bob and my number is 516-555-0199")

    assert resp.meta.get("mode") == "final_closed"
    assert resp.reply == BLOCKED_REPLY
    # No lead-field mutation, no question, no notification on blocked turns.
    assert (conversation.lead_name or "") == ""
    assert (conversation.lead_phone or "") == ""
    assert conversation.final_closed is True
    assert len(fakes.lead_sms) == lead_sms_before
    assert len(fakes.lead_email) == lead_email_before


# ---------------------------------------------------------------------------
# 5: closure persists from every intake stage
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("stage_overrides", [
    # reason stage: nothing captured yet
    dict(lead_reason=None, lead_name="", lead_phone="",
         lead_time_window=None, lead_email_opt_out=False,
         lead_is_new_patient=None),
    # name stage
    dict(lead_name="", lead_phone="", lead_time_window=None,
         lead_email_opt_out=False, lead_is_new_patient=None),
    # phone stage
    dict(lead_phone="", lead_time_window=None,
         lead_email_opt_out=False, lead_is_new_patient=None),
    # email stage
    dict(lead_time_window=None, lead_email_opt_out=False,
         lead_is_new_patient=None),
    # time-window stage
    dict(lead_time_window=None, lead_is_new_patient=None),
    # new/returning stage (builder default: one answer away)
    dict(lead_is_new_patient=None),
], ids=["reason", "name", "phone", "email", "time_window", "new_returning"])
def test_life_threatening_persists_closure_from_every_intake_stage(
        db, fakes, stage_overrides):
    client = make_client(db)
    conversation = make_conversation(db, client, **stage_overrides)

    resp = send(db, client, conversation, LIFE_THREATENING_TEXT)

    assert conversation.final_closed is True
    assert "?" not in resp.reply

    followup = send(db, client, conversation, "hello?")
    assert followup.meta.get("mode") == "final_closed"


# ---------------------------------------------------------------------------
# 6: during native booking — cleanup + closure + no continuation
# ---------------------------------------------------------------------------

def test_life_threatening_during_booking_cleans_up_closes_and_blocks(db, fakes):
    client = make_client(db, calendar_enabled=True)
    conversation = make_conversation(db, client, lead_status="completed")
    slot = seed_active_confirmation(db, client, conversation)

    resp = send(db, client, conversation, LIFE_THREATENING_TEXT)

    # Existing cleanup contract preserved (same assertions as the
    # pre-existing mid-booking cleanup test) ...
    assert resp.meta.get("mode") != "booking"
    assert (conversation.booking_state or "none") == BookingState.NONE
    assert conversation.booking_selected_slot_id is None
    slot_row = refreshed_slot(db, slot.id)
    assert slot_row.status == SlotStatus.AVAILABLE
    assert slot_row.held_by_conversation_id is None
    # ... plus the S4 closure.
    assert conversation.final_closed is True

    # A later attempt to continue booking is blocked and holds nothing.
    followup = send(db, client, conversation, "yes book it")
    assert followup.meta.get("mode") == "final_closed"
    assert (conversation.booking_state or "none") == BookingState.NONE
    slot_row2 = refreshed_slot(db, slot.id)
    assert slot_row2.status == SlotStatus.AVAILABLE
    assert slot_row2.held_by_conversation_id is None


# ---------------------------------------------------------------------------
# 7: cleanup failure never suppresses the reply; closure per contract
# ---------------------------------------------------------------------------

def test_cleanup_failure_still_replies_and_persists_closure(db, fakes, monkeypatch):
    client = make_client(db, calendar_enabled=True)
    conversation = make_conversation(db, client, lead_status="completed")
    seed_active_confirmation(db, client, conversation)

    def boom(*args, **kwargs):
        raise RuntimeError("simulated cleanup failure")

    monkeypatch.setattr(chat_module, "cancel_active_booking", boom)

    resp = send(db, client, conversation, LIFE_THREATENING_TEXT)

    # Verified production contract: the emergency reply is returned (no
    # 500), and the persistence block after the cleanup attempt still
    # closes the conversation via the tail commit.
    assert resp is not None
    assert resp.meta.get("mode") != "booking"
    assert "911" in resp.reply
    assert conversation.final_closed is True

    followup = send(db, client, conversation, "hello")
    assert followup.meta.get("mode") == "final_closed"


# ---------------------------------------------------------------------------
# 8: ordinary dental emergencies stay open with their contact prompt
# ---------------------------------------------------------------------------

def test_ordinary_dental_emergency_stays_open_with_contact_prompt(db, fakes):
    client = make_client(db)
    conversation = make_conversation(
        db, client, lead_name="", lead_phone="",
        lead_time_window=None, lead_is_new_patient=None,
    )

    resp = send(db, client, conversation, "I knocked out my tooth")

    assert conversation.final_closed is not True
    # The existing contact-capture behavior continues: the reply carries a
    # question and the next turn is NOT blocked.
    assert "?" in resp.reply
    followup = send(db, client, conversation, "Kevin")
    assert followup.meta.get("mode") != "final_closed"


# ---------------------------------------------------------------------------
# 9: Start Over (fresh conversation) remains the supported continuation
# ---------------------------------------------------------------------------

def test_start_over_fresh_conversation_works_and_old_stays_closed(db, fakes):
    client = make_client(db)
    conversation = make_conversation(db, client)
    send(db, client, conversation, LIFE_THREATENING_TEXT)
    assert conversation.final_closed is True

    # Start Over = the widget begins a NEW conversation (no conversation_id).
    fresh = send(db, client, None, "hi, I'd like to book a cleaning")
    assert fresh.meta.get("mode") != "final_closed"
    assert fresh.conversation_id != str(conversation.id)

    # The closed conversation stays closed.
    db.refresh(conversation)
    assert conversation.final_closed is True
    still_blocked = send(db, client, conversation, "hello again")
    assert still_blocked.meta.get("mode") == "final_closed"


# ---------------------------------------------------------------------------
# 10: trigger constant and predicate unchanged by S4
# ---------------------------------------------------------------------------

def test_trigger_constant_and_predicate_unchanged():
    assert chat_module.LIFE_THREATENING_TRIGGERS == [
        "can t breathe", "cant breathe", "cannot breathe",
        "trouble breathing", "difficulty breathing",
        "can t swallow", "cant swallow", "cannot swallow",
        "trouble swallowing", "difficulty swallowing",
        "uncontrolled bleeding", "won t stop bleeding", "wont stop bleeding",
        "can t stop bleeding", "cant stop bleeding", "cannot stop bleeding",
        "bleeding won t stop", "bleeding wont stop", "bleeding will not stop",
        "blood everywhere", "bleeding everywhere",
        "rapidly worsening swelling", "worsening swelling",
    ]
    assert chat_module.looks_like_life_threatening_emergency("trouble breathing")
    assert chat_module.looks_like_life_threatening_emergency(
        "my face is swelling and I'm having trouble breathing")
    assert not chat_module.looks_like_life_threatening_emergency(
        "I knocked out my tooth")
    assert not chat_module.looks_like_life_threatening_emergency(
        "severe tooth pain")
