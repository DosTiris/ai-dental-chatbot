# calendar_tests/test_checkpoint_b_time_window_routing.py
#
# Checkpoint B: time-window intake capture routed through the one capture
# owner, and the Calendar start seeded from the captured canonical value.
#
# Staging defects these pin (2026-07-27, STAGING_CONVERSATIONS_2026-07-27):
#
#   Patient: July 28 morning
#   Mia:     Great — Tuesday, July 28. Do you prefer morning or afternoon?
#
#   Patient: my pain is 7/10 and I can come in on July 28 morning
#   Mia:     The office is currently booking up to 30 days ahead. Could you
#            pick a sooner day?
#
# Root causes proven in code review:
#   1. The early intake guard stored the complete canonical value
#      ("Tue 2026-07-28 morning"), but route_completed_lead started the
#      Calendar dialog by RE-PARSING the raw message: _handle_start read
#      only the date and dropped the already-answered "morning", and for
#      messages carrying a rating token the pure intent parser picked the
#      WRONG candidate ("7/10" -> July 10 rolled to NEXT YEAR), which is
#      what reached the booking-horizon check.
#   2. The early guard was a second capture implementation (Rule 3
#      violation): a stored day-only explicit date was REPLACED by a later
#      bare "morning" answer instead of merged, and the owner's merge built
#      merged values from the weekday token alone, dropping the ISO date.
#
# Checkpoint B routes the guard through handle_time_window_capture() and
# passes chat.py-derived seeds (explicit date + PREF_* bucket) into
# begin_booking_after_intake, so the Calendar consumes the value the
# capture owner validated. These tests drive the REAL chat() endpoint
# against PostgreSQL (skipped without TEST_DATABASE_URL — see conftest.py).

import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from app.routes import chat as chat_module
    HAVE_CHAT = True
except ModuleNotFoundError:  # pragma: no cover - environment guard
    HAVE_CHAT = False

pytestmark = pytest.mark.skipif(not HAVE_CHAT, reason="app.routes.chat requires SQLAlchemy/FastAPI")

ISO_IN_TEXT = re.compile(r"\d{4}-\d{2}-\d{2}")

from calendar_tests.conftest import make_conversation as _base_make_conversation, requires_db  # noqa: E402
from calendar_tests.test_chat_integration import (  # noqa: E402,F401
    OPEN_ALL_WEEK_HOURS,
    fakes,
    make_client,
    make_conversation as make_normal_flow_conversation,
    make_slot,
    send,
)

# The SAME date owner the Calendar start uses resolves the expected date in
# these tests — no test-side weekday arithmetic (rev2 handoff requirement).
from app.services.appointment_intent import parse_preferred_date  # noqa: E402

if HAVE_CHAT:
    from app.calendar_models import BookingState  # noqa: E402

NY = ZoneInfo("America/New_York")

# The horizon the make_client fixture configures (calendar settings
# max_booking_days). Tests assert dates INSIDE it are never bounced.
CONFIGURED_HORIZON_DAYS = 30

HORIZON_REJECTION_FRAGMENT = "days ahead"
SOONER_DAY_FRAGMENT = "pick a sooner day"


def client_today():
    """The office-local (America/New_York) current date — the same clock the
    endpoint uses to resolve month/day phrases and the booking horizon."""
    return datetime.now(NY).date()


def weekday_target(days_ahead):
    """A weekday (Mon-Fri) at least `days_ahead` days out, office-local.

    Weekday-only so the capture owner's weekend validation (Sunday nudge /
    Saturday handling) can never make a fixture flaky by run date. Callers
    keep days_ahead <= 23 so the rolled date stays inside the configured
    30-day horizon."""
    d = client_today() + timedelta(days=days_ahead)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    assert (d - client_today()).days <= CONFIGURED_HORIZON_DAYS
    return d


def month_phrase(d):
    """'August 12' — the spelled-out form patients typed on staging."""
    return d.strftime("%B %d").replace(" 0", " ")


