# calendar_tests/test_slot_count_six.py
#
# SLOT-COUNT-6 ACCEPTANCE — owner decision: the normal/default Calendar
# experience offers UP TO 6 exact appointment times after a date is chosen.
#
# The ONLY production change under test is the settings-owner default:
#     DEFAULT_MAX_OFFERED_SLOTS  3 -> 6   (calendar_settings_service.py)
# The clamp [1, 10], explicit tenant overrides, ordering, filtering, and
# every downstream consumer are UNCHANGED and pinned here.
#
# Owner acceptance letters (task document) -> tests below:
#   A  busy date with >= 8 eligible slots offers exactly 6
#   B  the 6 offered are the FIRST 6 eligible slots chronologically
#   C  4 eligible -> exactly 4        D  1 eligible -> exactly 1
#   E  0 eligible -> existing no-availability behavior unchanged
#   F  explicit tenant max_offered_slots=3 still offers 3
#   G  explicit tenant max_offered_slots=8 still offers 8
#   H  values below/above the clamp keep the existing clamp behavior
#   I  legacy WAITING_FOR_TIME_PREFERENCE part-of-day filter precedes the cap
#   J  fresh PREF_ANY path exposes morning AND later-day times inside the 6
#   K  no duplicate slot actions        L  slot UUIDs remain opaque
#   M  confirmation is still required before booking
#   N  offering six causes no appointment/email/SMS side effect
#
# Run (PostgreSQL required, as every calendar_tests module):
#   python -m pytest calendar_tests/test_slot_count_six.py -v

import uuid
from datetime import datetime, timedelta, timezone as dt_timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.calendar_models import AppointmentSlot, BookingState, SlotStatus
from app.repositories import appointment_repository
from app.services import booking_conversation as bc
from app.services.appointment_intent import PREF_ANY
from app.services.availability_rules import (
    filter_bookable_slots,
    list_bookable_slots,
)
from app.services.calendar_settings_service import (
    DEFAULT_MAX_OFFERED_SLOTS,
    load_calendar_settings,
)

# The REAL DB harness (autouse fakes stub every AI/Twilio/Resend boundary).
from calendar_tests.test_chat_integration import (  # noqa: F401
    fakes, make_client, make_conversation, make_slot, send,
    OPEN_ALL_WEEK_HOURS,
)
from calendar_tests.test_universal_appointment_signal import (  # noqa: F401
    _gated_client, _fresh,
)
# Package B helpers reused verbatim (Rule 3: one owner per idiom — the date
# drive, the direct-offer shape assert, and the legacy-state builder already
# exist and are pinned by the Package B suite).
from calendar_tests.test_package_b_direct_slot_offer import (
    _assert_direct_offer,
    _drive_intake_to_date,
    _human,
    _legacy_time_pref_conversation,
    _next_open_weekday,
    _slot_on,
)

NY = ZoneInfo("America/New_York")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _set_cap(db, client, value):
    """Rewrite ONLY the max_offered_slots key on the client's calendar
    settings JSONB. value=None REMOVES the key, producing the unconfigured
    office that exercises the code default (the population the owner
    decision targets). Mirrors the settings-mutation idiom _gated_client
    itself uses — no new configuration pathway is introduced."""
    settings = dict(client.settings or {})
    calendar = dict(settings.get("calendar") or {})
    if value is None:
        calendar.pop("max_offered_slots", None)
    else:
        calendar["max_offered_slots"] = value
    settings["calendar"] = calendar
    client.settings = settings
    db.add(client)
    db.commit()
    return client


def _default_cap_client(db):
    """A booking+actions+picker-enabled office with NO explicit
    max_offered_slots — the office whose behavior the default governs."""
    return _set_cap(db, _gated_client(db), None)


def _offer_on_busy_day(db, client, hours):
    """Publish one slot per hour on the same open weekday, drive a fresh
    intake to the date turn, and return (response, conversation, day)."""
    d = _next_open_weekday()
    for h in hours:
        _slot_on(db, client, d, hour=h)
    conv = _fresh(db, client)
    _drive_intake_to_date(db, client, conv)
    resp = send(db, client, conv, _human(d))
    return resp, conv, d


def _labels(actions):
    return [entry["label"] for entry in actions]


def _choice_ids(actions):
    return [entry["action"]["choice_id"] for entry in actions]


def _expected_labels(hours):
    """Server-formatted local labels for whole-hour slots, chronological."""
    out = []
    for h in sorted(hours):
        suffix = "AM" if h < 12 else "PM"
        display = h if 1 <= h <= 12 else (h - 12 if h > 12 else 12)
        out.append(f"{display}:00 {suffix}")
    return out


