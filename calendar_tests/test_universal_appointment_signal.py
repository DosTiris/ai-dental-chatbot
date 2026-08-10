# calendar_tests/test_universal_appointment_signal.py
#
# UNIVERSAL appointment date/time-preference signal coverage.
#
# Includes the Night Guard appointment-collision regression suite (a recognized
# service selection owns the reason-capture turn and never leaks an evening time
# window from the token "night" in "night guard / teeth grinding").
#
# ONE centralized, STATE-BASED contract (no hard-coded button/reason list):
#   Any legitimate appointment flow that AUTHORITATIVELY enters date selection
#   receives the calendar picker — regardless of which service, reason, button,
#   synonym, or intake branch (capture-first vs short-symptom/urgent) caused it.
#   Eligibility is decided by two pure ROUTE-owned stage predicates
#   (app.routes.chat.capture_first_time_window_pending /
#    capture_first_short_symptom_time_window_pending), so a newly configured
#   dental service is covered without a code-label change.
#
# CONSOLIDATED-AUDIT CORRECTIONS (retained):
#   #1 REAL button/action payloads. The service-button payload is the exact
#      message the REAL widget builder (chat.build_widget_service_buttons)
#      emits — a constructed phrase ("I need fillings", "I have tooth pain"),
#      NOT the service display_name. test_widget_builder_messages_match_rule
#      proves the payload helper is byte-faithful to that owner.
#   #3 THREE-LAYER reason contract for EVERY registry-derived service:
#        expected registry key
#          -> exact real button/message payload
#          -> authoritative mapped lead_reason
#             (service_policy_mapping.calendar_policy_value_for_master_service)
#          -> matching lead_reason_source_text (owner normalization)
#          -> date-stage signal
#      with failure guards (wrong key / wrong mapped reason / absent-or-foreign
#      source / generic-fallback-only reach all FAIL the test).
#   #2 Book Appointment driven from a fresh conversation with per-boundary
#      asserts; never a blind name while a reason is still owed.
#   #4 booking_mode "capture_first" AND "hybrid" proven deterministically.
#   #5 full flow to server-owned slot calendar_actions (patient-facing labels;
#      opaque choice_id under action, never surfaced as a label).
#   #6 custom/future reason via the authoritative Other-acceptance owner
#      (Contract B: once accepted, the centralized state rule schedules with
#      no label-specific code change).
#   #7 completed exclusion matrix.
#   #8 unconditional same-day (typed "today", full month-name, message-mode).
#
# Run (PostgreSQL required, as every calendar_tests module):
#   python -m pytest calendar_tests/test_universal_appointment_signal.py -v

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

import app.routes.chat as chat_module
from app.routes.chat import (
    build_widget_service_buttons,
    capture_first_time_window_pending,
    capture_first_short_symptom_time_window_pending,
    conversation_is_locked,
)
from app.calendar_models import BookingState, SlotStatus
from app.services import mia_service_library as msl
from app.services.booking_conversation import (
    intake_date_stage_signal,
    INTAKE_TIME_PREFERENCE_PROMPT,
    INTAKE_TIME_PREFERENCE_TODAY_PROMPT,
    CALENDAR_INTAKE_DATE_ONLY_PROMPT_TAIL,
    CALENDAR_INTAKE_DATE_ONLY_SHORT_SYMPTOM_PROMPT_TAIL,
)
from app.services.service_policy_mapping import (
    calendar_policy_value_for_master_service,
)

# The REAL DB harness (autouse fakes stub every AI/Twilio/Resend boundary).
from calendar_tests.test_chat_integration import (  # noqa: F401
    fakes, make_client, make_conversation, make_slot, send,
    OPEN_ALL_WEEK_HOURS,
)

DATE_SIGNAL = {"stage": "date", "submit": "message"}
TIME_SIGNAL = {"stage": "time_preference"}
# PACKAGE B: a stored date proceeds DIRECTLY to the server-owned exact-slot
# offer; the slot_selection signal replaces the removed time_preference stage
# on every date turn, and TIME_SIGNAL below is retained solely to pin that
# the removed stage never reappears.
SLOT_SIGNAL = {"stage": "slot_selection"}
GENERIC_FALLBACK_REASON = "appointment request"

NY = ZoneInfo("America/New_York")

MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
            "Saturday", "Sunday"]

# --- Inventory DERIVED from the authoritative owners (Rule 9) -----------------
# Appointment-capable == has a Calendar-policy mapping (the mapping owner
# intentionally omits the four admin_other keys, so this is the same partition
# the production reason path uses). Never a hand-written list.
APPT_SERVICE_KEYS = sorted(
    k for k in msl.MASTER_DENTAL_SERVICES
    if calendar_policy_value_for_master_service(k) is not None
)
INFO_SERVICE_KEYS = sorted(
    k for k, s in msl.MASTER_DENTAL_SERVICES.items()
    if s.category == "admin_other"
)


# ---------------------------------------------------------------------------
# Payload + assertion helpers (single owners for the three-layer contract)
# ---------------------------------------------------------------------------

def _norm(text):
    """Compare source text with the SAME normalization the route owner applies
    (chat._norm_text) — never a stricter byte-for-byte rule."""
    return chat_module._norm_text(text or "")


def widget_message_for(key):
    """The EXACT message chat.build_widget_service_buttons emits for a service
    key. Mirrors that single owner's rule (Rule 3); its fidelity to the real
    builder is proven by test_widget_builder_messages_match_rule, so every
    payload used below is the real patient-facing button payload, not a
    display_name echo."""
    if key == "other":
        return "Other"
    label = msl.MASTER_DENTAL_SERVICES[key].display_name.strip()
    if key == "tooth_pain":
        return "I have tooth pain"
    if key in {"broken_tooth", "lost_crown_filling"}:
        return "I have a %s" % label.lower()
    return "I need %s" % label.lower()


def expected_reason_for(key):
    """The authoritative mapped lead_reason for a service key (policy owner)."""
    return calendar_policy_value_for_master_service(key)


def _gated_client(db, *, booking_mode="capture_first",
                  office_hours=OPEN_ALL_WEEK_HOURS, booking_url=None,
                  enable_all=True):
    """A booking-enabled office with the three strict picker gates true and a
    named booking_mode. No external booking_url by default, so the INTERNAL
    capture-first date path runs in both capture_first and hybrid modes (the
    external handoff is gated by has_external_booking)."""
    client = make_client(db, calendar_enabled=True, office_hours=office_hours,
                         booking_url=booking_url)
    settings = dict(client.settings or {})
    calendar = dict(settings.get("calendar") or {})
    calendar.update({
        "booking_enabled": True,
        "calendar_actions_enabled": True,
        "calendar_picker_enabled": True,
    })
    settings["calendar"] = calendar
    settings["booking_mode"] = booking_mode
    if enable_all:
        # Enable every appointment-capable service so any registry entry can be
        # started for this office (client isolation preserved: fresh client).
        settings["enabled_services"] = list(APPT_SERVICE_KEYS)
    client.settings = settings
    db.add(client)
    db.commit()
    return client


def _fresh(db, client):
    """A brand-new lead conversation with NO intake fields set; intake is
    completed entirely through the real route."""
    return make_conversation(
        db, client, lead_reason=None, lead_reason_source_text=None,
        lead_name=None, lead_phone=None, lead_time_window=None,
        lead_email_opt_out=False, lead_is_new_patient=None,
        lead_is_priority=False, lead_is_emergency=False)


def _date_message(d):
    """The full month-name message shape used by the Next-7-Days strip and the
    message-mode full-calendar submission."""
    return "%s, %s %d, %d" % (WEEKDAYS[d.weekday()], MONTHS[d.month - 1],
                              d.day, d.year)


def _last_assistant_text(db, conv):
    from app.models import Message
    m = (db.query(Message)
           .filter(Message.conversation_id == conv.id, Message.role == "assistant")
           .order_by(Message.created_at.desc()).first())
    return (m.content or "") if m else ""


