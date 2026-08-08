# calendar_tests/test_package_b_direct_slot_offer.py
#
# PACKAGE B ACCEPTANCE — Morning/Afternoon removal for Calendar tenants.
#
# For a booking_enabled Calendar flow, a selected date proceeds DIRECTLY to
# the server-owned exact-slot offer:
#
#     date -> slot_selection        (never date -> time_preference -> ...)
#
# on EVERY date path: typed, picker action (seven-day strip / full calendar
# submit the same pick-date action), seeded opening message, today, and a
# persisted/resumed day-only lead. Confirmation is still required, offers are
# never duplicated, slot ids stay opaque, and exactly one appointment with
# exactly one office email + one office SMS (and no patient SMS path) is
# created on booking.
#
# BASIC ACCEPTANCE — booking_enabled=False tenants keep the EXISTING
# morning/afternoon preference behavior, proven through the real route (the
# handoff requires this tested, not inferred).
#
# The tier decision is owned by ONE service predicate
# (booking_conversation.calendar_intake_day_only_sufficient: booking_enabled
# STRICT True) consumed by ONE route sufficiency owner
# (chat.time_window_sufficient_for_intake); the unit matrix below pins both.
#
# Run (PostgreSQL required, as every calendar_tests module):
#   python -m pytest calendar_tests/test_package_b_direct_slot_offer.py -v

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

import app.routes.chat as chat_module
from app.calendar_models import BookingState, SlotStatus
from app.repositories import appointment_repository
from app.services import booking_conversation as bc
from app.services.booking_conversation import (
    INTAKE_TIME_PREFERENCE_PROMPT,
    INTAKE_TIME_PREFERENCE_TODAY_PROMPT,
)

# The REAL DB harness (autouse fakes stub every AI/Twilio/Resend boundary).
from calendar_tests.test_chat_integration import (  # noqa: F401
    fakes, make_client, make_conversation, make_slot, send,
    OPEN_ALL_WEEK_HOURS,
)
from calendar_tests.test_universal_appointment_signal import (  # noqa: F401
    _gated_client, _fresh,
)

NY = ZoneInfo("America/New_York")
SLOT_SIGNAL = {"stage": "slot_selection"}
TIME_SIGNAL = {"stage": "time_preference"}
MA_WORDING = "morning or afternoon"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _next_open_weekday(days_ahead=3):
    """A real-calendar weekday at least `days_ahead` days out (make_slot and
    the engine clock both run on the real clock in these route tests)."""
    d = datetime.now(NY).date() + timedelta(days=days_ahead)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def _slot_on(db, client, day, hour=10):
    return make_slot(db, client, days_ahead=(day - datetime.now(NY).date()).days,
                     hour=hour, status=SlotStatus.AVAILABLE)


def _human(d):
    return d.strftime("%A, %B ") + str(d.day) + f", {d.year}"


def _canonical_day_only(d):
    return f"{d.strftime('%a')} {d.isoformat()}"


def _drive_intake_to_date(db, client, conv):
    """reason -> New/Returning -> name -> phone -> email skip: the next
    required field is the date/time-window question (Package A order)."""
    for t in ["I need a cleaning", "new patient", "Jordan Rivera",
              "516-555-0100", "skip email"]:
        send(db, client, conv, t)


def _assert_direct_offer(resp, conv):
    """The Package B shape on the completing date turn: slot_selection state,
    slot_selection signal, server-owned calendar_choice actions, and no trace
    of the removed time-preference stage in reply or meta."""
    assert conv.booking_state == BookingState.WAITING_FOR_SLOT_SELECTION
    meta = resp.meta or {}
    assert meta.get("calendar_picker") == SLOT_SIGNAL
    assert meta.get("calendar_picker") != TIME_SIGNAL
    assert resp.reply not in (INTAKE_TIME_PREFERENCE_PROMPT,
                              INTAKE_TIME_PREFERENCE_TODAY_PROMPT)
    assert MA_WORDING not in (resp.reply or "").lower()
    actions = meta.get("calendar_actions")
    assert actions, "server-owned slot actions expected on the date turn"
    for entry in actions:
        assert entry["action"]["type"] == "calendar_choice"
    return actions