def _selected_slot_hour(db, conv):
    """Local start hour of the slot the conversation has selected/held."""
    slot = db.query(AppointmentSlot).filter(
        AppointmentSlot.id == uuid.UUID(str(conv.booking_selected_slot_id))
    ).one()
    return slot.start_datetime.astimezone(NY).hour


class _SettingsClient:
    """Minimal client double for load_calendar_settings (pure read)."""
    def __init__(self, calendar):
        self.settings = {"timezone": "America/New_York", "calendar": calendar}
        self.timezone = None


def _stub_slot(start_local):
    """Pure-rules slot stub: only the attributes the rules read."""
    return SimpleNamespace(
        id=uuid.uuid4(),
        status="available",
        start_datetime=start_local.astimezone(dt_timezone.utc),
        held_until=None,
        service_key=None,
    )


# ===========================================================================
# Settings owner — the default itself, explicit values, and the clamp (F, G,
# H at the owner; the constant pin makes an accidental future drift loud).
# ===========================================================================

def test_settings_default_cap_is_six():
    # The owner decision, pinned at its single owner: an office with no
    # explicit key gets 6, and the module constant IS that documented value.
    assert DEFAULT_MAX_OFFERED_SLOTS == 6
    loaded = load_calendar_settings(_SettingsClient({"booking_enabled": True}))
    assert loaded.max_offered_slots == 6


@pytest.mark.parametrize("explicit", [3, 8])
def test_settings_explicit_value_loads_verbatim(explicit):
    # F/G at the owner: an intentionally configured office keeps its value —
    # the default change cannot overwrite explicit tenant configuration.
    loaded = load_calendar_settings(
        _SettingsClient({"booking_enabled": True,
                         "max_offered_slots": explicit})
    )
    assert loaded.max_offered_slots == explicit


@pytest.mark.parametrize("raw,expected", [
    (0, 1),        # below the floor: clamped up (unchanged contract)
    (99, 10),      # above the ceiling: clamped down (unchanged contract)
    ("lots", 6),   # malformed: falls back to the (new) default
    (None, 6),     # JSON null: falls back to the (new) default
])
def test_settings_clamp_and_malformed_fallback(raw, expected):
    # H at the owner: the [1, 10] clamp is untouched; only the fallback
    # value moved 3 -> 6.
    loaded = load_calendar_settings(
        _SettingsClient({"booking_enabled": True, "max_offered_slots": raw})
    )
    assert loaded.max_offered_slots == expected


def test_pure_rules_first_six_chronological_of_eight():
    # A/B at the pure owner: with the default settings, filter_bookable_slots
    # returns exactly the FIRST SIX of the uncapped chronological list even
    # when the input arrives shuffled (the sort precedes the slice).
    settings = load_calendar_settings(
        _SettingsClient({"booking_enabled": True})
    )
    base = datetime.now(NY).replace(minute=0, second=0, microsecond=0)
    base = base + timedelta(days=10)
    ordered = [_stub_slot(base.replace(hour=h)) for h in range(9, 17)]  # 8
    shuffled = [ordered[i] for i in (5, 0, 7, 2, 6, 1, 4, 3)]
    now_utc = datetime.now(dt_timezone.utc)

    capped = filter_bookable_slots(shuffled, now_utc, settings, PREF_ANY)
    uncapped = list_bookable_slots(shuffled, now_utc, settings, PREF_ANY)

    assert len(uncapped) == 8
    assert len(capped) == 6
    assert capped == uncapped[:6]
    assert [s.id for s in capped] == [s.id for s in ordered[:6]]


# ===========================================================================
# A + B — busy default-cap office: exactly the first six, chronologically,
# through the REAL route.
# ===========================================================================

def test_default_busy_day_offers_exactly_first_six_chronological(db, fakes):
    hours = [9, 10, 11, 12, 13, 14, 15, 16]  # 8 eligible
    resp, conv, _ = _offer_on_busy_day(db, _default_cap_client(db), hours)
    actions = _assert_direct_offer(resp, conv)
    assert len(actions) == 6
    assert _labels(actions) == _expected_labels(hours[:6])
    # The persisted offer is the SAME six, in the same order.
    assert conv.booking_offered_slot_ids == _choice_ids(actions)
    # The visible menu numbers all six and stops there.
    assert "6)" in resp.reply and "7)" not in resp.reply
    # The 7th/8th chronological times are not offered anywhere.
    assert "3:00 PM" not in _labels(actions)
    assert "4:00 PM" not in _labels(actions)


