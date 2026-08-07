# calendar_tests/test_faq_resume_once.py
#
# S8 — FAQ interruption + resume-once synchronization.
#
# Backports the single missing production owner
# last_assistant_asked_intake_question() and the production-gated FAQ resume
# condition at its three call sites (office-phone guard, insurance guard,
# operational FAQ block). The gate means an FAQ answer resumes an intake
# question ONLY when one is actually pending (the most recent assistant
# message asked it), never after the booking link was sent, never after
# completion — and the resumed question appears exactly once.
#
# Verified production behavior pinned here (owners are byte-identical in the
# production reference):
#   - An FAQ asked while the Other free-text reason is pending never reaches
#     the FAQ blocks: the Other capture block precedes every FAQ guard, so the
#     S7 dental-relevance gate answers with its rejection wording and the
#     Other step stays pending. Tests below pin that flow — S8 must not
#     invent an FAQ escape there.
#   - Standalone FAQs (no pending intake question) are answered plainly.
#
# All important cases run through the REAL chat() endpoint via the shared
# integration harness. Expected resumed questions are computed from the real
# owner (_next_intake_prompt) BEFORE the FAQ turn, seeded as the pending
# assistant message, and then asserted by exact reply equality — which also
# proves the one-required-question-per-response rule for every case.

import pytest

from datetime import datetime, timedelta, timezone as dt_timezone

import app.routes.chat as chat_module
from app.calendar_models import BookingState
from app.models import Message

from calendar_tests.test_chat_integration import (  # noqa: F401
    fakes,
    make_client,
    make_conversation,
    send,
)

# ---------------------------------------------------------------------------
# Deterministic FAQ turns and their exact owner-built answers.
# The hours FAQ exercises the operational FAQ block (no office_hours on the
# harness client and no ClientFAQ rows -> the deterministic fallback answer).
# The phone/insurance FAQs exercise their dedicated guards.
# ---------------------------------------------------------------------------
FAQ_HOURS = "What are your hours?"
HOURS_ANSWER = "Please call the office and our team can confirm our office hours."

FAQ_OFFICE_PHONE = "What's your office phone number?"
OFFICE_PHONE_ANSWER = "Our office number is (555) 123-4567."

FAQ_INSURANCE = "Do you take insurance?"
INSURANCE_ANSWER = (
    "Coverage may vary by plan. The office team can help verify your insurance benefits "
    "when they follow up."
)

FIRST_NAME_QUESTION = "What’s your first name?"
PHONE_QUESTION = "Thanks — what’s the best phone number to reach you?"

GENERIC_SEED_SOURCE = "I need an appointment"


# ---------------------------------------------------------------------------
# State builders
# ---------------------------------------------------------------------------

def _seed_assistant(db, conversation, text):
    """Make `text` the most recent assistant message — the pending question."""
    db.add(Message(conversation_id=conversation.id, role="assistant", content=text))
    db.commit()


def _pending_question(db, client, conversation):
    """Compute the real owner's next question for this state and seed it as
    the pending assistant message, exactly as the live flow would have."""
    question = chat_module._next_intake_prompt(client, conversation)
    assert question, "state under test must have a pending intake question"
    _seed_assistant(db, conversation, question)
    return question


def _fresh_generic_lead(db, client):
    """The 'Book Appointment' state: generic request captured, nothing else."""
    return make_conversation(
        db, client,
        lead_reason="appointment request",
        lead_reason_source_text=GENERIC_SEED_SOURCE,
        lead_name="",
        lead_phone="",
        lead_time_window=None,
        lead_email_opt_out=False,
        lead_is_new_patient=True,
    )


def _enter_other_step(db, client, conversation):
    """First turn of the two-turn Other flow: select Other, get the prompt."""
    resp = send(db, client, conversation, "Other")
    assert resp.meta.get("mode") == "other_service_prompt"
    return resp


def _snapshot_lead_fields(conversation):
    return dict(
        reason=conversation.lead_reason,
        reason_source=getattr(conversation, "lead_reason_source_text", None),
        name=conversation.lead_name,
        phone=conversation.lead_phone,
        email=conversation.lead_email,
        email_opt_out=bool(getattr(conversation, "lead_email_opt_out", False)),
        time_window=getattr(conversation, "lead_time_window", None),
        new_patient=getattr(conversation, "lead_is_new_patient", None),
    )