# ---------------------------------------------------------------------------
# CALENDAR ACCEPTANCE 1-6: typed date -> direct exact-slot offer; actions are
# server-owned; slot UUIDs stay opaque.
# ---------------------------------------------------------------------------

def test_typed_date_goes_directly_to_slot_offer(db, fakes):
    client = _gated_client(db)
    conv = _fresh(db, client)
    _drive_intake_to_date(db, client, conv)
    d = _next_open_weekday()
    _slot_on(db, client, d)
    resp = send(db, client, conv, _human(d))
    actions = _assert_direct_offer(resp, conv)
    # PREF_ANY is the recorded effective preference: the whole selected day
    # was offered with no fabricated stored preference.
    assert conv.booking_time_preference is None
    assert conv.booking_effective_time_preference == "any"
    # Opaque ids: never surfaced as a label or inside the reply text.
    for entry in actions:
        cid = entry["action"]["choice_id"]
        assert cid and cid != entry["label"]
        assert cid not in (resp.reply or "")


def test_picker_date_action_matches_typed_path(db, fakes):
    # Seven-day strip AND full-calendar selections submit the SAME
    # pick-date:<ISO> structured action; the engine resolver runs the typed
    # path's single continuation, so this pin covers both surfaces.
    client = _gated_client(db)
    d = _next_open_weekday()
    _slot_on(db, client, d)
    conv = make_conversation(
        db, client, lead_time_window=None, lead_email_opt_out=True,
        lead_is_new_patient=True, lead_status="completed")
    conv.booking_state = BookingState.WAITING_FOR_DATE
    db.add(conv)
    db.commit()
    outcome = bc.handle_booking_action(
        db, client, conv, f"pick-date:{d.isoformat()}")
    assert outcome.status == bc.ACTION_EXECUTED
    reply = outcome.reply
    assert conv.booking_state == BookingState.WAITING_FOR_SLOT_SELECTION
    assert reply.meta.get("calendar_picker") == SLOT_SIGNAL
    assert reply.meta.get("calendar_actions")
    assert MA_WORDING not in (reply.text or "").lower()


def test_seeded_opening_message_goes_directly_to_slot_offer(db, fakes):
    # "anything <day>?" as the OPENING booking message: _handle_start seeds
    # the date and must land on the same direct offer.
    client = _gated_client(db)
    d = _next_open_weekday()
    _slot_on(db, client, d)
    conv = make_conversation(db, client, lead_status="completed",
                             lead_is_new_patient=True)
    reply = bc.handle_booking_message(
        db, client, conv, f"can we book {_human(d)}?")
    assert reply.handled is True
    assert conv.booking_state == BookingState.WAITING_FOR_SLOT_SELECTION
    assert reply.meta.get("calendar_picker") == SLOT_SIGNAL
    assert MA_WORDING not in (reply.text or "").lower()


def test_today_behaves_like_future_dates(db, fakes, monkeypatch):
    # Acceptance 10: today == future. Both clocks pinned (sanctioned
    # _fixed_clock pattern) so the notice check is deterministic.
    client = _gated_client(db)
    base = datetime.now(NY).date()
    while base.weekday() >= 5:
        base += timedelta(days=1)
    pinned = datetime(base.year, base.month, base.day, 10, 0, tzinfo=NY)
    monkeypatch.setattr(chat_module, "get_client_now", lambda c: pinned)
    monkeypatch.setattr(bc, "client_now", lambda settings: pinned)
    _slot_on(db, client, pinned.date(), hour=15)
    conv = _fresh(db, client)
    _drive_intake_to_date(db, client, conv)
    resp = send(db, client, conv, "today")
    _assert_direct_offer(resp, conv)