def canonical(d, detail=""):
    base = f"{d.strftime('%a')} {d.isoformat()}"
    return f"{base} {detail}".strip()


def make_short_symptom_conversation(db, client, **overrides):
    """A short-symptom-flow conversation whose ONLY missing capture-first
    field is the time window (name + phone + symptom reason present), the
    exact shape of every staging transcript conversation."""
    conversation = _base_make_conversation(db, client)
    fields = dict(
        lead_reason="tooth pain",
        lead_name="Kyle",
        lead_phone="516-555-5555",
        lead_time_window=None,
        lead_email_opt_out=False,
        lead_is_new_patient=None,
    )
    fields.update(overrides)
    for field_name, field_value in fields.items():
        setattr(conversation, field_name, field_value)
    db.add(conversation)
    db.commit()
    db.refresh(conversation)
    return conversation


def booking_client(db):
    """Internal-calendar office with production-realistic seven-day hours."""
    return make_client(db, calendar_enabled=True, office_hours=OPEN_ALL_WEEK_HOURS)


def seed_slot_on(db, client, target, hour=10):
    """One staff-published slot on the target office-local date."""
    return make_slot(db, client, days_ahead=(target - client_today()).days, hour=hour)


def meta_strings(obj):
    """Every string anywhere inside the browser metadata."""
    if isinstance(obj, dict):
        for value in obj.values():
            yield from meta_strings(value)
    elif isinstance(obj, (list, tuple, set)):
        for value in obj:
            yield from meta_strings(value)
    elif isinstance(obj, str):
        yield obj


# ===========================================================================
# 1-3: a complete date + detail books in ONE turn — no morning/afternoon
# re-ask (staging conversations 1, 3, 4, 5, 8).
# ===========================================================================

@requires_db
def test_complete_explicit_date_plus_morning_books_in_one_turn(db, fakes):
    client = booking_client(db)
    conversation = make_short_symptom_conversation(db, client)
    target = weekday_target(14)
    seed_slot_on(db, client, target)

    resp = send(db, client, conversation, f"{month_phrase(target)} morning")

    assert resp.meta.get("mode") == "booking"
    assert "morning or afternoon" not in resp.reply.lower(), resp.reply
    assert conversation.lead_time_window == canonical(target, "morning")
    assert chat_module.time_window_is_complete(conversation.lead_time_window)
    assert conversation.booking_preferred_date == target.isoformat()
    assert conversation.booking_time_preference == "morning"
    assert conversation.booking_state == BookingState.WAITING_FOR_SLOT_SELECTION, resp.reply
    assert (conversation.lead_status or "").lower() == "completed"
    assert not ISO_IN_TEXT.search(resp.reply)


@requires_db
def test_comma_phrasing_completes_in_one_turn(db, fakes):
    """Staging conversation 8: 'July 28, morning please'."""
    client = booking_client(db)
    conversation = make_short_symptom_conversation(db, client)
    target = weekday_target(14)
    seed_slot_on(db, client, target)

    resp = send(db, client, conversation, f"{month_phrase(target)}, morning please")

    assert "morning or afternoon" not in resp.reply.lower(), resp.reply
    assert conversation.lead_time_window == canonical(target, "morning")
    assert conversation.booking_preferred_date == target.isoformat()
    assert conversation.booking_time_preference == "morning"
    assert conversation.booking_state == BookingState.WAITING_FOR_SLOT_SELECTION, resp.reply


@requires_db
def test_exact_time_stores_the_date_plus_the_time(db, fakes):
    """'<date> at 9am' keeps the exact time in the stored value and buckets
    it as a morning preference for slot filtering."""
    client = booking_client(db)
    conversation = make_short_symptom_conversation(db, client)
    target = weekday_target(14)
    seed_slot_on(db, client, target)

    resp = send(db, client, conversation, f"{month_phrase(target)} at 9am")

    assert conversation.lead_time_window == canonical(target, "9am")
    assert conversation.booking_preferred_date == target.isoformat()
    assert conversation.booking_time_preference == "morning"
    assert "morning or afternoon" not in resp.reply.lower(), resp.reply


