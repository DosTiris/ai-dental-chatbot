# calendar_tests/test_booking_day_suggestion_flow.py
#
# Database regression tests for the booking-conversation side of the
# availability-suggestion repair.
#
# Requires a throwaway Postgres via TEST_DATABASE_URL — see conftest.py.
#
# Covers:
#   * the exact staging scenario never reports one day as both unavailable
#     and available
#   * neither branch of _suggest_other_days asks a yes/no question, because
#     WAITING_FOR_DATE can only consume a date (Rule 14)
#   * the empty-results office-help fallback still asks for a specific day
#   * a suggested day really does produce a slot menu when chosen
#   * the existing relaxed same-day PREF_ANY offer is unchanged
#
# These call _offer_slots / _handle_date directly. That is deliberate: those
# are the owners under test, and going through chat() would drag in intake
# gating unrelated to this defect. End-to-end conversation coverage already
# lives in test_booking_db.py, which is not modified.

import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from calendar_tests.conftest import make_conversation, requires_db

from app.calendar_models import BookingState
from app.repositories.appointment_repository import create_slot
from app.services import booking_conversation
from app.services.appointment_intent import PREF_MORNING
from app.services.calendar_settings_service import load_calendar_settings

UTC = ZoneInfo("UTC")
pytestmark = requires_db

TOOTH_PAIN = "tooth pain"
CLEANING = "cleaning/checkup"

# Wording that must never come back on a WAITING_FOR_DATE turn. Both are
# yes/no questions the state cannot parse.
BANNED_YES_NO = "Would any of those work?"
BANNED_YES_NO_SENTENCE_START = "Would you like"

# Wording each branch must produce.
SUGGESTION_ASK = "Which day works best? Please reply with the day."
OFFICE_HELP = "The office can help directly."
FALLBACK_ASK = "What other specific day would you like me to check?"


def _now():
    return datetime.now(UTC)


def _tz(settings):
    return ZoneInfo(settings.timezone_name)


def _today_local(settings):
    return _now().astimezone(_tz(settings)).date()


def _seed_slot(db, client, settings, day: date, local_hour: int, service_key=None):
    """Publish one slot at a LOCAL hour on a LOCAL day."""
    start = datetime(
        day.year, day.month, day.day, local_hour, 0, tzinfo=_tz(settings)
    ).astimezone(UTC)
    slot = create_slot(
        db, client.id, start, start + timedelta(minutes=45), service_key=service_key
    )
    db.commit()
    return slot


def _conversation_wanting(db, client, day: date, preference=PREF_MORNING,
                          reason=TOOTH_PAIN):
    conversation = make_conversation(db, client)
    conversation.lead_reason = reason
    conversation.booking_preferred_date = day.isoformat()
    conversation.booking_time_preference = preference
    conversation.booking_state = BookingState.WAITING_FOR_TIME_PREFERENCE
    conversation.booking_offered_slot_ids = None
    conversation.booking_offer_expires_at = None
    conversation.booking_effective_time_preference = None
    db.add(conversation)
    db.commit()
    return conversation


def _fmt(day: date) -> str:
    return booking_conversation._fmt_day(day)


def _final_question(text: str) -> str:
    """The last sentence of a reply, used to prove it is a wh-question."""
    return text.rstrip().split(". ")[-1]


def _assert_no_yes_no_question(reply_text: str) -> None:
    """No branch of _suggest_other_days may ask something the state cannot
    parse. The closing sentence must be a wh-question — that is the
    substantive check; the two string assertions only rule out the known
    sentence-initial forms."""
    assert BANNED_YES_NO not in reply_text
    assert BANNED_YES_NO_SENTENCE_START not in reply_text
    assert _final_question(reply_text).startswith(("What ", "Which ")), (
        f"closing sentence is not a wh-question: {reply_text!r}"
    )


# ---------------------------------------------------------------------------
# 7. The exact staging scenario is no longer self-contradictory
# ---------------------------------------------------------------------------

def test_staging_scenario_never_reports_same_day_available_and_unavailable(
        db, client_row):
    """Three available cleaning/checkup slots on the requested day, against a
    severe-tooth-pain request that asked for the morning — the exact 2026-07-26
    staging data.

    Nothing else is published, so no LATER day matches either. The correct
    outcome is therefore the honest office-help fallback, NOT a suggestion:
    the rejected day must be named once as unavailable and never re-offered.
    """
    settings = load_calendar_settings(client_row)
    requested = _today_local(settings) + timedelta(days=1)
    for hour in (9, 11, 14):
        _seed_slot(db, client_row, settings, requested, hour, service_key=CLEANING)

    conversation = _conversation_wanting(db, client_row, requested)
    reply = booking_conversation._offer_slots(
        db, client_row, conversation, settings, _now()
    )

    label = _fmt(requested)
    assert reply.text.count(label) == 1, (
        "the rejected day must be named once as unavailable and never "
        f"re-offered; got: {reply.text}"
    )
    # No matching later day exists -> honest fallback, no invented option.
    assert OFFICE_HELP in reply.text
    assert FALLBACK_ASK in reply.text
    assert "nearest day" not in reply.text
    assert "nearest days" not in reply.text

    _assert_no_yes_no_question(reply.text)

    assert conversation.booking_state == BookingState.WAITING_FOR_DATE
    assert reply.meta.get("state") == BookingState.WAITING_FOR_DATE
    # No stale offer may survive a suggestion turn (Patch 2C).
    assert conversation.booking_offered_slot_ids is None
    assert conversation.booking_offer_expires_at is None
    assert conversation.booking_effective_time_preference is None