def _flow_just_asked_patient_type(db, conv):
    """True when the most recent assistant message asked New/Returning. Package A
    asks it first (right after the reason) for Mia-owned intake, but some safety
    owners (e.g. the self-treatment guard) preempt without asking it - answer the
    question only when it is genuinely pending."""
    return "new or returning" in _last_assistant_text(db, conv).lower()


def _complete_intake_to_date(db, client, conv):
    """After a reason is captured, complete name/phone (and email for the
    standard branch) through the REAL route. Returns the response at the turn
    that enters date selection. Handles standard (email step) and short-symptom
    (no email) without assuming which branch the reason took.

    Package A asks New/Returning first, right after the reason, so this drains
    the intake by answering whichever interstitial the flow raises - the
    New/Returning question, or a service-offer clarification that a safety owner
    (e.g. the self-treatment guard) can interpose - and otherwise supplies
    name / phone / email in order, until the date signal is reached."""
    fields = ["Jordan Rivera", "516-555-0100", "skip email"]
    resp = None
    for _ in range(12):
        db.refresh(conv)
        if resp is not None and (resp.meta or {}).get("calendar_picker") == DATE_SIGNAL:
            return resp
        last = _last_assistant_text(db, conv).lower()
        if "new or returning" in last:
            resp = send(db, client, conv, "new patient")     # patient type
        elif "just to confirm" in last:
            resp = send(db, client, conv, "yes")             # accept clarification
        elif fields:
            resp = send(db, client, conv, fields.pop(0))     # name / phone / email
        else:
            resp = send(db, client, conv, "skip email")
    return resp


def _assert_entry_identity(payload, key):
    """LAYER 1 — the exact real payload resolves through the authoritative
    matcher to THIS registry key (not merely any match)."""
    matched = msl.find_matching_service(payload, None)
    assert matched is not None, "payload %r matched no service" % payload
    assert matched.key == key, (
        "LAYER 1 entry-identity: payload %r resolved to %r, expected %r"
        % (payload, matched.key, key))


def _assert_persisted_and_provenance(conv, key, payload):
    """LAYER 2 (persisted policy reason) + LAYER 3 (source provenance) with the
    audit's failure guards."""
    expected = expected_reason_for(key)
    # LAYER 2: the persisted reason is EXACTLY the authoritative policy value.
    assert conv.lead_reason == expected, (
        "LAYER 2 policy-reason: %r stored %r, expected policy value %r"
        % (key, conv.lead_reason, expected))
    # FAILURE GUARD: a service whose policy value is specific must NOT have
    # reached the calendar via the generic appointment fallback.
    if expected != GENERIC_FALLBACK_REASON:
        assert conv.lead_reason != GENERIC_FALLBACK_REASON, (
            "generic fallback masked the specific reason for %r" % key)
    # LAYER 3: the stored source text is the exact payload that was sent
    # (owner normalization), never absent and never a different entry's text.
    src = conv.lead_reason_source_text
    assert src, "LAYER 3 provenance: source text absent for %r" % key
    assert _norm(src) == _norm(payload), (
        "LAYER 3 provenance: source %r does not reflect sent payload %r"
        % (src, payload))


def _assert_date_stage(conv, client, resp, key=None):
    """The date-stage signal is present and one matching pure predicate holds."""
    assert resp.meta.get("calendar_picker") == DATE_SIGNAL, (
        "date signal missing for %r" % key)
    reply = resp.reply or ""
    # UX-C: the signal pairs ONLY with the Calendar date-only tails.
    assert (reply.endswith(CALENDAR_INTAKE_DATE_ONLY_PROMPT_TAIL)
            or reply.endswith(CALENDAR_INTAKE_DATE_ONLY_SHORT_SYMPTOM_PROMPT_TAIL)), (
        "date prompt tail unrecognized for %r: %r" % (key, reply))
    assert (capture_first_time_window_pending(conv, client)
            or capture_first_short_symptom_time_window_pending(conv, client)), (
        "no pure date-stage predicate holds for %r" % key)


# ===========================================================================
# COMPLETENESS — inventory derived from the live registry (audit #9)
# ===========================================================================

def test_inventory_partition_is_complete_and_disjoint():
    # FUTURE-SAFE contract (no hard count): the two derived partitions exactly
    # cover the master registry and never overlap, so a correctly-mapped NEW
    # service is automatically included in the parametrized coverage below.
    appt = set(APPT_SERVICE_KEYS)
    info = set(INFO_SERVICE_KEYS)
    assert appt and info
    assert appt.isdisjoint(info)
    assert appt | info == set(msl.MASTER_DENTAL_SERVICES)
    # Partition owners: appointment == has a Calendar-policy mapping;
    # informational == admin_other and intentionally unmapped.
    assert all(calendar_policy_value_for_master_service(k) is not None for k in appt)
    assert all(calendar_policy_value_for_master_service(k) is None for k in info)
    # "other" is a widget button, NOT a master service key: it never enters the
    # one-turn parameterization and is proven via its dedicated two-turn tests
    # (validated Other + Contract B).
    assert "other" not in appt
    assert "other" not in msl.MASTER_DENTAL_SERVICES


def test_inventory_baseline_counts_snapshot():
    # BASELINE EVIDENCE ONLY (not the future-coverage contract): the exact
    # partition sizes observed at c7a5adc. Update deliberately when a mapped
    # service is added; the coverage machinery above adapts automatically.
    assert (len(APPT_SERVICE_KEYS), len(INFO_SERVICE_KEYS),
            len(msl.MASTER_DENTAL_SERVICES)) == (37, 4, 41)
    assert set(INFO_SERVICE_KEYS) == {
        "insurance_question", "payment_financing",
        "records_request", "prescription_question",
    }


def test_widget_builder_messages_match_rule(db, fakes):
    """Proves widget_message_for is byte-faithful to the REAL builder for the
    configured visible buttons, so every payload used below is a real button
    payload (audit #1)."""
    client = _gated_client(db)
    for button in build_widget_service_buttons(client):
        key = button["key"]
        assert button["message"] == widget_message_for(key), (
            key, button["message"], widget_message_for(key))


@pytest.mark.parametrize("key", APPT_SERVICE_KEYS)
def test_entry_identity_all_services(key):
    # LAYER 1 for every appointment-capable service: the real button payload
    # resolves through the authoritative matcher to this same key.
    _assert_entry_identity(widget_message_for(key), key)


# ===========================================================================
# THREE-LAYER CONTRACT — every registry service through the REAL route (audit
# #1 payloads, #3 persistence, #9 completeness)
# ===========================================================================

@pytest.mark.parametrize("key", APPT_SERVICE_KEYS)
def test_every_appointment_service_three_layer(db, fakes, key):
    client = _gated_client(db)
    payload = widget_message_for(key)
    _assert_entry_identity(payload, key)                      # LAYER 1

    conv = _fresh(db, client)
    send(db, client, conv, payload)                            # reason turn
    _assert_persisted_and_provenance(conv, key, payload)       # LAYER 2 + 3

    resp = _complete_intake_to_date(db, client, conv)
    _assert_date_stage(conv, client, resp, key)                # date signal


def test_configured_service_buttons_end_to_end(db, fakes):
    """Drive the REAL build_widget_service_buttons output through the route
    (audit #1 / #5F): the actual configured buttons, not a reconstruction."""
    client = _gated_client(db)
    for button in build_widget_service_buttons(client):
        key = button["key"]
        if key == "other":
            continue  # Other needs a validated free-text detail (covered below)
        payload = button["message"]
        _assert_entry_identity(payload, key)
        conv = _fresh(db, client)
        send(db, client, conv, payload)
        _assert_persisted_and_provenance(conv, key, payload)
        resp = _complete_intake_to_date(db, client, conv)
        _assert_date_stage(conv, client, resp, key)


