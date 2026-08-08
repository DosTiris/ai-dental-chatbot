# calendar_tests/test_widget_time_stage.py
#
# C2-A.3 — visual time stages, backend proofs.
#
# Scope of this module (the frozen-contract backend proofs):
#   * the meta signal `calendar_picker: {"stage": "time_preference"}`
#     appears on EVERY reply that leaves the conversation at
#     WAITING_FOR_TIME_PREFERENCE (v1.0.1: the stage is REMOVED for every
#     Calendar path, including the legacy persisted state T3-T5 cover), and
#     ONLY when booking_enabled, calendar_actions_enabled, AND
#     calendar_picker_enabled are all strict true;
#   * the meta signal `calendar_picker: {"stage": "slot_selection"}`
#     attaches ONLY alongside slot calendar_actions (all five S paths),
#     under the same strict triple gate;
#   * gates-false replies stay BYTE-IDENTICAL to the pre-C2-A.3 meta;
#   * typed behavior at the time-preference stage is unchanged apart from
#     the additive calendar_picker key (typed parity);
#   * the exact button strings "Morning" / "Afternoon" are ordinary
#     parser inputs (typed-parity transport — no new vocabulary);
#   * stale / boundary structured-action outcomes mutate nothing and
#     never carry any picker signal (the 409 stays state-free);
#   * no confirmation-stage reply and no actions-free slot-stage re-ask
#     carries a time-stage signal.
#
# Run (PostgreSQL required, as every calendar_tests module):
#   python -m pytest calendar_tests/test_widget_time_stage.py -v

import uuid
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import app.services.booking_conversation as bc
from app.calendar_models import AppointmentSlot, BookingState, SlotStatus
from app.models import Client, Conversation
from app.services.appointment_intent import parse_time_preference
from app.services.calendar_settings_service import load_calendar_settings

from calendar_tests.conftest import requires_db

pytestmark = requires_db

TZ = ZoneInfo("America/New_York")

TIME_SIGNAL = {"stage": bc.PICKER_STAGE_TIME_PREFERENCE}
SLOT_SIGNAL = {"stage": bc.PICKER_STAGE_SLOT_SELECTION}

# The seven non-all-true gate combinations. Every one must suppress both
# signals and leave the reply meta byte-identical to the pre-C2-A.3 shape.
GATE_COMBOS = [
    (True, True, False),
    (True, False, True),
    (False, True, True),
    (True, False, False),
    (False, True, False),
    (False, False, True),
    (False, False, False),
]


# ---------------------------------------------------------------------------
# Factories (mirroring calendar_tests/test_widget_date_picker.py so the
# time-stage tests exercise exactly the same tenant/conversation/slot
# shapes).
# ---------------------------------------------------------------------------

def _client(db, *, booking=True, actions=True, picker=True,
            practice_name="C2-A3 Time Stage Dental"):
    """Client whose calendar settings carry the three picker gates."""
    row = Client(
        id=uuid.uuid4(),
        practice_name=practice_name,
        api_key=f"key-{uuid.uuid4()}",
        active=True,
        settings={
            "timezone": "America/New_York",
            "booking_mode": "capture_first",
            "calendar": {
                "booking_enabled": booking,
                "calendar_actions_enabled": actions,
                "calendar_picker_enabled": picker,
            },
        },
    )
    db.add(row)
    db.commit()
    return row