# ---------------------------------------------------------------------------
# CALENDAR ACCEPTANCE 11: a lead PERSISTED with a day-only window before
# Package B never resurrects the time-preference stage on resume — its next
# message routes into the engine's slot offer exactly once.
# ---------------------------------------------------------------------------

def _persisted_day_only_lead(db, client, d):
    """The pre-deploy shape: every field captured, canonical day-only window
    stored, intake never completed (status unset), no booking dialog."""
    return make_conversation(
        db, client,
        lead_time_window=_canonical_day_only(d),
        lead_email_opt_out=True,
        lead_is_new_patient=True,
        lead_status=None,
    )


def test_persisted_day_only_lead_resumes_into_slot_offer(db, fakes):
    client = _gated_client(db)
    d = _next_open_weekday()
    _slot_on(db, client, d)
    conv = _persisted_day_only_lead(db, client, d)
    resp = send(db, client, conv, "hi")
    _assert_direct_offer(resp, conv)
    assert (conv.lead_status or "").strip().lower() == "completed"


def test_persisted_day_only_faq_resume_never_asks_time_preference(db, fakes):
    # The FAQ-resume path re-asks the pending intake question; for a
    # Calendar-sufficient day-only lead there IS no pending question, so the
    # FAQ answer stands alone and the NEXT message routes into the offer.
    client = _gated_client(db)
    d = _next_open_weekday()
    _slot_on(db, client, d)
    conv = _persisted_day_only_lead(db, client, d)
    r_faq = send(db, client, conv, "where are you located?")
    assert INTAKE_TIME_PREFERENCE_PROMPT not in (r_faq.reply or "")
    assert MA_WORDING not in (r_faq.reply or "").lower()
    r_next = send(db, client, conv, "ok")
    _assert_direct_offer(r_next, conv)


def test_completion_routes_exactly_once(db, fakes):
    # The transition fires once: after routing, a follow-up message never
    # re-routes, re-offers from scratch, or duplicates lead notifications.
    client = _gated_client(db)
    d = _next_open_weekday()
    _slot_on(db, client, d)
    conv = _fresh(db, client)
    _drive_intake_to_date(db, client, conv)
    send(db, client, conv, _human(d))
    assert (conv.lead_status or "").strip().lower() == "completed"
    offered = list(conv.booking_offered_slot_ids or [])
    lead_notifications = (len(fakes.lead_sms), len(fakes.lead_email))
    resp = send(db, client, conv, "hmm let me think")
    db.refresh(conv)
    # Still at slot selection with the SAME offer — no duplicate offer.
    assert conv.booking_state == BookingState.WAITING_FOR_SLOT_SELECTION
    assert list(conv.booking_offered_slot_ids or []) == offered
    assert (len(fakes.lead_sms), len(fakes.lead_email)) == lead_notifications
    assert appointment_repository.get_appointment_by_conversation(
        db, client.id, conv.id) is None


# ---------------------------------------------------------------------------
# CALENDAR ACCEPTANCE 13 + 22-25: confirmation still required; exactly one
# appointment; exactly one office email + one office SMS; no patient SMS.
# ---------------------------------------------------------------------------

def test_confirmation_still_required_then_exactly_once_booking(db, fakes):
    client = _gated_client(db)
    d = _next_open_weekday()
    _slot_on(db, client, d)
    conv = _fresh(db, client)
    _drive_intake_to_date(db, client, conv)
    r_offer = send(db, client, conv, _human(d))
    _assert_direct_offer(r_offer, conv)
    # Selecting an exact time does NOT book: confirmation is still required.
    r_pick = send(db, client, conv, "1")
    db.refresh(conv)
    assert conv.booking_state == BookingState.WAITING_FOR_CONFIRMATION
    assert appointment_repository.get_appointment_by_conversation(
        db, client.id, conv.id) is None
    assert len(fakes.booking_sms) == 0 and len(fakes.booking_email) == 0
    # Only the explicit Yes books — exactly one appointment, one office
    # email, one office SMS; the harness has NO patient SMS channel and the
    # schema forbids one (patient SMS unreachable by design).
    r_yes = send(db, client, conv, "yes")
    db.refresh(conv)
    appt = appointment_repository.get_appointment_by_conversation(
        db, client.id, conv.id)
    assert appt is not None
    assert (r_yes.meta or {}).get("booked") is True
    # Rule 14: booking state is CLEARED after completion (existing contract).
    assert (conv.booking_state or BookingState.NONE) == BookingState.NONE
    assert len(fakes.booking_sms) == 1
    assert len(fakes.booking_email) == 1
    assert len(fakes.lead_sms) == 0 and len(fakes.lead_email) == 0


