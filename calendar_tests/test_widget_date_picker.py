# calendar_tests/test_widget_date_picker.py
#
# C2-A.2 — patient-widget visual DATE picker, backend proofs.
#
# Scope of this module (mirrors the approved plan's 14 required proofs):
#   * the meta signal `calendar_picker: {"stage": "date"}` appears ONLY when
#     booking_enabled, calendar_actions_enabled, AND calendar_picker_enabled
#     are all strict JSON true, and only on replies that leave the
#     conversation at WAITING_FOR_DATE (entry and every re-entry);
#   * the picker's `pick-date:YYYY-MM-DD` choice travels through the
#     EXISTING C1-C structured-action lane (handle_booking_action) and is
#     revalidated server-side through the ONE availability-preview owner;
#   * every forged / full / unavailable / past / stale / malformed /
#     out-of-range / wrong-state / duplicate submission is rejected with the
#     established STALE_CHOICE outcome and ZERO state mutation;
#   * an accepted date runs EXACTLY the typed-date transition and stops at
#     the existing morning/afternoon question (no C2-A.3 time behavior);
#   * no booking, hold, appointment, or notification write occurs anywhere
#     in the date stage; typed-date behavior is unchanged.
#
# Run (PostgreSQL required, as every calendar_tests module):
#   python -m pytest calendar_tests/test_widget_date_picker.py -v

import uuid
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import app.services.booking_conversation as bc
from app.calendar_models import (
    Appointment,
    AppointmentSlot,
    BookingState,
    SlotStatus,
)
from app.models import Client, Conversation
from app.services.calendar_settings_service import load_calendar_settings

from calendar_tests.conftest import requires_db

pytestmark = requires_db

TZ = ZoneInfo("America/New_York")


# ---------------------------------------------------------------------------
# Factories (mirroring calendar_tests/test_chat_action_execution.py so the
# picker tests exercise exactly the same tenant/conversation/slot shapes).
# ---------------------------------------------------------------------------

def _client(db, *, booking=True, actions=True, picker=True,
            picker_raw=None, practice_name="C2-A2 Picker Dental"):
    """Client whose calendar settings carry the three picker gates.
    picker_raw, when not None, is written VERBATIM (malformed-value tests);
    otherwise the strict boolean `picker` is written."""
    calendar = {
        "booking_enabled": booking,
        "calendar_actions_enabled": actions,
    }
    if picker_raw is not None:
        calendar["calendar_picker_enabled"] = picker_raw
    elif picker is not None:
        calendar["calendar_picker_enabled"] = picker
    row = Client(
        id=uuid.uuid4(),
        practice_name=practice_name,
        api_key=f"key-{uuid.uuid4()}",
        active=True,
        settings={
            "timezone": "America/New_York",
            "booking_mode": "capture_first",
            "calendar": calendar,
        },
    )
    db.add(row)
    db.commit()
    return row


def _conversation(db, client, **overrides):
    values = {
        "id": uuid.uuid4(),
        "client_id": client.id,
        "visitor_id": "c2a2-visitor",
        "is_lead": False,
        "lead_status": "new",
        "lead_name": "Casey Patient",
        "lead_phone": "5165551234",
    }
    values.update(overrides)
    row = Conversation(**values)
    db.add(row)
    db.commit()
    return row


def _date_state(db, conversation):
    """Seed WAITING_FOR_DATE exactly as the dialog leaves it."""
    conversation.booking_state = BookingState.WAITING_FOR_DATE
    db.add(conversation)
    db.commit()


def _slot(db, client, *, days_ahead=3, hour=18, status=SlotStatus.AVAILABLE):
    """One published slot `days_ahead` days out at `hour` UTC (18:00 UTC is
    mid-day office-local year-round, safely past any minimum notice)."""
    start = (datetime.now(timezone.utc) + timedelta(days=days_ahead)).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )
    row = AppointmentSlot(
        id=uuid.uuid4(),
        client_id=client.id,
        start_datetime=start,
        end_datetime=start + timedelta(minutes=30),
        status=status,
    )
    db.add(row)
    db.commit()
    return row


def _office_day(slot):
    """The slot's office-local calendar day (the picker's ISO suffix)."""
    return slot.start_datetime.replace(tzinfo=timezone.utc).astimezone(TZ).date()


def _now_utc():
    return datetime.now(timezone.utc)


def _settings(client):
    return load_calendar_settings(client)


