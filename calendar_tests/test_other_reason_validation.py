# calendar_tests/test_other_reason_validation.py
#
# S7 — Balanced "Other" reason validation synchronization.
#
# Backports the production dental-relevance gate into the primary Other
# free-text capture path: classify_other_reason_detail() (verdicts "dental" /
# "unclear" / "non_dental"), the two production rejection reply builders, and
# the production pending-phrase list in last_assistant_asked_for_other_reason()
# so a rejected patient can retry inside the capture flow.
#
# The classifier vocabulary carries five owner-approved calendar deviations
# (D1-D5: "sore spot"/"sore spots"/"metallic taste" auto-accept,
# "irritation"/"irritated" problem terms, "book"/"booking"/"booked" request
# filler, "last" modifier). Everything else is verbatim production.
#
# Revision 2 (test-only correction; application patch unchanged):
#   - Non-library fixtures are POSITIVELY proven non-library inside these
#     tests (unconditional detect_library_dental_service() is None), and
#     their derived detail must equal the exact submitted text.
#   - Fixtures whose text maps to a specific legacy reason enum ("tooth
#     pain") are asserted against the real existing owner contract:
#     lead_reason carries the specific reason and the derived detail is
#     empty by get_other_reason_detail()'s existing rules.
#   - "I need a cleaning" real-flow recognized-service route added.
#   - The no-overwrite test also pins the derived-detail owner output.
#
# Two layers of proof:
#   1) The owner-pinned direct classifier matrix (real vocabulary, real
#      service library — no external harness).
#   2) Real chat() flow tests through the shared integration harness.

import pytest

import app.routes.chat as chat_module
from app.calendar_models import BookingState
from app.models import Message

from calendar_tests.test_chat_integration import (  # noqa: F401
    fakes,
    make_client,
    make_conversation,
    send,
)

REASON_QUESTION_FRAGMENTS = [
    "what brings you in",
    "what would you like the appointment for",
    "what do you need an appointment for",
]

GENERIC_SEED_SOURCE = "I need an appointment"


def _fresh_generic_lead(db, client):
    """The 'Book Appointment' state: generic appointment request captured,
    nothing else."""
    return make_conversation(
        db, client,
        lead_reason="appointment request",
        lead_reason_source_text=GENERIC_SEED_SOURCE,
        lead_name="",
        lead_phone="",
        lead_time_window=None,
        lead_email_opt_out=False,
        lead_is_new_patient=None,
    )


def _assert_no_reason_question(reply: str):
    low = (reply or "").lower()
    for frag in REASON_QUESTION_FRAGMENTS:
        assert frag not in low
    assert "which of these" not in low  # service menu re-prompt


def _enter_other_step(db, client, conversation):
    """First turn of the two-turn Other flow: select Other, get the prompt."""
    resp = send(db, client, conversation, "Other")
    assert resp.meta.get("mode") == "other_service_prompt"
    assert resp.reply == chat_module.build_other_reason_prompt()
    return resp


def _assert_rejected_and_pending(db, conversation, resp, expected_reply,
                                 expected_mode):
    """A rejected Other detail: exact production wording, nothing persisted,
    no intake advance, no booking, Other step still pending."""
    assert resp.reply == expected_reply
    assert resp.meta.get("mode") == expected_mode
    # Nothing persisted: the seeded generic source and reason are untouched.
    assert conversation.lead_reason_source_text == GENERIC_SEED_SOURCE
    assert conversation.lead_reason == "appointment request"
    # No advance to first name, no booking.
    assert "first name" not in (resp.reply or "").lower()
    assert (conversation.booking_state or "none") == BookingState.NONE
    assert resp.reply.count("?") <= 1
    # The Other step stays pending — the pending-phrase update in
    # last_assistant_asked_for_other_reason() must recognize this rejection.
    assert chat_module.last_assistant_asked_for_other_reason(
        db, conversation.id) is True


# Fixtures intended to represent NON-LIBRARY Other details. Every entry is
# POSITIVELY proven non-library inside the tests below (unconditional
# executed assertion against the real service library), and for every entry
# the derived-detail owner must return the exact submitted text.
NON_LIBRARY_EXACT_DETAIL_FIXTURES = [
    # Locked S6 fixture (owner decision: MUST remain valid; token "visit").
    "sore spot since my last visit",
    # Meaningful detail containing "last visit" (D2 + D4).
    "gum irritation after my last visit",
    # D5: metallic taste.
    "There is a metallic taste in my mouth",
    # Meaningful detail containing "schedule" (production filler).
    "I need to schedule a visit because my jaw clicks",
]

