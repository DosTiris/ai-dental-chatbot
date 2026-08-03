# calendar_tests/test_life_threatening_input_lock.py
#
# S10 — Defect 2: life-threatening responses must lock the widget input
# immediately, on the SAME turn that closes the conversation.
#
# Reuses the real-endpoint harness from test_chat_integration.py (builders,
# recording fakes, send helper) so every test drives the actual chat() flow
# against PostgreSQL with no network boundary.
#
# Staging regression these tests pin:
#   "I can't breathe and my face is swelling rapidly"
#     -> correct 911 response, conversation.final_closed persisted
#     -> BUT the widget input stayed usable for one more message
#
# Root cause: S4 backported the final_closed persistence from production but
# not the disable_input half of the same production change. The three
# life-threatening response paths now carry the existing widget contract:
#     **({"disable_input": True} if life_threatening_stop else {}),
#
# Deliberately NOT covered here (recorded as deferred drift):
#   * the quick-action bypass — sendQuickMessage() -> sendMessage() does not
#     check inputEl.disabled. Pre-existing; the backend final_closed guard
#     still prevents every state mutation.
#   * reload / localStorage lock persistence through the generic final_closed
#     guard, which is intentionally left identical to production.

import pytest

from app.calendar_models import BookingState

# Importing these registers the autouse `fakes` fixture in this module too.
from calendar_tests.test_chat_integration import (  # noqa: F401
    fakes,
    make_client,
    make_conversation,
    send,
)

from pathlib import Path

# The exact staging message.
STAGING_LIFE_THREATENING = "I can't breathe and my face is swelling rapidly"

BLOCKED_REPLY = (
    "This conversation has ended. Please tap Start Over to begin a new request."
)

# One message per life-threatening response owner. Each is routed by the FIRST
# guard listed, in the order those guards appear in chat(); the trigger words
# that select each owner are named in the comments so the routing is
# reviewable without running the suite.
LIFE_THREATENING_BY_OWNER = [
    # looks_like_dangerous_dental_instruction: "pull out" / "pliers" + "tooth".
    # Life-threatening via "bleeding won't stop".
    (
        "I tried to pull out my tooth with pliers and the bleeding won't stop",
        "dangerous_dental_self_treatment_guard",
    ),
    # Not a dangerous-instruction message. looks_like_urgent_dental_safety_issue:
    # trauma ("fell") + tooth damage ("broke my tooth").
    # Life-threatening via "blood everywhere".
    (
        "I fell and broke my tooth and there is blood everywhere",
        "urgent_dental_safety_guard",
    ),
    # Neither of the above. looks_like_emergency via "can t breathe".
    (STAGING_LIFE_THREATENING, "emergency_booking_mode"),
]

# Dental emergencies with NO life-threatening symptom. These reach the same
# emergency routing tier but must keep a usable input for the contact chain.
ORDINARY_DENTAL_EMERGENCIES = [
    "I knocked out my tooth",
    "I'm in severe pain, it's a dental emergency",
]


def empty_lead_conversation(db, client):
    """A brand-new conversation with no captured lead fields at all."""
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


def widget_source():
    """The authoritative widget, resolved repository-relative.

    calendar_tests/ -> parents[1] is the repository root.
    """
    widget_path = Path(__file__).resolve().parents[1] / "static" / "chat.html"
    assert widget_path.is_file(), f"widget not found at {widget_path}"
    return widget_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1: the exact staging message locks the input on the FIRST response
# ---------------------------------------------------------------------------

def test_exact_staging_life_threatening_message_locks_input(db, fakes):
    client = make_client(db)
    conversation = empty_lead_conversation(db, client)

    resp = send(db, client, conversation, STAGING_LIFE_THREATENING)

    assert resp.meta.get("mode") == "emergency_booking_mode"
    assert resp.meta.get("disable_input") is True
    assert "911" in resp.reply
    # Standalone safety instruction preserved: no appended question.
    assert "?" not in resp.reply
    assert resp.meta.get("emergency_mode") is True
    assert conversation.final_closed is True