# ===========================================================================
# 4-5: day-only storage, then the multi-turn merge KEEPS the explicit date.
# ===========================================================================

@requires_db
def test_day_only_stores_and_asks_for_the_time_detail(db, fakes):
    client = booking_client(db)
    conversation = make_short_symptom_conversation(db, client)
    target = weekday_target(14)

    resp = send(db, client, conversation, month_phrase(target))

    assert conversation.lead_time_window == canonical(target)
    assert not chat_module.time_window_is_complete(conversation.lead_time_window)
    assert "morning or afternoon" in resp.reply.lower(), resp.reply
    assert (conversation.lead_status or "").lower() != "completed"
    assert (conversation.booking_state or "none") == BookingState.NONE
    assert len(fakes.lead_sms) == 0 and len(fakes.lead_email) == 0
    assert not ISO_IN_TEXT.search(resp.reply)


@requires_db
def test_day_only_then_morning_merges_with_the_stored_date(db, fakes):
    """The behavior the second capture implementation destroyed: a later
    bare 'morning' must MERGE with the stored explicit date, never replace
    it (and the merged value must keep the ISO date, not just 'Tue')."""
    client = booking_client(db)
    conversation = make_short_symptom_conversation(db, client)
    target = weekday_target(14)
    seed_slot_on(db, client, target)

    send(db, client, conversation, month_phrase(target))
    assert conversation.lead_time_window == canonical(target)

    resp = send(db, client, conversation, "morning")

    assert conversation.lead_time_window == canonical(target, "morning"), (
        f"day-only value was replaced, not merged: {conversation.lead_time_window!r}"
    )
    assert (conversation.lead_status or "").lower() == "completed"
    assert conversation.booking_preferred_date == target.isoformat()
    assert conversation.booking_time_preference == "morning"
    assert conversation.booking_state == BookingState.WAITING_FOR_SLOT_SELECTION, resp.reply
    assert len(fakes.lead_sms) == 1 and len(fakes.lead_email) == 1


# ===========================================================================
# 6-9: rating / fraction / symptom-narration protections through the REAL
# endpoint, including the false 30-day rejection (staging conversation 2).
# ===========================================================================

@requires_db
def test_rating_only_phrase_stores_nothing_and_reasks(db, fakes):
    client = booking_client(db)
    conversation = make_short_symptom_conversation(db, client)

    resp = send(db, client, conversation, "pain 9/10 morning")

    assert not (conversation.lead_time_window or "").strip(), (
        f"a rating clause was stored as a time window: {conversation.lead_time_window!r}"
    )
    assert "?" in resp.reply, "Mia must ask for a valid day/time again"
    assert (conversation.lead_status or "").lower() != "completed"
    assert (conversation.booking_state or "none") == BookingState.NONE
    assert len(fakes.lead_sms) == 0 and len(fakes.lead_email) == 0


@requires_db
def test_valid_date_after_rating_is_not_rejected_by_the_horizon(db, fakes):
    """THE staging conversation 2 regression: 'my pain is 7/10 and I can
    come in on <date> morning' with the date INSIDE the configured 30-day
    horizon. The wrong candidate (7/10 read as next-year July 10) must
    never reach the horizon check; the captured canonical date must."""
    client = booking_client(db)
    conversation = make_short_symptom_conversation(db, client)
    target = weekday_target(14)
    seed_slot_on(db, client, target)

    resp = send(
        db, client, conversation,
        f"my pain is 7/10 and I can come in on {month_phrase(target)} morning",
    )

    assert HORIZON_REJECTION_FRAGMENT not in resp.reply, resp.reply
    assert SOONER_DAY_FRAGMENT not in resp.reply.lower(), resp.reply
    assert conversation.lead_time_window == canonical(target, "morning")
    assert conversation.booking_preferred_date == target.isoformat(), (
        f"the wrong parse candidate reached the Calendar: "
        f"{conversation.booking_preferred_date!r}"
    )
    assert conversation.booking_time_preference == "morning"
    assert "morning or afternoon" not in resp.reply.lower(), resp.reply
    assert (conversation.lead_status or "").lower() == "completed"