# Valid Other details whose text maps to a SPECIFIC legacy reason enum via
# map_reason_detail_to_enum(). The existing owner contract (unchanged by S7):
# lead_reason stores the specific enum, lead_reason_source_text stores the
# exact submitted text, and get_other_reason_detail() returns "" because a
# specific lead_reason already carries the reason —
# conversation_has_specific_lead_reason() is True through lead_reason alone.
# (text, expected_lead_reason, expected_library_display_or_None)
MAPPED_ENUM_VALID_FIXTURES = [
    # Meaningful detail containing "appointment" (production filler);
    # verified non-library, but "pain" maps it to the tooth pain enum.
    ("gum pain appointment", "tooth pain", None),
    # Meaningful detail containing "book" (D3); library-recognized text.
    ("my tooth hurts and I want to book a visit", "tooth pain", "Tooth Pain"),
]


def _assert_captured_flow_common(conversation, resp):
    assert resp.meta.get("mode") == "other_reason_detail_captured"
    # Package A: once the Other detail is a valid, authoritative dental reason,
    # New/Returning is the next question (before name).
    assert "new or returning patient" in resp.reply
    assert resp.reply.count("?") == 1  # at most one required question
    _assert_no_reason_question(resp.reply)
    assert resp.meta.get("mode") != "booking"
    assert (conversation.booking_state or "none") == BookingState.NONE


def _assert_captured_non_library_exact(conversation, resp, detail_text):
    # POSITIVE non-library proof against the real service library — executed
    # here, not merely assumed by fixture placement.
    assert chat_module.detect_library_dental_service(detail_text) is None
    _assert_captured_flow_common(conversation, resp)
    # Exact persistence and exact derived detail.
    assert conversation.lead_reason_source_text == detail_text
    assert (conversation.lead_reason or "").strip() != ""
    assert chat_module.get_other_reason_detail(conversation) == detail_text


# ===========================================================================
# 1. Owner-pinned direct classifier matrix (real service library)
# ===========================================================================

PINNED_DENTAL = [
    "I have a sore spot in my mouth",
    "I have a sore spot since my last visit",
    "sore spot since my last visit",
    "I have gum irritation",
    "gum irritation after my last visit",
    "There is a metallic taste in my mouth",
    "My jaw clicks when I chew",
    "my tooth hurts and I want to book a visit",
    "book me for jaw pain",
    "I need to schedule a visit because my jaw clicks",
    "I need a root canal",
    "I need dentures",
    "I need a cleaning",
]

PINNED_UNCLEAR = [
    "not a root canal",
    "I do not need a cleaning",
]

PINNED_NON_DENTAL = [
    "appointment",
    "I need an appointment",
    "something",
    "not sure",
    "other",
    "yes",
    "okay",
    "my knee hurts",
    "pizza delivery",
    "new tires",
    "what time is it",
    # D1 guardrail
    "sore spot on my arm",
    # D2 guardrails
    "knee irritation",
    "skin irritation",
    # D3 guardrails
    "book a hotel",
    "book club last week",
    "booking a flight",
    # D4 guardrail
    "last minute meeting",
    # D5 guardrails
    "metallic taste in music",
    "metallic paint",
    # library-alias words in non-dental contexts (coverage rule B)
    "knee braces",
    "legal retainer",
    "wood veneer",
]


@pytest.mark.parametrize("text", PINNED_DENTAL)
def test_classifier_pins_dental(text):
    assert chat_module.classify_other_reason_detail(text) == "dental"


@pytest.mark.parametrize("text", PINNED_UNCLEAR)
def test_classifier_pins_unclear(text):
    assert chat_module.classify_other_reason_detail(text) == "unclear"


@pytest.mark.parametrize("text", PINNED_NON_DENTAL)
def test_classifier_pins_non_dental(text):
    assert chat_module.classify_other_reason_detail(text) == "non_dental"


@pytest.mark.parametrize("text", [
    "<script>alert(1)</script>",  # payload shape
    "a" * 300,                    # overlength
])
def test_unsafe_owner_still_rejects_before_classification(text):
    # Input safety remains the EXISTING owner's job; the classifier is not a
    # second safety system.
    assert chat_module.looks_like_safe_reason_detail(text) is False