# ---------------------------------------------------------------------------
# 2: all three life-threatening owners carry the contract
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "message,expected_mode",
    LIFE_THREATENING_BY_OWNER,
    ids=[mode for _, mode in LIFE_THREATENING_BY_OWNER],
)
def test_all_three_life_threatening_owners_carry_disable_input(
        db, fakes, message, expected_mode):
    client = make_client(db)
    conversation = empty_lead_conversation(db, client)

    resp = send(db, client, conversation, message)

    assert resp.meta.get("mode") == expected_mode
    assert resp.meta.get("disable_input") is True
    assert resp.meta.get("emergency_mode") is True
    assert "911" in resp.reply
    assert conversation.final_closed is True


# ---------------------------------------------------------------------------
# 3: ordinary dental emergencies must NOT lock the input
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("message", ORDINARY_DENTAL_EMERGENCIES)
def test_ordinary_dental_emergency_does_not_disable_input(db, fakes, message):
    """The conditional merge is what keeps the contact chain usable. Without
    it, every emergency_booking_mode reply would lock the widget."""
    client = make_client(db)
    conversation = empty_lead_conversation(db, client)

    resp = send(db, client, conversation, message)

    assert not resp.meta.get("disable_input")
    assert conversation.final_closed is not True
    # The existing contact-capture behavior continues.
    assert "first name" in resp.reply.lower()


# ---------------------------------------------------------------------------
# 4: closure still persists on the next message
# ---------------------------------------------------------------------------

def test_final_closed_persists_on_later_blocked_message(db, fakes):
    client = make_client(db)
    conversation = empty_lead_conversation(db, client)
    send(db, client, conversation, STAGING_LIFE_THREATENING)

    lead_sms_before = len(fakes.lead_sms)
    lead_email_before = len(fakes.lead_email)

    later = send(db, client, conversation, "hello")

    assert later.meta.get("mode") == "final_closed"
    assert later.reply == BLOCKED_REPLY
    assert conversation.final_closed is True
    # No lead mutation, no notification, no booking state on a blocked turn.
    assert (conversation.lead_name or "") == ""
    assert (conversation.lead_phone or "") == ""
    assert len(fakes.lead_sms) == lead_sms_before
    assert len(fakes.lead_email) == lead_email_before
    assert (conversation.booking_state or "none") == BookingState.NONE


# ---------------------------------------------------------------------------
# 5: Start Over stays reachable on both turns
# ---------------------------------------------------------------------------

def test_show_start_over_true_on_locking_and_blocked_turns(db, fakes):
    client = make_client(db)
    conversation = empty_lead_conversation(db, client)

    locking = send(db, client, conversation, STAGING_LIFE_THREATENING)
    blocked = send(db, client, conversation, "hello")

    assert locking.meta.get("show_start_over") is True
    assert blocked.meta.get("show_start_over") is True


def test_start_over_fresh_conversation_still_works(db, fakes):
    """Start Over = the widget begins a NEW conversation (no conversation_id).
    The locked conversation stays locked."""
    client = make_client(db)
    conversation = empty_lead_conversation(db, client)
    send(db, client, conversation, STAGING_LIFE_THREATENING)
    assert conversation.final_closed is True

    fresh = send(db, client, None, "hi, I'd like to book a cleaning")

    assert fresh.meta.get("mode") != "final_closed"
    assert fresh.conversation_id != str(conversation.id)
    assert not fresh.meta.get("disable_input")

    db.refresh(conversation)
    assert conversation.final_closed is True


# ---------------------------------------------------------------------------
# 6: structural widget contract
#
# These are CONTRACT-PRESENCE checks, not behavioral proof — they do not
# execute JavaScript. They prove the backend now emits the key that the
# widget's one existing disable-input owner already consumes, and that
# Start Over still releases it. Actual lock behavior is proven by the manual
# staging checklist.
# ---------------------------------------------------------------------------

def test_widget_disable_input_contract_present():
    source = widget_source()

    assert "data.meta.disable_input" in source
    assert "inputEl.disabled = true;" in source
    assert "sendBtn.disabled = true;" in source
    assert 'inputEl.placeholder = "Please call the office directly.";' in source


def test_widget_start_over_releases_input():
    source = widget_source()

    start = source.index("function startOver()")
    brace = source.index("{", start)
    depth, end = 0, brace

    while True:
        ch = source[end]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                break
        end += 1

    body = source[start:end + 1]

    assert "inputEl.disabled = false;" in body
    assert "sendBtn.disabled = false;" in body
    assert "resetInputPlaceholder();" in body