@requires_db
def test_valid_date_after_fraction_is_never_february_third(db, fakes):
    """Staging conversation 3: '2/3 of my tooth broke; appointment on
    <date> morning' books the date — never February 3."""
    client = booking_client(db)
    conversation = make_short_symptom_conversation(db, client)
    target = weekday_target(14)
    seed_slot_on(db, client, target)

    resp = send(
        db, client, conversation,
        f"2/3 of my tooth broke; appointment on {month_phrase(target)} morning",
    )

    assert conversation.lead_time_window == canonical(target, "morning")
    assert "-02-03" not in (conversation.lead_time_window or "")
    assert conversation.booking_preferred_date == target.isoformat()
    assert conversation.booking_time_preference == "morning"
    assert HORIZON_REJECTION_FRAGMENT not in resp.reply, resp.reply
    assert "morning or afternoon" not in resp.reply.lower(), resp.reply


@requires_db
def test_symptom_narration_time_never_replaces_the_selected_detail(db, fakes):
    """Staging conversation 5: '<date> morning and my pain started at 9am'
    keeps morning — the 9am is when the pain started."""
    client = booking_client(db)
    conversation = make_short_symptom_conversation(db, client)
    target = weekday_target(14)
    seed_slot_on(db, client, target)

    resp = send(
        db, client, conversation,
        f"{month_phrase(target)} morning and my pain started at 9am",
    )

    assert conversation.lead_time_window == canonical(target, "morning")
    assert "9am" not in (conversation.lead_time_window or "")
    assert conversation.booking_preferred_date == target.isoformat()
    assert conversation.booking_time_preference == "morning", (
        "the symptom narration's 9am leaked into the booking preference"
    )
    assert "morning or afternoon" not in resp.reply.lower(), resp.reply


# ===========================================================================
# 10-11: invalid-date protection and the plain in-horizon acceptance.
# ===========================================================================

@requires_db
def test_invalid_date_does_not_degrade_or_overwrite(db, fakes):
    """'February 30 morning' must not become a bare 'morning' and must not
    destroy an already-stored partial date."""
    client = booking_client(db)
    conversation = make_short_symptom_conversation(db, client)
    target = weekday_target(14)
    conversation.lead_time_window = canonical(target)
    db.add(conversation)
    db.commit()

    resp = send(db, client, conversation, "February 30 morning")

    db.refresh(conversation)
    assert conversation.lead_time_window == canonical(target), (
        f"the stored partial date was destroyed: {conversation.lead_time_window!r}"
    )
    assert (conversation.lead_status or "").lower() != "completed"
    assert (conversation.booking_state or "none") == BookingState.NONE
    assert "?" in resp.reply
    assert not ISO_IN_TEXT.search(resp.reply)


@requires_db
def test_plain_valid_date_inside_the_horizon_is_accepted(db, fakes):
    """A valid date well inside the configured 30-day horizon — with NO
    rating anywhere — is stored and reaches the Calendar unrejected even
    when no slots are published (the no-availability reply asks for another
    day; it never claims the office is 'booking up to 30 days ahead')."""
    client = booking_client(db)
    conversation = make_short_symptom_conversation(db, client)
    target = weekday_target(21)

    resp = send(db, client, conversation, f"{month_phrase(target)} morning")

    assert HORIZON_REJECTION_FRAGMENT not in resp.reply, resp.reply
    assert SOONER_DAY_FRAGMENT not in resp.reply.lower(), resp.reply
    assert conversation.lead_time_window == canonical(target, "morning")
    assert conversation.booking_preferred_date == target.isoformat()
    assert conversation.booking_time_preference == "morning"


# ===========================================================================
# 12-13: output safety and notification behavior.
# ===========================================================================