def _entry_reply(db, client, conversation):
    """Drive the REAL text entry into the date question (no seeded date)."""
    return bc._handle_start(
        db, client, conversation, _settings(client),
        "I need an appointment", _now_utc(),
    )


def _expected_label(day):
    """The server-side transcript label contract: 'Wednesday, August 14'."""
    return f"{day.strftime('%A, %B')} {day.day}"


def _assert_no_writes(db, client, conversation):
    """No appointment exists and no slot is held/booked for this tenant."""
    assert (
        db.query(Appointment)
        .filter(Appointment.client_id == client.id)
        .count() == 0
    )
    assert (
        db.query(AppointmentSlot)
        .filter(AppointmentSlot.client_id == client.id)
        .filter(AppointmentSlot.status != SlotStatus.AVAILABLE)
        .count() == 0
    )
    assert conversation.booking_selected_slot_id is None


# ---------------------------------------------------------------------------
# 1–2. Gate strictness: the meta signal never appears without all three
#      strict-true flags; missing/malformed values never enable it.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("booking,actions,picker", [
    (False, True, True),
    (True, False, True),
    (True, True, False),
    (False, False, False),
])
def test_meta_absent_when_any_flag_false(db, booking, actions, picker):
    client = _client(db, booking=booking, actions=actions, picker=picker)
    conversation = _conversation(db, client)
    reply = _entry_reply(db, client, conversation)
    assert conversation.booking_state == BookingState.WAITING_FOR_DATE
    assert "calendar_picker" not in reply.meta


@pytest.mark.parametrize("raw", ["true", "yes", 1, 0, None, [], {}, "True"])
def test_meta_absent_when_flag_malformed(db, raw):
    # The strict-bool reader falls back to the SAFE default (False) for any
    # non-boolean value, so garbage can never switch the picker on.
    client = _client(db, picker=None, picker_raw=raw)
    conversation = _conversation(db, client)
    reply = _entry_reply(db, client, conversation)
    assert "calendar_picker" not in reply.meta


def test_meta_absent_when_flag_missing(db):
    client = _client(db, picker=None)
    conversation = _conversation(db, client)
    reply = _entry_reply(db, client, conversation)
    assert "calendar_picker" not in reply.meta


# ---------------------------------------------------------------------------
# 3–4. The signal appears at the date-stage entry and at every re-entry.
# ---------------------------------------------------------------------------

def test_meta_present_at_entry(db):
    client = _client(db)
    conversation = _conversation(db, client)
    reply = _entry_reply(db, client, conversation)
    assert conversation.booking_state == BookingState.WAITING_FOR_DATE
    assert reply.meta["calendar_picker"] == {"stage": "date"}
    # The existing meta contract is untouched alongside the new key.
    assert reply.meta["mode"] == "booking"
    assert reply.meta["state"] == BookingState.WAITING_FOR_DATE


def test_meta_present_on_suggest_other_days_reentry(db):
    client = _client(db)
    conversation = _conversation(db, client)
    _date_state(db, conversation)
    day = date.today() + timedelta(days=3)
    reply = bc._suggest_other_days(
        db, client, conversation, _settings(client), day, _now_utc()
    )
    assert conversation.booking_state == BookingState.WAITING_FOR_DATE
    assert reply.meta["calendar_picker"] == {"stage": "date"}


def test_meta_present_on_unparseable_typed_day_reask(db):
    client = _client(db)
    conversation = _conversation(db, client)
    _date_state(db, conversation)
    reply = bc._handle_date(
        db, client, conversation, _settings(client),
        "whenever honestly", _now_utc(),
    )
    assert conversation.booking_state == BookingState.WAITING_FOR_DATE
    assert reply.meta["calendar_picker"] == {"stage": "date"}


def test_meta_present_on_out_of_range_typed_day_reask(db):
    client = _client(db)
    conversation = _conversation(db, client)
    _date_state(db, conversation)
    settings = _settings(client)
    horizon_break = date.today() + timedelta(days=settings.max_booking_days + 40)
    reply = bc._validate_and_store_date(
        db, conversation, settings, horizon_break, date.today()
    )
    assert reply is not None
    assert conversation.booking_state == BookingState.WAITING_FOR_DATE
    assert reply.meta["calendar_picker"] == {"stage": "date"}