# ===========================================================================
# C, D — fewer eligible slots than the cap offer exactly what exists.
# ===========================================================================

def test_four_eligible_offers_four(db, fakes):
    hours = [9, 10, 11, 13]
    resp, conv, _ = _offer_on_busy_day(db, _default_cap_client(db), hours)
    actions = _assert_direct_offer(resp, conv)
    assert len(actions) == 4
    assert _labels(actions) == _expected_labels(hours)


def test_one_eligible_offers_one(db, fakes):
    resp, conv, _ = _offer_on_busy_day(db, _default_cap_client(db), [10])
    actions = _assert_direct_offer(resp, conv)
    assert len(actions) == 1
    assert _labels(actions) == ["10:00 AM"]


# ===========================================================================
# E — zero eligible slots: the existing no-availability behavior is
# unchanged (day suggestions when they exist; a clean day re-ask when the
# scan finds nothing; never a slot menu, never leftover offer state).
# ===========================================================================

def test_zero_eligible_suggests_matching_days_unchanged(db, fakes):
    client = _default_cap_client(db)
    d = _next_open_weekday()
    nearby = _next_open_weekday(6)
    _slot_on(db, client, nearby)  # nothing on d; one real day later
    conv = _fresh(db, client)
    _drive_intake_to_date(db, client, conv)
    resp = send(db, client, conv, _human(d))
    assert conv.booking_state == BookingState.WAITING_FOR_DATE
    assert "matching availability" in resp.reply
    assert not (resp.meta or {}).get("calendar_actions")
    assert conv.booking_offered_slot_ids is None


def test_zero_eligible_no_days_reasks_cleanly(db, fakes):
    client = _default_cap_client(db)
    d = _next_open_weekday()  # no slots exist anywhere for this office
    conv = _fresh(db, client)
    _drive_intake_to_date(db, client, conv)
    resp = send(db, client, conv, _human(d))
    assert conv.booking_state == BookingState.WAITING_FOR_DATE
    assert not (resp.meta or {}).get("calendar_actions")
    assert conv.booking_offered_slot_ids is None
    assert conv.booking_offer_expires_at is None


# ===========================================================================
# F, G — explicit tenant values keep winning through the real route.
# ===========================================================================

def test_explicit_three_still_offers_three(db, fakes):
    client = _set_cap(db, _gated_client(db), 3)
    hours = [9, 10, 11, 12, 13, 14, 15, 16]
    resp, conv, _ = _offer_on_busy_day(db, client, hours)
    actions = _assert_direct_offer(resp, conv)
    assert len(actions) == 3
    assert _labels(actions) == _expected_labels(hours[:3])


def test_explicit_eight_still_offers_eight(db, fakes):
    client = _set_cap(db, _gated_client(db), 8)
    hours = [8, 9, 10, 11, 12, 13, 14, 15, 16]  # 9 eligible
    resp, conv, _ = _offer_on_busy_day(db, client, hours)
    actions = _assert_direct_offer(resp, conv)
    assert len(actions) == 8
    assert _labels(actions) == _expected_labels(hours[:8])


# ===========================================================================
# H — the clamp through the real route: floor and ceiling are unchanged.
# ===========================================================================

def test_route_cap_below_floor_clamps_to_one(db, fakes):
    client = _set_cap(db, _gated_client(db), 0)
    resp, conv, _ = _offer_on_busy_day(db, client, [9, 10, 11, 12])
    actions = _assert_direct_offer(resp, conv)
    assert len(actions) == 1
    assert _labels(actions) == ["9:00 AM"]


def test_route_cap_above_ceiling_clamps_to_ten(db, fakes):
    client = _set_cap(db, _gated_client(db), 99)
    hours = [7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17]  # 11 eligible
    resp, conv, _ = _offer_on_busy_day(db, client, hours)
    actions = _assert_direct_offer(resp, conv)
    assert len(actions) == 10
    assert _labels(actions) == _expected_labels(hours[:10])


# ===========================================================================
# I — legacy WAITING_FOR_TIME_PREFERENCE: the answered part of day filters
# BEFORE the cap (morning yields only mornings; a six-deep afternoon is
# capped at six with zero morning leakage).
# ===========================================================================