@requires_db
def test_reply_and_browser_metadata_are_iso_free(db, fakes):
    client = booking_client(db)
    conversation = make_short_symptom_conversation(db, client)
    target = weekday_target(14)
    seed_slot_on(db, client, target)

    day_only_resp = send(db, client, conversation, month_phrase(target))
    booked_resp = send(db, client, conversation, "morning")

    for resp in (day_only_resp, booked_resp):
        assert not ISO_IN_TEXT.search(resp.reply), resp.reply
        for value in meta_strings(resp.meta):
            assert not ISO_IN_TEXT.search(value), (
                f"raw ISO date leaked into browser metadata: {value!r}"
            )


@requires_db
def test_office_notification_behavior_is_unchanged(db, fakes):
    """Completion still notifies each office channel exactly once, with no
    ISO in either body, and later turns re-send nothing."""
    client = booking_client(db)
    conversation = make_short_symptom_conversation(db, client)
    target = weekday_target(14)
    seed_slot_on(db, client, target)

    send(db, client, conversation, f"{month_phrase(target)} morning")

    assert len(fakes.lead_sms) == 1 and len(fakes.lead_email) == 1
    _, sms_body = fakes.lead_sms[0]
    _, _, email_body = fakes.lead_email[0]
    assert not ISO_IN_TEXT.search(sms_body), sms_body
    assert not ISO_IN_TEXT.search(email_body)
    # No booking exists yet, so the separate booking notification has not
    # fired; the lead channels are not re-sent by a later message.
    assert len(fakes.booking_sms) == 0 and len(fakes.booking_email) == 0

    send(db, client, conversation, "what are your hours?")

    assert len(fakes.lead_sms) == 1 and len(fakes.lead_email) == 1


# ===========================================================================
# 14-15: emergency behavior unchanged.
# ===========================================================================

@requires_db
def test_ordinary_dental_emergency_intake_is_unchanged(db, fakes):
    """Staging conversation 1's opening: 'I have severe tooth pain and
    swelling' still starts the normal symptom intake (first name, then
    phone) — never a booking dialog, never a closed conversation."""
    client = booking_client(db)
    conversation = make_short_symptom_conversation(
        db, client,
        lead_reason=None, lead_name=None, lead_phone=None, is_lead=False,
    )

    resp = send(db, client, conversation, "I have severe tooth pain and swelling")

    assert "first name" in resp.reply.lower(), resp.reply
    assert not bool(getattr(conversation, "final_closed", False))
    assert (conversation.booking_state or "none") == BookingState.NONE

    follow_up = send(db, client, conversation, "kyle")

    assert "phone" in follow_up.reply.lower(), follow_up.reply
    assert not bool(getattr(conversation, "final_closed", False))


@requires_db
def test_life_threatening_emergency_persistent_stop_is_unchanged(db, fakes):
    """A life-threatening message still permanently closes the conversation;
    a later valid scheduling message is intercepted by the final-closed
    guard and captures nothing, completes nothing, books nothing."""
    client = booking_client(db)
    conversation = make_short_symptom_conversation(db, client)
    target = weekday_target(14)

    send(db, client, conversation, "I'm having trouble breathing and my face is swelling")

    assert bool(getattr(conversation, "final_closed", False)) is True

    resp = send(db, client, conversation, f"{month_phrase(target)} morning")

    assert resp.meta.get("mode") == "final_closed"
    assert not (conversation.lead_time_window or "").strip()
    assert (conversation.booking_state or "none") == BookingState.NONE
    assert (conversation.lead_status or "").lower() != "completed"
    assert len(fakes.lead_sms) == 0 and len(fakes.lead_email) == 0

# ===========================================================================
# CHECKPOINT B rev2 — a complete preference captured EARLIER is consumed
# when a LATER intake answer completes the lead. The Calendar start now
# handles every supported canonical stored form: explicit ISO dates
# (Checkpoint A, unchanged above), legacy weekday words ("Tuesday
# morning"), relative words ("tomorrow morning"), exact times
# ("Tuesday 3pm"), the unresolvable "Weekday morning" (ask ONLY the day,
# keep morning), and "ASAP" (unchanged). Stored day words are resolved by
# the appointment-intent date owner, never by local weekday arithmetic.
# ===========================================================================