# Aliases below are REAL registry aliases (svc.aliases) that the authoritative
# matcher resolves to the expected key — verified by the Layer-1 assertion.
@pytest.mark.parametrize("alias,expect_key", [
    ("dental cleaning", "cleaning_checkup"),   # ordinary
    ("my tooth hurts", "tooth_pain"),          # short-symptom/urgent
    ("whitening", "teeth_whitening"),          # cosmetic
    ("clear aligners", "invisalign"),          # orthodontic
    ("pull a tooth", "tooth_extraction"),      # surgical
])
def test_free_text_alias_three_layer(db, fakes, alias, expect_key):
    client = _gated_client(db)
    _assert_entry_identity(alias, expect_key)                 # LAYER 1
    conv = _fresh(db, client)
    send(db, client, conv, alias)
    _assert_persisted_and_provenance(conv, expect_key, alias)  # LAYER 2 + 3
    resp = _complete_intake_to_date(db, client, conv)
    _assert_date_stage(conv, client, resp, expect_key)


# --- FAILURE-GUARD proof: the three-layer harness rejects wrong outcomes -----

def test_three_layer_guard_rejects_wrong_service(db, fakes):
    """The contract FAILS (a) when a payload resolves to a different key,
    (b) when the persisted mapped reason differs from the policy value, and
    (c) when the source belongs to another entry. Proven with a cleaning
    payload checked against a foreign key so a false pass is impossible."""
    client = _gated_client(db)
    payload = widget_message_for("cleaning_checkup")
    # (a) entry identity must reject a foreign key.
    with pytest.raises(AssertionError):
        _assert_entry_identity(payload, "tooth_extraction")

    conv = _fresh(db, client)
    send(db, client, conv, payload)
    # (b) policy-reason must reject a foreign expectation.
    with pytest.raises(AssertionError):
        _assert_persisted_and_provenance(conv, "tooth_extraction", payload)
    # (c) provenance must reject a foreign source payload.
    with pytest.raises(AssertionError):
        _assert_persisted_and_provenance(
            conv, "cleaning_checkup", widget_message_for("root_canal"))
    # Correct triple still holds.
    _assert_persisted_and_provenance(conv, "cleaning_checkup", payload)


# ===========================================================================
# BOOK APPOINTMENT — fresh conversation, per-boundary asserts (audit #2)
# ===========================================================================

def test_book_appointment_sequence_per_boundary(db, fakes):
    client = _gated_client(db)
    conv = _fresh(db, client)

    # "Book Appointment" is a GENERIC scheduling trigger: the route stores the
    # generic "appointment request" (not a specific reason), must NOT emit a
    # date signal, must NOT accept a name yet, and must actively REQUEST a
    # service/reason. (Regression target: never blind-send a name here.)
    r0 = send(db, client, conv, "Book Appointment")
    assert (r0.meta or {}).get("calendar_picker") != DATE_SIGNAL
    assert (conv.lead_reason or "") in ("", GENERIC_FALLBACK_REASON), (
        "Book Appointment must not resolve to a specific reason on its own")
    assert not (conv.lead_name or "").strip(), (
        "route must not accept a name while a specific reason is still owed")
    assert "brings you in" in (r0.reply or "").lower(), (
        "Mia must request a service/reason after Book Appointment; got %r"
        % r0.reply)

    # Supply a REAL appointment-capable service button payload as the reason;
    # run the FULL three-layer persistence/provenance assertion (not only the
    # mapped lead_reason).
    payload = widget_message_for("fillings")
    _assert_entry_identity(payload, "fillings")               # LAYER 1
    send(db, client, conv, payload)
    # LAYER 2 (persisted policy reason): the specific fillings payload ENRICHES
    # the generic "appointment request" to the authoritative policy value.
    assert conv.lead_reason == expected_reason_for("fillings"), (
        "Book Appointment -> fillings must enrich lead_reason to the policy "
        "value; stored %r" % conv.lead_reason)
    assert conv.lead_reason != GENERIC_FALLBACK_REASON
    # LAYER 3 (provenance): lead_reason_source_text is owned as the ORIGINAL
    # scheduling trigger and is written SET-ONCE (only when empty). The route
    # therefore keeps "Book Appointment" here instead of replacing it with the
    # later fillings text -- verified live against the main-capture owner. This
    # is the authoritative source semantics, NOT a replacement contract, so the
    # correction is to the test's expectation, not to production.
    assert _norm(conv.lead_reason_source_text) == _norm("Book Appointment"), (
        "source provenance must remain the original 'Book Appointment' trigger; "
        "stored %r" % conv.lead_reason_source_text)

    # Package A: New/Returning is asked first, right after the (now authoritative)
    # reason and before name. Answer it, then continue name -> phone -> (email).
    db.refresh(conv)
    if getattr(conv, "lead_is_new_patient", None) is None:
        send(db, client, conv, "new patient")

    # Only now: name -> phone -> (email) -> date signal, per boundary.
    r_name = send(db, client, conv, "Jordan Rivera")
    assert (r_name.meta or {}).get("calendar_picker") != DATE_SIGNAL
    resp = send(db, client, conv, "516-555-0100")
    if (resp.meta or {}).get("calendar_picker") != DATE_SIGNAL:
        resp = send(db, client, conv, "skip email")
    _assert_date_stage(conv, client, resp, "fillings-after-book")


# ===========================================================================
# BOOKING MODES — capture_first AND hybrid, deterministically (audit #4)
# ===========================================================================

@pytest.mark.parametrize("mode", ["capture_first", "hybrid"])
def test_mode_standard_service_reaches_date_signal(db, fakes, mode):
    client = _gated_client(db, booking_mode=mode)   # no external booking_url
    payload = widget_message_for("cleaning_checkup")
    conv = _fresh(db, client)
    send(db, client, conv, payload)
    _assert_persisted_and_provenance(conv, "cleaning_checkup", payload)
    resp = _complete_intake_to_date(db, client, conv)
    _assert_date_stage(conv, client, resp, "cleaning_checkup(%s)" % mode)


@pytest.mark.parametrize("mode", ["capture_first", "hybrid"])
def test_mode_short_symptom_service_reaches_date_signal(db, fakes, mode):
    client = _gated_client(db, booking_mode=mode)
    payload = widget_message_for("tooth_pain")       # "I have tooth pain"
    conv = _fresh(db, client)
    send(db, client, conv, payload)
    _assert_persisted_and_provenance(conv, "tooth_pain", payload)
    resp = _complete_intake_to_date(db, client, conv)
    _assert_date_stage(conv, client, resp, "tooth_pain(%s)" % mode)
    # Short-symptom branch specifically owns the short tail.
    assert (resp.reply or "").endswith(CALENDAR_INTAKE_DATE_ONLY_SHORT_SYMPTOM_PROMPT_TAIL)


# ===========================================================================
# VALIDATED "OTHER" + CUSTOM/FUTURE REASON (audit #5C, #6 — Contract B)
# ===========================================================================

def test_validated_other_dental_reason_reaches_date_signal(db, fakes):
    client = _gated_client(db)
    conv = _fresh(db, client)
    send(db, client, conv, "Other")
    # A detail the AUTHORITATIVE Other classifier accepts as dental
    # (classify_other_reason_detail -> "dental"). "...need it looked at" failed
    # coverage rule B and returned non_dental; verified against the real owner.
    detail = "I chipped a molar"
    send(db, client, conv, detail)
    # Provenance for the Other path: the free-text detail is the stored source.
    assert _norm(conv.lead_reason_source_text) == _norm(detail)
    assert (conv.lead_reason or "").strip(), "validated Other must store a reason"
    resp = _complete_intake_to_date(db, client, conv)
    _assert_date_stage(conv, client, resp, "other:dental")


# CONTRACT B (custom/future reason) is proven end-to-end by
# test_full_flow_E_custom_contract_b in the FULL FLOW section below, which
# additionally asserts the custom phrase is unrecognized by the master matcher.


# ===========================================================================
# SAME-DAY — unconditional, clock pinned to an open weekday (audit #8)
# ===========================================================================

def _pin_open_weekday(monkeypatch, client, hour=10):
    base = chat_module.get_client_now(client).date()
    while base.weekday() >= 5:              # roll to Mon-Fri
        base += timedelta(days=1)
    pinned = datetime(base.year, base.month, base.day, hour, 0, tzinfo=NY)
    monkeypatch.setattr(chat_module, "get_client_now", lambda c: pinned)
    return pinned


