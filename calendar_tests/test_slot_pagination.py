# calendar_tests/test_slot_pagination.py
#
# UX-A ACCEPTANCE - server-authoritative slot pagination:
#   "See later times" / "Back to earlier times"
#
# The production changes under test:
#   availability_service.get_bookable_slots_for_day  (UNCAPPED per-day
#       sibling of get_available_slots; same single rule owner)
#   booking_conversation: nav choice vocabulary, _slot_nav_actions,
#       _select_slot_page (pure page math), _offer_adjacent_slot_page,
#       first-page has_later inside _offer_slots, nav resolution inside
#       _resolve_selection_action, and the mechanically extracted
#       _refresh_expired_offer shared by the typed and action paths.
#
# Owner acceptance letters (UX-A task document) -> tests below:
#   A  <= page-size eligible slots: no See later times action
#   B  8 eligible / page 6: first page = first six; later action present;
#      hidden 7th/8th slot UUIDs never sent
#   C  See later times: same date, same state, slots 7-8, Back action,
#      zero holds
#   D  Back to earlier times: revalidated earlier page, no intake restart,
#      zero holds
#   E  >2 pages: middle page carries BOTH directions (Back first)
#   F  last page: no See later times
#   G  explicit max_offered_slots=3 pages by 3
#   H  explicit max_offered_slots=8 pages by 8
#   I  clamp 1..10 unchanged + capped/uncapped owner equivalence pinned
#   J  availability change between page 1 and page 2 excluded
#   K  availability change before Back: earlier page revalidated too
#   L  a later-page slot selects and confirms normally
#   M  later -> back -> select earlier works without resurrection
#   N  navigation ids can never be mistaken for slot UUIDs
#   O  no duplicate slot actions on any page
#   P  slot UUIDs stay opaque; only the CURRENT page's ids are exposed
#   Q  navigation creates 0 appointments / 0 office SMS / 0 office email /
#      0 patient SMS
#   R  a real slot still creates only a HOLD at the existing stage, never
#      during pagination
#   S  final confirmation still produces exactly one appointment and the
#      existing exactly-once office notifications
#   T  a prior page's slot id submitted after navigating is STALE_CHOICE
#      with the CURRENT page as the replacement set (backend analog; the
#      widget epoch/Start Over sweep is pinned in
#      tests/test_widget_slot_pagination.js)
#   U  basic/non-Calendar behavior unchanged; actions-disabled offices
#      stay ACTION_NOT_ACTIVE
#   V  legacy WAITING_FOR_TIME_PREFERENCE: nav tokens resolve stale with
#      zero mutation
#   W  existing six-slot first-page behavior unchanged (wording, chip
#      order, no nav text in the reply)
#
# Run (PostgreSQL required, as every calendar_tests module):
#   python -m pytest calendar_tests/test_slot_pagination.py -v

import threading
import uuid
from datetime import datetime, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import sessionmaker

import app.routes.chat as chat_module
from app.calendar_models import Appointment, AppointmentSlot, BookingState, SlotStatus
from app.models import Client, Conversation, Message
from app.schemas import ChatRequest
from app.services import availability_service
from app.services import booking_conversation as bc
from app.services.appointment_intent import PREF_ANY
from app.services.calendar_settings_service import load_calendar_settings

# The REAL DB harness (autouse fakes stub every AI/Twilio/Resend boundary).
from calendar_tests.test_chat_integration import (  # noqa: F401
    fakes, make_client, make_conversation, make_slot, send, refreshed_slot,
    OPEN_ALL_WEEK_HOURS, _FakeRequest,
)
from calendar_tests.test_universal_appointment_signal import (  # noqa: F401
    _gated_client, _fresh,
)
from calendar_tests.test_package_b_direct_slot_offer import (
    _drive_intake_to_date, _human, _legacy_time_pref_conversation,
    _next_open_weekday, _slot_on,
)
from calendar_tests.test_slot_count_six import (
    _set_cap, _labels, _choice_ids, _expected_labels,
)

NY = ZoneInfo("America/New_York")
LATER_ID = bc.SLOTS_LATER_CHOICE_ID
EARLIER_ID = bc.SLOTS_EARLIER_CHOICE_ID
LATER_LABEL = "See later times"
EARLIER_LABEL = "Back to earlier times"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _act(db, client, conversation, choice_id, message="tap"):
    """Submit ONE structured calendar_choice through the real route."""
    req = ChatRequest(
        client_key=client.api_key,
        message=message,
        conversation_id=str(conversation.id),
        visitor_id="test-visitor",
        action={"type": "calendar_choice", "choice_id": choice_id},
    )
    resp = chat_module.chat(req, _FakeRequest(), db)
    db.refresh(conversation)
    return resp


def _act_409(db, client, conversation, choice_id):
    """Submit an action expected to be rejected; return the 409 detail."""
    with pytest.raises(HTTPException) as exc:
        _act(db, client, conversation, choice_id)
    db.refresh(conversation)
    assert exc.value.status_code == 409
    assert isinstance(exc.value.detail, dict)
    return exc.value.detail


def _paged_setup(db, cap, hours):
    """A gated office at the given cap (None = code default 6), one slot
    per hour on the same open weekday, intake driven to the date turn, and
    the FIRST-PAGE response returned."""
    client = _set_cap(db, _gated_client(db), cap)
    d = _next_open_weekday()
    slots = [_slot_on(db, client, d, hour=h) for h in hours]
    conv = _fresh(db, client)
    _drive_intake_to_date(db, client, conv)
    resp = send(db, client, conv, _human(d))
    return client, conv, d, slots, resp


def _slot_entries(actions):
    return [e for e in actions
            if e["action"]["choice_id"] not in bc.SLOT_NAV_CHOICE_IDS]


def _nav_labels(actions):
    return [e["label"] for e in actions
            if e["action"]["choice_id"] in bc.SLOT_NAV_CHOICE_IDS]


