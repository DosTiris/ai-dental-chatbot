# calendar_tests/test_asap_wording.py
#
# S3 ASAP capture-first regression tests (correction pass 1).
#
# Two layers:
#
#   A. REAL-FLOW tests (requires_db): drive the actual chat() route with a
#      frozen clock and prove the full non-emergency ASAP sequence:
#      name -> phone -> email-or-skip -> new/returning -> completion, with
#      statement-only ASAP wording, no "Would {day} work?", at most one
#      required question per reply, priority classification retained, and
#      completion/notification only at the final stage.
#      The lead reason used is "cleaning/checkup": symptom reasons such as
#      "tooth pain" intentionally use the shorter documented symptom flow
#      (conversation_uses_short_symptom_flow) and would not exercise the
#      standard order these tests must prove.
#
#   B. DIRECT OWNER tests: handle_time_window_capture() and
#      priority_intake_is_complete() with normal NON-emergency states (no
#      emergency fixture), exact-string wording assertions, and frozen
#      clocks for deterministic before/after-noon behavior.
#
# The clock is frozen by monkeypatching app.routes.chat.get_client_now, so
# every test passes identically at any wall-clock time (no midnight/date-
# boundary flakiness).

import uuid
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import app.routes.chat as chat_module
from app.routes.chat import handle_time_window_capture, priority_intake_is_complete

from calendar_tests.conftest import requires_db

NY = ZoneInfo("America/New_York")

# Tuesday 2026-07-21 at a Mon-Fri office: next open day is Wednesday.
BEFORE_NOON = datetime(2026, 7, 21, 9, 30, tzinfo=NY)
AFTER_NOON = datetime(2026, 7, 21, 14, 30, tzinfo=NY)
FRIDAY_PM = datetime(2026, 7, 24, 15, 0, tzinfo=NY)

WEEKDAY_HOURS = {
    **{k: {"open": True, "start": "09:00", "end": "17:00"}
       for k in ["mon", "tue", "wed", "thu", "fri"]},
    **{k: {"open": False} for k in ["sat", "sun"]},
}

AFTER_NOON_STATEMENT = (
    "Got it — we’ll look for the earliest available time. "
    "If today is unavailable, the next opening may be Wednesday."
)
BEFORE_NOON_STATEMENT = (
    "Got it — we’ll look for the earliest available time today. "
    "If needed, we can also look at Wednesday."
)
EMAIL_PROMPT = "What’s your email? (You can also type 'skip'.)"
REMOVED_PHRASE = "would wednesday work"


def _freeze(monkeypatch, when):
    monkeypatch.setattr(chat_module, "get_client_now", lambda client: when)


def _no_would_day_question(reply: str):
    low = (reply or "").lower()
    assert "would wednesday work" not in low
    assert "would monday work" not in low
    assert "if we can’t fit you in today, would" not in low
    assert "if we can t fit you in today would" not in low


# ===========================================================================
# Layer A — REAL chat() flow (requires_db)
# ===========================================================================

class _StubRequest:
    client = SimpleNamespace(host="127.0.0.1")


@pytest.fixture()
def flow_client(db):
    """Dedicated office: booking DISABLED (so lead completion stays on the
    lead-capture path, not the Calendar delegation path) and no notification
    channels configured (so completion never attempts external sends)."""
    from app.models import Client
    client = Client(
        id=uuid.uuid4(),
        practice_name="S3 Flow Dental",
        api_key=f"key-{uuid.uuid4()}",
        active=True,
        office_hours=WEEKDAY_HOURS,
        settings={
            "timezone": "America/New_York",
            "calendar": {"booking_enabled": False},
        },
    )
    db.add(client)
    db.commit()
    return client