def test_meta_never_on_non_date_states(db):
    # C2-A.3 retarget + PACKAGE B: the DATE signal is date-stage ONLY and
    # must not leak into the reply that FOLLOWS a stored date. That reply is
    # now the exact-slot offer, which legitimately carries the APPROVED
    # slot_selection signal — never "date", and never the removed
    # time-preference stage.
    client = _client(db)
    conversation = _conversation(db, client)
    slot = _slot(db, client)
    _date_state(db, conversation)
    day = _office_day(slot)
    # Deterministic: drive the transition through the picker resolver's
    # accepted path (typed ISO parsing is not part of this proof).
    outcome = bc.handle_booking_action(
        db, client, conversation, f"pick-date:{day.isoformat()}"
    )
    assert outcome.status == bc.ACTION_EXECUTED
    assert outcome.reply.meta["calendar_picker"] == {"stage": "slot_selection"}
    assert outcome.reply.meta["calendar_picker"]["stage"] != "date"
    assert outcome.reply.meta["calendar_picker"]["stage"] != "time_preference"


# ---------------------------------------------------------------------------
# 5–7, 13. The existing structured-action lane resolves a genuinely open
#          date through the preview owner and the typed-date transition,
#          stopping at the morning/afternoon question.
# ---------------------------------------------------------------------------

def test_prefix_contract(db):
    assert bc.DATE_SELECT_CHOICE_PREFIX == "pick-date:"


def test_valid_pick_revalidates_through_preview_owner(db, monkeypatch):
    client = _client(db)
    conversation = _conversation(db, client)
    slot = _slot(db, client)
    _date_state(db, conversation)
    day = _office_day(slot)

    calls = []
    real_preview = bc.build_availability_preview

    def _recording_preview(pdb, pclient, request, now_utc):
        calls.append(request)
        return real_preview(pdb, pclient, request, now_utc)

    monkeypatch.setattr(bc, "build_availability_preview", _recording_preview)
    outcome = bc.handle_booking_action(
        db, client, conversation, f"pick-date:{day.isoformat()}"
    )
    assert outcome.status == bc.ACTION_EXECUTED
    # Exactly one revalidation, single-day window, PUBLIC preview
    # parameters (service_key None) — the browser's earlier preview is
    # never trusted.
    assert len(calls) == 1
    assert calls[0].start_day == day
    assert calls[0].end_day == day
    assert calls[0].service_key is None


def test_open_date_follows_typed_transition_and_goes_to_slot_offer(db):
    # PACKAGE B (was: ...stops_at_time_stage): the accepted picker date runs
    # EXACTLY the typed path's continuation, which now proceeds DIRECTLY to
    # the exact-slot offer — the morning/afternoon stage is removed.
    client = _client(db)
    conversation = _conversation(db, client)
    slot = _slot(db, client)
    _date_state(db, conversation)
    day = _office_day(slot)

    outcome = bc.handle_booking_action(
        db, client, conversation, f"pick-date:{day.isoformat()}"
    )
    assert outcome.status == bc.ACTION_EXECUTED
    # Exactly the typed path's stored value and transition...
    assert conversation.booking_preferred_date == day.isoformat()
    assert conversation.booking_state == BookingState.WAITING_FOR_SLOT_SELECTION
    # ...and exactly the typed path's next question: the slot offer, never
    # the removed morning/afternoon ask.
    assert "morning or afternoon" not in outcome.reply.text.lower()
    assert outcome.reply.meta["state"] == BookingState.WAITING_FOR_SLOT_SELECTION
    # PACKAGE B: the offer reply carries exactly mode, state, the approved
    # slot_selection signal, the server-owned slot actions, and the
    # existing offered_slots inventory — the unchanged _offer_slots meta.
    assert set(outcome.reply.meta.keys()) == {
        "mode", "state", "calendar_picker", "calendar_actions", "offered_slots"}
    assert outcome.reply.meta["calendar_picker"] == {"stage": "slot_selection"}
    assert outcome.reply.meta["calendar_actions"]
    # 10. The transcript label is SERVER-formatted from the accepted date.
    assert outcome.user_label == _expected_label(day)
    # 11. Nothing was booked, held, or notified on the offering turn.
    _assert_no_writes(db, client, conversation)


def test_server_label_is_depadded_and_browser_free(db):
    # handle_booking_action's signature contains NO browser text parameter:
    # the label can only ever be server-derived (locked decision D-3).
    import inspect
    params = list(inspect.signature(bc.handle_booking_action).parameters)
    assert params == ["db", "client", "conversation", "choice_id"]
    # And the format contract de-pads single-digit days.
    assert _expected_label(date(2026, 8, 5)) == "Wednesday, August 5"