def weekday_word_target(days_ahead=3):
    """(weekday word, resolved date) for a Mon-Fri day 3-5 days out.

    The word is written the way a stored legacy value carries it
    ("Wednesday"), and the expected date is resolved by the SAME
    appointment-intent owner the Calendar start uses — so the assertion can
    never disagree with the production resolution. The offset guarantees
    the word never names the client-local current weekday, keeping slot
    minimum-notice rules out of these fixtures on every run date."""
    d = client_today() + timedelta(days=days_ahead)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    word = d.strftime("%A")
    resolved = parse_preferred_date(word.lower(), client_today())
    assert resolved == d, "owner resolution must match the fixture day"
    return word, resolved


@requires_db
def test_stored_weekday_morning_is_consumed_at_patient_type_completion(db, fakes):
    """Rev2 blocker: stored 'Tuesday morning', final intake answer
    'returning'. The Calendar start resolves the next matching weekday via
    the date owner, keeps morning, and offers the matching slot in the SAME
    turn — no day question, no morning/afternoon question."""
    client = booking_client(db)
    word, expected = weekday_word_target(3)
    conversation = make_normal_flow_conversation(
        db, client, lead_time_window=f"{word} morning"
    )
    seed_slot_on(db, client, expected, hour=10)

    resp = send(db, client, conversation, "returning")

    assert resp.meta.get("mode") == "booking"
    assert conversation.booking_state == BookingState.WAITING_FOR_SLOT_SELECTION, resp.reply
    assert conversation.booking_preferred_date == expected.isoformat()
    assert conversation.booking_time_preference == "morning"
    assert "open on" in resp.reply, resp.reply
    assert "What day" not in resp.reply and "Which day" not in resp.reply, resp.reply
    assert "morning or afternoon" not in resp.reply.lower(), resp.reply
    # The captured preference itself is untouched by consumption.
    assert conversation.lead_time_window == f"{word} morning"
    assert (conversation.lead_status or "").lower() == "completed"
    # Dedupe patch: routine native-Calendar completion — no generic
    # lead notification (the booking notification is the office alert).
    assert len(fakes.lead_sms) == 0 and len(fakes.lead_email) == 0
    assert not ISO_IN_TEXT.search(resp.reply)


@requires_db
def test_stored_tomorrow_morning_is_consumed_at_completion(db, fakes):
    """Rev2 behavior 2: stored 'tomorrow morning' + final answer
    'returning' resolves tomorrow, keeps morning, asks nothing again."""
    client = booking_client(db)
    conversation = make_normal_flow_conversation(
        db, client, lead_time_window="tomorrow morning"
    )
    expected = client_today() + timedelta(days=1)
    make_slot(db, client, days_ahead=1, hour=10)

    resp = send(db, client, conversation, "returning")

    assert resp.meta.get("mode") == "booking"
    assert conversation.booking_state == BookingState.WAITING_FOR_SLOT_SELECTION, resp.reply
    assert conversation.booking_preferred_date == expected.isoformat()
    assert conversation.booking_time_preference == "morning"
    assert "What day" not in resp.reply and "Which day" not in resp.reply, resp.reply
    assert "morning or afternoon" not in resp.reply.lower(), resp.reply
    assert conversation.lead_time_window == "tomorrow morning"