def _assert_answer_then_question_once(resp, answer, question):
    """The S8 contract for one FAQ turn: answer first, the pending question
    resumed exactly once, and nothing else in the reply."""
    assert resp.reply == f"{answer}\n\n{question}"
    assert resp.reply.count(question) == 1
    assert resp.reply.index(answer) < resp.reply.index(question)


# ===========================================================================
# 1-2. No pending question -> the FAQ answer stands alone
# ===========================================================================

def test_faq_with_no_pending_question_does_not_append_intake(db, fakes):
    """The core S8 delta: captured lead data exists, but NO assistant message
    asked an intake question — so the FAQ answer must NOT auto-start intake
    by appending the next question (pre-S8 the calendar branch appended)."""
    client = make_client(db)
    conversation = make_conversation(db, client)  # lead data, no messages

    resp = send(db, client, conversation, FAQ_HOURS)

    assert resp.reply == HOURS_ANSWER
    assert resp.meta.get("mode") == "faq_operational_no_match"
    assert resp.meta.get("faq_match") is False
    assert "new or returning" not in resp.reply.lower()
    assert "?" not in resp.reply


def test_faq_with_no_lead_data_answers_plainly(db, fakes):
    client = make_client(db)
    conversation = make_conversation(
        db, client,
        is_lead=False, lead_reason="", lead_reason_source_text="",
        lead_name="", lead_phone="", lead_time_window=None,
        lead_email_opt_out=False, lead_is_new_patient=None,
    )

    resp = send(db, client, conversation, FAQ_HOURS)

    assert resp.reply == HOURS_ANSWER
    assert "?" not in resp.reply


# ===========================================================================
# 3-8. Each pending intake question resumes exactly once, answer first
# ===========================================================================

def test_faq_resumes_service_reason_question_once(db, fakes):
    client = make_client(db)
    conversation = _fresh_generic_lead(db, client)
    question = _pending_question(db, client, conversation)  # service menu

    resp = send(db, client, conversation, FAQ_HOURS)

    _assert_answer_then_question_once(resp, HOURS_ANSWER, question)
    assert FIRST_NAME_QUESTION not in resp.reply  # no stacked second question
    assert conversation.lead_reason == "appointment request"  # not advanced


def test_faq_resumes_first_name_once(db, fakes):
    client = make_client(db)
    conversation = make_conversation(
        db, client, lead_name="", lead_phone="",
        lead_time_window=None, lead_email_opt_out=False,
        lead_is_new_patient=True,
    )
    question = _pending_question(db, client, conversation)
    assert question == FIRST_NAME_QUESTION

    resp = send(db, client, conversation, FAQ_HOURS)

    _assert_answer_then_question_once(resp, HOURS_ANSWER, question)
    assert (conversation.lead_name or "") == ""


def test_insurance_faq_resumes_phone_once(db, fakes):
    client = make_client(db)
    conversation = make_conversation(
        db, client, lead_phone="",
        lead_time_window=None, lead_email_opt_out=False,
        lead_is_new_patient=True,
    )
    question = _pending_question(db, client, conversation)
    assert question == PHONE_QUESTION

    resp = send(db, client, conversation, FAQ_INSURANCE)

    assert resp.meta.get("mode") == "insurance_info"
    _assert_answer_then_question_once(resp, INSURANCE_ANSWER, question)
    assert (conversation.lead_phone or "") == ""


def test_faq_resumes_email_once(db, fakes):
    client = make_client(db)
    conversation = make_conversation(
        db, client, lead_time_window=None, lead_email_opt_out=False,
        lead_is_new_patient=True,
    )
    question = _pending_question(db, client, conversation)
    assert "email" in question.lower()

    resp = send(db, client, conversation, FAQ_HOURS)

    _assert_answer_then_question_once(resp, HOURS_ANSWER, question)
    assert (conversation.lead_email or "") == ""
    assert bool(getattr(conversation, "lead_email_opt_out", False)) is False


