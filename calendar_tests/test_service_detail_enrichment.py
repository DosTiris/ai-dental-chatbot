# calendar_tests/test_service_detail_enrichment.py
#
# S6 — Root Canal / Dentures service-detail enrichment.
#
# Real chat() flow tests via the shared integration harness. Proves that a
# recognized specific service whose legacy bucket is the generic
# "appointment request" (Root Canal, Dentures) ENRICHES the existing generic
# reason — the specific submitted message stored in lead_reason_source_text,
# with get_other_reason_detail() (the single existing owner) deriving the
# display label; lead_reason preserved; intake advancing to first name —
# instead of looping on the
# reason question. Plain generic appointment wording still asks for the
# reason. The Other free-text path is untouched (S7 scope).

import pytest

import app.routes.chat as chat_module
from app.calendar_models import BookingState

from calendar_tests.test_chat_integration import (  # noqa: F401
    fakes,
    make_client,
    make_conversation,
    send,
)

# Representative office-CONFIGURED quick-reply button messages for these
# services (the default widget button set does not include them; offices add
# them via serviceButtons config). The S1 label/message separation is proven
# by the Node tests; these strings match the library aliases exactly.
ROOT_CANAL_BUTTON_MESSAGE = "I need root canal treatment"
DENTURES_BUTTON_MESSAGE = "I need dentures"

REASON_QUESTION_FRAGMENTS = [
    "what brings you in",
    "what would you like the appointment for",
    "what do you need an appointment for",
]