def _conversation(db, client, **overrides):
    values = {
        "id": uuid.uuid4(),
        "client_id": client.id,
        "visitor_id": "c2a3-visitor",
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
    """The slot's office-local calendar day."""
    return slot.start_datetime.replace(tzinfo=timezone.utc).astimezone(TZ).date()


def _now_utc():
    return datetime.now(timezone.utc)


def _settings(client):
    return load_calendar_settings(client)


def _time_pref_state(db, conversation, *, day=None):
    """Seed WAITING_FOR_TIME_PREFERENCE exactly as the dialog leaves it."""
    conversation.booking_state = BookingState.WAITING_FOR_TIME_PREFERENCE
    if day is not None:
        conversation.booking_preferred_date = day.isoformat()
    db.add(conversation)
    db.commit()


def _date_state(db, conversation):
    conversation.booking_state = BookingState.WAITING_FOR_DATE
    db.add(conversation)
    db.commit()


def _slot_state(db, conversation, slot, *, expired=False):
    """Seed WAITING_FOR_SLOT_SELECTION with a persisted live (or expired)
    offer, exactly as _offer_slots leaves it."""
    conversation.booking_state = BookingState.WAITING_FOR_SLOT_SELECTION
    conversation.booking_preferred_date = _office_day(slot).isoformat()
    conversation.booking_time_preference = "any"
    conversation.booking_offered_slot_ids = [str(slot.id)]
    conversation.booking_effective_time_preference = "any"
    delta = timedelta(minutes=-1 if expired else 20)
    conversation.booking_offer_expires_at = _now_utc() + delta
    db.add(conversation)
    db.commit()


# ---------------------------------------------------------------------------
# T1-T5 (PACKAGE B v1.0.1): NO reply ever leaves a Calendar conversation at
# WAITING_FOR_TIME_PREFERENCE anymore. T1/T2 pin the fresh date paths; T3-T5
# pin the LEGACY persisted state: answers are honored, the stored date is
# authoritative otherwise, and the restate never advertises the removed
# question. TIME_SIGNAL survives below only inside never-again assertions.
# ---------------------------------------------------------------------------

def test_t1_handle_start_post_date_goes_straight_to_slot_offer(db):
    # PACKAGE B (was: ...carries_signal at the time stage): an opening
    # message that names a day proceeds DIRECTLY to the exact-slot offer —
    # the time-preference stage is never entered on any date continuation.
    client = _client(db)
    conversation = _conversation(db, client)
    slot = _slot(db, client)
    day = _office_day(slot)
    reply = bc._handle_start(
        db, client, conversation, _settings(client),
        f"I need an appointment {day.strftime('%B')} {day.day}", _now_utc(),
    )
    assert reply.handled is True
    assert conversation.booking_state == BookingState.WAITING_FOR_SLOT_SELECTION
    assert reply.meta["state"] == BookingState.WAITING_FOR_SLOT_SELECTION
    assert reply.meta["calendar_picker"] == SLOT_SIGNAL
    assert reply.meta.get("calendar_actions")
    assert "morning or afternoon" not in reply.text.lower()


def test_t2_accepted_picker_date_goes_straight_to_slot_offer(db):
    # PACKAGE B: the accepted picker date runs EXACTLY the typed path's
    # continuation (_after_date_stored) — straight to the slot offer.
    client = _client(db)
    conversation = _conversation(db, client)
    slot = _slot(db, client)
    _date_state(db, conversation)
    day = _office_day(slot)
    outcome = bc.handle_booking_action(
        db, client, conversation, f"pick-date:{day.isoformat()}"
    )
    assert outcome.status == bc.ACTION_EXECUTED
    assert outcome.reply.meta["state"] == BookingState.WAITING_FOR_SLOT_SELECTION
    assert outcome.reply.meta["calendar_picker"] == SLOT_SIGNAL
    assert outcome.reply.meta.get("calendar_actions")
    assert "morning or afternoon" not in (outcome.reply.text or "").lower()


def test_t3_legacy_no_parse_goes_straight_to_slot_offer(db):
    # PACKAGE B v1.0.1 (was: ...re_ask_carries_signal): the legacy persisted
    # state treats the stored date as AUTHORITATIVE — an unparseable resumed
    # message gets the exact-slot offer with PREF_ANY, never the removed
    # morning/afternoon re-ask, never its signal.
    client = _client(db)
    conversation = _conversation(db, client)
    _slot(db, client)                       # aligned with day=+3 below
    _time_pref_state(db, conversation, day=date.today() + timedelta(days=3))
    reply = bc._handle_time_preference(
        db, client, conversation, _settings(client), "hello there", _now_utc()
    )
    assert "morning or afternoon" not in reply.text.lower()
    assert reply.meta["state"] == BookingState.WAITING_FOR_SLOT_SELECTION
    assert reply.meta["calendar_picker"] == SLOT_SIGNAL
    assert reply.meta["calendar_picker"] != TIME_SIGNAL
    assert reply.meta.get("calendar_actions")
    assert conversation.booking_time_preference is None
    assert conversation.booking_effective_time_preference == "any"


def test_t4_legacy_new_date_without_preference_goes_to_slot_offer(db):
    # PACKAGE B v1.0.1 (was: ...carries_signal): a new day alone at the
    # legacy state runs the ONE date continuation straight to the exact-slot
    # offer for the NEW day — the "Okay — <day>. Morning or afternoon?"
    # re-ask is removed with the stage.
    client = _client(db)
    conversation = _conversation(db, client)
    _slot(db, client, days_ahead=1)
    _time_pref_state(db, conversation, day=date.today() + timedelta(days=3))
    reply = bc._handle_time_preference(
        db, client, conversation, _settings(client), "tomorrow", _now_utc()
    )
    tomorrow = date.today() + timedelta(days=1)
    assert conversation.booking_preferred_date == tomorrow.isoformat()
    assert not reply.text.startswith("Okay —")
    assert "morning or afternoon" not in reply.text.lower()
    assert reply.meta["state"] == BookingState.WAITING_FOR_SLOT_SELECTION
    assert reply.meta["calendar_picker"] == SLOT_SIGNAL


def test_t5_truthful_restate_never_advertises_time_preference(db):
    # PACKAGE B v1.0.1 (was: ...carries_signal): the race-loser restate for
    # the legacy state never advertises the removed question, carries no
    # time-preference signal, and — per its read-only contract — mutates
    # NOTHING. With a readable stored day it truthfully points at the offer
    # the resolver returns on any next message.
    client = _client(db)
    conversation = _conversation(db, client)
    day = date.today() + timedelta(days=3)
    _time_pref_state(db, conversation, day=day)
    offered_before = conversation.booking_offered_slot_ids
    reply = bc._truthful_current_state_reply(
        db, client, conversation, _settings(client), _now_utc()
    )
    assert "morning or afternoon" not in reply.text.lower()
    assert "calendar_picker" not in reply.meta
    assert reply.meta["state"] == BookingState.WAITING_FOR_TIME_PREFERENCE
    db.refresh(conversation)
    assert conversation.booking_state == BookingState.WAITING_FOR_TIME_PREFERENCE
    assert conversation.booking_offered_slot_ids == offered_before


def test_t5b_truthful_restate_without_date_asks_day(db):
    # PACKAGE B v1.0.1: the legacy state WITHOUT a readable stored day
    # restates the answerable day question (dead-hold precedent) — never
    # morning/afternoon — still mutation-free.
    client = _client(db)
    conversation = _conversation(db, client)
    _time_pref_state(db, conversation)          # no day seeded
    reply = bc._truthful_current_state_reply(
        db, client, conversation, _settings(client), _now_utc()
    )
    assert reply.text == "What day would work best for your appointment?"
    assert "calendar_picker" not in reply.meta
    db.refresh(conversation)
    assert conversation.booking_state == BookingState.WAITING_FOR_TIME_PREFERENCE


# ---------------------------------------------------------------------------
# Gate combinations: any false or non-true gate suppresses the
# time_preference signal and leaves the meta byte-identical to the
# pre-C2-A.3 shape.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("booking,actions,picker", GATE_COMBOS)
def test_time_preference_signal_never_emitted_any_gate_combo(db, booking, actions, picker):
    # PACKAGE B v1.0.1 (was: ...signal_gated): the removed stage's signal is
    # never emitted for ANY gate combination, and no combination re-asks
    # morning/afternoon — the stage no longer exists at this owner.
    client = _client(db, booking=booking, actions=actions, picker=picker)
    conversation = _conversation(db, client)
    _time_pref_state(db, conversation, day=date.today() + timedelta(days=3))
    reply = bc._handle_time_preference(
        db, client, conversation, _settings(client), "hello there", _now_utc()
    )
    assert "morning or afternoon" not in reply.text.lower()
    assert (reply.meta or {}).get("calendar_picker") != TIME_SIGNAL


# ---------------------------------------------------------------------------
# Emission paths S: the slot_selection signal attaches alongside slot
# calendar_actions on every offer-shaped reply.
# ---------------------------------------------------------------------------

def test_s_offer_slots_normal_carries_signal(db):
    client = _client(db)
    conversation = _conversation(db, client)
    slot = _slot(db, client)
    _time_pref_state(db, conversation, day=_office_day(slot))
    conversation.booking_time_preference = "any"
    reply = bc._offer_slots(
        db, client, conversation, _settings(client), _now_utc()
    )
    assert conversation.booking_state == BookingState.WAITING_FOR_SLOT_SELECTION
    assert reply.meta["offered_slots"] == [str(slot.id)]
    assert reply.meta["calendar_actions"]
    assert reply.meta["calendar_picker"] == SLOT_SIGNAL


def test_s_relaxed_offer_carries_signal(db):
    # Morning preference with only a mid-day-UTC (afternoon-local) slot:
    # the relaxed PREF_ANY re-query still carries the signal.
    client = _client(db)
    conversation = _conversation(db, client)
    slot = _slot(db, client, hour=18)
    _time_pref_state(db, conversation, day=_office_day(slot))
    conversation.booking_time_preference = "morning"
    reply = bc._offer_slots(
        db, client, conversation, _settings(client), _now_utc()
    )
    assert "don’t have morning" in reply.text or "don\u2019t have morning" in reply.text
    assert reply.meta["calendar_picker"] == SLOT_SIGNAL


def test_s_reoffer_after_conflict_carries_signal(db):
    client = _client(db)
    conversation = _conversation(db, client)
    slot = _slot(db, client)
    _time_pref_state(db, conversation, day=_office_day(slot))
    conversation.booking_time_preference = "any"
    reply = bc._reoffer_after_conflict(
        db, client, conversation, _settings(client), _now_utc()
    )
    assert reply.text.startswith("I’m sorry") or reply.text.startswith("I\u2019m sorry")
    assert reply.meta["calendar_picker"] == SLOT_SIGNAL


def test_s_expired_offer_refresh_carries_signal(db):
    client = _client(db)
    conversation = _conversation(db, client)
    slot = _slot(db, client)
    _slot_state(db, conversation, slot, expired=True)
    reply = bc._handle_slot_selection(
        db, client, conversation, _settings(client), "1", _now_utc()
    )
    assert reply.meta.get("reason") == "offer_expired"
    assert reply.meta["calendar_picker"] == SLOT_SIGNAL


def test_s_truthful_live_offer_reshow_carries_signal(db):
    client = _client(db)
    conversation = _conversation(db, client)
    slot = _slot(db, client)
    _slot_state(db, conversation, slot)
    reply = bc._truthful_current_state_reply(
        db, client, conversation, _settings(client), _now_utc()
    )
    assert reply.meta["state"] == BookingState.WAITING_FOR_SLOT_SELECTION
    assert reply.meta["calendar_actions"]
    assert reply.meta["calendar_picker"] == SLOT_SIGNAL


# ---------------------------------------------------------------------------
# Slot-signal gate combinations: any non-true gate suppresses the signal;
# with actions disabled there are no calendar_actions either, and the meta
# stays byte-identical to the pre-C2-A.3 shape in both cases.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("booking,actions,picker", GATE_COMBOS)
def test_slot_selection_signal_gated(db, booking, actions, picker):
    client = _client(db, booking=booking, actions=actions, picker=picker)
    conversation = _conversation(db, client)
    slot = _slot(db, client)
    _time_pref_state(db, conversation, day=_office_day(slot))
    conversation.booking_time_preference = "any"
    reply = bc._offer_slots(
        db, client, conversation, _settings(client), _now_utc()
    )
    expected = {
        "mode": "booking",
        "state": BookingState.WAITING_FOR_SLOT_SELECTION,
        "offered_slots": [str(slot.id)],
    }
    if actions:
        # calendar_actions may be present without the picker flag; the
        # signal itself requires all three gates.
        assert reply.meta.get("calendar_actions")
        assert "calendar_picker" not in reply.meta
        assert {k: v for k, v in reply.meta.items()
                if k != "calendar_actions"} == expected
    else:
        assert reply.meta == expected


# ---------------------------------------------------------------------------
# Non-emission: replies that do NOT open a visual time stage carry no
# signal.
# ---------------------------------------------------------------------------

def test_unmatched_selection_re_ask_has_no_signal(db):
    client = _client(db)
    conversation = _conversation(db, client)
    slot = _slot(db, client)
    _slot_state(db, conversation, slot)
    reply = bc._handle_slot_selection(
        db, client, conversation, _settings(client),
        "uh whichever", _now_utc(),
    )
    assert reply.text.startswith("Just to be sure")
    assert "calendar_actions" not in reply.meta
    assert "calendar_picker" not in reply.meta


def test_confirmation_re_ask_has_no_signal(db):
    client = _client(db)
    conversation = _conversation(db, client)
    slot = _slot(db, client, status=SlotStatus.HELD)
    conversation.booking_state = BookingState.WAITING_FOR_CONFIRMATION
    conversation.booking_selected_slot_id = slot.id
    db.add(conversation)
    db.commit()
    reply = bc._handle_confirmation(
        db, client, conversation, _settings(client), "hmm", _now_utc()
    )
    # Confirmation buttons stay generic quick replies: actions may be
    # present, but the time-stage signal never is.
    assert reply.meta["state"] == BookingState.WAITING_FOR_CONFIRMATION
    assert "calendar_picker" not in reply.meta


# ---------------------------------------------------------------------------
# Stale / boundary structured-action outcomes: nothing mutates and no
# picker signal exists anywhere (the 409 envelope stays state-free).
# ---------------------------------------------------------------------------

def test_stale_date_replay_mutates_nothing_carries_current_stage(db):
    # PACKAGE B (was: ...no_signal): after the accepted date the conversation
    # sits at SLOT SELECTION, so a stale date replay's restate legitimately
    # carries the CURRENT stage's server-owned slot actions — mirroring the
    # race-loser restate pattern. The stale replay still mutates NOTHING:
    # stored date and state are untouched, and no second offer is recorded.
    client = _client(db)
    conversation = _conversation(db, client)
    slot = _slot(db, client)
    _date_state(db, conversation)
    day = _office_day(slot)
    choice = f"pick-date:{day.isoformat()}"
    first = bc.handle_booking_action(db, client, conversation, choice)
    assert first.status == bc.ACTION_EXECUTED
    stored = conversation.booking_preferred_date
    offered = list(conversation.booking_offered_slot_ids or [])
    replay = bc.handle_booking_action(db, client, conversation, choice)
    assert replay.status == bc.ACTION_STALE_CHOICE
    assert replay.calendar_actions            # restate of the CURRENT stage
    db.refresh(conversation)
    assert conversation.booking_preferred_date == stored
    assert list(conversation.booking_offered_slot_ids or []) == offered
    assert conversation.booking_state == BookingState.WAITING_FOR_SLOT_SELECTION


def test_stale_slot_choice_replacements_carry_no_signal(db):
    client = _client(db)
    conversation = _conversation(db, client)
    slot = _slot(db, client)
    other = _slot(db, client, days_ahead=4)
    _slot_state(db, conversation, slot)
    outcome = bc.handle_booking_action(
        db, client, conversation, str(other.id)  # Not a member of the offer.
    )
    assert outcome.status == bc.ACTION_STALE_CHOICE
    # The live replacement set is plain label/message/action dicts — no
    # calendar_picker key anywhere inside it.
    assert outcome.calendar_actions
    for entry in outcome.calendar_actions:
        assert "calendar_picker" not in entry
        assert set(entry) == {"label", "message", "action"}
    db.refresh(conversation)
    assert conversation.booking_state == BookingState.WAITING_FOR_SLOT_SELECTION


def test_stale_confirm_token_wrong_state_mutates_nothing(db):
    client = _client(db)
    conversation = _conversation(db, client)
    slot = _slot(db, client)
    _time_pref_state(db, conversation)
    outcome = bc.handle_booking_action(
        db, client, conversation, f"confirm-yes:{slot.id}"
    )
    assert outcome.status == bc.ACTION_STALE_CHOICE
    db.refresh(conversation)
    assert conversation.booking_state == BookingState.WAITING_FOR_TIME_PREFERENCE


# ---------------------------------------------------------------------------
# Typed parity: gates-on and gates-off tenants produce identical text,
# state, and meta apart from the additive calendar_picker key.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("typed", [
    "morning",
    "afternoon",
    "any time",
    "pm",
    "evening",
    "3 pm",
    "tomorrow",
])
def test_typed_parity_at_time_preference_stage(db, typed):
    # V3 (owner-observed correction): parity is proven on the SAME client
    # and the SAME published slot, with two separate conversations and
    # ONLY calendar_picker_enabled toggled between the runs. The offered
    # slot UUID and action choice_id are therefore genuinely identical,
    # and the two replies must match on text, resulting state, database
    # behavior, offered slot, and action choice — differing ONLY by the
    # additive calendar_picker metadata.
    client = _client(db, picker=True)
    slot = _slot(db, client)

    def set_picker(enabled):
        # Reassign the settings dict so SQLAlchemy detects the JSON change.
        settings = dict(client.settings)
        calendar = dict(settings["calendar"])
        calendar["calendar_picker_enabled"] = enabled
        settings["calendar"] = calendar
        client.settings = settings
        db.add(client)
        db.commit()

    def drive():
        conversation = _conversation(db, client)
        _time_pref_state(db, conversation, day=_office_day(slot))
        reply = bc._handle_time_preference(
            db, client, conversation, _settings(client), typed, _now_utc()
        )
        return conversation, reply

    set_picker(True)
    conv_on, on = drive()
    set_picker(False)
    conv_off, off = drive()

    assert on.text == off.text
    assert conv_on.booking_state == conv_off.booking_state
    assert conv_on.booking_preferred_date == conv_off.booking_preferred_date
    assert conv_on.booking_time_preference == conv_off.booking_time_preference
    # Identical non-picker metadata INCLUDING the same offered slot UUID
    # and the same calendar_actions choice_id (same slot, same client).
    on_meta = {k: v for k, v in on.meta.items() if k != "calendar_picker"}
    assert on_meta == off.meta
    if "offered_slots" in off.meta:
        assert off.meta["offered_slots"] == [str(slot.id)]
    assert "calendar_picker" not in off.meta