def test_office_phone_faq_resumes_time_window_once(db, fakes):
    client = make_client(db)
    conversation = make_conversation(db, client, lead_time_window=None, lead_is_new_patient=True)
    question = _pending_question(db, client, conversation)
    assert "day/time" in question or "weekday" in question.lower()

    resp = send(db, client, conversation, FAQ_OFFICE_PHONE)

    assert resp.meta.get("mode") == "office_phone"
    _assert_answer_then_question_once(resp, OFFICE_PHONE_ANSWER, question)
    assert (getattr(conversation, "lead_time_window", None) or "") == ""


def test_faq_resumes_new_returning_once(db, fakes):
    client = make_client(db)
    conversation = make_conversation(db, client)  # only new/returning missing
    question = _pending_question(db, client, conversation)
    assert "new or returning patient" in question.lower()

    resp = send(db, client, conversation, FAQ_HOURS)

    _assert_answer_then_question_once(resp, HOURS_ANSWER, question)
    assert getattr(conversation, "lead_is_new_patient", None) is None


# ===========================================================================
# 9-11. Other-reason flow: the S7 gate owns FAQ-shaped turns; Other stays
# pending (verified production behavior — the Other block precedes every
# FAQ guard, so no FAQ answer is produced there)
# ===========================================================================

def _assert_other_gate_reply_and_pending(db, conversation, resp, faq_text):
    """The real S7 owners decide the verdict; the flow reply must match the
    corresponding rejection builder exactly, and the Other step must stay
    pending with nothing persisted."""
    assert chat_module.looks_like_safe_reason_detail(faq_text) is True
    verdict = chat_module.classify_other_reason_detail(faq_text)
    assert verdict in {"unclear", "non_dental"}  # an FAQ is never 'dental'

    if verdict == "unclear":
        expected_reply = chat_module.build_unclear_dental_reason_reply()
        expected_mode = "unclear_other_reason_detail"
    else:
        expected_reply = chat_module.build_non_dental_reason_detail_reply()
        expected_mode = "non_dental_other_reason_detail"

    assert resp.reply == expected_reply
    assert resp.meta.get("mode") == expected_mode
    assert conversation.lead_reason == "appointment request"
    assert conversation.lead_reason_source_text == GENERIC_SEED_SOURCE
    assert chat_module.last_assistant_asked_for_other_reason(
        db, conversation.id) is True


def test_faq_while_other_pending_keeps_other_pending(db, fakes):
    client = make_client(db)
    conversation = _fresh_generic_lead(db, client)
    _enter_other_step(db, client, conversation)

    resp = send(db, client, conversation, FAQ_HOURS)

    _assert_other_gate_reply_and_pending(db, conversation, resp, FAQ_HOURS)


def test_faq_after_non_dental_rejection_keeps_other_pending(db, fakes):
    client = make_client(db)
    conversation = _fresh_generic_lead(db, client)
    _enter_other_step(db, client, conversation)

    first = send(db, client, conversation, "my car is making a weird noise")
    assert first.meta.get("mode") == "non_dental_other_reason_detail"

    resp = send(db, client, conversation, FAQ_HOURS)

    _assert_other_gate_reply_and_pending(db, conversation, resp, FAQ_HOURS)


def test_faq_after_unclear_rejection_keeps_other_pending(db, fakes):
    client = make_client(db)
    conversation = _fresh_generic_lead(db, client)
    _enter_other_step(db, client, conversation)

    first = send(db, client, conversation, "not a root canal")
    assert first.meta.get("mode") == "unclear_other_reason_detail"

    resp = send(db, client, conversation, FAQ_HOURS)

    _assert_other_gate_reply_and_pending(db, conversation, resp, FAQ_HOURS)


# ===========================================================================
# 12-14. The interrupted flow continues normally after the FAQ turn
# ===========================================================================

def test_valid_other_reply_after_faq_advances(db, fakes):
    client = make_client(db)
    conversation = _fresh_generic_lead(db, client)
    _enter_other_step(db, client, conversation)
    send(db, client, conversation, FAQ_HOURS)  # FAQ-shaped turn, gate rejects

    resp = send(db, client, conversation, "sore spot since my last visit")

    assert resp.meta.get("mode") == "other_reason_detail_captured"
    assert conversation.lead_reason_source_text == "sore spot since my last visit"
    assert FIRST_NAME_QUESTION in resp.reply


