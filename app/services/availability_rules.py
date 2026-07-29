# app/services/availability_rules.py
#
# OWNER OF: the PURE availability business rules (no database, no SQLAlchemy,
# no client object). Split from availability_service so the exact rules are
# unit-testable on any machine (see calendar_tests/test_availability_rules.py)
# and so the DB wrapper stays a thin fetch-then-filter layer.

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional, Sequence
from zoneinfo import ZoneInfo

from app.services.appointment_intent import slot_matches_preference
from app.services.calendar_settings_service import CalendarSettings, ensure_utc

# Slot status strings are duplicated as literals here ON PURPOSE? No — they
# are imported nowhere else pure. To keep this module import-light while
# preserving one source of truth, the names below are defined in
# calendar_models.SlotStatus; a regression test asserts they stay in sync.
STATUS_AVAILABLE = "available"
STATUS_HELD = "held"

# Policy reason codes (Patch 2C). Deterministic, machine-readable, and the
# COMPLETE vocabulary — evaluate_slot_policy returns nothing else.
POLICY_OK = "ok"
POLICY_TOO_SOON = "too_soon"
POLICY_BEYOND_HORIZON = "beyond_horizon"
POLICY_PREFERENCE_MISMATCH = "preference_mismatch"
POLICY_SERVICE_MISMATCH = "service_mismatch"


@dataclass(frozen=True)
class SlotPolicyResult:
    """Outcome of the pure booking-policy evaluation — no boolean guessing."""
    eligible: bool
    reason: str  # POLICY_OK / POLICY_TOO_SOON / POLICY_BEYOND_HORIZON /
                 # POLICY_PREFERENCE_MISMATCH / POLICY_SERVICE_MISMATCH


def evaluate_slot_policy(
    slot,
    *,
    now_utc: datetime,
    settings: CalendarSettings,
    time_preference: str,
    service_key: Optional[str],
) -> SlotPolicyResult:
    """
    Purpose: THE single pure owner of the booking-POLICY rules (Patch 2C —
             Senior Audit Critical #8). Display filtering, hold creation,
             and final booking all call THIS function, so a slot that was
             eligible when displayed is re-judged by the same rule text at
             every later step. There are no other copies of these rules.
    Inputs (all explicit — this function never reads the clock or the DB):
        slot:            object with start_datetime and (optionally)
                         service_key attributes.
        now_utc:         current aware-UTC time, injected by the caller.
        settings:        the client's CalendarSettings (request-level
                         snapshot loaded at the start of the current patient
                         message — see booking_conversation).
        time_preference: the preference bucket to enforce — for
                         revalidation, the EFFECTIVE preference the offer
                         was filtered with.
        service_key:     the same service value display filtering uses
                         (conversation.lead_reason or None).
    Returns: SlotPolicyResult(eligible, reason) — reason is exactly one of
             the POLICY_* constants above.
    Database effects: none (pure).
    Possible failures: none — inputs are normalized via ensure_utc; the
        rules themselves cannot raise on well-formed slots.

    The rules and their semantics are UNCHANGED from Patch 2B; only their
    ownership moved here so they can be re-applied under the slot row lock:
      too_soon:            slot starts before now + minimum_notice_minutes —
                           an EXACT elapsed-time rule (never calendar-based);
                           this also rejects every past slot.
      beyond_horizon:      slot's LOCAL calendar date is after today_local +
                           max_booking_days (the Patch 2B local-date rule
                           matching the booking conversation's contract).
      preference_mismatch: local start hour outside the requested bucket.
      service_mismatch:    slot reserved for a different service (same
                           equality rule display filtering has always used).
    """
    tz = ZoneInfo(settings.timezone_name)
    # Aware-UTC normalization first; every derived value below comes only
    # from these normalized values (Patch 2B discipline).
    normalized_now = ensure_utc(now_utc)
    slot_start = ensure_utc(slot.start_datetime)

    min_start = normalized_now + timedelta(minutes=settings.minimum_notice_minutes)
    if slot_start < min_start:
        return SlotPolicyResult(False, POLICY_TOO_SOON)

    local_start = slot_start.astimezone(tz)
    today_local = normalized_now.astimezone(tz).date()
    if local_start.date() > today_local + timedelta(days=settings.max_booking_days):
        return SlotPolicyResult(False, POLICY_BEYOND_HORIZON)

    if not slot_matches_preference(local_start.hour, time_preference):
        return SlotPolicyResult(False, POLICY_PREFERENCE_MISMATCH)

    slot_service = getattr(slot, "service_key", None)
    if slot_service and service_key and slot_service != service_key:
        return SlotPolicyResult(False, POLICY_SERVICE_MISMATCH)

    return SlotPolicyResult(True, POLICY_OK)


def hold_is_active(slot, now_utc: datetime) -> bool:
    """
    Purpose: Decide whether a slot's hold still counts.
    Rule: status == "held" AND held_until >= now. An expired hold is treated
    as available EVERYWHERE (lazy reclaim; see appointment_hold_service).
    """
    if getattr(slot, "status", None) != STATUS_HELD:
        return False
    held_until = getattr(slot, "held_until", None)
    return held_until is not None and ensure_utc(held_until) >= now_utc


def filter_bookable_slots(
    slots: Sequence,
    now_utc: datetime,
    settings: CalendarSettings,
    time_preference: str,
    service_key: Optional[str] = None,
) -> List:
    """
    Purpose: Apply every availability business rule to raw slot rows.
    Inputs:
        slots:            slot objects (ORM rows or test stubs) with
                          status/start_datetime/held_until/service_key attrs.
        now_utc:          current aware UTC time.
        settings:         the client's CalendarSettings.
        time_preference:  PREF_* bucket from appointment_intent.
        service_key:      the patient's requested service, if known.
    Returns: bookable slots, soonest first, capped at max_offered_slots.
    Database effects: none (pure).
    Possible failures: none — bad rows are excluded, never guessed at.

    A slot is offered only when ALL of these hold:
      1. status is "available", OR "held" but the hold expired (lazy reclaim)
         — availability-STATUS rules, owned here because they are context-
         dependent (hold placement may retake your own hold; finalization
         requires your own active hold).
      2-5. The four booking-POLICY rules — minimum notice (exact elapsed
         time, also rejecting past slots), the Patch 2B local-date horizon,
         time preference, and service compatibility — DELEGATED to
         evaluate_slot_policy, the single pure owner (Patch 2C), so display,
         hold creation, and finalization all apply identical rule text.
    """
    normalized_now = ensure_utc(now_utc)

    bookable = []
    for slot in slots:
        if hold_is_active(slot, normalized_now):
            continue  # Actively held by another patient.
        if slot.status not in (STATUS_AVAILABLE, STATUS_HELD):
            continue  # booked / blocked / cancelled are never offered.
        policy = evaluate_slot_policy(
            slot,
            now_utc=normalized_now,
            settings=settings,
            time_preference=time_preference,
            service_key=service_key,
        )
        if not policy.eligible:
            continue  # too_soon / beyond_horizon / preference / service.
        bookable.append(slot)

    bookable.sort(key=lambda s: ensure_utc(s.start_datetime))
    return bookable[: settings.max_offered_slots]