# ---------------------------------------------------------------------------
# Empty results: the office-help fallback must still ask for a day
# ---------------------------------------------------------------------------

def test_empty_results_use_office_help_fallback_and_ask_for_a_day(db, client_row):
    """Every published slot is reserved for another service, so the scan
    correctly returns nothing. The fallback must be honest AND parseable:
    'yes' used to dead-end here in exactly the same way."""
    settings = load_calendar_settings(client_row)
    requested = _today_local(settings) + timedelta(days=1)
    _seed_slot(db, client_row, settings, requested, 14, service_key=CLEANING)
    _seed_slot(db, client_row, settings, requested + timedelta(days=2), 10,
               service_key=CLEANING)
    _seed_slot(db, client_row, settings, requested + timedelta(days=3), 15,
               service_key=CLEANING)

    conversation = _conversation_wanting(db, client_row, requested)
    reply = booking_conversation._offer_slots(
        db, client_row, conversation, settings, _now()
    )

    assert OFFICE_HELP in reply.text
    assert FALLBACK_ASK in reply.text
    assert SUGGESTION_ASK not in reply.text
    _assert_no_yes_no_question(reply.text)
    assert conversation.booking_state == BookingState.WAITING_FOR_DATE
    assert reply.meta.get("state") == BookingState.WAITING_FOR_DATE


# ---------------------------------------------------------------------------
# 8. The suggestion reply asks for a day, not yes/no
# ---------------------------------------------------------------------------

def test_suggestion_reply_asks_for_a_day_and_not_yes_no(db, client_row):
    """WAITING_FOR_DATE can only consume a date, so the prompt must ask for
    one. 'yes' was previously unparseable and dead-ended the conversation."""
    settings = load_calendar_settings(client_row)
    requested = _today_local(settings) + timedelta(days=1)
    suggested = requested + timedelta(days=1)
    _seed_slot(db, client_row, settings, requested, 14, service_key=CLEANING)
    _seed_slot(db, client_row, settings, suggested, 10, service_key=TOOTH_PAIN)

    conversation = _conversation_wanting(db, client_row, requested)
    reply = booking_conversation._offer_slots(
        db, client_row, conversation, settings, _now()
    )

    assert SUGGESTION_ASK in reply.text
    assert _fmt(suggested) in reply.text
    assert OFFICE_HELP not in reply.text
    _assert_no_yes_no_question(reply.text)
    assert conversation.booking_state == BookingState.WAITING_FOR_DATE


def test_suggested_days_are_actually_offered(db, client_row):
    """Two later days with matching morning availability are both named."""
    settings = load_calendar_settings(client_row)
    requested = _today_local(settings) + timedelta(days=1)
    first = requested + timedelta(days=1)
    second = requested + timedelta(days=2)
    _seed_slot(db, client_row, settings, requested, 14, service_key=CLEANING)
    _seed_slot(db, client_row, settings, first, 10)
    _seed_slot(db, client_row, settings, second, 11)

    conversation = _conversation_wanting(db, client_row, requested)
    reply = booking_conversation._offer_slots(
        db, client_row, conversation, settings, _now()
    )

    assert _fmt(first) in reply.text
    assert _fmt(second) in reply.text
    assert " and " in reply.text, "multiple days should read as a list"
    _assert_no_yes_no_question(reply.text)


# ---------------------------------------------------------------------------
# 9. A suggested day really produces slots when the patient picks it
# ---------------------------------------------------------------------------

def test_choosing_a_suggested_day_produces_matching_slots(db, client_row):
    """The contract that was broken: anything Mia suggests must be bookable.
    The patient replies with the suggested day and gets a real menu."""
    settings = load_calendar_settings(client_row)
    requested = _today_local(settings) + timedelta(days=1)
    suggested = requested + timedelta(days=2)
    _seed_slot(db, client_row, settings, requested, 14, service_key=CLEANING)
    _seed_slot(db, client_row, settings, suggested, 10)

    conversation = _conversation_wanting(db, client_row, requested)
    suggestion = booking_conversation._offer_slots(
        db, client_row, conversation, settings, _now()
    )
    assert _fmt(suggested) in suggestion.text

    # The patient answers with that day, exactly as the prompt asked.
    follow_up = booking_conversation._handle_date(
        db, client_row, conversation, settings,
        suggested.strftime("%B %d"), _now(),
    )

    assert conversation.booking_state == BookingState.WAITING_FOR_SLOT_SELECTION
    assert follow_up.meta.get("offered_slots"), (
        f"suggested day produced no bookable menu: {follow_up.text}"
    )
    assert "Which works best?" in follow_up.text


# ---------------------------------------------------------------------------
# 10. The relaxed same-day any-time offer is unchanged
# ---------------------------------------------------------------------------

def test_relaxed_same_day_any_time_offer_unchanged(db, client_row):
    """When the requested day HAS compatible slots outside the preferred
    daypart, the existing relaxed offer still wins — no suggestion turn."""
    settings = load_calendar_settings(client_row)
    requested = _today_local(settings) + timedelta(days=1)
    _seed_slot(db, client_row, settings, requested, 14)  # afternoon, no service lock

    conversation = _conversation_wanting(db, client_row, requested)
    reply = booking_conversation._offer_slots(
        db, client_row, conversation, settings, _now()
    )

    assert "but I do have" in reply.text
    assert SUGGESTION_ASK not in reply.text
    assert OFFICE_HELP not in reply.text
    assert BANNED_YES_NO not in reply.text
    assert conversation.booking_state == BookingState.WAITING_FOR_SLOT_SELECTION
    assert conversation.booking_effective_time_preference == "any"