# ---------------------------------------------------------------------------
# BASIC ACCEPTANCE — booking_enabled=False keeps morning/afternoon, proven
# through the REAL route (never inferred from code).
# ---------------------------------------------------------------------------

def _basic_client(db):
    return make_client(db, calendar_enabled=False,
                       office_hours=OPEN_ALL_WEEK_HOURS)


def test_basic_day_only_still_asks_morning_or_afternoon(db, fakes):
    client = _basic_client(db)
    conv = _fresh(db, client)
    _drive_intake_to_date(db, client, conv)
    d = _next_open_weekday()
    resp = send(db, client, conv, _human(d))
    db.refresh(conv)
    assert resp.reply == INTAKE_TIME_PREFERENCE_PROMPT
    assert conv.lead_time_window == _canonical_day_only(d)
    assert (conv.booking_state or BookingState.NONE) == BookingState.NONE
    assert (conv.lead_status or "").strip().lower() != "completed"
    assert len(fakes.lead_sms) == 0 and len(fakes.lead_email) == 0


def test_basic_morning_answer_completes_generic_handoff(db, fakes):
    client = _basic_client(db)
    conv = _fresh(db, client)
    _drive_intake_to_date(db, client, conv)
    d = _next_open_weekday()
    send(db, client, conv, _human(d))
    resp = send(db, client, conv, "morning")
    db.refresh(conv)
    # The detail MERGES with the stored day, completes intake, and takes the
    # generic notified handoff — never the Calendar engine.
    assert conv.lead_time_window == f"{_canonical_day_only(d)} morning"
    assert (conv.lead_status or "").strip().lower() == "completed"
    assert (conv.booking_state or BookingState.NONE) == BookingState.NONE
    assert not (resp.meta or {}).get("calendar_actions")
    assert len(fakes.lead_sms) == 1 and len(fakes.lead_email) == 1


def test_basic_resume_prompt_still_offers_time_preference(db, fakes):
    # The resume owner still asks the M/A question for a Basic tenant with a
    # stored day-only window (the Package B short-circuit is Calendar-only).
    client = _basic_client(db)
    d = _next_open_weekday()
    conv = make_conversation(
        db, client, lead_time_window=_canonical_day_only(d),
        lead_email_opt_out=True, lead_is_new_patient=True)
    assert chat_module._next_intake_prompt(client, conv) == INTAKE_TIME_PREFERENCE_PROMPT


# ---------------------------------------------------------------------------
# OWNER UNIT MATRIX — the single sufficiency owners (Rule 3/4: strict
# vocabulary; anything but booking_enabled STRICT True fails closed).
# ---------------------------------------------------------------------------

def test_sufficiency_owner_matrix(db, fakes):
    gated = _gated_client(db)
    basic = _basic_client(db)
    d = _next_open_weekday()
    day_only = _canonical_day_only(d)
    complete = f"{day_only} morning"

    # Service tier owner: strict True only.
    assert bc.calendar_intake_day_only_sufficient(gated) is True
    assert bc.calendar_intake_day_only_sufficient(basic) is False

    # Route sufficiency owner.
    assert chat_module.time_window_sufficient_for_intake(gated, day_only) is True
    assert chat_module.time_window_sufficient_for_intake(basic, day_only) is False
    assert chat_module.time_window_sufficient_for_intake(None, day_only) is False
    # A COMPLETE window is sufficient for every tier (unchanged Basic rule).
    assert chat_module.time_window_sufficient_for_intake(basic, complete) is True
    assert chat_module.time_window_sufficient_for_intake(gated, complete) is True
    # Detail-only ("Weekday morning") never sufficient: no specific day.
    assert chat_module.time_window_sufficient_for_intake(gated, "Weekday morning") is False
    # The STRICT predicate is untouched: day-only stays incomplete by it.
    assert chat_module.time_window_is_complete(day_only) is False