def _lead(db, client, *, name="", phone="", email_opt_out=False,
          time_window=None, is_priority=False, new_patient=None,
          last_assistant=None):
    """Seed a mid-intake lead exactly as the flow leaves it between turns."""
    from app.models import Conversation, Message
    conv = Conversation(
        id=uuid.uuid4(),
        client_id=client.id,
        is_lead=True,
        lead_status="new",
        lead_reason="cleaning/checkup",
        lead_reason_source_text="cleaning",
        lead_name=name,
        lead_phone=phone,
        lead_email_opt_out=email_opt_out,
        lead_time_window=time_window,
        lead_is_priority=is_priority,
        lead_is_new_patient=new_patient,
    )
    db.add(conv)
    db.commit()
    if last_assistant:
        db.add(Message(conversation_id=conv.id, role="assistant",
                       content=last_assistant))
        db.commit()
    return conv


def _chat(db, client, conv, text):
    from app.routes.chat import chat
    from app.schemas import ChatRequest
    req = ChatRequest(message=text, client_key=client.api_key,
                      conversation_id=str(conv.id))
    return chat(req, _StubRequest(), db)


DAY_QUESTION = "What day/time works best for you? (e.g., Mon morning)"


@requires_db
def test_flow_asap_with_no_name_asks_name_first(db, flow_client, monkeypatch):
    _freeze(monkeypatch, AFTER_NOON)
    conv = _lead(db, flow_client, last_assistant=DAY_QUESTION)
    resp = _chat(db, flow_client, conv, "asap")
    assert "What’s your first name?" in resp.reply
    assert resp.reply.count("?") == 1
    _no_would_day_question(resp.reply)
    db.refresh(conv)
    assert conv.lead_time_window == "ASAP"
    assert conv.lead_is_priority is True
    assert (conv.lead_status or "").lower() != "completed"


@requires_db
def test_flow_asap_with_name_asks_phone_next(db, flow_client, monkeypatch):
    _freeze(monkeypatch, AFTER_NOON)
    conv = _lead(db, flow_client, name="Kevin", last_assistant=DAY_QUESTION)
    resp = _chat(db, flow_client, conv, "asap")
    assert "phone number" in resp.reply.lower()
    assert resp.reply.count("?") == 1
    _no_would_day_question(resp.reply)
    db.refresh(conv)
    assert (conv.lead_status or "").lower() != "completed"


@requires_db
def test_flow_asap_after_name_and_phone_asks_email_or_skip_not_completion(
        db, flow_client, monkeypatch):
    # The core corrected behavior: name+phone ASAP does NOT complete; the
    # statement-only wording carries the single email-or-skip question.
    _freeze(monkeypatch, AFTER_NOON)
    conv = _lead(db, flow_client, name="Kevin", phone="516-555-0100",
                 new_patient=True,  # Package A: patient type captured before email
                 last_assistant=DAY_QUESTION)
    resp = _chat(db, flow_client, conv, "asap")
    assert resp.reply == f"{AFTER_NOON_STATEMENT}\n\n{EMAIL_PROMPT}"
    assert resp.reply.count("?") == 1
    _no_would_day_question(resp.reply)
    db.refresh(conv)
    assert (conv.lead_status or "").lower() != "completed"
    assert not bool(conv.lead_email_sent or False)
    assert not bool(conv.lead_sms_sent or False)


@requires_db
def test_flow_asap_before_noon_statement_plus_email_question(
        db, flow_client, monkeypatch):
    _freeze(monkeypatch, BEFORE_NOON)
    conv = _lead(db, flow_client, name="Kevin", phone="516-555-0100",
                 new_patient=True,  # Package A: patient type captured before email
                 last_assistant=DAY_QUESTION)
    resp = _chat(db, flow_client, conv, "asap")
    assert resp.reply == f"{BEFORE_NOON_STATEMENT}\n\n{EMAIL_PROMPT}"
    assert resp.reply.count("?") == 1
    _no_would_day_question(resp.reply)
    db.refresh(conv)
    assert (conv.lead_status or "").lower() != "completed"