def _at_date_stage(db, client, reason="cleaning/checkup"):
    """A conversation already at the standard date stage (email opted out, time
    window unset)."""
    return make_conversation(
        db, client, lead_reason=reason,
        lead_reason_source_text="cleaning", lead_email_opt_out=True,
        lead_time_window=None, lead_is_new_patient=True)


def _publish_slot_on(db, client, day, hour=10):
    """PACKAGE B: one AVAILABLE slot on an EXACT local calendar day, derived
    through the same make_slot owner (days_ahead relative to the real clock),
    so date-turn tests can assert the direct exact-slot offer."""
    real_today = datetime.now(NY).date()
    return make_slot(db, client, days_ahead=(day - real_today).days, hour=hour,
                     status=SlotStatus.AVAILABLE)


def _pin_both_clocks(monkeypatch, client, hour=10):
    """PACKAGE B: same-day tests reach the ENGINE on the date turn now, so
    the engine's single clock owner (booking_conversation.client_now — the
    sanctioned test_booking_db._fixed_clock pattern) must agree with the
    route clock; otherwise the slot-eligibility notice check floats with the
    real run time."""
    import app.services.booking_conversation as _bc
    pinned = _pin_open_weekday(monkeypatch, client, hour=hour)
    monkeypatch.setattr(_bc, "client_now", lambda settings: pinned)
    return pinned


def _assert_direct_slot_offer(resp, conv):
    """PACKAGE B shared pin: a stored date proceeds DIRECTLY to the
    server-owned exact-slot offer — WAITING_FOR_SLOT_SELECTION with
    calendar_choice actions and the slot_selection picker signal — and the
    removed time-preference stage never reappears in any form."""
    assert conv.booking_state == BookingState.WAITING_FOR_SLOT_SELECTION
    assert (resp.meta or {}).get("calendar_picker") == SLOT_SIGNAL
    assert resp.reply != INTAKE_TIME_PREFERENCE_PROMPT
    assert resp.reply != INTAKE_TIME_PREFERENCE_TODAY_PROMPT
    assert "morning or afternoon" not in (resp.reply or "").lower()
    actions = (resp.meta or {}).get("calendar_actions")
    assert actions, "server-owned slot buttons expected on the date turn"
    for e in actions:
        assert e["action"]["type"] == "calendar_choice"


def test_future_date_advances_directly_to_slot_offer(db, fakes, monkeypatch):
    # PACKAGE B (was: ...emits_time_preference): a valid FUTURE date at the
    # intake date stage completes intake and returns the engine's exact-slot
    # offer on the SAME turn — no morning/afternoon question, no
    # time_preference signal, ever.
    client = _gated_client(db)
    pinned = _pin_both_clocks(monkeypatch, client)
    conv = _at_date_stage(db, client)
    d = pinned.date() + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    _publish_slot_on(db, client, d)
    resp = send(db, client, conv, _date_message(d))
    _assert_direct_slot_offer(resp, conv)


def test_same_day_full_month_name_advances_directly_to_slot_offer(db, fakes, monkeypatch):
    # PACKAGE B: today (full month-name message) behaves exactly like a
    # future date — straight to the exact-slot offer.
    client = _gated_client(db)
    pinned = _pin_both_clocks(monkeypatch, client)
    conv = _at_date_stage(db, client)
    _publish_slot_on(db, client, pinned.date(), hour=15)
    resp = send(db, client, conv, _date_message(pinned.date()))
    _assert_direct_slot_offer(resp, conv)


def test_same_day_typed_today_advances_directly_to_slot_offer(db, fakes, monkeypatch):
    # PACKAGE B: typed "today" — straight to the exact-slot offer.
    client = _gated_client(db)
    pinned = _pin_both_clocks(monkeypatch, client)
    conv = _at_date_stage(db, client)
    _publish_slot_on(db, client, pinned.date(), hour=15)
    resp = send(db, client, conv, "today")
    _assert_direct_slot_offer(resp, conv)


# ===========================================================================
# FULL FLOW — real entry -> intake boundaries -> date signal -> valid date ->
# time_preference -> Morning -> server-owned slot calendar_actions (audit #1/#5/#7)
# ===========================================================================

def _publish_future_open_slot(db, client, monkeypatch, hour=10):
    """Pin the clock and publish one AVAILABLE slot on a future weekday.
    make_slot uses the real clock, so pinning 'now' to real today's date keeps
    the offered date aligned with the published slot on any run day."""
    real_today = datetime.now(NY).date()
    target = real_today + timedelta(days=2)
    while target.weekday() >= 5:            # a weekday (valid even for 5-day offices)
        target += timedelta(days=1)
    days_ahead = (target - real_today).days
    make_slot(db, client, days_ahead=days_ahead, hour=hour,
              status=SlotStatus.AVAILABLE)
    pinned = datetime(real_today.year, real_today.month, real_today.day, 9, 0,
                      tzinfo=NY)
    monkeypatch.setattr(chat_module, "get_client_now", lambda c: pinned)
    return pinned, target


def _run_full_flow(db, client, monkeypatch, *, entry, expect_key=None,
                   other_detail=None, short_symptom=False):
    """Prove ONE complete REAL-ENTRY chain end to end:

        real entry payload
          -> reason persistence + provenance (Layer 2/3 when a specific key is
             expected; source provenance for the Other detail otherwise)
          -> name / phone / email boundaries as applicable
          -> {"stage": "date", "submit": "message"}
          -> valid date
          -> (PACKAGE B) server-owned slot calendar_actions on the SAME turn
             (action.type == "calendar_choice"; patient-facing label == message;
              opaque choice_id never shown as a label or in the reply).
    """
    _, target = _publish_future_open_slot(db, client, monkeypatch)
    conv = _fresh(db, client)

    if other_detail is not None:
        send(db, client, conv, entry)          # "Other"
        send(db, client, conv, other_detail)   # dental detail
        assert _norm(conv.lead_reason_source_text) == _norm(other_detail)
        assert (conv.lead_reason or "").strip(), "Other must persist a reason"
    else:
        send(db, client, conv, entry)          # real service payload
        if expect_key is not None:
            _assert_persisted_and_provenance(conv, expect_key, entry)

    # Package A: New/Returning is asked first, right after the authoritative
    # reason (for symptom openers that first turn also carries the urgent-care
    # safety guidance). Answer it before name / phone / email when genuinely asked.
    db.refresh(conv)
    if _flow_just_asked_patient_type(db, conv):
        send(db, client, conv, "new patient")

    # name / phone / email boundaries -> date signal
    r_name = send(db, client, conv, "Jordan Rivera")
    assert (r_name.meta or {}).get("calendar_picker") != DATE_SIGNAL
    resp = send(db, client, conv, "516-555-0100")
    if (resp.meta or {}).get("calendar_picker") != DATE_SIGNAL:
        resp = send(db, client, conv, "skip email")
    assert resp.meta.get("calendar_picker") == DATE_SIGNAL
    if short_symptom:
        assert (resp.reply or "").endswith(
            CALENDAR_INTAKE_DATE_ONLY_SHORT_SYMPTOM_PROMPT_TAIL)

    # PACKAGE B: valid date -> the engine's exact-slot offer on the SAME
    # turn. The morning/afternoon step is removed for Calendar tenants;
    # the date turn IS the slot turn, carrying the slot_selection signal.
    r_slots = send(db, client, conv, _date_message(target))
    assert r_slots.meta.get("calendar_picker") == SLOT_SIGNAL
    assert (r_slots.reply or "") not in (INTAKE_TIME_PREFERENCE_PROMPT,
                                         INTAKE_TIME_PREFERENCE_TODAY_PROMPT)
    assert conv.booking_state == BookingState.WAITING_FOR_SLOT_SELECTION
    actions = r_slots.meta.get("calendar_actions")
    assert actions, "server-owned slot buttons expected"
    reply_text = r_slots.reply or ""
    for e in actions:
        assert e["action"]["type"] == "calendar_choice"
        label = e["label"]
        assert label and e.get("message") == label, "label/message patient-facing"
        cid = e["action"]["choice_id"]
        assert cid and cid not in label and cid not in reply_text, (
            "opaque choice_id must not be shown as a label or in the reply")
    assert r_slots.meta.get("calendar_picker") != DATE_SIGNAL
    return conv, r_slots