def test_calendar_resume_prompt_is_empty_for_sufficient_day_only(db, fakes):
    # The Calendar-side of the resume owner: no prompt (routing owns the
    # next turn) — the Basic side is pinned above.
    client = _gated_client(db)
    d = _next_open_weekday()
    conv = make_conversation(
        db, client, lead_time_window=_canonical_day_only(d),
        lead_email_opt_out=True, lead_is_new_patient=True)
    assert chat_module._next_intake_prompt(client, conv) == ""

# ---------------------------------------------------------------------------
# v1.0.1 LEGACY ENGINE STATE (audit-required regressions 1-8): a conversation
# PERSISTED at WAITING_FOR_TIME_PREFERENCE before deployment — the REAL
# booking-engine state, driven through the REAL dispatch
# (handle_booking_message) — never sees Morning/Afternoon again. Answers a
# patient may have typed to the old question are still honored.
# ---------------------------------------------------------------------------

def _legacy_time_pref_conversation(db, client, *, date_value):
    """The pre-deploy engine shape: parked AT the removed stage."""
    conv = make_conversation(db, client, lead_status="completed",
                             lead_is_new_patient=True)
    conv.booking_state = BookingState.WAITING_FOR_TIME_PREFERENCE
    conv.booking_preferred_date = date_value
    db.add(conv)
    db.commit()
    return conv


def _assert_legacy_offer(reply, conv):
    assert reply.handled is True
    assert "morning or afternoon" not in (reply.text or "").lower()
    assert (reply.meta or {}).get("calendar_picker") != TIME_SIGNAL
    assert (reply.meta or {}).get("calendar_picker") == SLOT_SIGNAL
    assert (reply.meta or {}).get("calendar_actions")
    assert conv.booking_state == BookingState.WAITING_FOR_SLOT_SELECTION


def test_legacy_state_afternoon_answer_honored(db, fakes):
    # Required regression 1 (+ case B by symmetry of the same owner path):
    # an explicit part-of-day answer to the OLD question still counts.
    client = _gated_client(db)
    d = _next_open_weekday()
    _slot_on(db, client, d, hour=14)
    conv = _legacy_time_pref_conversation(db, client, date_value=d.isoformat())
    reply = bc.handle_booking_message(db, client, conv, "afternoon")
    _assert_legacy_offer(reply, conv)
    assert conv.booking_time_preference == "afternoon"


def test_legacy_state_any_time_answer_honored(db, fakes):
    # Required regression 2.
    client = _gated_client(db)
    d = _next_open_weekday()
    _slot_on(db, client, d)
    conv = _legacy_time_pref_conversation(db, client, date_value=d.isoformat())
    reply = bc.handle_booking_message(db, client, conv, "any time")
    _assert_legacy_offer(reply, conv)
    assert conv.booking_effective_time_preference == "any"


def test_legacy_state_unrelated_resume_offers_pref_any(db, fakes):
    # Required regression 3: the persisted date is authoritative — an
    # unrelated resumed message gets the direct PREF_ANY offer, never the
    # removed re-ask ("Do you prefer morning or afternoon? ...").
    client = _gated_client(db)
    d = _next_open_weekday()
    _slot_on(db, client, d)
    conv = _legacy_time_pref_conversation(db, client, date_value=d.isoformat())
    reply = bc.handle_booking_message(db, client, conv, "ok")
    _assert_legacy_offer(reply, conv)
    assert conv.booking_time_preference is None
    assert conv.booking_effective_time_preference == "any"