def _hour_of(db, slot_id):
    row = db.query(AppointmentSlot).filter(
        AppointmentSlot.id == uuid.UUID(str(slot_id))
    ).one()
    return row.start_datetime.astimezone(NY).hour


def _assert_all_available(db, slots):
    for s in slots:
        row = refreshed_slot(db, s.id)
        assert row.status == SlotStatus.AVAILABLE
        assert row.held_by_conversation_id is None


def _appointments(db, conv):
    """Appointments belonging to THIS conversation (the harness isolates
    by client, not by truncation, so unscoped counts would see leftovers
    from earlier tests in a full-suite run)."""
    return (db.query(Appointment)
            .filter(Appointment.conversation_id == conv.id).all())


def _counts(fakes):
    return (len(fakes.lead_sms), len(fakes.lead_email),
            len(fakes.booking_sms), len(fakes.booking_email))


def _transcript(db, conv):
    rows = (db.query(Message)
            .filter(Message.conversation_id == conv.id)
            .order_by(Message.created_at, Message.id).all())
    return [(r.role, r.content) for r in rows]


# ===========================================================================
# A + B + W + P - the first page
# ===========================================================================

def test_first_page_of_eight_offers_six_plus_see_later(db, fakes):
    # B: eight eligible, default cap -> the first six chronologically, the
    # See later times action, and NO hidden 7th/8th UUID anywhere in the
    # payload (P). W: the frozen first-page wording and chip order are
    # untouched; the navigation chip is purely additive.
    client, conv, d, slots, resp = _paged_setup(db, None, range(9, 17))
    assert conv.booking_state == BookingState.WAITING_FOR_SLOT_SELECTION
    actions = resp.meta["calendar_actions"]
    chips = _slot_entries(actions)
    assert _labels(chips) == _expected_labels(range(9, 15))
    assert _choice_ids(chips) == [str(s.id) for s in slots[:6]]
    assert _nav_labels(actions) == [LATER_LABEL]
    assert actions[-1]["action"]["choice_id"] == LATER_ID
    # W: frozen reply wording - and the nav label lives ONLY in the chip.
    assert resp.reply.startswith("Here\u2019s what\u2019s open on ")
    assert resp.reply.endswith("Which works best?")
    assert LATER_LABEL not in resp.reply
    # P: hidden later ids are absent from the ENTIRE serialized payload.
    payload = str(resp.meta) + resp.reply
    for hidden in slots[6:]:
        assert str(hidden.id) not in payload
    assert resp.meta["offered_slots"] == [str(s.id) for s in slots[:6]]
    assert conv.booking_offered_slot_ids == [str(s.id) for s in slots[:6]]


@pytest.mark.parametrize("hour_count", [6, 3])
def test_page_fitting_day_has_no_navigation(db, fakes, hour_count):
    # A + W: when every eligible slot fits one page the action set stays
    # byte-identical to the frozen behavior - no navigation entry at all.
    hours = range(9, 9 + hour_count)
    client, conv, d, slots, resp = _paged_setup(db, None, hours)
    actions = resp.meta["calendar_actions"]
    assert _nav_labels(actions) == []
    assert _choice_ids(actions) == [str(s.id) for s in slots]
    assert LATER_LABEL not in resp.reply
    assert EARLIER_LABEL not in resp.reply


# ===========================================================================
# C + D + Q + R - navigation itself
# ===========================================================================

def test_see_later_second_page(db, fakes):
    # C: later page = slots 7-8, same date, same state, Back action, zero
    # holds; the transcript carries the tapped label as the user row.
    client, conv, d, slots, resp = _paged_setup(db, None, range(9, 17))
    before = _counts(fakes)
    resp2 = _act(db, client, conv, LATER_ID)
    assert conv.booking_state == BookingState.WAITING_FOR_SLOT_SELECTION
    assert conv.booking_preferred_date == d.isoformat()
    actions = resp2.meta["calendar_actions"]
    chips = _slot_entries(actions)
    assert _labels(chips) == _expected_labels(range(15, 17))
    assert _choice_ids(chips) == [str(s.id) for s in slots[6:]]
    assert _nav_labels(actions) == [EARLIER_LABEL]
    assert resp2.reply.startswith("Here are later times on ")
    assert resp2.meta["offered_slots"] == [str(s.id) for s in slots[6:]]
    # Q + R: navigation never held, booked, or notified anything.
    assert _counts(fakes) == before
    assert _appointments(db, conv) == []
    _assert_all_available(db, slots)
    # Executed action transcript: BOTH rows persisted - the tapped label
    # as the user row and the page reply as the assistant row. (created_at
    # can tie inside one request and ids are random UUIDs, so row ORDER is
    # deliberately not asserted here.)
    tail = _transcript(db, conv)[-2:]
    assert ("user", LATER_LABEL) in tail
    assert ("assistant", resp2.reply) in tail


def test_back_to_earlier_first_page(db, fakes):
    # D: Back returns the revalidated earlier page - no intake restart, no
    # date re-ask, zero holds - and the first page carries only See later.
    client, conv, d, slots, resp = _paged_setup(db, None, range(9, 17))
    _act(db, client, conv, LATER_ID)
    before = _counts(fakes)
    resp3 = _act(db, client, conv, EARLIER_ID)
    assert conv.booking_state == BookingState.WAITING_FOR_SLOT_SELECTION
    assert conv.booking_preferred_date == d.isoformat()
    actions = resp3.meta["calendar_actions"]
    assert _choice_ids(_slot_entries(actions)) == [str(s.id) for s in slots[:6]]
    assert _nav_labels(actions) == [LATER_LABEL]
    assert resp3.reply.startswith("Here are earlier times on ")
    assert "What day" not in resp3.reply
    assert _counts(fakes) == before
    _assert_all_available(db, slots)