# A. standard service
def test_full_flow_A_standard_service(db, fakes, monkeypatch):
    client = _gated_client(db)
    _run_full_flow(db, client, monkeypatch,
                   entry=widget_message_for("cleaning_checkup"),
                   expect_key="cleaning_checkup")


# B. short-symptom / ordinary urgent service
def test_full_flow_B_short_symptom_service(db, fakes, monkeypatch):
    client = _gated_client(db)
    _run_full_flow(db, client, monkeypatch,
                   entry=widget_message_for("tooth_pain"),
                   expect_key="tooth_pain", short_symptom=True)


# C. validated dental Other
def test_full_flow_C_validated_other(db, fakes, monkeypatch):
    client = _gated_client(db)
    _run_full_flow(db, client, monkeypatch, entry="Other",
                   other_detail="I chipped a molar")   # classifier-accepted dental detail


# D. hybrid-mode service (no external booking_url -> internal capture path)
def test_full_flow_D_hybrid_mode(db, fakes, monkeypatch):
    client = _gated_client(db, booking_mode="hybrid")
    _run_full_flow(db, client, monkeypatch,
                   entry=widget_message_for("crowns"), expect_key="crowns")


# E. custom / future accepted reason — CONTRACT B (genuinely custom)
def test_full_flow_E_custom_contract_b(db, fakes, monkeypatch):
    # CONTRACT B: the custom phrase must NOT resolve to any existing registry
    # service via the authoritative matcher; it is accepted only through the
    # dental Other-acceptance owner and then scheduled by the centralized state
    # rule. This proves Contract B, NOT a live Supabase button source (A).
    custom = "bleeding when I floss"
    assert msl.find_matching_service(custom, None) is None, (
        "Contract B requires a phrase the master matcher does not recognize")
    client = _gated_client(db)
    _run_full_flow(db, client, monkeypatch, entry="Other", other_detail=custom)


# F. actual configured service-button entry (drives the real builder output)
def test_full_flow_F_configured_button(db, fakes, monkeypatch):
    client = _gated_client(db)
    button = next(b for b in build_widget_service_buttons(client)
                  if b["key"] != "other")
    _run_full_flow(db, client, monkeypatch, entry=button["message"],
                   expect_key=button["key"])


# ===========================================================================
# PREDICATE CONTRACTS — pure, mutually exclusive, side-effect-free
# ===========================================================================

def test_predicates_are_mutually_exclusive_and_pure(db, fakes, monkeypatch):
    client = _gated_client(db)
    std = _at_date_stage(db, client)
    assert capture_first_time_window_pending(std, client) is True
    assert capture_first_short_symptom_time_window_pending(std, client) is False

    sy = make_conversation(db, client, lead_reason="tooth pain",
                           lead_reason_source_text="I have tooth pain",
                           lead_time_window=None, lead_is_new_patient=None)
    assert capture_first_short_symptom_time_window_pending(sy, client) is True
    assert capture_first_time_window_pending(sy, client) is False

    def boom(*a, **k):
        raise AssertionError("pure predicate invoked an active/consuming owner")
    for name in ("receptionist_bypass_reply", "classify_other_reason_detail",
                 "map_reason_detail_to_enum", "detect_appointment_reason",
                 "extract_lead_fields_with_ai"):
        if hasattr(chat_module, name):
            monkeypatch.setattr(chat_module, name, boom)
    assert capture_first_short_symptom_time_window_pending(sy, client) is True
    assert capture_first_time_window_pending(std, client) is True


def test_direct_helper_matrix_kinds_and_gate(db, fakes):
    client = _gated_client(db)
    # UX-C: the matcher recognizes the live Calendar date-only prompts.
    std_prompt = "Great-thanks Kevin. " + CALENDAR_INTAKE_DATE_ONLY_PROMPT_TAIL
    short_prompt = "Thanks Kevin. " + CALENDAR_INTAKE_DATE_ONLY_SHORT_SYMPTOM_PROMPT_TAIL
    assert intake_date_stage_signal(client, std_prompt, True, "standard") == DATE_SIGNAL
    assert intake_date_stage_signal(client, short_prompt, True, "short_symptom") == DATE_SIGNAL
    # Cross-kind must never match.
    assert intake_date_stage_signal(client, short_prompt, True, "standard") is None
    assert intake_date_stage_signal(client, std_prompt, True, "short_symptom") is None
    # Non-genuine entry gate.
    for bad in (False, None, 1, "true", [], {}):
        assert intake_date_stage_signal(client, std_prompt, bad, "standard") is None
    # Closed-vocabulary kind (Rule 4).
    for kind in (None, "", "STANDARD", "date", "urgent"):
        assert intake_date_stage_signal(client, std_prompt, True, kind) is None


# ===========================================================================
# EXCLUSION MATRIX — no picker/time signal for non-scheduling paths (audit #7)
# ===========================================================================

@pytest.mark.parametrize("key", INFO_SERVICE_KEYS)
def test_informational_services_do_not_schedule(db, fakes, key):
    client = _gated_client(db)
    conv = _fresh(db, client)
    resp = send(db, client, conv, msl.MASTER_DENTAL_SERVICES[key].display_name)
    assert (resp.meta or {}).get("calendar_picker") != DATE_SIGNAL, key
    assert (resp.meta or {}).get("calendar_picker") != TIME_SIGNAL, key


@pytest.mark.parametrize("text", [
    "do you take my insurance?",
    "what are your hours?",
    "where are you located?",
    "how much is a cleaning?",
])
def test_informational_questions_emit_no_signal(db, fakes, text):
    client = _gated_client(db)
    conv = _fresh(db, client)
    resp = send(db, client, conv, text)
    assert (resp.meta or {}).get("calendar_picker") != DATE_SIGNAL
    assert (resp.meta or {}).get("calendar_picker") != TIME_SIGNAL


def test_non_dental_other_emits_no_signal(db, fakes):
    client = _gated_client(db)
    conv = _fresh(db, client)
    send(db, client, conv, "Other")
    resp = send(db, client, conv, "I want to buy a car")
    assert (resp.meta or {}).get("calendar_picker") != DATE_SIGNAL
    assert (resp.meta or {}).get("calendar_picker") != TIME_SIGNAL


def test_unclear_other_emits_no_signal(db, fakes):
    client = _gated_client(db)
    conv = _fresh(db, client)
    send(db, client, conv, "Other")
    resp = send(db, client, conv, "not a root canal")
    assert (resp.meta or {}).get("calendar_picker") != DATE_SIGNAL


def test_life_threatening_emits_no_signal(db, fakes):
    client = _gated_client(db)
    conv = _fresh(db, client)
    resp = send(db, client, conv, "I can't breathe and my face is swelling badly")
    assert "calendar_picker" not in (resp.meta or {})


def test_locked_conversation_emits_no_signal(db, fakes):
    client = _gated_client(db)
    locked_until = datetime.now(NY) + timedelta(days=1)
    conv = _at_date_stage(db, client)
    conv.abuse_locked_until = locked_until
    db.add(conv)
    db.commit()
    assert conversation_is_locked(conv) is True
    resp = send(db, client, conv, _date_message(
        chat_module.get_client_now(client).date() + timedelta(days=2)))
    assert (resp.meta or {}).get("calendar_picker") != TIME_SIGNAL
    assert (resp.meta or {}).get("calendar_picker") != DATE_SIGNAL


def test_final_closed_conversation_emits_no_signal(db, fakes):
    client = _gated_client(db)
    conv = _at_date_stage(db, client)
    conv.final_closed = True
    db.add(conv)
    db.commit()
    resp = send(db, client, conv, _date_message(
        chat_module.get_client_now(client).date() + timedelta(days=2)))
    assert (resp.meta or {}).get("calendar_picker") != TIME_SIGNAL
    assert (resp.meta or {}).get("calendar_picker") != DATE_SIGNAL