def test_persisted_preference_parity_with_typed_path(db):
    # Rule 3 parity: a preference persisted BEFORE the date (seeded flows)
    # is honored by the picker exactly as by typed dates — the very same
    # _after_date_stored continuation runs (this is existing C1-C slot
    # offering, not new C2-A.3 UI).
    client = _client(db)
    conversation = _conversation(db, client, booking_time_preference="afternoon")
    slot = _slot(db, client)
    _date_state(db, conversation)
    day = _office_day(slot)
    outcome = bc.handle_booking_action(
        db, client, conversation, f"pick-date:{day.isoformat()}"
    )
    assert outcome.status == bc.ACTION_EXECUTED
    assert conversation.booking_preferred_date == day.isoformat()
    # The stored preference short-circuits the question, as typed does.
    assert conversation.booking_state in (
        BookingState.WAITING_FOR_SLOT_SELECTION,
        BookingState.WAITING_FOR_DATE,  # no matching afternoon slot path
    )


# ---------------------------------------------------------------------------
# 8. Every bad date is rejected with STALE_CHOICE and zero mutation.
# ---------------------------------------------------------------------------

def _assert_stale_no_mutation(db, client, conversation, outcome):
    assert outcome.status == bc.ACTION_STALE_CHOICE
    assert outcome.reply is None
    db.refresh(conversation)
    assert conversation.booking_state == BookingState.WAITING_FOR_DATE
    assert conversation.booking_preferred_date is None
    _assert_no_writes(db, client, conversation)


def test_forged_open_looking_date_rejected(db):
    # No slot exists on the forged day: the preview owner reports it
    # "unavailable" no matter what the browser claimed to have seen.
    client = _client(db)
    conversation = _conversation(db, client)
    _slot(db, client, days_ahead=3)
    _date_state(db, conversation)
    forged = (date.today() + timedelta(days=9)).isoformat()
    outcome = bc.handle_booking_action(
        db, client, conversation, f"pick-date:{forged}"
    )
    _assert_stale_no_mutation(db, client, conversation, outcome)


def test_full_day_rejected(db):
    client = _client(db)
    conversation = _conversation(db, client)
    slot = _slot(db, client, status=SlotStatus.BOOKED)
    _date_state(db, conversation)
    day = _office_day(slot)
    outcome = bc.handle_booking_action(
        db, client, conversation, f"pick-date:{day.isoformat()}"
    )
    assert outcome.status == bc.ACTION_STALE_CHOICE
    db.refresh(conversation)
    assert conversation.booking_state == BookingState.WAITING_FOR_DATE
    assert conversation.booking_preferred_date is None


def test_past_date_rejected(db):
    client = _client(db)
    conversation = _conversation(db, client)
    _date_state(db, conversation)
    past = (date.today() - timedelta(days=2)).isoformat()
    outcome = bc.handle_booking_action(
        db, client, conversation, f"pick-date:{past}"
    )
    _assert_stale_no_mutation(db, client, conversation, outcome)


def test_out_of_window_date_rejected(db):
    client = _client(db)
    conversation = _conversation(db, client)
    _date_state(db, conversation)
    settings = _settings(client)
    far = (date.today() + timedelta(days=settings.max_booking_days + 45))
    outcome = bc.handle_booking_action(
        db, client, conversation, f"pick-date:{far.isoformat()}"
    )
    _assert_stale_no_mutation(db, client, conversation, outcome)


@pytest.mark.parametrize("choice", [
    "pick-date:",
    "pick-date:2026-8-14",
    "pick-date:2026-02-30",
    "pick-date:20260814",
    "pick-date:2026/08/14",
    "pick-date:2026-08-14T10:00",
    "pick-date:tomorrow",
    "2026-08-14",
    "pickdate:2026-08-14",
    "pick-date:2026-13-01",
])
def test_malformed_choice_rejected(db, choice):
    client = _client(db)
    conversation = _conversation(db, client)
    _slot(db, client)
    _date_state(db, conversation)
    outcome = bc.handle_booking_action(db, client, conversation, choice)
    _assert_stale_no_mutation(db, client, conversation, outcome)


def test_picker_disabled_choice_is_stale(db):
    # booking + actions on, picker OFF: the choice could never have been
    # issued, so it resolves indistinguishably from a forgery.
    client = _client(db, picker=False)
    conversation = _conversation(db, client)
    slot = _slot(db, client)
    _date_state(db, conversation)
    day = _office_day(slot)
    outcome = bc.handle_booking_action(
        db, client, conversation, f"pick-date:{day.isoformat()}"
    )
    _assert_stale_no_mutation(db, client, conversation, outcome)