def test_navigation_causes_zero_side_effects(db, fakes):
    # Q: a full later -> back -> later tour writes nothing but the offer.
    client, conv, d, slots, resp = _paged_setup(db, None, range(9, 17))
    before = _counts(fakes)
    _act(db, client, conv, LATER_ID)
    _act(db, client, conv, EARLIER_ID)
    _act(db, client, conv, LATER_ID)
    assert _counts(fakes) == before
    assert _appointments(db, conv) == []
    _assert_all_available(db, slots)
    assert conv.booking_selected_slot_id is None


# ===========================================================================
# E + F + G + H + O - page geometry at explicit tenant caps
# ===========================================================================

def test_three_pages_middle_has_both_directions(db, fakes):
    # E + F + G at cap 3 over 8 slots: page 1 = later only; page 2 = Back
    # FIRST then See later (owner ordering); page 3 = Back only. O: every
    # page's choice ids stay unique.
    client, conv, d, slots, resp = _paged_setup(db, 3, range(9, 17))
    pages = [resp.meta["calendar_actions"]]
    pages.append(_act(db, client, conv, LATER_ID).meta["calendar_actions"])
    pages.append(_act(db, client, conv, LATER_ID).meta["calendar_actions"])
    assert _nav_labels(pages[0]) == [LATER_LABEL]
    assert _nav_labels(pages[1]) == [EARLIER_LABEL, LATER_LABEL]
    assert _nav_labels(pages[2]) == [EARLIER_LABEL]
    assert _labels(_slot_entries(pages[0])) == _expected_labels(range(9, 12))
    assert _labels(_slot_entries(pages[1])) == _expected_labels(range(12, 15))
    assert _labels(_slot_entries(pages[2])) == _expected_labels(range(15, 17))
    for page in pages:
        ids = _choice_ids(page)
        assert len(ids) == len(set(ids))
    _assert_all_available(db, slots)


def test_cap_eight_single_page(db, fakes):
    # H: explicit max_offered_slots=8 over 8 slots is ONE page - no nav.
    client, conv, d, slots, resp = _paged_setup(db, 8, range(9, 17))
    actions = resp.meta["calendar_actions"]
    assert len(_slot_entries(actions)) == 8
    assert _nav_labels(actions) == []


def test_uncapped_owner_equivalence_and_clamp(db, fakes):
    # I: the capped fetch IS the uncapped sibling plus the clamped slice -
    # pinned at the service boundary for the clamp floor, the default, and
    # the clamp ceiling - and the sibling never mutates a row.
    client = _gated_client(db)
    d = _next_open_weekday()
    slots = [_slot_on(db, client, d, hour=h) for h in range(9, 17)]
    now_utc = datetime.now(dt_timezone.utc)
    for raw, effective in ((0, 1), (None, 6), (99, 10)):
        client = _set_cap(db, client, raw)
        settings = load_calendar_settings(client)
        assert settings.max_offered_slots == effective
        capped = availability_service.get_available_slots(
            db, client.id, settings, d, PREF_ANY, now_utc)
        uncapped = availability_service.get_bookable_slots_for_day(
            db, client.id, settings, d, PREF_ANY, now_utc)
        assert len(uncapped) == 8
        assert [s.id for s in capped] == [s.id for s in uncapped[:effective]]
    _assert_all_available(db, slots)


# ===========================================================================
# J + K - revalidation on every navigation
# ===========================================================================

def test_slot_removed_between_pages_not_shown(db, fakes):
    # J: slot 7 (3 PM) books between page views -> the later page shows
    # ONLY 4 PM and, being last, carries no See later action.
    client, conv, d, slots, resp = _paged_setup(db, None, range(9, 17))
    taken = refreshed_slot(db, slots[6].id)
    taken.status = SlotStatus.BOOKED
    db.add(taken)
    db.commit()
    resp2 = _act(db, client, conv, LATER_ID)
    actions = resp2.meta["calendar_actions"]
    assert _labels(_slot_entries(actions)) == _expected_labels(range(16, 17))
    assert str(slots[6].id) not in str(resp2.meta)
    assert _nav_labels(actions) == [EARLIER_LABEL]


def test_earlier_page_revalidated_on_back(db, fakes):
    # K: 10 AM books while the patient is on page 2 -> Back shows the
    # CURRENT earlier eligibility (9, 11, 12, 1, 2) without the lost slot.
    client, conv, d, slots, resp = _paged_setup(db, None, range(9, 17))
    _act(db, client, conv, LATER_ID)
    taken = refreshed_slot(db, slots[1].id)
    taken.status = SlotStatus.BOOKED
    db.add(taken)
    db.commit()
    resp3 = _act(db, client, conv, EARLIER_ID)
    actions = resp3.meta["calendar_actions"]
    assert _labels(_slot_entries(actions)) == _expected_labels([9, 11, 12, 13, 14])
    assert str(slots[1].id) not in str(resp3.meta)
    assert _nav_labels(actions) == [LATER_LABEL]


# ===========================================================================
# L + M + R + S - selection and booking through the pages
# ===========================================================================

def test_later_page_selection_confirms_exactly_once(db, fakes):
    # L + R + S: pick 3 PM off page 2 -> hold placed AT SELECTION (never
    # during paging), confirm -> exactly one appointment and the existing
    # exactly-once office SMS + email; the patient channel stays silent.
    client, conv, d, slots, resp = _paged_setup(db, None, range(9, 17))
    _act(db, client, conv, LATER_ID)
    pre_sms, pre_email = len(fakes.booking_sms), len(fakes.booking_email)
    chosen = slots[6]
    _act(db, client, conv, str(chosen.id))
    assert conv.booking_state == BookingState.WAITING_FOR_CONFIRMATION
    held = refreshed_slot(db, chosen.id)
    assert held.status == SlotStatus.HELD
    assert held.held_by_conversation_id == conv.id
    assert len(fakes.booking_sms) == pre_sms
    assert len(fakes.booking_email) == pre_email
    _act(db, client, conv, bc.CONFIRM_YES_CHOICE_PREFIX + str(chosen.id))
    booked = _appointments(db, conv)
    assert len(booked) == 1
    appt = booked[0]
    assert appt.slot_id == chosen.id
    assert _hour_of(db, chosen.id) == 15
    assert len(fakes.booking_sms) == pre_sms + 1
    assert len(fakes.booking_email) == pre_email + 1
    assert refreshed_slot(db, chosen.id).status == SlotStatus.BOOKED