def test_legacy_morning_answer_filters_before_cap(db, fakes):
    client = _default_cap_client(db)
    d = _next_open_weekday()
    for h in [9, 10, 11, 12, 13, 14, 15, 16, 17]:
        _slot_on(db, client, d, hour=h)
    conv = _legacy_time_pref_conversation(db, client, date_value=d.isoformat())
    reply = bc.handle_booking_message(db, client, conv, "morning")
    actions = (reply.meta or {}).get("calendar_actions")
    assert actions and len(actions) == 3
    assert all(label.endswith("AM") for label in _labels(actions))
    assert conv.booking_time_preference == "morning"


def test_legacy_afternoon_bucket_complete_and_unpolluted(db, fakes):
    # The frozen vocabulary defines afternoon as [12:00, 17:00): with the
    # day holding 3 morning + 5 afternoon + 1 evening slot, the answered
    # bucket is offered COMPLETE (all five, under the six cap) with zero
    # morning leakage and 5:00 PM correctly excluded as evening.
    client = _default_cap_client(db)
    d = _next_open_weekday()
    for h in [9, 10, 11, 12, 13, 14, 15, 16, 17]:
        _slot_on(db, client, d, hour=h)
    conv = _legacy_time_pref_conversation(db, client, date_value=d.isoformat())
    reply = bc.handle_booking_message(db, client, conv, "afternoon")
    actions = (reply.meta or {}).get("calendar_actions")
    assert actions and len(actions) == 5
    assert _labels(actions) == _expected_labels([12, 13, 14, 15, 16])
    assert "5:00 PM" not in _labels(actions)
    assert all(label.endswith("PM") for label in _labels(actions))


# ===========================================================================
# J — fresh PREF_ANY Package B path: the first six naturally span morning
# AND afternoon when the day does.
# ===========================================================================

def test_pref_any_first_six_span_morning_and_afternoon(db, fakes):
    hours = [9, 10, 11, 13, 14, 16, 17]  # 7 eligible across the day
    resp, conv, _ = _offer_on_busy_day(db, _default_cap_client(db), hours)
    actions = _assert_direct_offer(resp, conv)
    labels = _labels(actions)
    assert len(actions) == 6
    assert labels == _expected_labels(hours[:6])
    assert any(label.endswith("AM") for label in labels)
    assert any(label.endswith("PM") for label in labels)
    assert "5:00 PM" not in labels  # the 7th chronological time is excluded
    assert conv.booking_effective_time_preference == "any"


# ===========================================================================
# K, L — six actions carry no duplicates and stay opaque.
# ===========================================================================

def test_no_duplicate_actions_at_six(db, fakes):
    hours = [9, 10, 11, 12, 13, 14, 15, 16]
    resp, conv, _ = _offer_on_busy_day(db, _default_cap_client(db), hours)
    actions = _assert_direct_offer(resp, conv)
    ids = _choice_ids(actions)
    assert len(ids) == 6 and len(set(ids)) == 6
    assert len(set(_labels(actions))) == 6
    assert conv.booking_offered_slot_ids == ids


def test_slot_ids_remain_opaque_at_six(db, fakes):
    hours = [9, 10, 11, 12, 13, 14, 15, 16]
    resp, conv, _ = _offer_on_busy_day(db, _default_cap_client(db), hours)
    actions = _assert_direct_offer(resp, conv)
    assert len(actions) == 6
    for entry in actions:
        cid = entry["action"]["choice_id"]
        assert cid and cid != entry["label"]
        assert cid not in (resp.reply or "")


# ===========================================================================
# Selection mechanics at six — bare digits 4..6 pick the 4th..6th offer, the
# documented time-over-index precedence is untouched, and (M) confirmation
# is still the only path to a booking.
# ===========================================================================

def test_digit_six_selects_sixth_offer(db, fakes):
    # The bare digit "6" (no offered 6:00 time to collide with) picks the
    # SIXTH offer — index selection is list-length-driven, not capped at 3.
    client = _default_cap_client(db)
    hours = [9, 10, 11, 12, 13, 14, 15, 16]
    resp, conv, _ = _offer_on_busy_day(db, client, hours)
    actions = _assert_direct_offer(resp, conv)
    assert len(actions) == 6
    send(db, client, conv, "6")
    assert conv.booking_state == BookingState.WAITING_FOR_CONFIRMATION
    assert _selected_slot_hour(db, conv) == 14  # the sixth offer: 2:00 PM