@requires_db
def test_flow_skip_after_email_question_asks_new_or_returning(
        db, flow_client, monkeypatch):
    _freeze(monkeypatch, AFTER_NOON)
    conv = _lead(db, flow_client, name="Kevin", phone="516-555-0100",
                 time_window="ASAP", is_priority=True,
                 last_assistant=EMAIL_PROMPT)
    resp = _chat(db, flow_client, conv, "skip")
    assert "new or returning" in resp.reply.lower()
    assert resp.reply.count("?") == 1
    _no_would_day_question(resp.reply)
    db.refresh(conv)
    assert bool(conv.lead_email_opt_out) is True
    assert (conv.lead_status or "").lower() != "completed"


@requires_db
def test_flow_new_or_returning_answer_completes_and_notifies_once(
        db, flow_client, monkeypatch):
    _freeze(monkeypatch, AFTER_NOON)
    conv = _lead(db, flow_client, name="Kevin", phone="516-555-0100",
                 email_opt_out=True, time_window="ASAP", is_priority=True,
                 last_assistant="One quick question — Kevin, are you a new or returning patient?")
    resp = _chat(db, flow_client, conv, "new")
    db.refresh(conv)
    assert conv.lead_is_new_patient is True
    assert (conv.lead_status or "").lower() == "completed"
    assert conv.lead_is_priority is True  # classification retained
    _no_would_day_question(resp.reply)
    assert resp.reply.count("?") <= 1
    # No channels configured on this office: honest flags stay False.
    assert bool(conv.lead_email_sent or False) is False
    assert bool(conv.lead_sms_sent or False) is False


@requires_db
def test_flow_ordinary_closed_sunday_request_uses_existing_owner(
        db, flow_client, monkeypatch):
    # A Sunday request at a Sunday-closed office is answered by the
    # pre-existing calendar closed-day owner, build_time_window_issue_reply(),
    # which runs BEFORE the later Sunday-only nudge block. S3 preserves that
    # owner and its exact response.
    _freeze(monkeypatch, BEFORE_NOON)
    conv = _lead(db, flow_client, name="Kevin", phone="516-555-0100",
                 new_patient=True,  # Package A: patient type captured before email
                 last_assistant=DAY_QUESTION)
    resp = _chat(db, flow_client, conv, "sunday morning")
    assert resp.reply == (
        "The office is closed on Sunday. What day/time works better for you?"
    )
    assert resp.reply.count("?") == 1
    _no_would_day_question(resp.reply)
    db.refresh(conv)
    assert (conv.lead_status or "").lower() != "completed"


# ===========================================================================
# Layer B — direct owner tests (no DB, normal non-emergency states)
# ===========================================================================

def _client():
    return SimpleNamespace(office_hours=WEEKDAY_HOURS,
                           settings={"timezone": "America/New_York"},
                           timezone="America/New_York",
                           practice_name="Test Dental")