def test_valid_name_reply_after_faq_resume_advances(db, fakes):
    client = make_client(db)
    conversation = make_conversation(
        db, client, lead_name="", lead_phone="",
        lead_time_window=None, lead_email_opt_out=False,
        lead_is_new_patient=True,
    )
    _pending_question(db, client, conversation)
    send(db, client, conversation, FAQ_HOURS)  # combined answer + question

    resp = send(db, client, conversation, "Kevin")

    assert (conversation.lead_name or "").strip() == "Kevin"
    # The continuation reply is owned by receptionist_bypass_reply, whose
    # phone wording ("Thanks Kevin! What's the best phone number to reach
    # you?") differs from _next_intake_prompt's — assert the shared phone
    # question core rather than the resume-owner's exact string.
    assert "best phone number to reach you" in resp.reply
    assert resp.reply.count("?") == 1


def test_recognized_service_after_faq_resume_routes_normally(db, fakes):
    """Root Canal / Dentures / Cleaning routing is untouched: a recognized
    service selection right after the FAQ resume advances the normal flow."""
    client = make_client(db)
    conversation = _fresh_generic_lead(db, client)
    _pending_question(db, client, conversation)  # service menu pending
    send(db, client, conversation, FAQ_HOURS)

    resp = send(db, client, conversation, "Cleaning")

    assert conversation.lead_reason == "cleaning/checkup"
    assert FIRST_NAME_QUESTION in resp.reply


# ===========================================================================
# 15-17. The FAQ turn changes nothing it should not change
# ===========================================================================

def test_faq_does_not_mutate_captured_fields(db, fakes):
    client = make_client(db)
    conversation = make_conversation(db, client)
    before = _snapshot_lead_fields(conversation)
    _pending_question(db, client, conversation)

    send(db, client, conversation, FAQ_HOURS)

    assert _snapshot_lead_fields(conversation) == before


def test_faq_does_not_begin_native_booking(db, fakes):
    client = make_client(db, calendar_enabled=True)
    conversation = make_conversation(db, client)  # one answer from completion
    question = _pending_question(db, client, conversation)

    resp = send(db, client, conversation, FAQ_HOURS)

    _assert_answer_then_question_once(resp, HOURS_ANSWER, question)
    assert (conversation.booking_state or "none") == BookingState.NONE
    assert (conversation.lead_status or "").lower() != "completed"
    assert "book" not in resp.reply.lower()


def test_faq_during_priority_intake_preserves_completeness(db, fakes):
    client = make_client(db)
    conversation = make_conversation(
        db, client, lead_is_priority=True, lead_phone="",
        lead_time_window=None, lead_email_opt_out=False,
        lead_is_new_patient=True,
    )
    question = _pending_question(db, client, conversation)
    assert question == PHONE_QUESTION

    resp = send(db, client, conversation, FAQ_HOURS)

    _assert_answer_then_question_once(resp, HOURS_ANSWER, question)
    assert chat_module.priority_intake_is_complete(conversation) is False

    # Answering the resumed question keeps the FULL S3 order: phone is
    # captured, the next required field (email) is asked, and no priority
    # handoff fires early.
    resp2 = send(db, client, conversation, "516-555-9999")
    assert (conversation.lead_phone or "").strip() != ""
    assert "email" in resp2.reply.lower()
    assert chat_module.priority_intake_is_complete(conversation) is False
    assert "call you back shortly" not in resp2.reply.lower()


# ===========================================================================
# 18. Two consecutive FAQs never duplicate or stack questions
# ===========================================================================

def test_two_consecutive_faqs_do_not_stack_questions(db, fakes):
    client = make_client(db)
    conversation = make_conversation(
        db, client, lead_name="", lead_phone="",
        lead_time_window=None, lead_email_opt_out=False,
        lead_is_new_patient=True,
    )
    question = _pending_question(db, client, conversation)
    assert question == FIRST_NAME_QUESTION

    first = send(db, client, conversation, FAQ_HOURS)
    _assert_answer_then_question_once(first, HOURS_ANSWER, question)

    second = send(db, client, conversation, FAQ_INSURANCE)
    _assert_answer_then_question_once(second, INSURANCE_ANSWER, question)
    assert HOURS_ANSWER not in second.reply
    assert (conversation.lead_name or "") == ""


# ===========================================================================
# 19-20. Completed and final-closed conversations are never reopened
# ===========================================================================