def test_later_back_select_earlier_books(db, fakes):
    # M: later -> back -> select 9 AM -> confirm. The earlier page's ids
    # were re-persisted by Back, so the selection maps to the RIGHT slot
    # and nothing stale is resurrected.
    client, conv, d, slots, resp = _paged_setup(db, None, range(9, 17))
    _act(db, client, conv, LATER_ID)
    _act(db, client, conv, EARLIER_ID)
    chosen = slots[0]
    _act(db, client, conv, str(chosen.id))
    assert conv.booking_state == BookingState.WAITING_FOR_CONFIRMATION
    assert str(conv.booking_selected_slot_id) == str(chosen.id)
    _act(db, client, conv, bc.CONFIRM_YES_CHOICE_PREFIX + str(chosen.id))
    booked = _appointments(db, conv)
    assert len(booked) == 1
    appt = booked[0]
    assert appt.slot_id == chosen.id
    assert _hour_of(db, chosen.id) == 9


def test_typed_number_selects_on_current_page(db, fakes):
    # The typed path stays keyed to the CURRENT page: "2" on page two is
    # 4 PM, never the first page's 10 AM.
    client, conv, d, slots, resp = _paged_setup(db, None, range(9, 17))
    _act(db, client, conv, LATER_ID)
    send(db, client, conv, "2")
    assert conv.booking_state == BookingState.WAITING_FOR_CONFIRMATION
    assert str(conv.booking_selected_slot_id) == str(slots[7].id)
    assert _hour_of(db, conv.booking_selected_slot_id) == 16


def test_conflict_reoffer_first_page_keeps_nav(db, fakes):
    # A page-2 pick that loses the race re-offers through the EXISTING
    # _reoffer_after_conflict -> _offer_slots owner: apology wording, the
    # fresh FIRST page, and the See later action all present.
    client, conv, d, slots, resp = _paged_setup(db, None, range(9, 17))
    _act(db, client, conv, LATER_ID)
    taken = refreshed_slot(db, slots[6].id)
    taken.status = SlotStatus.BOOKED
    db.add(taken)
    db.commit()
    resp3 = _act(db, client, conv, str(slots[6].id))
    assert resp3.reply.startswith("I\u2019m sorry \u2014 that time")
    actions = resp3.meta["calendar_actions"]
    assert _choice_ids(_slot_entries(actions)) == [
        str(s.id) for s in (slots[:6])
    ]
    assert _nav_labels(actions) == [LATER_LABEL]
    assert conv.booking_state == BookingState.WAITING_FOR_SLOT_SELECTION


# ===========================================================================
# N + T + V + stale/expiry - navigation is never selection
# ===========================================================================

def test_navigation_ids_never_selection(db, fakes):
    # N: the literals are non-UUID by construction; a lookalike token is
    # an ordinary stale forgery; and at CONFIRMATION a nav token is stale
    # with the hold byte-untouched.
    for cid in bc.SLOT_NAV_CHOICE_IDS:
        with pytest.raises(ValueError):
            uuid.UUID(cid)
    client, conv, d, slots, resp = _paged_setup(db, None, range(9, 17))
    detail = _act_409(db, client, conv, "slots-latest")
    assert detail["code"] == "STALE_CHOICE"
    assert conv.booking_state == BookingState.WAITING_FOR_SLOT_SELECTION
    assert conv.booking_offered_slot_ids == [str(s.id) for s in slots[:6]]
    chosen = slots[0]
    _act(db, client, conv, str(chosen.id))
    held_until = refreshed_slot(db, chosen.id).held_until
    detail = _act_409(db, client, conv, LATER_ID)
    assert detail["code"] == "STALE_CHOICE"
    assert conv.booking_state == BookingState.WAITING_FOR_CONFIRMATION
    assert refreshed_slot(db, chosen.id).held_until == held_until


def test_stale_prior_page_slot_action_after_nav(db, fakes):
    # T (backend analog) + round-2 audit F4 regression 1: after See later,
    # the OLD page's slot id resolves STALE_CHOICE with the CURRENT page
    # re-issued as the replacement set INCLUDING that page's truthful
    # "Back to earlier times" control (v1.0.1 dropped it - the patient was
    # stranded on the later page). Zero state mutation, no hidden earlier/
    # later slot UUIDs, and the tapped old slot left untouched.
    client, conv, d, slots, resp = _paged_setup(db, None, range(9, 17))
    _act(db, client, conv, LATER_ID)
    page2_ids = [str(s.id) for s in slots[6:]]
    expires_before = bc.ensure_utc(conv.booking_offer_expires_at)
    before = _counts(fakes)
    detail = _act_409(db, client, conv, str(slots[0].id))
    assert detail["code"] == "STALE_CHOICE"
    replacement = detail.get("calendar_actions") or []
    chips = _slot_entries(replacement)
    assert [e["action"]["choice_id"] for e in chips] == page2_ids
    assert _nav_labels(replacement) == [EARLIER_LABEL]
    # No hidden earlier/later slot UUIDs beyond the current page + the
    # two fixed navigation literals.
    assert {e["action"]["choice_id"] for e in replacement} <= (
        set(page2_ids) | {LATER_ID, EARLIER_ID}
    )
    assert conv.booking_offered_slot_ids == page2_ids
    assert bc.ensure_utc(conv.booking_offer_expires_at) == expires_before
    assert conv.booking_state == BookingState.WAITING_FOR_SLOT_SELECTION
    assert conv.booking_selected_slot_id is None
    assert refreshed_slot(db, slots[0].id).status == SlotStatus.AVAILABLE
    assert _counts(fakes) == before
    assert _appointments(db, conv) == []