def test_impossible_date_emits_no_time_signal_no_mutation(db, fakes):
    client = _gated_client(db)
    conv = _at_date_stage(db, client)
    before = conv.lead_time_window
    resp = send(db, client, conv, "February 30")
    assert resp.reply != INTAKE_TIME_PREFERENCE_PROMPT
    assert resp.reply != INTAKE_TIME_PREFERENCE_TODAY_PROMPT
    assert (resp.meta or {}).get("calendar_picker") != TIME_SIGNAL
    assert conv.lead_time_window == before


def test_past_date_emits_no_time_signal_no_mutation(db, fakes, monkeypatch):
    client = _gated_client(db)
    pinned = _pin_open_weekday(monkeypatch, client)
    conv = _at_date_stage(db, client)
    before = conv.lead_time_window
    past = pinned.date() - timedelta(days=3)
    resp = send(db, client, conv, _date_message(past))
    assert resp.reply != INTAKE_TIME_PREFERENCE_PROMPT
    assert (resp.meta or {}).get("calendar_picker") != TIME_SIGNAL
    assert conv.lead_time_window == before


@pytest.mark.parametrize("closed_dow,dow_key", [(5, "sat"), (6, "sun")])
def test_closed_weekend_date_emits_no_time_signal(db, fakes, closed_dow, dow_key):
    hours = {d: {"open": True, "start": "09:00", "end": "17:00"}
             for d in ["mon", "tue", "wed", "thu", "fri"]}
    hours["sat"] = {"open": False, "start": "09:00", "end": "17:00"}
    hours["sun"] = {"open": False, "start": "09:00", "end": "17:00"}
    client = _gated_client(db, office_hours=hours)
    conv = _at_date_stage(db, client)
    d = chat_module.get_client_now(client).date() + timedelta(days=1)
    while d.weekday() != closed_dow:
        d += timedelta(days=1)
    resp = send(db, client, conv, _date_message(d))
    assert resp.reply != INTAKE_TIME_PREFERENCE_PROMPT
    assert (resp.meta or {}).get("calendar_picker") != TIME_SIGNAL


# ===========================================================================
# NIGHT-GUARD APPOINTMENT COLLISION -- dedicated route-level regression tests.
# The service phrase "night guard / teeth grinding" contains the token "night",
# which canonicalizes to an evening time window. These freeze that a recognized
# Night Guard selection OWNS the reason-capture turn -- decided by the existing
# authoritative reason-ownership owner (conversation_has_specific_lead_reason:
# a missing OR bare-generic reason is still claimable) -- and never leaks an
# evening lead_time_window, including the Book Appointment -> Night Guard path
# where the stored reason is already the generic fallback. Bare time answers and
# genuine service+time messages are unaffected. (Night Guard Collision Fix v1.1.)
# ===========================================================================

NIGHT_GUARD_KEY = "night_guard"
# Real registry aliases the authoritative matcher resolves to night_guard
# (Layer-1 asserted); each carries "night"/"grind"/"clench" wording that must
# NOT be consumed as a time preference.
NIGHT_GUARD_ALIASES = ["night guard", "teeth grinding", "grinding teeth",
                       "bruxism", "mouth guard", "clenching"]


def _assert_no_time_window_leak(conv):
    tw = (getattr(conv, "lead_time_window", None) or "").strip()
    assert tw == "", "night-guard turn leaked a time window: %r" % tw


def test_night_guard_button_reason_no_time_leak(db, fakes):
    # #1 Real configured Night Guard button: resolves to night_guard, persists
    # the mapped appointment reason, and does NOT set lead_time_window merely
    # because "night" appears in the service phrase.
    client = _gated_client(db)
    payload = widget_message_for(NIGHT_GUARD_KEY)
    assert payload == "I need night guard / teeth grinding"
    _assert_entry_identity(payload, NIGHT_GUARD_KEY)                 # LAYER 1
    conv = _fresh(db, client)
    send(db, client, conv, payload)
    _assert_persisted_and_provenance(conv, NIGHT_GUARD_KEY, payload)  # LAYER 2+3
    _assert_no_time_window_leak(conv)


def test_night_guard_after_book_appointment_owns_reason(db, fakes):
    # #2 Book Appointment -> Night Guard. Book Appointment stores the generic
    # "appointment request"; the Night Guard turn must still OWN the reason turn
    # despite the pre-existing generic reason, leak no evening window, not be
    # consumed as a time-window turn, and reach the date signal.
    client = _gated_client(db)
    conv = _fresh(db, client)
    send(db, client, conv, "Book Appointment")
    assert (conv.lead_reason or "").strip() == GENERIC_FALLBACK_REASON, (
        "Book Appointment should store the generic reason")
    payload = widget_message_for(NIGHT_GUARD_KEY)
    _assert_entry_identity(payload, NIGHT_GUARD_KEY)
    r_ng = send(db, client, conv, payload)
    assert (conv.lead_reason or "").strip() == expected_reason_for(NIGHT_GUARD_KEY)
    _assert_no_time_window_leak(conv)
    assert (r_ng.meta or {}).get("mode") != "intake_time_window_capture", (
        "Night Guard was consumed as a time-window turn: %r" % (r_ng.meta or {}))
    resp = _complete_intake_to_date(db, client, conv)
    _assert_date_stage(conv, client, resp, NIGHT_GUARD_KEY)


@pytest.mark.parametrize("alias", NIGHT_GUARD_ALIASES)
def test_night_guard_alias_reason_no_time_leak(db, fakes, alias):
    # #3 Accepted Night Guard aliases/free text resolve through the authoritative
    # matcher, persist the mapped reason, and do not leak an evening preference.
    client = _gated_client(db)
    _assert_entry_identity(alias, NIGHT_GUARD_KEY)                  # LAYER 1
    conv = _fresh(db, client)
    send(db, client, conv, alias)
    _assert_persisted_and_provenance(conv, NIGHT_GUARD_KEY, alias)   # LAYER 2+3
    _assert_no_time_window_leak(conv)


def test_night_guard_full_flow_to_slot_actions(db, fakes, monkeypatch):
    # #4 Full Night Guard chain: reason -> name -> phone -> optional email ->
    # date signal -> valid date -> time_preference -> new/returning boundary ->
    # server-owned calendar_choice slot actions (the same visual calendar path
    # as every other service).
    client = _gated_client(db)
    conv, _ = _run_full_flow(
        db, client, monkeypatch,
        entry=widget_message_for(NIGHT_GUARD_KEY), expect_key=NIGHT_GUARD_KEY)
    assert conv.booking_state == BookingState.WAITING_FOR_SLOT_SELECTION


def test_night_guard_bare_evening_still_captures(db, fakes, monkeypatch):
    # #5 A bare "evening" is still consumable as a genuine time answer
    # (proves the fix suppresses only service-selection turns, not time
    # answers). PACKAGE B: a stored DATE now hands the conversation to the
    # booking engine on the same turn, so the bare time answer is pinned at
    # the intake time-window stage itself — the only place route-level
    # window capture still runs for a Calendar tenant.
    client = _gated_client(db)
    _publish_future_open_slot(db, client, monkeypatch)
    conv = _fresh(db, client)
    send(db, client, conv, "dental cleaning")          # specific reason first
    _complete_intake_to_date(db, client, conv)
    send(db, client, conv, "evening")
    db.refresh(conv)
    assert "evening" in (conv.lead_time_window or "").lower(), (
        "bare evening was not captured: %r" % conv.lead_time_window)


def test_night_guard_i_prefer_evening_still_captures(db, fakes, monkeypatch):
    # #6 "I prefer evening" still captures an evening window (same PACKAGE B
    # placement rationale as #5).
    client = _gated_client(db)
    _publish_future_open_slot(db, client, monkeypatch)
    conv = _fresh(db, client)
    send(db, client, conv, "dental cleaning")
    _complete_intake_to_date(db, client, conv)
    send(db, client, conv, "I prefer evening")
    db.refresh(conv)
    assert "evening" in (conv.lead_time_window or "").lower(), (
        "'I prefer evening' was not captured: %r" % conv.lead_time_window)