# ===========================================================================
# 2. Real chat() flow — acceptance
# ===========================================================================

def test_other_selection_shows_other_prompt(db, fakes):
    client = make_client(db)
    conversation = _fresh_generic_lead(db, client)
    _enter_other_step(db, client, conversation)


@pytest.mark.parametrize("detail_text", NON_LIBRARY_EXACT_DETAIL_FIXTURES)
def test_non_library_fixtures_are_proven_non_library(detail_text):
    # Issue 1 (Rev 2): the intended non-library status of every fixture is a
    # tested fact against the real calendar service library, not a comment.
    assert chat_module.detect_library_dental_service(detail_text) is None


@pytest.mark.parametrize("detail_text", NON_LIBRARY_EXACT_DETAIL_FIXTURES)
def test_valid_non_library_detail_persists_exactly_and_advances(db, fakes,
                                                                detail_text):
    client = make_client(db)
    conversation = _fresh_generic_lead(db, client)
    _enter_other_step(db, client, conversation)
    resp = send(db, client, conversation, detail_text)
    _assert_captured_non_library_exact(conversation, resp, detail_text)


@pytest.mark.parametrize(
    "detail_text, expected_reason, expected_library_display",
    MAPPED_ENUM_VALID_FIXTURES)
def test_valid_mapped_enum_detail_persists_and_advances(db, fakes,
                                                        detail_text,
                                                        expected_reason,
                                                        expected_library_display):
    # Executed pin of the library status stated in the fixture table.
    matched = chat_module.detect_library_dental_service(detail_text)
    if expected_library_display is None:
        assert matched is None
    else:
        assert matched is not None
        assert matched.display_name == expected_library_display

    client = make_client(db)
    conversation = _fresh_generic_lead(db, client)
    _enter_other_step(db, client, conversation)
    resp = send(db, client, conversation, detail_text)
    _assert_captured_flow_common(conversation, resp)
    # Exact source persistence; specific enum reason; derived detail follows
    # the EXISTING owner contract (empty when lead_reason is specific).
    assert conversation.lead_reason_source_text == detail_text
    assert conversation.lead_reason == expected_reason
    assert chat_module.conversation_has_specific_lead_reason(conversation)
    assert chat_module.get_other_reason_detail(conversation) == ""


def test_valid_capture_then_intake_continues_normally(db, fakes):
    # The turn AFTER a valid capture continues normal intake (name captured,
    # phone asked) — the capture did not corrupt the state machine.
    client = make_client(db)
    conversation = _fresh_generic_lead(db, client)
    _enter_other_step(db, client, conversation)
    send(db, client, conversation, "sore spot since my last visit")
    # Package A: New/Returning is asked first once the Other detail is valid.
    send(db, client, conversation, "new patient")
    resp2 = send(db, client, conversation, "Kevin")
    _assert_no_reason_question(resp2.reply)
    assert conversation.lead_name.strip() != ""
    assert "phone number" in resp2.reply.lower()
    assert resp2.reply.count("?") == 1


# ===========================================================================
# 3. Real chat() flow — rejection without persistence, then successful retry
# ===========================================================================

@pytest.mark.parametrize("invalid_text", [
    "pizza delivery",        # clearly off-topic
    "appointment",           # generic
    "something",             # incomplete
    "not sure",              # incomplete
    "yes",                   # incomplete
])
def test_non_dental_rejected_exact_wording_nothing_persisted(db, fakes,
                                                             invalid_text):
    client = make_client(db)
    conversation = _fresh_generic_lead(db, client)
    _enter_other_step(db, client, conversation)
    resp = send(db, client, conversation, invalid_text)
    _assert_rejected_and_pending(
        db, conversation, resp,
        chat_module.build_non_dental_reason_detail_reply(),
        "non_dental_other_reason_detail",
    )


def test_valid_detail_accepted_immediately_after_non_dental_rejection(db,
                                                                      fakes):
    # Proves the last_assistant_asked_for_other_reason() pending-phrase
    # update: the rejection wording keeps the Other step active, so the
    # very next valid reply is captured normally.
    client = make_client(db)
    conversation = _fresh_generic_lead(db, client)
    _enter_other_step(db, client, conversation)
    send(db, client, conversation, "pizza delivery")
    resp = send(db, client, conversation, "gum irritation after my last visit")
    _assert_captured_non_library_exact(
        conversation, resp, "gum irritation after my last visit")


