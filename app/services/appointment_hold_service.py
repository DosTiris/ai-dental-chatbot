# app/services/appointment_hold_service.py
#
# OWNER OF: temporarily reserving a slot while one patient confirms.
#
# Why holds exist: two patients can be shown 2:00 PM at the same moment.
# Whoever selects it first gets a short exclusive hold; the other patient is
# told the slot was just taken and is shown fresh availability.
#
# HOLD LIFECYCLE (no hidden behavior — Rule 4):
#   place_hold:    status available (or held-but-expired)  -> held, with
#                  held_until = now + settings.hold_minutes and
#                  held_by_conversation_id = this conversation.
#   release_hold:  held by THIS conversation -> available. Releasing a hold
#                  you don't own is a no-op failure, never a takeover.
#   expiry:        LAZY. There is no cron job in the MVP. An expired hold
#                  (held_until < now) is treated as available by
#                  availability filtering AND may be taken over by place_hold.
#                  finalize_booking rejects expired holds. Therefore a slot
#                  can never be stranded in "held" in any way that blocks
#                  booking (Rule 10: "Can it leave a slot permanently held?").
#
# Concurrency: every mutation happens under the slot's row lock
# (repository.get_slot_for_update), inside a transaction owned HERE.

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from app.calendar_models import SlotStatus
from app.repositories import appointment_repository
from app.services.availability_rules import evaluate_slot_policy
from app.services.calendar_settings_service import CalendarSettings, ensure_utc


@dataclass(frozen=True)
class HoldResult:
    """Explicit outcome of a hold attempt — no boolean guessing (Rule 4)."""
    success: bool
    reason: str            # machine-readable: ok / slot_taken / slot_missing /
                           #   slot_blocked / not_owner / slot_ineligible
    held_until: Optional[datetime] = None
    detail: Optional[str] = None   # For slot_ineligible: the exact policy
                                   # reason from evaluate_slot_policy
                                   # (too_soon / beyond_horizon /
                                   # preference_mismatch / service_mismatch).


def place_hold(
    db: Session,
    client_id: uuid.UUID,
    slot_id: uuid.UUID,
    conversation_id: uuid.UUID,
    *,
    settings: CalendarSettings,
    time_preference: str,
    service_key: Optional[str],
    now_utc: datetime,
) -> HoldResult:
    """
    Purpose: Atomically reserve one slot for one conversation.
    Inputs:  ids, then KEYWORD-ONLY policy context (Patch 2C — no permissive
             defaults): the client settings, the EFFECTIVE time preference
             the offer was filtered with, the same service value display
             filtering uses, and the current aware-UTC time. PREF_ANY and
             None are valid values but must be passed intentionally.
             Settings are a request-level snapshot loaded at the beginning
             of the current patient message; this function does not lock the
             client row or guarantee visibility of an admin edit occurring
             after that read but before the slot-row lock.
    Returns: HoldResult (success + reason + held_until on success; detail
             carries the policy reason on slot_ineligible).
    Database effects: locks the slot row; on success UPDATEs it to held and
        COMMITS. On failure ROLLS BACK (releasing the lock, changing nothing).
    Possible failures:
        slot_missing    — id doesn't exist for this client (or wrong office —
                          client isolation makes the two indistinguishable on
                          purpose).
        slot_taken      — booked, or actively held by another conversation.
        slot_blocked    — staff blocked/cancelled it after it was displayed.
        slot_ineligible — the slot no longer satisfies CURRENT booking
                          policy (Patch 2C — Critical #8): the offer may be
                          hours old, so notice/horizon/preference/service
                          are re-judged HERE, under the lock, by the single
                          pure owner. The slot is NOT mutated into HELD.
    """
    try:
        slot = appointment_repository.get_slot_for_update(db, client_id, slot_id)

        if slot is None:
            db.rollback()
            return HoldResult(False, "slot_missing")

        if slot.status in (SlotStatus.BLOCKED, SlotStatus.CANCELLED):
            db.rollback()
            return HoldResult(False, "slot_blocked")

        if slot.status == SlotStatus.BOOKED:
            db.rollback()
            return HoldResult(False, "slot_taken")

        # Re-check the hold INSIDE the lock: another patient may have held it
        # after availability was displayed (the classic race — Rule 9 case
        # "two patients selecting the same slot").
        if slot.status == SlotStatus.HELD:
            held_until = ensure_utc(slot.held_until) if slot.held_until else None
            still_active = held_until is not None and held_until >= now_utc
            owned_by_us = slot.held_by_conversation_id == conversation_id
            if still_active and not owned_by_us:
                db.rollback()
                return HoldResult(False, "slot_taken")
            # Expired hold, or our own re-selection: taking (re-taking) it is safe.

        # PATCH 2C (Critical #8): revalidate CURRENT booking policy under
        # this same lock — the slot was judged eligible when DISPLAYED,
        # possibly hours ago. Only a slot that is eligible RIGHT NOW may
        # transition to HELD; an ineligible one is left exactly as found.
        policy = evaluate_slot_policy(
            slot,
            now_utc=now_utc,
            settings=settings,
            time_preference=time_preference,
            service_key=service_key,
        )
        if not policy.eligible:
            db.rollback()  # Slot NOT mutated; lock released.
            return HoldResult(False, "slot_ineligible", detail=policy.reason)

        new_until = now_utc + timedelta(minutes=settings.hold_minutes)
        slot.status = SlotStatus.HELD
        slot.held_until = new_until
        slot.held_by_conversation_id = conversation_id
        db.commit()
        return HoldResult(True, "ok", held_until=new_until)
    except Exception:
        # Make the failure visible to the caller (Rule 16): undo, then
        # re-raise so the route layer logs it and tells the patient safely.
        db.rollback()
        raise


def release_hold(
    db: Session,
    client_id: uuid.UUID,
    slot_id: uuid.UUID,
    conversation_id: uuid.UUID,
) -> HoldResult:
    """
    Purpose: Free a slot this conversation held (patient said "no" / changed
             direction / abandoned the confirmation).
    Returns: HoldResult; releasing an already-free or expired slot reports
             success=True reason="ok" because the desired end state holds.
             Attempting to release SOMEONE ELSE'S active hold fails with
             not_owner and changes nothing.
    Database effects: locks the row; UPDATE + COMMIT on ownership match,
        ROLLBACK otherwise.
    """
    try:
        slot = appointment_repository.get_slot_for_update(db, client_id, slot_id)
        if slot is None:
            db.rollback()
            return HoldResult(False, "slot_missing")

        if slot.status != SlotStatus.HELD:
            db.rollback()  # Nothing to release; state already as desired.
            return HoldResult(True, "ok")

        if slot.held_by_conversation_id != conversation_id:
            db.rollback()
            return HoldResult(False, "not_owner")

        slot.status = SlotStatus.AVAILABLE
        slot.held_until = None
        slot.held_by_conversation_id = None
        db.commit()
        return HoldResult(True, "ok")
    except Exception:
        db.rollback()
        raise