def test_legacy_state_new_date_only_offers_new_day(db, fakes):
    # Required regression 4: a new day alone proceeds through the ONE date
    # continuation to the new day's offer — never "Okay — <day>. Morning or
    # afternoon?".
    client = _gated_client(db)
    d_old = _next_open_weekday(3)
    d_new = _next_open_weekday(6)
    _slot_on(db, client, d_new)
    conv = _legacy_time_pref_conversation(db, client, date_value=d_old.isoformat())
    reply = bc.handle_booking_message(db, client, conv, _human(d_new))
    _assert_legacy_offer(reply, conv)
    assert not (reply.text or "").startswith("Okay —")
    assert conv.booking_preferred_date == d_new.isoformat()


def test_legacy_state_new_date_with_afternoon_filters(db, fakes):
    # Required regression 5: new day + explicit preference honors both via
    # the existing preference behavior.
    client = _gated_client(db)
    d_old = _next_open_weekday(3)
    d_new = _next_open_weekday(6)
    _slot_on(db, client, d_new, hour=15)
    conv = _legacy_time_pref_conversation(db, client, date_value=d_old.isoformat())
    reply = bc.handle_booking_message(db, client, conv, f"{_human(d_new)} afternoon")
    _assert_legacy_offer(reply, conv)
    assert conv.booking_preferred_date == d_new.isoformat()
    assert conv.booking_time_preference == "afternoon"


@pytest.mark.parametrize("bad_date", [None, "not-a-date"])
def test_legacy_state_corrupt_date_fails_safely_to_day_question(db, fakes, bad_date):
    # Required regression 6: missing/corrupt persisted date -> the existing
    # day question (date stage), never Morning/Afternoon.
    client = _gated_client(db)
    conv = _legacy_time_pref_conversation(db, client, date_value=bad_date)
    reply = bc.handle_booking_message(db, client, conv, "ok")
    assert reply.handled is True
    assert "morning or afternoon" not in (reply.text or "").lower()
    assert (reply.meta or {}).get("calendar_picker") != TIME_SIGNAL
    assert reply.text == "Let me pull up fresh times. What day works best?"
    assert conv.booking_state == BookingState.WAITING_FOR_DATE
    assert (reply.meta or {}).get("calendar_picker") == {"stage": "date"}


def test_legacy_state_meta_never_carries_time_preference_stage(db, fakes):
    # Required regression 7: across every legacy resolution shape, no
    # returned meta carries stage="time_preference".
    client = _gated_client(db)
    d = _next_open_weekday()
    _slot_on(db, client, d, hour=14)
    for text in ["afternoon", "any time", "ok", _human(d)]:
        conv = _legacy_time_pref_conversation(db, client, date_value=d.isoformat())
        reply = bc.handle_booking_message(db, client, conv, text)
        assert (reply.meta or {}).get("calendar_picker") != TIME_SIGNAL, text
    conv = _legacy_time_pref_conversation(db, client, date_value=None)
    reply = bc.handle_booking_message(db, client, conv, "ok")
    assert (reply.meta or {}).get("calendar_picker") != TIME_SIGNAL


def test_legacy_state_truthful_restate_no_time_preference_ad(db, fakes):
    # Required regression 8: the read-only race-loser restate never
    # advertises Morning/Afternoon for the legacy Calendar state, carries no
    # time-preference signal, and mutates nothing.
    from app.services.calendar_settings_service import load_calendar_settings
    from datetime import timezone as _tz
    client = _gated_client(db)
    d = _next_open_weekday()
    conv = _legacy_time_pref_conversation(db, client, date_value=d.isoformat())
    reply = bc._truthful_current_state_reply(
        db, client, conv, load_calendar_settings(client),
        datetime.now(_tz.utc))
    assert "morning or afternoon" not in (reply.text or "").lower()
    assert "calendar_picker" not in (reply.meta or {})
    db.refresh(conv)
    assert conv.booking_state == BookingState.WAITING_FOR_TIME_PREFERENCE
    assert conv.booking_offered_slot_ids in (None, [])