def test_expired_offer_nav_refreshes_first_page(db, fakes):
    # An EXPIRED offer navigates through the SAME Patch 2C recovery the
    # typed path uses: executed, reason=offer_expired, a fresh first page.
    client, conv, d, slots, resp = _paged_setup(db, None, range(9, 17))
    conv.booking_offer_expires_at = (
        datetime.now(dt_timezone.utc) - timedelta(minutes=1))
    db.add(conv)
    db.commit()
    resp2 = _act(db, client, conv, LATER_ID)
    assert resp2.meta.get("reason") == "offer_expired"
    assert conv.booking_state == BookingState.WAITING_FOR_SLOT_SELECTION
    assert conv.booking_offered_slot_ids == [str(s.id) for s in slots[:6]]
    assert _nav_labels(resp2.meta["calendar_actions"]) == [LATER_LABEL]
    _assert_all_available(db, slots)
    tail = _transcript(db, conv)[-2:]
    assert ("user", LATER_LABEL) in tail


def test_nav_without_offer_is_stale_no_mutation(db, fakes):
    # A selection-state conversation whose persisted offer is GONE treats
    # a nav token exactly like any forged token: stale, zero mutation.
    client = _set_cap(db, _gated_client(db), None)
    conv = make_conversation(db, client, lead_status="completed",
                             lead_is_new_patient=True)
    conv.booking_state = BookingState.WAITING_FOR_SLOT_SELECTION
    conv.booking_offered_slot_ids = None
    db.add(conv)
    db.commit()
    detail = _act_409(db, client, conv, LATER_ID)
    assert detail["code"] == "STALE_CHOICE"
    assert conv.booking_state == BookingState.WAITING_FOR_SLOT_SELECTION
    assert conv.booking_offered_slot_ids is None
    assert conv.booking_offer_expires_at is None


def test_nav_at_legacy_time_pref_state_is_stale(db, fakes):
    # V: the legacy parked state issues no nav choices, so a nav token is
    # stale there and the state is untouched.
    client = _set_cap(db, _gated_client(db), None)
    d = _next_open_weekday()
    _slot_on(db, client, d, hour=14)
    conv = _legacy_time_pref_conversation(db, client, date_value=d.isoformat())
    detail = _act_409(db, client, conv, LATER_ID)
    assert detail["code"] == "STALE_CHOICE"
    assert conv.booking_state == BookingState.WAITING_FOR_TIME_PREFERENCE


def test_nav_when_actions_disabled_not_active(db, fakes):
    # U (guard): an actions-disabled office rejects a nav token with the
    # existing ACTION_NOT_ACTIVE outcome and mutates nothing.
    client = _set_cap(db, _gated_client(db), None)
    settings = dict(client.settings or {})
    calendar = dict(settings.get("calendar") or {})
    calendar["calendar_actions_enabled"] = False
    settings["calendar"] = calendar
    client.settings = settings
    db.add(client)
    db.commit()
    d = _next_open_weekday()
    slot = _slot_on(db, client, d, hour=10)
    conv = make_conversation(db, client, lead_status="completed",
                             lead_is_new_patient=True)
    conv.booking_state = BookingState.WAITING_FOR_SLOT_SELECTION
    conv.booking_offered_slot_ids = [str(slot.id)]
    conv.booking_offer_expires_at = (
        datetime.now(dt_timezone.utc) + timedelta(minutes=30))
    db.add(conv)
    db.commit()
    detail = _act_409(db, client, conv, LATER_ID)
    assert detail["code"] == "ACTION_NOT_ACTIVE"
    assert conv.booking_offered_slot_ids == [str(slot.id)]


def test_basic_smoke_non_calendar_unchanged(db, fakes):
    # U: a plain non-Calendar office keeps its ordinary reply shape - no
    # calendar actions, no booking state.
    client = make_client(db)
    conv = make_conversation(db, client)
    resp = send(db, client, conv, "What are your office hours?")
    assert (resp.meta or {}).get("calendar_actions") is None
    assert conv.booking_state in (None, BookingState.NONE)


# ===========================================================================
# Effective-preference paging (relaxed offers)
# ===========================================================================

def test_relaxed_offer_pages_pref_any(db, fakes):
    # A morning-preference conversation over an afternoon-only day relaxes
    # to PREF_ANY (frozen wording) - and paging honors the EFFECTIVE
    # preference, so page 2 is the later PREF_ANY tail, never re-narrowed.
    client = _set_cap(db, _gated_client(db), 3)
    d = _next_open_weekday()
    slots = [_slot_on(db, client, d, hour=h) for h in range(13, 18)]
    conv = _legacy_time_pref_conversation(db, client, date_value=d.isoformat())
    reply = bc.handle_booking_message(db, client, conv, "morning")
    db.refresh(conv)
    assert conv.booking_time_preference == "morning"
    assert conv.booking_effective_time_preference == PREF_ANY
    assert reply.text.startswith("I don\u2019t have morning openings on ")
    assert _nav_labels(reply.meta["calendar_actions"]) == [LATER_LABEL]
    assert _choice_ids(_slot_entries(reply.meta["calendar_actions"])) == [
        str(s.id) for s in slots[:3]
    ]
    resp2 = _act(db, client, conv, LATER_ID)
    actions = resp2.meta["calendar_actions"]
    assert _labels(_slot_entries(actions)) == _expected_labels(range(16, 18))
    assert resp2.reply.startswith("Here are later times on ")
    assert conv.booking_effective_time_preference == PREF_ANY