@requires_db
def test_stored_generic_weekday_morning_asks_only_the_day_then_uses_morning(db, fakes):
    """Rev2 behavior 3, two turns. 'Weekday morning' has no resolvable day,
    so the next question asks only for the weekday while morning stays
    preserved; the weekday answer then completes the lead and books with
    the preserved morning automatically — morning/afternoon is never asked
    again."""
    client = booking_client(db)
    conversation = make_short_symptom_conversation(
        db, client, lead_time_window="Weekday morning"
    )
    word, expected = weekday_word_target(3)
    seed_slot_on(db, client, expected, hour=10)

    first = send(db, client, conversation, "returning")

    # The unresolvable window cannot complete the lead: nothing is booked,
    # nothing is notified, morning is preserved in the stored value, and
    # the follow-up asks a question that is NOT morning/afternoon.
    assert conversation.lead_time_window == "Weekday morning"
    assert (conversation.lead_status or "").lower() != "completed"
    assert (conversation.booking_state or "none") == BookingState.NONE
    assert "morning or afternoon" not in first.reply.lower(), first.reply
    assert "?" in first.reply
    assert len(fakes.lead_sms) == 0 and len(fakes.lead_email) == 0

    second = send(db, client, conversation, word.lower())

    # The weekday answer merges with the preserved morning (capture owner),
    # completes the lead, and the Calendar consumes BOTH — same-turn slot
    # offer, no morning/afternoon question.
    assert conversation.lead_time_window == f"{expected.strftime('%a')} morning"
    assert (conversation.lead_status or "").lower() == "completed"
    assert conversation.booking_state == BookingState.WAITING_FOR_SLOT_SELECTION, second.reply
    assert conversation.booking_preferred_date == expected.isoformat()
    assert conversation.booking_time_preference == "morning"
    assert "morning or afternoon" not in second.reply.lower(), second.reply
    assert len(fakes.lead_sms) == 1 and len(fakes.lead_email) == 1


@requires_db
def test_stored_weekday_exact_time_keeps_the_window_and_seeds_afternoon(db, fakes):
    """Rev2 behavior 4: stored 'Tuesday 3pm' + final answer 'new'. The
    stored value stays byte-identical, the day resolves via the date owner,
    and 3pm buckets to the afternoon preference — no questions re-asked."""
    client = booking_client(db)
    word, expected = weekday_word_target(3)
    conversation = make_normal_flow_conversation(
        db, client, lead_time_window=f"{word} 3pm"
    )
    seed_slot_on(db, client, expected, hour=14)

    resp = send(db, client, conversation, "new")

    assert resp.meta.get("mode") == "booking"
    assert conversation.lead_time_window == f"{word} 3pm", (
        "consumption must never rewrite the captured window"
    )
    assert conversation.booking_preferred_date == expected.isoformat()
    assert conversation.booking_time_preference == "afternoon"
    assert conversation.booking_state == BookingState.WAITING_FOR_SLOT_SELECTION, resp.reply
    assert "What day" not in resp.reply and "Which day" not in resp.reply, resp.reply
    assert "morning or afternoon" not in resp.reply.lower(), resp.reply
    assert not ISO_IN_TEXT.search(resp.reply)


@requires_db
def test_completed_lead_current_message_beats_history_and_ratings(db, fakes):
    """Rev2 post-completion ownership: a COMPLETED lead sends a new message
    containing a rating token and a valid in-horizon date. The seeds come
    from THIS message safely canonicalized (rating-aware), never from the
    historical stored window, never from the raw 7/10 token — and the
    historical window is not overwritten."""
    client = booking_client(db)
    conversation = make_short_symptom_conversation(
        db, client,
        lead_status="completed",
        lead_time_window="Monday afternoon",
        lead_email_sent=True,
        lead_sms_sent=True,
    )
    target = weekday_target(14)
    seed_slot_on(db, client, target, hour=10)

    resp = send(
        db, client, conversation,
        f"my pain is 7/10 and I can come in on {month_phrase(target)} morning",
    )

    assert HORIZON_REJECTION_FRAGMENT not in resp.reply, resp.reply
    assert SOONER_DAY_FRAGMENT not in resp.reply.lower(), resp.reply
    assert conversation.booking_preferred_date == target.isoformat(), (
        "the current message's canonical date must win — not history, "
        f"not the rating token: {conversation.booking_preferred_date!r}"
    )
    assert conversation.booking_time_preference == "morning"
    assert conversation.booking_state == BookingState.WAITING_FOR_SLOT_SELECTION, resp.reply
    # The historical captured window is history — consulted never,
    # mutated never.
    assert conversation.lead_time_window == "Monday afternoon"
    # Already-notified lead: no re-sends.
    assert len(fakes.lead_sms) == 0 and len(fakes.lead_email) == 0
    assert not ISO_IN_TEXT.search(resp.reply)