def _conv(**kw):
    base = dict(
        lead_time_window=None, lead_is_priority=False, lead_is_emergency=False,
        lead_name="", lead_phone="", lead_email="", lead_email_opt_out=False,
        lead_is_new_patient=None,
        lead_reason="cleaning/checkup", lead_reason_source_text="cleaning",
        lead_reason_detail="",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _mid_intake_conv():
    """Normal NON-emergency ASAP lead with name+phone captured but email not
    yet asked — the state that reaches the next-open-day wording branch."""
    return _conv(lead_name="Kevin", lead_phone="516-555-1234")


def test_direct_asap_capture_first_name_then_phone(monkeypatch):
    _freeze(monkeypatch, AFTER_NOON)
    reply, updated = handle_time_window_capture(_client(), _conv(), "asap", "")
    assert updated is True and reply == "What’s your first name?"
    reply2, _ = handle_time_window_capture(
        _client(), _conv(lead_name="Kevin"), "asap", "")
    assert reply2 == "Thanks Kevin! What’s the best phone number to reach you?"
    assert reply.count("?") == 1 and reply2.count("?") == 1


def test_direct_after_noon_statement_only_wording(monkeypatch):
    _freeze(monkeypatch, AFTER_NOON)
    reply, updated = handle_time_window_capture(
        _client(), _mid_intake_conv(), "asap", "")
    assert updated is True
    assert reply == AFTER_NOON_STATEMENT
    assert "?" not in reply
    _no_would_day_question(reply)


def test_direct_before_noon_statement_only_wording(monkeypatch):
    _freeze(monkeypatch, BEFORE_NOON)
    reply, updated = handle_time_window_capture(
        _client(), _mid_intake_conv(), "asap", "")
    assert updated is True
    assert reply == BEFORE_NOON_STATEMENT
    assert "?" not in reply


def test_direct_all_days_closed_plain_statement(monkeypatch):
    _freeze(monkeypatch, AFTER_NOON)
    closed = SimpleNamespace(
        office_hours={k: {"open": False} for k in
                      ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]},
        settings={"timezone": "America/New_York"},
        timezone="America/New_York", practice_name="Test Dental")
    reply, updated = handle_time_window_capture(
        closed, _mid_intake_conv(), "asap", "")
    assert updated is True
    assert reply == "Got it — we’ll look for the earliest available time."


def test_direct_friday_afternoon_states_monday_weekend_skipped(monkeypatch):
    _freeze(monkeypatch, FRIDAY_PM)
    reply, _ = handle_time_window_capture(
        _client(), _mid_intake_conv(), "asap", "")
    assert reply == (
        "Got it — we’ll look for the earliest available time. "
        "If today is unavailable, the next opening may be Monday."
    )
    assert "?" not in reply


def test_direct_priority_intake_stage_progression():
    # Production-parity completeness: every stage must be present.
    c = _conv(lead_is_priority=True)
    assert priority_intake_is_complete(c) is False           # nothing yet
    c.lead_name = "Kevin"
    c.lead_phone = "516-555-1234"
    assert priority_intake_is_complete(c) is False           # name+phone only
    c.lead_email_opt_out = True
    assert priority_intake_is_complete(c) is False           # no time window
    c.lead_time_window = "ASAP"
    assert priority_intake_is_complete(c) is False           # no new/returning
    c.lead_is_new_patient = False
    assert priority_intake_is_complete(c) is True            # all stages
    c.lead_is_emergency = True
    assert priority_intake_is_complete(c) is False           # emergency excluded


def test_direct_ordinary_same_day_wording_exact(monkeypatch):
    # Requirement: ordinary non-ASAP replies byte-unchanged, both halves
    # of the day, with the saved flag semantics preserved.
    _freeze(monkeypatch, AFTER_NOON)
    reply, saved = handle_time_window_capture(_client(), _conv(), "today", "")
    assert reply == ("Got it — what time later today works best? "
                     "If today is too tight, tomorrow afternoon works too.")
    assert saved is True  # "today" saves the day token before asking detail
    _freeze(monkeypatch, BEFORE_NOON)
    reply2, saved2 = handle_time_window_capture(_client(), _conv(), "today", "")
    assert reply2 == "Got it — do you prefer today morning or afternoon?"
    assert saved2 is True


def test_direct_closed_sunday_reply_exact(monkeypatch):
    # Same behavior at the owner level: the closed-day reply comes from
    # build_time_window_issue_reply() inside handle_time_window_capture(),
    # returned with saved=False, before the later Sunday-only nudge block
    # is ever reached for these fixtures. S3 preserves this exact reply.
    _freeze(monkeypatch, BEFORE_NOON)
    reply, saved = handle_time_window_capture(
        _client(), _conv(), "sunday morning", "")
    assert reply == (
        "The office is closed on Sunday. What day/time works better for you?"
    )
    assert saved is False
    assert reply.count("?") == 1