# ===========================================================================
# F1 concurrency (round-1 audit) - navigation vs concurrent winners.
#
# The three deterministic tests drive bc._resolve_selection_action DIRECTLY
# with a second-session conversation object still holding the PRE-WINNER
# snapshot. That is the exact documented race window: the V4 boundary
# reload runs at handle_booking_action ENTRY, so a winner that commits
# between that reload and the per-state resolver's mutation leaves the
# resolver working from "an older ORM snapshot" (module V2 header rule).
# Driving the resolver with the stale object reproduces that window
# DETERMINISTICALLY - the same doctrine as the approved synchronized-hook
# races in test_booking_db (test-only construction; no synchronization
# hooks in production modules; here, no hooks at all). Each deterministic
# test FAILS against the unlocked v1.0 pagination payload and passes
# v1.0.1. The fourth test is the route-level barrier race in the
# established T-21 style, asserting the invariant set that must hold under
# EVERY legal interleaving.
# ===========================================================================

def _stale_pair(session2, client, conv):
    """Load the SAME tenant rows into an independent session. Captured
    BEFORE the winner acts, this pair is the older ORM snapshot."""
    client2 = session2.query(Client).filter(Client.id == client.id).one()
    conv2 = (session2.query(Conversation)
             .filter(Conversation.id == conv.id).one())
    return client2, conv2


def _open_weekday_after(day):
    d = day + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def test_race_nav_loses_to_committed_selection(db, fakes):
    # Audit race A: a slot selection (hold + CONFIRMATION) commits while a
    # See-later request is in flight from the page-1 snapshot. The stale
    # navigation must lose with ZERO mutation - the forbidden triple
    # (selection state + selected slot + active hold) must never exist.
    client, conv, d, slots, resp = _paged_setup(db, None, range(9, 17))
    session2 = sessionmaker(bind=db.get_bind())()
    try:
        client2, conv2 = _stale_pair(session2, client, conv)
        assert list(conv2.booking_offered_slot_ids) == [
            str(s.id) for s in slots[:6]
        ]
        # Winner: real selection through the route.
        chosen = slots[0]
        _act(db, client, conv, str(chosen.id))
        assert conv.booking_state == BookingState.WAITING_FOR_CONFIRMATION
        held_until = refreshed_slot(db, chosen.id).held_until
        before = _counts(fakes)

        outcome = bc._resolve_selection_action(
            session2, client2, conv2, load_calendar_settings(client2),
            bc.SLOTS_LATER_CHOICE_ID, datetime.now(dt_timezone.utc),
        )

        assert outcome.status == bc.ACTION_EXECUTED
        loser = outcome.reply
        assert loser.handled is True
        # The loser never re-offers over the winner's confirmation.
        assert "Which works best?" not in (loser.text or "")

        db.expire_all()
        db.refresh(conv)
        assert conv.booking_state == BookingState.WAITING_FOR_CONFIRMATION
        assert str(conv.booking_selected_slot_id) == str(chosen.id)
        assert conv.booking_offered_slot_ids in (None, [])
        fresh = refreshed_slot(db, chosen.id)
        assert fresh.status == SlotStatus.HELD
        assert fresh.held_by_conversation_id == conv.id
        assert fresh.held_until == held_until  # byte-untouched by the loser
        # The forbidden triple, stated explicitly:
        assert not (
            conv.booking_state == BookingState.WAITING_FOR_SLOT_SELECTION
            and conv.booking_selected_slot_id is not None
        )
        assert _counts(fakes) == before
        assert _appointments(db, conv) == []
    finally:
        session2.close()


def test_race_double_see_later_single_page_advance(db, fakes):
    # Audit race B: two See-later requests that BOTH began on page 1. The
    # first pages to 2; the second (stale entry) must lose with zero
    # mutation - final page stays 2 (never 3) and the winner's persisted
    # expiry is byte-untouched (v1.0 rewrote it).
    client, conv, d, slots, resp = _paged_setup(db, None, range(9, 17))
    session2 = sessionmaker(bind=db.get_bind())()
    try:
        client2, conv2 = _stale_pair(session2, client, conv)
        page1_ids = [str(s.id) for s in slots[:6]]
        page2_ids = [str(s.id) for s in slots[6:]]
        assert list(conv2.booking_offered_slot_ids) == page1_ids

        _act(db, client, conv, LATER_ID)  # winner: page 2 committed
        assert conv.booking_offered_slot_ids == page2_ids
        winner_expiry = bc.ensure_utc(conv.booking_offer_expires_at)
        before = _counts(fakes)

        outcome = bc._resolve_selection_action(
            session2, client2, conv2, load_calendar_settings(client2),
            bc.SLOTS_LATER_CHOICE_ID, datetime.now(dt_timezone.utc),
        )

        assert outcome.status == bc.ACTION_EXECUTED
        loser = outcome.reply
        assert loser.handled is True
        # Truthful re-show of the CURRENT page - not a page turn.
        assert loser.text.startswith("Here\u2019s what\u2019s open on ")
        assert loser.meta.get("offered_slots") == page2_ids
        # Round-2 audit F4 regression 3: the truthful restate of the
        # later page carries its "Back to earlier times" control, and
        # exposes nothing beyond the current page + the nav literals.
        loser_actions = loser.meta.get("calendar_actions") or []
        assert [e["action"]["choice_id"] for e in _slot_entries(loser_actions)] == page2_ids
        assert _nav_labels(loser_actions) == [EARLIER_LABEL]
        assert {e["action"]["choice_id"] for e in loser_actions} <= (
            set(page2_ids) | {LATER_ID, EARLIER_ID}
        )

        db.expire_all()
        db.refresh(conv)
        assert conv.booking_offered_slot_ids == page2_ids  # never page 3
        assert bc.ensure_utc(conv.booking_offer_expires_at) == winner_expiry
        assert conv.booking_state == BookingState.WAITING_FOR_SLOT_SELECTION
        assert conv.booking_selected_slot_id is None
        assert _counts(fakes) == before
        _assert_all_available(db, slots)
    finally:
        session2.close()