def test_faq_after_completion_does_not_reopen_intake(db, fakes):
    client = make_client(db)
    conversation = make_conversation(
        db, client, lead_status="completed", lead_is_new_patient=True,
    )
    # Even a stale pending-question message must not resurrect intake.
    _seed_assistant(db, conversation, FIRST_NAME_QUESTION)

    resp = send(db, client, conversation, FAQ_HOURS)

    assert resp.reply == HOURS_ANSWER
    assert FIRST_NAME_QUESTION not in resp.reply
    assert (conversation.lead_status or "").lower() == "completed"


def test_faq_after_final_closed_stays_closed(db, fakes):
    client = make_client(db)
    conversation = make_conversation(db, client, final_closed=True)
    _seed_assistant(db, conversation, FIRST_NAME_QUESTION)

    resp = send(db, client, conversation, FAQ_HOURS)

    assert resp.meta.get("mode") == "final_closed"
    assert resp.reply == (
        "This conversation has ended. Please tap Start Over to begin a new request."
    )
    assert HOURS_ANSWER not in resp.reply
    assert bool(getattr(conversation, "final_closed", False)) is True


# ===========================================================================
# Revision 3 — ISSUE 1: booking_link_sent suppresses the resume at every
# changed FAQ owner, even with a valid pending question and intake mode on
# ===========================================================================

FAQ_OWNER_CASES = [
    ("operational_hours", FAQ_HOURS, HOURS_ANSWER, "faq_operational_no_match"),
    ("office_phone_guard", FAQ_OFFICE_PHONE, OFFICE_PHONE_ANSWER, "office_phone"),
    ("insurance_guard", FAQ_INSURANCE, INSURANCE_ANSWER, "insurance_info"),
]


@pytest.mark.parametrize(
    "owner_id,faq_text,answer,mode",
    FAQ_OWNER_CASES,
    ids=[c[0] for c in FAQ_OWNER_CASES],
)
def test_booking_link_sent_suppresses_resume_at_every_faq_owner(
        db, fakes, owner_id, faq_text, answer, mode):
    """The `not booking_link_sent` clause of the S8 gate, per changed site:
    incomplete lead + in_intake_mode + a genuinely pending intake question,
    yet no resume once the booking link has been sent."""
    client = make_client(db)
    conversation = make_conversation(
        db, client, lead_name="", lead_phone="",
        lead_time_window=None, lead_email_opt_out=False,
        booking_link_sent=True, lead_is_new_patient=True,
    )
    before = _snapshot_lead_fields(conversation)
    question = _pending_question(db, client, conversation)
    assert question == FIRST_NAME_QUESTION  # valid pending question exists

    resp = send(db, client, conversation, faq_text)

    assert resp.reply == answer                     # the FAQ answer, alone
    assert question not in resp.reply               # pending question absent
    assert "?" not in resp.reply.replace(answer, "")  # no second question
    assert resp.meta.get("mode") == mode            # existing mode retained
    assert _snapshot_lead_fields(conversation) == before
    assert bool(conversation.booking_link_sent) is True
    assert (conversation.booking_state or "none") == BookingState.NONE


# ===========================================================================
# Revision 3 — ISSUE 2: only the LATEST assistant message controls the
# resume; an older intake question in history must not cause resumption
# ===========================================================================

def test_only_latest_assistant_message_controls_resume(db, fakes):
    client = make_client(db)
    conversation = make_conversation(
        db, client, lead_name="", lead_phone="",
        lead_time_window=None, lead_email_opt_out=False,
        lead_is_new_patient=True,
    )
    before = _snapshot_lead_fields(conversation)

    # Older assistant message: a valid intake question. Newer assistant
    # message: a plain non-intake response. Explicit timestamps make the
    # ordering deterministic regardless of insert timing.
    now = datetime.now(dt_timezone.utc)
    db.add(Message(
        conversation_id=conversation.id, role="assistant",
        content=FIRST_NAME_QUESTION, created_at=now - timedelta(minutes=2),
    ))
    db.add(Message(
        conversation_id=conversation.id, role="assistant",
        content="Happy to help!", created_at=now - timedelta(minutes=1),
    ))
    db.commit()
    assert chat_module.last_assistant_asked_intake_question(
        db, conversation.id) is False  # the helper reads the LATEST message

    resp = send(db, client, conversation, FAQ_HOURS)

    assert resp.reply == HOURS_ANSWER               # answer stands alone
    assert FIRST_NAME_QUESTION not in resp.reply    # old question not resumed
    assert "?" not in resp.reply                    # no intake question at all
    assert _snapshot_lead_fields(conversation) == before
    assert (conversation.booking_state or "none") == BookingState.NONE