def test_night_guard_service_plus_evening_keeps_reason(db, fakes):
    # #7 A recognized service PLUS a genuine evening preference never loses the
    # service reason; the time preference is handled through the existing
    # authoritative order (re-asked), not silently discarded on the reason turn.
    client = _gated_client(db)
    conv = _fresh(db, client)
    send(db, client, conv, "I need a cleaning in the evening")
    db.refresh(conv)
    assert conv.lead_reason == expected_reason_for("cleaning_checkup"), (
        "service reason lost on service+time message: %r" % conv.lead_reason)
    resp = _complete_intake_to_date(db, client, conv)
    _assert_date_stage(conv, client, resp, "cleaning_checkup")



# ===========================================================================
# CLOSED-DAY SERVER REVALIDATION -- all seven weekdays, ONE owner.
#
# Every patient date submission is revalidated against the tenant's
# authoritative office-hours owner (is_day_open) BEFORE entering time
# preference. A day whose weekday is configured closed is rejected without
# mutating lead_time_window, emits no time-preference signal, and remains at
# date selection; a later configured-OPEN date then advances normally. There is
# no hard-coded Mon-Fri assumption and no Sat/Sun universally-closed assumption:
# an OPEN weekend advances and a CLOSED weekday is rejected by the SAME rule.
#
# _date_message() is the EXACT month-name shape used by BOTH the typed-date
# path AND the Next-7-Days strip / full-calendar message-mode submissions (see
# its docstring), so every closed-date case below simultaneously covers the
# message-mode server revalidation (audit #13 Next-7-Days, #14 full-calendar):
# the same message text traverses the same capture owner regardless of which
# picker control produced it.
# ===========================================================================

_ALL_DOW_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _hours_closing(*closed_keys):
    """Configured office hours (non-empty struct) with the named day keys
    closed and every other day open 09:00-17:00. Non-empty is what engages the
    closure rule -- an office with NO hours struct advertises no closures."""
    return {d: {"open": d not in closed_keys, "start": "09:00", "end": "17:00"}
            for d in _ALL_DOW_KEYS}


def _next_date_with_weekday(client, weekday, min_ahead=1):
    """The next calendar date whose weekday() == weekday, at least min_ahead
    days out, so a chosen weekday never collides with the same-day edge."""
    d = chat_module.get_client_now(client).date() + timedelta(days=min_ahead)
    while d.weekday() != weekday:
        d += timedelta(days=1)
    return d


_CLOSED_WEEKDAY_CASES = [(0, "mon"), (1, "tue"), (2, "wed"), (3, "thu"), (4, "fri")]


@pytest.mark.parametrize("weekday,day_key", _CLOSED_WEEKDAY_CASES)
def test_closed_weekday_date_emits_no_time_signal(db, fakes, weekday, day_key):
    # audit #1-#5, #15, #18: EACH Mon-Fri closed in turn. That weekday's date
    # (standard capture-first) must NOT advance, must NOT mutate
    # lead_time_window, must emit no time signal, and must return the truthful
    # closed-day correction -- not the time-preference prompt.
    client = _gated_client(db, office_hours=_hours_closing(day_key))
    conv = _at_date_stage(db, client)
    before = conv.lead_time_window
    resp = send(db, client, conv, _date_message(_next_date_with_weekday(client, weekday)))
    db.refresh(conv)
    assert resp.reply != INTAKE_TIME_PREFERENCE_PROMPT
    assert resp.reply != INTAKE_TIME_PREFERENCE_TODAY_PROMPT
    assert (resp.meta or {}).get("calendar_picker") != TIME_SIGNAL
    assert conv.lead_time_window == before                 # no mutation
    assert "closed" in (resp.reply or "").lower()          # truthful correction


@pytest.mark.parametrize("weekday,day_key", [(5, "sat"), (6, "sun")])
def test_closed_weekend_date_rejected_by_same_owner(db, fakes, weekday, day_key):
    # audit #6, #7: closed-weekend rejection is preserved and now flows through
    # the SAME is_day_open owner as the weekday rule (no mutation, no signal).
    client = _gated_client(db, office_hours=_hours_closing("sat", "sun"))
    conv = _at_date_stage(db, client)
    before = conv.lead_time_window
    resp = send(db, client, conv, _date_message(_next_date_with_weekday(client, weekday)))
    db.refresh(conv)
    assert resp.reply != INTAKE_TIME_PREFERENCE_PROMPT
    assert (resp.meta or {}).get("calendar_picker") != TIME_SIGNAL
    assert conv.lead_time_window == before


@pytest.mark.parametrize("weekday,day_key", [(5, "sat"), (6, "sun")])
def test_open_weekend_date_advances(db, fakes, weekday, day_key):
    # audit #8, #9 (PACKAGE B): a tenant that OPENS a weekend day advances
    # that weekend date straight to the exact-slot offer, exactly like a
    # weekday. Weekend availability is tenant-owned, never assumed closed.
    client = _gated_client(db, office_hours=_hours_closing())   # all seven open
    conv = _at_date_stage(db, client)
    d = _next_date_with_weekday(client, weekday)
    _publish_slot_on(db, client, d)
    resp = send(db, client, conv, _date_message(d))
    _assert_direct_slot_offer(resp, conv)


def test_open_weekday_date_advances(db, fakes):
    # audit #10 (PACKAGE B): control -- an OPEN weekday still advances,
    # straight to the exact-slot offer, when a DIFFERENT weekday is closed.
    client = _gated_client(db, office_hours=_hours_closing("wed"))
    conv = _at_date_stage(db, client)
    d = _next_date_with_weekday(client, 3)                      # Thu open
    _publish_slot_on(db, client, d)
    resp = send(db, client, conv, _date_message(d))
    _assert_direct_slot_offer(resp, conv)


def test_today_closed_typed_is_rejected(db, fakes, monkeypatch):
    # audit #11: "today" is rejected when today's configured weekday is closed.
    # Pin now to a Wednesday, then close Wednesday.
    client = _gated_client(db, office_hours=_hours_closing("wed"))
    base = chat_module.get_client_now(client).date()
    while base.weekday() != 2:
        base += timedelta(days=1)
    pinned = datetime(base.year, base.month, base.day, 10, 0, tzinfo=NY)
    monkeypatch.setattr(chat_module, "get_client_now", lambda c: pinned)
    conv = _at_date_stage(db, client)
    before = conv.lead_time_window
    resp = send(db, client, conv, "today")
    db.refresh(conv)
    assert resp.reply != INTAKE_TIME_PREFERENCE_PROMPT
    assert resp.reply != INTAKE_TIME_PREFERENCE_TODAY_PROMPT
    assert (resp.meta or {}).get("calendar_picker") != TIME_SIGNAL
    assert conv.lead_time_window == before


def test_today_open_typed_advances(db, fakes, monkeypatch):
    # audit #12 (PACKAGE B): "today" advances straight to the exact-slot
    # offer when today's configured weekday is genuinely open.
    client = _gated_client(db, office_hours=_hours_closing())   # all seven open
    pinned = _pin_both_clocks(monkeypatch, client)              # Mon-Fri, open
    conv = _at_date_stage(db, client)
    _publish_slot_on(db, client, pinned.date(), hour=15)
    resp = send(db, client, conv, "today")
    _assert_direct_slot_offer(resp, conv)


