# calendar_tests/test_notification_wording.py
#
# S5 — honest lead-notification wording.
#
# Drives the REAL chat() completion paths via the shared integration harness
# and proves the patient-facing wording claims office notification only when
# at least one configured lead channel actually succeeded this conversation;
# otherwise it truthfully says the information was saved. Provider errors are
# never exposed, no notification is sent just to decide wording, and repeated
# turns never resend (per-channel idempotency preserved).
#
# Scope guard: these tests exercise the ORDINARY lead-notification flow
# (lead_email_sent / lead_sms_sent) only. The Patch 9A appointment-
# notification ledger has its own tests and is untouched by S5 — proven here
# only indirectly by test_native_booking_reply_unchanged (routing to the
# Calendar returns before the wording owner runs).

import uuid

import pytest

import app.routes.chat as chat_module
from app.models import Client
from app.calendar_models import BookingState

from calendar_tests.test_chat_integration import (  # noqa: F401
    fakes,
    make_client,
    make_conversation,
    send,
)

SENT_REPLY = (
    "Thanks Kevin Alvarado! We’ve got your request—"
    "our team will contact you shortly to confirm the appointment time."
)
SAVED_REPLY = (
    "Thanks Kevin Alvarado! "
    "Your appointment-request information has been saved for the office."
)
PROVIDER_SECRET = "provider-secret-detail-XYZ-123"


def make_client_channels(db, *, email: bool, sms: bool):
    """Office with an exact channel configuration (the shared builder only
    offers all-or-none)."""
    client = Client(
        id=uuid.uuid4(),
        practice_name="S5 Wording Dental",
        api_key=f"key-{uuid.uuid4()}",
        active=True,
        settings={"timezone": "America/New_York"},
        notification_email="office@example.com" if email else None,
        notification_phone="+15550001111" if sms else None,
    )
    db.add(client)
    db.commit()
    return client


def _complete(db, client):
    """Patient-type completion: the builder-default conversation is one
    'returning' answer away from completion; no calendar settings, so the
    lead-capture reply (not booking) is the response."""
    conversation = make_conversation(db, client)
    resp = send(db, client, conversation, "returning")
    return conversation, resp


def _fail_email(monkeypatch):
    def boom(**kwargs):
        raise RuntimeError(PROVIDER_SECRET)
    monkeypatch.setattr(chat_module, "send_office_lead_email", boom)