def _fresh_generic_lead(db, client):
    """The 'Book Appointment' state: generic appointment request captured,
    nothing else — the state that used to loop."""
    return make_conversation(
        db, client,
        lead_reason="appointment request",
        lead_reason_source_text="I need an appointment",
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


def _assert_enriched_and_advanced(conversation, resp, expected_detail,
                                  expected_source):
    assert conversation.lead_reason == "appointment request"  # preserved
    # Calendar detail contract: lead_reason_source_text stores the submitted
    # specific message (replaced only because the prior source was generic);
    # get_other_reason_detail() derives the clean display label from it.
    assert chat_module.get_other_reason_detail(conversation) == expected_detail
    assert conversation.lead_reason_source_text == expected_source
    assert conversation.lead_reason_source_text != "I need an appointment"
    assert "new or returning patient" in resp.reply  # Package A: patient type first
    assert resp.reply.count("?") == 1  # at most one required question
    _assert_no_reason_question(resp.reply)


# ---------------------------------------------------------------------------
# 1-6: typed and button messages advance without looping
# ---------------------------------------------------------------------------

def test_typed_root_canal_enriches_and_advances(db, fakes):
    client = make_client(db)
    conversation = _fresh_generic_lead(db, client)
    resp = send(db, client, conversation, "I need a root canal")
    _assert_enriched_and_advanced(conversation, resp, "Root Canal",
                                  "I need a root canal")


def test_typed_dentures_enriches_and_advances(db, fakes):
    client = make_client(db)
    conversation = _fresh_generic_lead(db, client)
    resp = send(db, client, conversation, "I need dentures")
    _assert_enriched_and_advanced(conversation, resp, "Dentures",
                                  "I need dentures")


def test_root_canal_button_message_enriches_and_advances(db, fakes):
    client = make_client(db)
    conversation = _fresh_generic_lead(db, client)
    resp = send(db, client, conversation, ROOT_CANAL_BUTTON_MESSAGE)
    _assert_enriched_and_advanced(conversation, resp, "Root Canal",
                                  ROOT_CANAL_BUTTON_MESSAGE)


def test_dentures_button_message_enriches_and_advances(db, fakes):
    client = make_client(db)
    conversation = _fresh_generic_lead(db, client)
    resp = send(db, client, conversation, DENTURES_BUTTON_MESSAGE)
    _assert_enriched_and_advanced(conversation, resp, "Dentures",
                                  DENTURES_BUTTON_MESSAGE)


def test_root_canal_does_not_loop_on_second_turn(db, fakes):
    # The pre-fix defect was a LOOP: the reason question repeated. Prove the
    # next turn continues normal intake (name captured, phone asked).
    client = make_client(db)
    conversation = _fresh_generic_lead(db, client)
    send(db, client, conversation, "I need a root canal")
    # Package A: New/Returning is asked first once the reason is authoritative.
    send(db, client, conversation, "new patient")
    resp2 = send(db, client, conversation, "Kevin")
    _assert_no_reason_question(resp2.reply)
    assert conversation.lead_name.strip() != ""
    assert "phone number" in resp2.reply.lower()
    assert resp2.reply.count("?") == 1


def test_enrichment_never_overwrites_existing_detail(db, fakes):
    # Meaningful patient-provided detail must never be silently replaced by
    # a later generic-bucket service message. The single narrow owner is
    # False for the seeded source (meaningful non-vocabulary tokens), so
    # the enrichment replacement gate must leave it untouched.
    client = make_client(db)
    seeded = "sore spot since my last visit"
    # Verified with the real library owner: the seed names no DentalService,
    # and the single owner classifies it as NOT generic (not replaceable).
    assert chat_module.detect_library_dental_service(seeded) is None
    assert chat_module.source_text_is_generic_appointment_wording(seeded) is False
    conversation = make_conversation(
        db, client,
        lead_reason="appointment request",
        lead_reason_source_text=seeded,
        lead_name="",
    )
    send(db, client, conversation, "I need a root canal")
    assert conversation.lead_reason_source_text == seeded
    assert chat_module.get_other_reason_detail(conversation) == seeded

    # An existing LIBRARY-service source is equally protected.
    conversation2 = make_conversation(
        db, client,
        lead_reason="appointment request",
        lead_reason_source_text="I need dentures",
        lead_name="",
    )
    send(db, client, conversation2, "I need a root canal")
    assert conversation2.lead_reason_source_text == "I need dentures"
    assert chat_module.get_other_reason_detail(conversation2) == "Dentures"


# ---------------------------------------------------------------------------
# 9-10: generic wording still asks for the reason
# ---------------------------------------------------------------------------

def test_generic_appointment_request_still_asks_reason(db, fakes):
    client = make_client(db)
    conversation = make_conversation(
        db, client,
        lead_reason=None, lead_reason_source_text=None,
        lead_name="", lead_phone="",
        lead_time_window=None, lead_email_opt_out=False,
        lead_is_new_patient=None,
    )
    resp = send(db, client, conversation, "I need an appointment")
    # Still incomplete: the reply IS the existing reason/service prompt
    # (owner-based equality), the widget is told to show the service menu,
    # exactly one question is asked, nothing advances, nothing books.
    assert chat_module.build_service_menu_prompt(client) in resp.reply
    assert chat_module.reply_should_show_service_menu(resp.reply) is True
    if "show_service_menu" in (resp.meta or {}):
        assert resp.meta["show_service_menu"] is True
    assert resp.reply.count("?") == 1
    assert "first name" not in resp.reply.lower()
    assert conversation.lead_reason == "appointment request"
    assert chat_module.get_other_reason_detail(conversation) == ""
    assert (conversation.booking_state or "none") == BookingState.NONE


def test_generic_appointment_please_does_not_advance(db, fakes):
    client = make_client(db)
    conversation = make_conversation(
        db, client,
        lead_reason=None, lead_reason_source_text=None,
        lead_name="", lead_phone="",
        lead_time_window=None, lead_email_opt_out=False,
        lead_is_new_patient=None,
    )
    resp = send(db, client, conversation, "appointment please")
    # Same owner as the full generic phrase: the reason/service prompt
    # remains active and intake does not advance.
    assert chat_module.build_service_menu_prompt(client) in resp.reply
    assert chat_module.reply_should_show_service_menu(resp.reply) is True
    if "show_service_menu" in (resp.meta or {}):
        assert resp.meta["show_service_menu"] is True
    assert resp.reply.count("?") == 1
    assert "first name" not in resp.reply.lower()
    assert conversation.lead_reason == "appointment request"
    assert chat_module.get_other_reason_detail(conversation) == ""
    assert (conversation.booking_state or "none") == BookingState.NONE


# ---------------------------------------------------------------------------
# 11-12: existing services and the Other path unchanged
# ---------------------------------------------------------------------------

def test_cleaning_checkup_service_unchanged(db, fakes):
    client = make_client(db)
    conversation = make_conversation(
        db, client,
        lead_reason=None, lead_reason_source_text=None,
        lead_name="", lead_phone="",
        lead_time_window=None, lead_email_opt_out=False,
        lead_is_new_patient=None,
    )
    resp = send(db, client, conversation, "I need a cleaning")
    # Distinct legacy bucket: captured by the pre-existing primary branch.
    assert conversation.lead_reason == "cleaning/checkup"
    assert "new or returning" in resp.reply.lower()  # Package A: patient type first
    assert resp.reply.count("?") == 1


def test_other_path_unchanged(db, fakes):
    client = make_client(db)
    conversation = _fresh_generic_lead(db, client)
    resp = send(db, client, conversation, "Other")
    # The Other free-text prompt is the pre-existing owner, untouched by S6
    # (its balanced validator is S7 scope).
    assert resp.meta.get("mode") == "other_service_prompt"
    assert chat_module.build_other_reason_prompt() == resp.reply


# ---------------------------------------------------------------------------
# 15: native booking still waits for the full intake
# ---------------------------------------------------------------------------

def test_native_booking_does_not_begin_before_intake_complete(db, fakes):
    client = make_client(db, calendar_enabled=True)
    conversation = _fresh_generic_lead(db, client)
    resp = send(db, client, conversation, "I need a root canal")
    # Enrichment advances intake — it must not jump into the Calendar.
    assert resp.meta.get("mode") != "booking"
    assert (conversation.booking_state or "none") == BookingState.NONE
    assert "new or returning patient" in resp.reply  # Package A: patient type first


# ---------------------------------------------------------------------------
# Revision 5: two-turn Other flow and the narrow-classifier matrix
# ---------------------------------------------------------------------------

def test_other_flow_preserves_meaningful_detail_with_scheduling_token(db, fakes):
    # REAL two-turn Other flow. The detail "sore spot since my last visit"
    # is verified non-library and contains the scheduling token "visit":
    # revision 4 wrongly blanked such details; revision 5 must preserve
    # them and advance intake.
    client = make_client(db)
    conversation = _fresh_generic_lead(db, client)

    resp1 = send(db, client, conversation, "Other")
    assert resp1.meta.get("mode") == "other_service_prompt"
    assert resp1.reply == chat_module.build_other_reason_prompt()

    detail_text = "sore spot since my last visit"
    # Executable proof against the real library owner: no DentalService
    # matches the chosen detail (not a comment, not an external harness).
    assert chat_module.detect_library_dental_service(detail_text) is None
    resp2 = send(db, client, conversation, detail_text)

    assert resp2.meta.get("mode") == "other_reason_detail_captured"
    # Exact source persisted; the meaningful detail is the derived detail.
    assert conversation.lead_reason_source_text == detail_text
    assert chat_module.get_other_reason_detail(conversation) == detail_text
    # Package A: intake advances to New/Returning first; menu is NOT repeated.
    assert "new or returning patient" in resp2.reply
    assert resp2.reply.count("?") == 1
    _assert_no_reason_question(resp2.reply)
    assert (conversation.booking_state or "none") == BookingState.NONE


def test_generic_wording_single_owner_matrix(db, fakes):
    # The SINGLE generic-wording owner, executed against the real service
    # library: True ONLY for blank/scheduling-only wording; False for
    # meaningful details containing scheduling tokens, for service-naming
    # text, and for both configured button messages.
    N = chat_module.source_text_is_generic_appointment_wording
    assert not hasattr(chat_module, "source_text_is_scheduling_only_wording")
    assert N("") is True
    assert N("I need an appointment") is True
    assert N("appointment please") is True
    assert N("book a visit") is True
    assert N("please schedule an appointment") is True
    assert N("sore spot since my last visit") is False
    assert N("gum irritation after my last visit") is False
    # Meaningful non-library details containing "schedule" / "book":
    assert chat_module.detect_library_dental_service(
        "burning tongue that follows my work schedule") is None
    assert N("burning tongue that follows my work schedule") is False
    assert chat_module.detect_library_dental_service(
        "cheek irritation from my book club snacks") is None
    assert N("cheek irritation from my book club snacks") is False
    assert N("I need a root canal") is False
    assert N("I need dentures") is False
    assert N(ROOT_CANAL_BUTTON_MESSAGE) is False
    assert N(DENTURES_BUTTON_MESSAGE) is False
    assert N("metallic taste in my mouth") is False