def test_race_stale_nav_never_overwrites_new_date_offer(db, fakes):
    # Audit race C: the patient types a NEW day (fresh offer committed for
    # day 2) while a See-later request from the day-1 page is in flight.
    # The stale navigation must not overwrite the new date's offer/state.
    client, conv, d1, slots, resp = _paged_setup(db, None, range(9, 17))
    d2 = _open_weekday_after(d1)
    day2_slot = _slot_on(db, client, d2, hour=10)
    session2 = sessionmaker(bind=db.get_bind())()
    try:
        client2, conv2 = _stale_pair(session2, client, conv)
        assert list(conv2.booking_offered_slot_ids) == [
            str(s.id) for s in slots[:6]
        ]

        send(db, client, conv, _human(d2))  # winner: new-date offer
        assert conv.booking_preferred_date == d2.isoformat()
        day2_ids = list(conv.booking_offered_slot_ids)
        assert day2_ids == [str(day2_slot.id)]
        winner_expiry = bc.ensure_utc(conv.booking_offer_expires_at)
        before = _counts(fakes)

        outcome = bc._resolve_selection_action(
            session2, client2, conv2, load_calendar_settings(client2),
            bc.SLOTS_LATER_CHOICE_ID, datetime.now(dt_timezone.utc),
        )

        assert outcome.status == bc.ACTION_EXECUTED
        loser = outcome.reply
        assert loser.handled is True
        assert loser.text.startswith("Here\u2019s what\u2019s open on ")
        assert loser.meta.get("offered_slots") == day2_ids

        db.expire_all()
        db.refresh(conv)
        assert conv.booking_preferred_date == d2.isoformat()
        assert conv.booking_offered_slot_ids == day2_ids
        assert bc.ensure_utc(conv.booking_offer_expires_at) == winner_expiry
        assert conv.booking_state == BookingState.WAITING_FOR_SLOT_SELECTION
        assert _counts(fakes) == before
    finally:
        session2.close()


def test_race_barrier_nav_vs_selection_invariants(db, fakes):
    # Route-level barrier race in the established T-21 style: a real slot
    # selection and a See-later navigation start together through chat().
    # Whichever interleaving PostgreSQL serializes, the invariant set must
    # hold - and the forbidden triple must never exist.
    if db.get_bind().dialect.name != "postgresql":
        pytest.skip("PostgreSQL-only concurrency semantics")

    client, conv, d, slots, resp = _paged_setup(db, None, range(9, 17))
    page1_ids = [str(s.id) for s in slots[:6]]
    page2_ids = [str(s.id) for s in slots[6:]]
    chosen = slots[1]
    before = _counts(fakes)

    Session = sessionmaker(bind=db.get_bind())
    requests = [
        ChatRequest(client_key=client.api_key, message="tap",
                    conversation_id=str(conv.id), visitor_id="test-visitor",
                    action={"type": "calendar_choice",
                            "choice_id": str(chosen.id)}),
        ChatRequest(client_key=client.api_key, message="tap",
                    conversation_id=str(conv.id), visitor_id="test-visitor",
                    action={"type": "calendar_choice",
                            "choice_id": LATER_ID}),
    ]
    barrier = threading.Barrier(2, timeout=15)
    outcomes = [None, None]

    def worker(index):
        session = Session()
        try:
            barrier.wait(timeout=10)
            try:
                response = chat_module.chat(requests[index], _FakeRequest(),
                                            session)
                outcomes[index] = ("ok", response.reply)
            except HTTPException as exc:
                outcomes[index] = ("409", exc.detail)
        except Exception as exc:  # pragma: no cover - surfaced below
            outcomes[index] = ("error", repr(exc))
        finally:
            session.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert all(o is not None and o[0] != "error" for o in outcomes), outcomes
    for status, payload in outcomes:
        if status == "409":
            assert payload["code"] == "STALE_CHOICE", outcomes

    db.expire_all()
    db.refresh(conv)
    state = conv.booking_state
    held = (db.query(AppointmentSlot)
            .filter(AppointmentSlot.client_id == client.id,
                    AppointmentSlot.held_by_conversation_id == conv.id,
                    AppointmentSlot.status == SlotStatus.HELD).all())

    # The forbidden triple can never exist under ANY interleaving.
    assert not (state == BookingState.WAITING_FOR_SLOT_SELECTION
                and conv.booking_selected_slot_id is not None
                and held)

    if state == BookingState.WAITING_FOR_CONFIRMATION:
        # The selection won (before or after the page turn): its hold and
        # cleared offer trio survive untouched.
        assert str(conv.booking_selected_slot_id) == str(chosen.id)
        assert [s.id for s in held] == [chosen.id]
        assert conv.booking_offered_slot_ids in (None, [])
    elif state == BookingState.WAITING_FOR_SLOT_SELECTION:
        # The navigation outcome survived: a coherent live page, zero
        # selection, zero holds.
        assert conv.booking_selected_slot_id is None
        assert held == []
        assert conv.booking_offered_slot_ids in (page1_ids, page2_ids)
        assert bc.ensure_utc(conv.booking_offer_expires_at) > (
            datetime.now(dt_timezone.utc))
    else:
        raise AssertionError(f"unexpected post-race state: {state}")

    assert _appointments(db, conv) == []
    assert _counts(fakes) == before


# ===========================================================================
# Round-2 audit F4 - truthful pagination controls on authoritative
# restates and STALE_CHOICE replacements. Regressions 2, 4, and 5 (1 and 3
# live inside the strengthened tests above); every one FAILS v1.0.1
# (controls missing) and passes v1.0.2.
# ===========================================================================