def _fail_sms(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError(PROVIDER_SECRET)
    monkeypatch.setattr(chat_module, "send_office_lead_sms", boom)


# ---------------------------------------------------------------------------
# 1-2: single configured channel succeeds -> notified wording
# ---------------------------------------------------------------------------

def test_email_success_sms_absent_says_notified(db, fakes):
    client = make_client_channels(db, email=True, sms=False)
    conversation, resp = _complete(db, client)
    assert resp.reply == SENT_REPLY
    assert conversation.lead_email_sent is True
    assert bool(conversation.lead_sms_sent) is False
    assert len(fakes.lead_email) == 1 and len(fakes.lead_sms) == 0


def test_sms_success_email_absent_says_notified(db, fakes):
    client = make_client_channels(db, email=False, sms=True)
    conversation, resp = _complete(db, client)
    assert resp.reply == SENT_REPLY
    assert conversation.lead_sms_sent is True
    assert bool(conversation.lead_email_sent) is False
    assert len(fakes.lead_sms) == 1 and len(fakes.lead_email) == 0


# ---------------------------------------------------------------------------
# 3-5: partial failure still notified; both-success notified exactly once
# ---------------------------------------------------------------------------

def test_email_success_sms_failure_still_says_notified(db, fakes, monkeypatch):
    client = make_client_channels(db, email=True, sms=True)
    _fail_sms(monkeypatch)
    conversation, resp = _complete(db, client)
    assert resp.reply == SENT_REPLY
    assert conversation.lead_email_sent is True
    assert bool(conversation.lead_sms_sent) is False
    assert PROVIDER_SECRET not in resp.reply


def test_sms_success_email_failure_still_says_notified(db, fakes, monkeypatch):
    client = make_client_channels(db, email=True, sms=True)
    _fail_email(monkeypatch)
    conversation, resp = _complete(db, client)
    assert resp.reply == SENT_REPLY
    assert conversation.lead_sms_sent is True
    assert bool(conversation.lead_email_sent) is False
    assert PROVIDER_SECRET not in resp.reply


def test_both_channels_succeed_notified_exactly_once(db, fakes):
    client = make_client_channels(db, email=True, sms=True)
    conversation, resp = _complete(db, client)
    assert resp.reply == SENT_REPLY
    # Requirement 9: the wording consumed THIS turn's results — exactly one
    # send per channel, none added to decide wording.
    assert len(fakes.lead_email) == 1 and len(fakes.lead_sms) == 1


# ---------------------------------------------------------------------------
# 6-8: zero successes -> honest saved wording, provider error never exposed
# ---------------------------------------------------------------------------

def test_both_channels_fail_says_saved_not_notified(db, fakes, monkeypatch):
    client = make_client_channels(db, email=True, sms=True)
    _fail_email(monkeypatch)
    _fail_sms(monkeypatch)
    conversation, resp = _complete(db, client)
    assert resp.reply == SAVED_REPLY
    assert "contact you shortly" not in resp.reply
    assert PROVIDER_SECRET not in resp.reply
    assert bool(conversation.lead_email_sent) is False
    assert bool(conversation.lead_sms_sent) is False
    # The lead itself is still completed and preserved.
    assert (conversation.lead_status or "").lower() == "completed"


def test_no_channels_configured_uses_saved_wording(db, fakes):
    client = make_client_channels(db, email=False, sms=False)
    conversation, resp = _complete(db, client)
    assert resp.reply == SAVED_REPLY
    assert len(fakes.lead_email) == 0 and len(fakes.lead_sms) == 0


# ---------------------------------------------------------------------------
# 10: repeated/follow-up turns never resend; ending wording is honest
# ---------------------------------------------------------------------------

def test_followup_turn_does_not_resend_and_keeps_honest_ending(db, fakes):
    client = make_client_channels(db, email=True, sms=True)
    conversation, _ = _complete(db, client)
    assert len(fakes.lead_email) == 1 and len(fakes.lead_sms) == 1

    resp = send(db, client, conversation, "thanks, that's all")

    # Per-channel idempotency: no resend on the follow-up turn.
    assert len(fakes.lead_email) == 1 and len(fakes.lead_sms) == 1
    # Non-priority completed lead: unchanged polite ending wording.
    assert "You’re welcome" in resp.reply or "Thank you for choosing" in resp.reply


def test_priority_ending_with_no_channels_uses_saved_ending(db, fakes):
    # The ending guard now builds wording AFTER the single idempotent
    # finalize call, from its real outcome: priority + zero successful
    # channels -> the honest saved ending, no promised follow-up.
    #
    # The input must be an EXPLICIT ending phrase ("goodbye"): a completed
    # conversation that still carries lead data keeps in_intake_mode True,
    # so a simple "thank you" is owned by the post_completion_polite branch
    # (covered by test_followup_turn_does_not_resend_and_keeps_honest_ending)
    # and never reaches the conversation-ending guard this test targets.
    client = make_client_channels(db, email=False, sms=False)
    conversation = make_conversation(
        db, client,
        lead_is_priority=True,
        lead_time_window="ASAP",
        lead_is_new_patient=True,
        lead_status="completed",
    )

    resp = send(db, client, conversation, "goodbye")

    assert resp.meta.get("mode") == "conversation_ending"
    assert resp.reply == (
        "You’re welcome. Your information has been saved for the office. "
        "If you need help sooner, please call the office directly."
    )
    # Zero sends, no notified claim, and nothing to duplicate on this turn.
    assert len(fakes.lead_email) == 0 and len(fakes.lead_sms) == 0
    assert "follow up shortly" not in resp.reply
    assert "contact you shortly" not in resp.reply
    assert bool(conversation.lead_email_sent) is False
    assert bool(conversation.lead_sms_sent) is False


# ---------------------------------------------------------------------------
# 11-12: non-completion replies and native booking replies unchanged
# ---------------------------------------------------------------------------

def test_ordinary_non_completion_reply_unchanged(db, fakes):
    client = make_client_channels(db, email=True, sms=True)
    conversation = make_conversation(db, client, lead_name="",
                                     lead_time_window=None)
    resp = send(db, client, conversation, "asap")
    # S3 capture-first behavior, untouched by S5, and no sends mid-intake.
    assert resp.reply == "What’s your first name?"
    assert len(fakes.lead_email) == 0 and len(fakes.lead_sms) == 0


def test_native_booking_reply_unchanged(db, fakes):
    # Calendar-enabled office: completion routes to the booking owner and
    # returns before the notification-wording owner runs.
    #
    # Checkpoint B consumes the already-complete stored preference
    # "Tuesday morning". When no matching slot exists, the booking owner
    # asks for another specific day instead of repeating the initial prompt.
    client = make_client(db, calendar_enabled=True)
    conversation = make_conversation(db, client)
    resp = send(db, client, conversation, "returning")

    assert resp.meta.get("mode") == "booking"
    assert "matching online openings around" in resp.reply
    assert "The office can help directly." in resp.reply
    assert "What other specific day would you like me to check?" in resp.reply
    assert resp.reply != "What day would work best for your appointment?"
    assert "morning or afternoon" not in resp.reply.lower()

    assert conversation.booking_preferred_date is not None
    assert conversation.booking_time_preference == "morning"
    assert conversation.booking_state == BookingState.WAITING_FOR_DATE

    # Dedupe patch: a ROUTINE native-Calendar completion defers the
    # office alert to the exact-time booking notification — no generic
    # lead send, per-channel flags stay false.
    assert len(fakes.lead_email) == 0 and len(fakes.lead_sms) == 0
    assert bool(conversation.lead_email_sent) is False
    assert bool(conversation.lead_sms_sent) is False