def test_bare_digit_time_precedence_unchanged_at_six(db, fakes):
    # Offered includes 4:00 PM; the bare digit "4" matches the TIME (the
    # documented precedence) and never index 4 — unchanged at six.
    client = _default_cap_client(db)
    hours = [11, 12, 13, 14, 15, 16]  # index 4 -> 3:00 PM; 16h -> 4:00 PM
    resp, conv, _ = _offer_on_busy_day(db, client, hours)
    actions = _assert_direct_offer(resp, conv)
    assert _labels(actions)[-1] == "4:00 PM"
    send(db, client, conv, "4")
    assert conv.booking_state == BookingState.WAITING_FOR_CONFIRMATION
    assert _selected_slot_hour(db, conv) == 16  # the 4:00 PM slot, not 3:00


def test_confirmation_required_then_books_exactly_once_via_fifth(db, fakes):
    # M (+ the digit-beyond-three pin): picking option "5" of six holds the
    # 1:00 PM slot and does NOT book; only the explicit Yes creates exactly
    # one appointment, one office email, one office SMS, no patient SMS.
    client = _default_cap_client(db)
    hours = [9, 10, 11, 12, 13, 14, 15, 16]
    resp, conv, _ = _offer_on_busy_day(db, client, hours)
    actions = _assert_direct_offer(resp, conv)
    assert len(actions) == 6

    send(db, client, conv, "5")
    assert conv.booking_state == BookingState.WAITING_FOR_CONFIRMATION
    assert _selected_slot_hour(db, conv) == 13  # the fifth offer: 1:00 PM
    assert appointment_repository.get_appointment_by_conversation(
        db, client.id, conv.id) is None
    assert len(fakes.booking_sms) == 0 and len(fakes.booking_email) == 0

    r_yes = send(db, client, conv, "yes")
    appt = appointment_repository.get_appointment_by_conversation(
        db, client.id, conv.id)
    assert appt is not None
    assert (r_yes.meta or {}).get("booked") is True
    assert (conv.booking_state or BookingState.NONE) == BookingState.NONE
    assert len(fakes.booking_sms) == 1
    assert len(fakes.booking_email) == 1


# ===========================================================================
# N — merely OFFERING six causes no appointment and no notification of any
# kind beyond what the intake itself already produced.
# ===========================================================================

def test_offer_of_six_causes_no_side_effects(db, fakes):
    client = _default_cap_client(db)
    d = _next_open_weekday()
    for h in [9, 10, 11, 12, 13, 14, 15, 16]:
        _slot_on(db, client, d, hour=h)
    conv = _fresh(db, client)
    _drive_intake_to_date(db, client, conv)
    lead_sms_before = len(fakes.lead_sms)
    lead_email_before = len(fakes.lead_email)

    resp = send(db, client, conv, _human(d))
    actions = _assert_direct_offer(resp, conv)
    assert len(actions) == 6
    assert appointment_repository.get_appointment_by_conversation(
        db, client.id, conv.id) is None
    assert len(fakes.booking_sms) == 0 and len(fakes.booking_email) == 0
    assert len(fakes.lead_sms) == lead_sms_before
    assert len(fakes.lead_email) == lead_email_before


# ===========================================================================
# Round-1 audit regression -- the no-match clarification at six is
# count-agnostic: never "1, 2, or 3", always the number-or-time guidance,
# with all six offers left authoritative and no side effects from the
# failed turn.
# ===========================================================================

def test_nonmatching_reply_clarifies_count_agnostic(db, fakes):
    client = _default_cap_client(db)
    hours = [9, 10, 11, 12, 13, 14, 15, 16]
    resp, conv, _ = _offer_on_busy_day(db, client, hours)
    actions = _assert_direct_offer(resp, conv)
    assert len(actions) == 6
    offered_before = list(conv.booking_offered_slot_ids)

    r_clarify = send(db, client, conv, "banana")
    assert conv.booking_state == BookingState.WAITING_FOR_SLOT_SELECTION
    assert "1, 2, or 3" not in (r_clarify.reply or "")
    assert "You can reply with a number or a time." in (r_clarify.reply or "")
    # All six stay authoritative: the persisted offer is untouched...
    assert conv.booking_offered_slot_ids == offered_before
    assert conv.booking_selected_slot_id is None
    # ...and no hold, appointment, or notification came from the turn.
    held = db.query(AppointmentSlot).filter(
        AppointmentSlot.client_id == client.id,
        AppointmentSlot.status == SlotStatus.HELD).count()
    assert held == 0
    assert appointment_repository.get_appointment_by_conversation(
        db, client.id, conv.id) is None
    assert len(fakes.booking_sms) == 0 and len(fakes.booking_email) == 0
    # The sixth choice still selects -- the offers remained live.
    send(db, client, conv, "6")
    assert conv.booking_state == BookingState.WAITING_FOR_CONFIRMATION
    assert _selected_slot_hour(db, conv) == 14