# ===========================================================================
# CHECKPOINT B rev3 — composite ASAP semantics. "ASAP / tomorrow ok" means
# "earliest available; tomorrow is ACCEPTABLE as a fallback" — the patient
# did not select tomorrow. Neither canonical ASAP form may ever seed a day,
# a day word, or a preference; genuine "tomorrow morning" preferences keep
# their tomorrow seed.
# ===========================================================================


def test_seed_helper_asap_composite_forms_yield_no_seeds():
    """Rev3 direct helper pin: both established ASAP canonical forms yield
    three Nones (case- and whitespace-robust), while genuine day
    preferences — including real 'tomorrow morning' values — are untouched
    by the guard."""
    seeds = chat_module._booking_seeds_from_time_window

    assert seeds("ASAP") == (None, None, None)
    assert seeds("ASAP / tomorrow ok") == (None, None, None)
    assert seeds("asap / tomorrow ok") == (None, None, None)
    assert seeds(" ASAP / tomorrow ok ") == (None, None, None)

    # The guard is a canonical-shape check, not a word ban: real stored
    # day preferences keep their seeds exactly as in rev2.
    assert seeds("tomorrow morning") == (None, "tomorrow", chat_module.PREF_MORNING)
    assert seeds("Tuesday morning") == (None, "tuesday", chat_module.PREF_MORNING)
    assert seeds("Tue 2026-08-11 morning") == (
        date(2026, 8, 11), None, chat_module.PREF_MORNING
    )


@requires_db
def test_stored_composite_asap_never_becomes_a_tomorrow_booking(db, fakes):
    """Rev3 endpoint regression: stored 'ASAP / tomorrow ok' + completing
    answer 'returning'. The Calendar must behave exactly as an ASAP
    completion always has — ask for the day — and must never treat
    tomorrow as the patient's chosen date, never rewrite the composite
    status, and never disturb priority or notification behavior."""
    client = booking_client(db)
    conversation = make_normal_flow_conversation(
        db, client,
        lead_time_window="ASAP / tomorrow ok",
        lead_is_priority=True,
    )
    # A published slot TOMORROW morning must NOT be offered as if tomorrow
    # had been selected: the ASAP completion asks for the day first.
    make_slot(db, client, days_ahead=1, hour=10)

    resp = send(db, client, conversation, "returning")

    assert resp.meta.get("mode") == "booking"
    assert conversation.booking_state == BookingState.WAITING_FOR_DATE, resp.reply
    assert resp.reply == "What day would work best for your appointment?"
    assert "tomorrow" not in resp.reply.lower(), (
        "the reply must not imply tomorrow was the chosen date"
    )
    tomorrow_iso = (client_today() + timedelta(days=1)).isoformat()
    assert conversation.booking_preferred_date != tomorrow_iso
    assert not conversation.booking_preferred_date
    assert not conversation.booking_time_preference
    # The composite priority status is untouched by consumption.
    assert conversation.lead_time_window == "ASAP / tomorrow ok"
    assert conversation.lead_is_priority is True
    assert (conversation.lead_status or "").lower() == "completed"
    # INTENTIONAL priority exception (dedupe patch): this lead is
    # PRIORITY, so the immediate legacy alert still runs (from the
    # routing owner) before the Calendar starts.
    assert len(fakes.lead_sms) == 1 and len(fakes.lead_email) == 1
    assert not ISO_IN_TEXT.search(resp.reply)
    for value in meta_strings(resp.meta):
        assert not ISO_IN_TEXT.search(value), value