def test_unclear_negated_wording_rejected_then_valid_retry_accepted(db,
                                                                    fakes):
    client = make_client(db)
    conversation = _fresh_generic_lead(db, client)
    _enter_other_step(db, client, conversation)
    resp = send(db, client, conversation, "not a root canal")
    _assert_rejected_and_pending(
        db, conversation, resp,
        chat_module.build_unclear_dental_reason_reply(),
        "unclear_other_reason_detail",
    )
    resp2 = send(db, client, conversation, "I need a root canal")
    assert resp2.meta.get("mode") == "other_reason_detail_captured"
    assert conversation.lead_reason_source_text == "I need a root canal"
    assert "new or returning patient" in resp2.reply


def test_unsafe_text_uses_existing_unsafe_owner_and_stays_retryable(db,
                                                                    fakes):
    client = make_client(db)
    conversation = _fresh_generic_lead(db, client)
    _enter_other_step(db, client, conversation)
    resp = send(db, client, conversation, "<script>alert(1)</script>")
    _assert_rejected_and_pending(
        db, conversation, resp,
        chat_module.build_unsafe_reason_detail_reply(),
        "unsafe_other_reason_detail",
    )
    resp2 = send(db, client, conversation, "sore spot since my last visit")
    _assert_captured_non_library_exact(
        conversation, resp2, "sore spot since my last visit")


# ===========================================================================
# 4. Recognized services keep their existing routes (not forced through the
#    Other validator), and S6 protections hold
# ===========================================================================

@pytest.mark.parametrize("service_text, expected_source", [
    ("I need a root canal", "I need a root canal"),
    ("I need dentures", "I need dentures"),
])
def test_recognized_services_still_route_via_enrichment(db, fakes,
                                                        service_text,
                                                        expected_source):
    # From the generic-lead state (NOT the Other prompt) recognized services
    # continue through the S6 enrichment route and advance — they are never
    # forced through the Other validator.
    client = make_client(db)
    conversation = _fresh_generic_lead(db, client)
    resp = send(db, client, conversation, service_text)
    assert resp.meta.get("mode") not in (
        "non_dental_other_reason_detail",
        "unclear_other_reason_detail",
        "unsafe_other_reason_detail",
    )
    assert conversation.lead_reason_source_text == expected_source
    assert "new or returning patient" in resp.reply
    assert resp.reply.count("?") == 1


def test_cleaning_routes_via_reason_replacement(db, fakes):
    # Issue 2 (Rev 2). "I need a cleaning" maps to its OWN legacy enum
    # ("cleaning/checkup" — executed against detect_service_selection()), so
    # from the generic-lead state it takes the existing reason-REPLACEMENT
    # route, not the S6 enrichment route and never the Other validator:
    # lead_reason becomes "cleaning/checkup"; the existing non-empty source
    # is left untouched by that route's own rule (source is written only
    # when blank).
    client = make_client(db)
    conversation = _fresh_generic_lead(db, client)
    resp = send(db, client, conversation, "I need a cleaning")
    assert resp.meta.get("mode") not in (
        "non_dental_other_reason_detail",
        "unclear_other_reason_detail",
        "unsafe_other_reason_detail",
    )
    assert conversation.lead_reason == "cleaning/checkup"
    assert conversation.lead_reason_source_text == GENERIC_SEED_SOURCE
    assert chat_module.conversation_has_specific_lead_reason(conversation)
    assert "new or returning patient" in resp.reply
    assert resp.reply.count("?") == 1
    _assert_no_reason_question(resp.reply)
    assert (conversation.booking_state or "none") == BookingState.NONE


def test_existing_meaningful_source_text_not_overwritten(db, fakes):
    # S6 no-overwrite protection is untouched by S7: a later generic-bucket
    # service message must not replace an existing meaningful source.
    client = make_client(db)
    seeded = "sore spot since my last visit"
    conversation = make_conversation(
        db, client,
        lead_reason="appointment request",
        lead_reason_source_text=seeded,
        lead_name="",
        lead_phone="",
        lead_time_window=None,
        lead_email_opt_out=False,
        lead_is_new_patient=None,
    )
    send(db, client, conversation, "I need a root canal")
    assert conversation.lead_reason_source_text == seeded
    # Issue 3 (Rev 2): the derived-detail owner remains intact — the
    # meaningful seeded source is still the derived detail.
    assert chat_module.get_other_reason_detail(conversation) == seeded