# ===========================================================================
# Revision 3 — ISSUE 3: FAQ followed by irrelevant text
# ===========================================================================
# Fixture: "my neighbor has a friendly dog" — verified inert against every
# single-text-argument looks_like_*/detect_* owner in chat.py (44 detectors
# falsy; the only truthy one, looks_like_safe_reason_detail, is the Other
# block's safety allowlist and no Other step is pending here). It is not a
# name (looks_like_name_only -> None), service, phone, email, time window,
# new/returning answer, FAQ, info intent, or symptom. Routing owner for the
# turn: the deterministic intake-continuation block -> the byte-identical
# production owner receptionist_bypass_reply -> the name-stage reply below,
# mode "bypass".

IRRELEVANT_FIXTURE = "my neighbor has a friendly dog"
BYPASS_NAME_REPLY = (
    "No problem — I can help you schedule an appointment. What’s your first name?"
)


def test_irrelevant_text_after_faq_resume_does_not_advance(db, fakes):
    client = make_client(db)
    conversation = make_conversation(
        db, client, lead_name="", lead_phone="",
        lead_time_window=None, lead_email_opt_out=False,
        lead_is_new_patient=True,
    )
    question = _pending_question(db, client, conversation)
    first = send(db, client, conversation, FAQ_HOURS)
    _assert_answer_then_question_once(first, HOURS_ANSWER, question)

    # Preconditions pinned against the real owners: the fixture cannot be
    # captured as a name and is not FAQ/info-shaped.
    assert chat_module.looks_like_name_only(IRRELEVANT_FIXTURE) in (None, False)
    assert chat_module.looks_like_info_intent(IRRELEVANT_FIXTURE) is False

    resp = send(db, client, conversation, IRRELEVANT_FIXTURE)

    assert (conversation.lead_name or "") == ""          # field not populated
    assert resp.reply == BYPASS_NAME_REPLY               # existing owner reply
    assert resp.meta.get("mode") == "bypass"             # existing owner mode
    assert resp.reply.count(FIRST_NAME_QUESTION) == 1    # asked once, no stack
    assert HOURS_ANSWER not in resp.reply
    assert (conversation.booking_state or "none") == BookingState.NONE
    assert (conversation.lead_status or "").lower() != "completed"


# ===========================================================================
# Revision 3 — ISSUE 4: FAQ interruption at every meaningful priority stage
# ===========================================================================
# Stage overrides seed a priority (non-emergency, non-symptom) lead exactly
# one field short at each capture-first stage; the pending question is
# computed from the real _next_intake_prompt owner, never hard-coded.

PRIORITY_STAGE_OVERRIDES = {
    "first_name": dict(
        lead_name="", lead_phone="",
        lead_time_window=None, lead_email_opt_out=False,
    ),
    "phone": dict(
        lead_phone="", lead_time_window=None, lead_email_opt_out=False,
    ),
    "email_or_skip": dict(
        lead_time_window=None, lead_email_opt_out=False,
    ),
    "time_window": dict(lead_time_window=None),
    "new_returning": dict(),
}


@pytest.mark.parametrize("stage", sorted(PRIORITY_STAGE_OVERRIDES))
def test_faq_during_priority_stage_resumes_once(db, fakes, stage):
    client = make_client(db)
    conversation = make_conversation(
        db, client, lead_is_priority=True, **PRIORITY_STAGE_OVERRIDES[stage],
    )
    before = _snapshot_lead_fields(conversation)
    question = _pending_question(db, client, conversation)

    resp = send(db, client, conversation, FAQ_HOURS)

    _assert_answer_then_question_once(resp, HOURS_ANSWER, question)
    assert _snapshot_lead_fields(conversation) == before
    assert chat_module.priority_intake_is_complete(conversation) is False
    assert (conversation.lead_status or "").lower() != "completed"
    assert (conversation.booking_state or "none") == BookingState.NONE
    assert "call you back shortly" not in resp.reply.lower()
    assert "contact you shortly" not in resp.reply.lower()