# ---------------------------------------------------------------------------
# 9 + duplicates. Wrong state and replays never advance anything.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("state", [
    BookingState.NONE,
    BookingState.WAITING_FOR_TIME_PREFERENCE,
    BookingState.BOOKED,
])
def test_rejected_outside_waiting_for_date(db, state):
    client = _client(db)
    conversation = _conversation(db, client)
    slot = _slot(db, client)
    conversation.booking_state = state
    db.add(conversation)
    db.commit()
    day = _office_day(slot)
    outcome = bc.handle_booking_action(
        db, client, conversation, f"pick-date:{day.isoformat()}"
    )
    assert outcome.status == bc.ACTION_STALE_CHOICE
    db.refresh(conversation)
    assert conversation.booking_state == state


def test_duplicate_submission_after_success_is_stale(db):
    client = _client(db)
    conversation = _conversation(db, client)
    slot = _slot(db, client)
    _date_state(db, conversation)
    day = _office_day(slot)
    choice = f"pick-date:{day.isoformat()}"
    first = bc.handle_booking_action(db, client, conversation, choice)
    assert first.status == bc.ACTION_EXECUTED
    stored = conversation.booking_preferred_date
    replay = bc.handle_booking_action(db, client, conversation, choice)
    assert replay.status == bc.ACTION_STALE_CHOICE
    db.refresh(conversation)
    # The replay changed nothing the first execution stored.
    assert conversation.booking_preferred_date == stored
    assert conversation.booking_state == BookingState.WAITING_FOR_SLOT_SELECTION


# ---------------------------------------------------------------------------
# 12. Typed-date behavior is byte-for-byte unchanged in wording and flow.
# ---------------------------------------------------------------------------

def _slot_on_local_day(db, client, local_day, hour_local=14):
    """PACKAGE B: one AVAILABLE slot on an EXACT client-local calendar day
    (same row shape as _slot), so typed-date tests can pin the direct
    exact-slot offer deterministically in any run timezone."""
    start_local = datetime(local_day.year, local_day.month, local_day.day,
                           hour_local, 0, tzinfo=ZoneInfo("America/New_York"))
    start = start_local.astimezone(timezone.utc)
    row = AppointmentSlot(
        id=uuid.uuid4(),
        client_id=client.id,
        start_datetime=start,
        end_datetime=start + timedelta(minutes=30),
        status=SlotStatus.AVAILABLE,
    )
    db.add(row)
    db.commit()
    return row


def test_typed_date_flow_unchanged_with_picker_enabled(db):
    # PACKAGE B: the typed path stores the date and proceeds DIRECTLY to the
    # exact-slot offer — no morning/afternoon question.
    client = _client(db)
    conversation = _conversation(db, client)
    _date_state(db, conversation)
    tomorrow = date.today() + timedelta(days=1)
    _slot_on_local_day(db, client, tomorrow)
    reply = bc._handle_date(
        db, client, conversation, _settings(client), "tomorrow", _now_utc()
    )
    assert conversation.booking_preferred_date == tomorrow.isoformat()
    assert conversation.booking_state == BookingState.WAITING_FOR_SLOT_SELECTION
    assert "morning or afternoon" not in reply.text.lower()
    assert conversation.booking_offered_slot_ids


def test_typed_date_flow_unchanged_with_picker_disabled(db):
    # PACKAGE B: identical typed-path transition with the picker gate off —
    # the flow parity the original test pinned, at the new slot-offer stop.
    client = _client(db, picker=False)
    conversation = _conversation(db, client)
    _date_state(db, conversation)
    tomorrow = date.today() + timedelta(days=1)
    _slot_on_local_day(db, client, tomorrow)
    reply = bc._handle_date(
        db, client, conversation, _settings(client), "tomorrow", _now_utc()
    )
    assert conversation.booking_preferred_date == tomorrow.isoformat()
    assert conversation.booking_state == BookingState.WAITING_FOR_SLOT_SELECTION
    assert "morning or afternoon" not in reply.text.lower()
    assert conversation.booking_offered_slot_ids


# ---------------------------------------------------------------------------
# V2 (Defect 1): the FULL date-stage reply inventory. Every reply whose
# persisted state is WAITING_FOR_DATE carries the signal — including the
# four paths the v1 audit found missing — and a race won by a NON-date
# state never advertises the picker.
# ---------------------------------------------------------------------------