# ---------------------------------------------------------------------------
# Button strings are ordinary parser inputs (typed-parity transport).
# ---------------------------------------------------------------------------

def test_button_string_morning_parses():
    assert parse_time_preference("Morning") == "morning"


def test_button_string_afternoon_parses():
    assert parse_time_preference("Afternoon") == "afternoon"


# ---------------------------------------------------------------------------
# Boundaries: emergency cleanup and final_closed behavior are unchanged.
# ---------------------------------------------------------------------------

def test_emergency_action_at_slot_stage_cleans_up_and_blocks(db):
    client = _client(db)
    conversation = _conversation(db, client, lead_is_emergency=True)
    slot = _slot(db, client)
    _slot_state(db, conversation, slot)
    outcome = bc.handle_booking_action(db, client, conversation, str(slot.id))
    assert outcome.status == bc.ACTION_BOUNDARY
    assert outcome.boundary == bc.BOUNDARY_SAFETY_BLOCKED
    db.refresh(conversation)
    assert conversation.booking_state == BookingState.NONE


def test_final_closed_action_is_boundary_no_mutation(db):
    client = _client(db)
    conversation = _conversation(db, client, final_closed=True)
    slot = _slot(db, client)
    _slot_state(db, conversation, slot)
    outcome = bc.handle_booking_action(db, client, conversation, str(slot.id))
    assert outcome.status == bc.ACTION_BOUNDARY
    assert outcome.boundary == bc.BOUNDARY_FINAL_CLOSED
    db.refresh(conversation)
    assert conversation.booking_state == BookingState.WAITING_FOR_SLOT_SELECTION