def test_stale_replacement_on_middle_page_has_both_directions(db, fakes):
    # F4 regression 2: with three pages, a stale slot action on the MIDDLE
    # page re-issues that page with BOTH controls in owner order (Back
    # first), zero mutation, no hidden UUIDs.
    client, conv, d, slots, resp = _paged_setup(db, 3, range(8, 17))
    _act(db, client, conv, LATER_ID)  # -> middle page (slots 3..5)
    middle_ids = [str(s.id) for s in slots[3:6]]
    assert conv.booking_offered_slot_ids == middle_ids
    expires_before = bc.ensure_utc(conv.booking_offer_expires_at)
    before = _counts(fakes)

    detail = _act_409(db, client, conv, str(slots[0].id))
    assert detail["code"] == "STALE_CHOICE"
    replacement = detail.get("calendar_actions") or []
    chips = _slot_entries(replacement)
    assert [e["action"]["choice_id"] for e in chips] == middle_ids
    assert _nav_labels(replacement) == [EARLIER_LABEL, LATER_LABEL]
    assert {e["action"]["choice_id"] for e in replacement} <= (
        set(middle_ids) | {LATER_ID, EARLIER_ID}
    )

    assert conv.booking_offered_slot_ids == middle_ids
    assert bc.ensure_utc(conv.booking_offer_expires_at) == expires_before
    assert conv.booking_state == BookingState.WAITING_FOR_SLOT_SELECTION
    assert conv.booking_selected_slot_id is None
    _assert_all_available(db, slots)
    assert _counts(fakes) == before
    assert _appointments(db, conv) == []


def test_race_loser_truthful_restate_middle_page_both_directions(db, fakes):
    # F4 regression 4: a stale-entry navigation loses to a committed page
    # turn whose CURRENT page is the MIDDLE page; the truthful restate
    # re-shows that page WITH both controls, in owner order, and mutates
    # nothing (the winner's expiry stays byte-identical).
    client, conv, d, slots, resp = _paged_setup(db, 3, range(8, 17))
    session2 = sessionmaker(bind=db.get_bind())()
    try:
        client2, conv2 = _stale_pair(session2, client, conv)
        page1_ids = [str(s.id) for s in slots[:3]]
        middle_ids = [str(s.id) for s in slots[3:6]]
        assert list(conv2.booking_offered_slot_ids) == page1_ids

        _act(db, client, conv, LATER_ID)  # winner -> middle page
        assert conv.booking_offered_slot_ids == middle_ids
        winner_expiry = bc.ensure_utc(conv.booking_offer_expires_at)
        before = _counts(fakes)

        outcome = bc._resolve_selection_action(
            session2, client2, conv2, load_calendar_settings(client2),
            bc.SLOTS_LATER_CHOICE_ID, datetime.now(dt_timezone.utc),
        )
        assert outcome.status == bc.ACTION_EXECUTED
        loser = outcome.reply
        assert loser.handled is True
        assert loser.text.startswith("Here\u2019s what\u2019s open on ")
        assert loser.meta.get("offered_slots") == middle_ids
        loser_actions = loser.meta.get("calendar_actions") or []
        assert [e["action"]["choice_id"]
                for e in _slot_entries(loser_actions)] == middle_ids
        assert _nav_labels(loser_actions) == [EARLIER_LABEL, LATER_LABEL]

        db.expire_all()
        db.refresh(conv)
        assert conv.booking_offered_slot_ids == middle_ids
        assert bc.ensure_utc(conv.booking_offer_expires_at) == winner_expiry
        assert conv.booking_state == BookingState.WAITING_FOR_SLOT_SELECTION
        assert conv.booking_selected_slot_id is None
        assert _counts(fakes) == before
        _assert_all_available(db, slots)
    finally:
        session2.close()


def test_nav_controls_recomputed_from_current_availability(db, fakes):
    # F4 regression 5: the restored controls come from CURRENT
    # availability, never the stored offer. On the middle page, blocking
    # every earlier AND later slot yields a replacement with ZERO nav; re-
    # opening only the later slots yields exactly "See later times".
    # Throughout: zero offer-field mutation, zero holds, zero
    # appointments, zero notifications of any kind.
    client, conv, d, slots, resp = _paged_setup(db, 3, range(8, 17))
    _act(db, client, conv, LATER_ID)  # -> middle page (slots 3..5)
    middle_ids = [str(s.id) for s in slots[3:6]]
    expires_before = bc.ensure_utc(conv.booking_offer_expires_at)
    before = _counts(fakes)

    for s in slots[:3] + slots[6:]:
        row = refreshed_slot(db, s.id)
        row.status = SlotStatus.BLOCKED
        db.add(row)
    db.commit()

    detail = _act_409(db, client, conv, str(slots[0].id))
    assert detail["code"] == "STALE_CHOICE"
    replacement = detail.get("calendar_actions") or []
    assert [e["action"]["choice_id"]
            for e in _slot_entries(replacement)] == middle_ids
    assert _nav_labels(replacement) == []  # no direction is advertised

    for s in slots[6:]:
        row = refreshed_slot(db, s.id)
        row.status = SlotStatus.AVAILABLE
        db.add(row)
    db.commit()

    detail2 = _act_409(db, client, conv, str(slots[1].id))
    assert detail2["code"] == "STALE_CHOICE"
    replacement2 = detail2.get("calendar_actions") or []
    assert [e["action"]["choice_id"]
            for e in _slot_entries(replacement2)] == middle_ids
    assert _nav_labels(replacement2) == [LATER_LABEL]  # later only

    assert conv.booking_offered_slot_ids == middle_ids
    assert bc.ensure_utc(conv.booking_offer_expires_at) == expires_before
    assert conv.booking_state == BookingState.WAITING_FOR_SLOT_SELECTION
    assert conv.booking_selected_slot_id is None
    held = (db.query(AppointmentSlot)
            .filter(AppointmentSlot.client_id == client.id,
                    AppointmentSlot.status == SlotStatus.HELD).count())
    assert held == 0
    assert _counts(fakes) == before
    assert _appointments(db, conv) == []