def test_meta_present_on_vanished_offer_fallback(db):
    # WAITING_FOR_SLOT_SELECTION whose persisted offer rows vanished
    # (staff edits): the clean restart back to the day question is a
    # date-stage reply and must carry the signal.
    client = _client(db)
    conversation = _conversation(db, client)
    conversation.booking_state = BookingState.WAITING_FOR_SLOT_SELECTION
    conversation.booking_offered_slot_ids = [str(uuid.uuid4())]  # gone rows
    conversation.booking_offer_expires_at = (
        datetime.now(timezone.utc) + timedelta(minutes=15)
    )
    db.add(conversation)
    db.commit()
    reply = bc._handle_slot_selection(
        db, client, conversation, _settings(client), "1", _now_utc()
    )
    assert conversation.booking_state == BookingState.WAITING_FOR_DATE
    assert "fresh times" in reply.text
    assert reply.meta["calendar_picker"] == {"stage": "date"}
    assert reply.meta["state"] == BookingState.WAITING_FOR_DATE


def test_meta_present_on_confirmation_decline_restart(db):
    # Patient declines at confirmation: "No problem — what day would work
    # better?" leaves the conversation at the date stage.
    client = _client(db)
    conversation = _conversation(db, client)
    slot = _slot(db, client, status=SlotStatus.HELD)
    slot.held_by_conversation_id = conversation.id
    slot.held_until = datetime.now(timezone.utc) + timedelta(minutes=5)
    conversation.booking_state = BookingState.WAITING_FOR_CONFIRMATION
    conversation.booking_selected_slot_id = slot.id
    db.add(slot)
    db.add(conversation)
    db.commit()
    reply = bc._decline_and_restart(
        db, client, conversation, _settings(client), slot.id
    )
    assert "what day would work better" in reply.text
    assert conversation.booking_state == BookingState.WAITING_FOR_DATE
    assert reply.meta["calendar_picker"] == {"stage": "date"}


def test_meta_present_on_missing_selected_slot_fallback(db):
    # Corrupt confirmation state (no selected slot): the restart at the
    # day question is a date-stage reply.
    client = _client(db)
    conversation = _conversation(db, client)
    conversation.booking_state = BookingState.WAITING_FOR_CONFIRMATION
    db.add(conversation)
    db.commit()
    reply = bc._finalize_and_reply(
        db, client, conversation, _settings(client), None, _now_utc()
    )
    assert "what day works best" in reply.text
    assert conversation.booking_state == BookingState.WAITING_FOR_DATE
    assert reply.meta["calendar_picker"] == {"stage": "date"}


def test_meta_present_on_truthful_state_reply_in_date_state(db):
    # A lost per-conversation race whose SURVIVING state is the date
    # stage: the truthful restatement carries the signal.
    client = _client(db)
    conversation = _conversation(db, client)
    _date_state(db, conversation)
    reply = bc._truthful_current_state_reply(
        db, client, conversation, _settings(client), _now_utc()
    )
    assert reply.meta["state"] == BookingState.WAITING_FOR_DATE
    assert reply.meta["calendar_picker"] == {"stage": "date"}


def test_truthful_state_reply_never_advertises_picker_for_other_states(db):
    # The same truthful fallback for a race won by NONE asks the day
    # question but reports the surviving state — and must NOT attach a
    # picker that state cannot consume.
    client = _client(db)
    conversation = _conversation(db, client)
    reply = bc._truthful_current_state_reply(
        db, client, conversation, _settings(client), _now_utc()
    )
    assert reply.meta["state"] == BookingState.NONE
    assert "calendar_picker" not in reply.meta


def test_vanished_offer_fallback_gated_off_has_no_picker(db):
    # The new date-stage paths obey the same strict triple gate.
    client = _client(db, picker=False)
    conversation = _conversation(db, client)
    conversation.booking_state = BookingState.WAITING_FOR_SLOT_SELECTION
    conversation.booking_offered_slot_ids = [str(uuid.uuid4())]
    conversation.booking_offer_expires_at = (
        datetime.now(timezone.utc) + timedelta(minutes=15)
    )
    db.add(conversation)
    db.commit()
    reply = bc._handle_slot_selection(
        db, client, conversation, _settings(client), "1", _now_utc()
    )
    assert conversation.booking_state == BookingState.WAITING_FOR_DATE
    assert "calendar_picker" not in reply.meta