def test_short_symptom_closed_date_no_mutation(db, fakes):
    # audit #16: the short-symptom flow reaches date selection through the SAME
    # capture owner, so a closed date there is rejected identically -- no
    # mutation, no time signal.
    client = _gated_client(db, office_hours=_hours_closing("wed"))
    conv = _fresh(db, client)
    send(db, client, conv, widget_message_for("tooth_pain"))   # short-symptom reason
    resp = _complete_intake_to_date(db, client, conv)
    assert (resp.meta or {}).get("calendar_picker") == DATE_SIGNAL
    before = conv.lead_time_window
    r = send(db, client, conv, _date_message(_next_date_with_weekday(client, 2)))
    db.refresh(conv)
    assert r.reply != INTAKE_TIME_PREFERENCE_PROMPT
    assert (r.meta or {}).get("calendar_picker") != TIME_SIGNAL
    assert conv.lead_time_window == before


def test_hybrid_closed_date_no_mutation(db, fakes):
    # audit #17: the supported hybrid flow (internal capture-first date path,
    # no external booking_url) behaves identically for a closed date.
    client = _gated_client(db, booking_mode="hybrid", office_hours=_hours_closing("wed"))
    conv = _fresh(db, client)
    send(db, client, conv, widget_message_for("cleaning_checkup"))
    resp = _complete_intake_to_date(db, client, conv)
    assert (resp.meta or {}).get("calendar_picker") == DATE_SIGNAL
    before = conv.lead_time_window
    r = send(db, client, conv, _date_message(_next_date_with_weekday(client, 2)))
    db.refresh(conv)
    assert r.reply != INTAKE_TIME_PREFERENCE_PROMPT
    assert (r.meta or {}).get("calendar_picker") != TIME_SIGNAL
    assert conv.lead_time_window == before


def test_closed_date_then_open_date_advances(db, fakes):
    # audit #19: after a rejected CLOSED date, a later VALID OPEN date advances
    # normally through the same conversation (recovery is not blocked).
    client = _gated_client(db, office_hours=_hours_closing("wed"))
    conv = _at_date_stage(db, client)
    before = conv.lead_time_window
    r_bad = send(db, client, conv, _date_message(_next_date_with_weekday(client, 2)))  # Wed closed
    db.refresh(conv)
    assert (r_bad.meta or {}).get("calendar_picker") != TIME_SIGNAL
    assert conv.lead_time_window == before
    d_good = _next_date_with_weekday(client, 3)                 # Thu open
    _publish_slot_on(db, client, d_good)
    r_good = send(db, client, conv, _date_message(d_good))
    _assert_direct_slot_offer(r_good, conv)                     # PACKAGE B


# ===========================================================================
# NO-HOURS / EXACT-TIME CONTRACT (consolidated-audit blocker).
#
# The closed-day rule must not broaden unconfigured-tenant scheduling. BOTH
# pre-existing behaviors of an office with NO configured office_hours struct
# are frozen here alongside the new day-only rule, at the single validation
# owner (build_time_window_issue_reply) and through the real route:
#
#   no hours + DAY-ONLY date  -> advances (the day-only fallback: an
#                                unconfigured office advertises no closures);
#   no hours + EXACT day/time -> rejected as closed (pre-patch semantics: the
#                                empty struct yields is_day_open False, exactly
#                                as the empty row yielded is_open False before);
#   configured open  + exact  -> accepted;
#   configured closed + exact -> rejected;
#   configured closed + day-only -> rejected (the correction this patch adds).
# ===========================================================================


def _no_hours_client(db):
    """A booking-gated office with NO office_hours struct configured."""
    return _gated_client(db, office_hours=None)


def test_no_hours_day_only_date_advances(db, fakes):
    # no configured hours + DAY-ONLY date: fallback preserved end to end
    # through the real route -- PACKAGE B: the date advances straight to the
    # exact-slot offer (an unconfigured office advertises no closures).
    client = _no_hours_client(db)
    conv = _at_date_stage(db, client)
    d = _next_date_with_weekday(client, 2)
    _publish_slot_on(db, client, d)
    resp = send(db, client, conv, _date_message(d))
    _assert_direct_slot_offer(resp, conv)


def test_no_hours_exact_time_still_rejected(db, fakes):
    # no configured hours + EXACT day/time: the pre-patch rejection is
    # preserved at the single validation owner -- never silently accepted.
    client = _no_hours_client(db)
    reply = chat_module.build_time_window_issue_reply(client, "Wed 2pm")
    assert reply is not None and "closed" in reply.lower()


def test_configured_open_exact_time_accepted(db, fakes):
    # configured OPEN weekday + exact in-hours time: no issue reported.
    client = _gated_client(db, office_hours=_hours_closing())   # all seven open
    assert chat_module.build_time_window_issue_reply(client, "Wed 2pm") is None


def test_configured_closed_exact_time_rejected(db, fakes):
    # configured CLOSED weekday + exact time: rejected as closed.
    client = _gated_client(db, office_hours=_hours_closing("wed"))
    reply = chat_module.build_time_window_issue_reply(client, "Wed 2pm")
    assert reply is not None and "closed" in reply.lower()


def test_configured_closed_day_only_rejected_at_validator(db, fakes):
    # configured CLOSED weekday + DAY-ONLY value: rejected at the same single
    # owner (the route-level closed-day matrix above proves the full path).
    client = _gated_client(db, office_hours=_hours_closing("wed"))
    reply = chat_module.build_time_window_issue_reply(client, "Wed")
    assert reply is not None and "closed" in reply.lower()


def test_rejected_date_then_valid_date_advances(db, fakes, monkeypatch):
    """A rejected date leaves lead_time_window unchanged and emits no time
    signal; a subsequent VALID date then advances normally (audit #7)."""
    client = _gated_client(db)
    pinned = _pin_open_weekday(monkeypatch, client)
    conv = _at_date_stage(db, client)
    before = conv.lead_time_window

    r_bad = send(db, client, conv, _date_message(pinned.date() - timedelta(days=3)))
    assert (r_bad.meta or {}).get("calendar_picker") != TIME_SIGNAL
    assert conv.lead_time_window == before   # no mutation on rejection

    good = pinned.date() + timedelta(days=1)
    while good.weekday() >= 5:
        good += timedelta(days=1)
    _publish_slot_on(db, client, good)
    r_good = send(db, client, conv, _date_message(good))
    _assert_direct_slot_offer(r_good, conv)                     # PACKAGE B


def test_safety_blocked_conversation_emits_no_signal(db, fakes):
    """A conversation the SAFETY owner has blocked (a real life-threatening turn
    -> persisted final_closed) must not schedule: a later scheduling message
    emits no date/time signal and mutates no scheduling field (audit #4)."""
    client = _gated_client(db)
    conv = _at_date_stage(db, client)
    before = conv.lead_time_window
    # Real safety turn locks the conversation.
    send(db, client, conv, "I can't breathe and my face is swelling badly")
    db.refresh(conv)
    assert conv.final_closed is True
    # A later scheduling attempt stays blocked.
    resp = send(db, client, conv, "today")
    assert (resp.meta or {}).get("mode") == "final_closed"
    assert (resp.meta or {}).get("calendar_picker") != DATE_SIGNAL
    assert (resp.meta or {}).get("calendar_picker") != TIME_SIGNAL
    db.refresh(conv)
    assert conv.lead_time_window == before


def test_unavailable_cross_tenant_conversation_emits_no_signal(db, fakes, monkeypatch):
    """Tenant isolation (Rule 15): office A cannot drive office B's conversation.
    Sending B's conversation id under A's key resolves to no B-conversation, so
    it emits no date/time signal and leaves B's scheduling field untouched
    (audit #4)."""
    client_a = _gated_client(db)
    client_b = _gated_client(db)
    pinned = _pin_open_weekday(monkeypatch, client_b)
    conv_b = _at_date_stage(db, client_b)    # B is genuinely at the date stage
    before_b = conv_b.lead_time_window
    d = pinned.date() + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    # Drive B's conversation id with A's client key + an otherwise-valid date.
    resp = send(db, client_a, conv_b, _date_message(d))
    assert (resp.meta or {}).get("calendar_picker") != DATE_SIGNAL
    assert (resp.meta or {}).get("calendar_picker") != TIME_SIGNAL
    assert resp.conversation_id != str(conv_b.id)   # A never reached B's row
    db.refresh(conv_b)
    assert conv_b.lead_time_window == before_b       # B untouched by A
