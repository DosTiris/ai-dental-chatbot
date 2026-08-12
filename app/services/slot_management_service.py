# app/services/slot_management_service.py
#
# OWNER OF: the per-slot staff lifecycle mutations "block" and "unblock"
# (P4-A - Portal Slot Schedule Controls v1, contract v1.2 SS4).
#
# WHY THIS FILE EXISTS (Rule 3 / Rule 20, ChatGPT v1.1 Correction B): before
# P4-A the block mutation rule lived inline in routes/calendar.py::block_slot.
# P4-A adds a second surface (the JWT-authenticated portal) that must apply
# THE SAME rule, so the rule moved here - one owner - and BOTH routes
# delegate. The extraction is behavior-preserving BY CONTRACT: the admin
# endpoint's observable behavior (status codes, wording, which states may be
# blocked, hold clearing) is byte-equivalent to the frozen parent and pinned
# by calendar_tests/test_slot_management_owner.py.
#
# TRANSACTION OWNERSHIP: block_slot / unblock_slot own their transaction
# (lock -> check -> mutate -> commit, rollback on every refusal and every
# unexpected error), mirroring appointment_hold_service. apply_block is the
# PURE mutation rule shared by the single-slot path and the P4-A bulk sweep
# (portal_schedule_service.block_all_open) so "blocking clears holds" has
# exactly one rule text in the codebase.
#
# CLOSED OUTCOME VOCABULARY (Rule 4/16): SlotActionResult.reason is exactly
# one of REASON_OK / REASON_SLOT_MISSING / REASON_SLOT_BOOKED /
# REASON_SLOT_NOT_BLOCKED. Routes map these to HTTP; nothing else is ever
# returned, and no raw database or tenant information rides on a result.

import uuid
from dataclasses import dataclass
from typing import Optional

from sqlalchemy.orm import Session

from app.calendar_models import AppointmentSlot, SlotStatus
from app.repositories import appointment_repository

# The complete outcome vocabulary - evaluate nothing else anywhere.
REASON_OK = "ok"
REASON_SLOT_MISSING = "slot_missing"        # unknown id OR foreign tenant -
                                            # indistinguishable by design
                                            # (Rule 15, the calendar.py 404).
REASON_SLOT_BOOKED = "slot_booked"          # block refused: a patient's
                                            # appointment occupies the slot.
REASON_SLOT_NOT_BLOCKED = "slot_not_blocked"  # unblock refused: only
                                              # blocked -> available exists.


@dataclass(frozen=True)
class SlotActionResult:
    """Outcome of one staff slot action - no boolean guessing.

    detail carries ONLY a closed SlotStatus vocabulary word (for the
    unblock 409 wording) - never tenant, patient, hold, or database data.
    """
    ok: bool
    reason: str  # exactly one of the REASON_* constants above
    slot: Optional[AppointmentSlot] = None
    detail: Optional[str] = None


def apply_block(slot: AppointmentSlot) -> None:
    """
    Purpose: THE single pure mutation rule for "staff removes this slot from
        booking" (extracted from routes/calendar.py::block_slot, P4-A SS4).
        Single-slot block AND the bulk sweep both call THIS function, so the
        rule text exists exactly once (Rule 3).
    Inputs:  a slot row already loaded UNDER A ROW LOCK by the caller.
    Returns: nothing - mutates the instance in place.
    Database effects: attribute mutation only; the CALLER owns flush/commit.
    Possible failures: none - this function never inspects status. Deciding
        WHETHER a slot may be blocked (booked refusal, bulk-sweep status
        selection) is the caller's documented responsibility; the rule here
        is only WHAT blocking means: status becomes blocked and any hold is
        cleared, exactly as the frozen admin endpoint has always done.
    """
    slot.status = SlotStatus.BLOCKED
    slot.held_until = None
    slot.held_by_conversation_id = None


def block_slot(
    db: Session,
    client_id: uuid.UUID,
    slot_id: uuid.UUID,
) -> SlotActionResult:
    """
    Purpose: Staff removes ONE slot from booking (meeting, lunch, partial
        closure) - the P4-A shared owner of the transaction the admin route
        previously ran inline.
    Inputs:  request session; the AUTHENTICATED tenant's id (never a
        caller-supplied tenant); the slot id.
    Returns: SlotActionResult -
        ok                -> slot blocked (hold cleared), committed;
        slot_missing      -> no such slot FOR THIS TENANT (unknown and
                             foreign ids indistinguishable - Rule 15);
        slot_booked       -> refused: a booked appointment occupies the
                             slot; staff must cancel the appointment first
                             so a patient's booking never silently vanishes
                             (Rule 4 / Rule 16 - the frozen admin rule).
    Database effects: one locked transaction (SELECT ... FOR UPDATE via the
        repository owner); commit on success, rollback on every refusal.
    Possible failures: unexpected database errors roll back and propagate
        (Rule 16 - never hidden).

    BEHAVIOR-PRESERVATION NOTE (Rule 12, pinned by tests): the frozen admin
    rule blocks ANY non-booked status - available, held (active or expired,
    clearing the hold), an already-blocked slot (idempotent re-block), and
    even a cancelled slot. That behavior is deliberately PRESERVED here, not
    "improved": tightening it would change the admin surface, which P4-A
    must not do.
    """
    try:
        slot = appointment_repository.get_slot_for_update(db, client_id, slot_id)
        if slot is None:
            db.rollback()
            return SlotActionResult(False, REASON_SLOT_MISSING)
        if slot.status == SlotStatus.BOOKED:
            db.rollback()
            return SlotActionResult(False, REASON_SLOT_BOOKED)
        apply_block(slot)
        db.commit()
        return SlotActionResult(True, REASON_OK, slot=slot)
    except Exception:
        # Make the failure visible to the caller (Rule 16): undo, then
        # re-raise so the route layer logs it and fails closed.
        db.rollback()
        raise


def unblock_slot(
    db: Session,
    client_id: uuid.UUID,
    slot_id: uuid.UUID,
) -> SlotActionResult:
    """
    Purpose: Staff returns ONE previously blocked slot to booking - the NEW
        P4-A rule (contract v1.2 SS4): blocked -> available ONLY.
    Inputs:  request session; the AUTHENTICATED tenant's id; the slot id.
    Returns: SlotActionResult -
        ok                 -> slot is available again, committed;
        slot_missing       -> no such slot FOR THIS TENANT (unknown and
                              foreign indistinguishable - Rule 15);
        slot_not_blocked   -> refused: the slot is available / held /
                              booked / cancelled. detail carries ONLY the
                              closed SlotStatus word for the 409 wording.
    Database effects: one locked transaction; commit on success, rollback
        on every refusal.
    Possible failures: unexpected database errors roll back and propagate.

    WHY ONLY blocked -> available (Rule 4 - no hidden behavior): every other
    transition is refused, never coerced. Unblock does not touch hold fields
    (a blocked slot has none by the apply_block rule; refused states are
    left byte-untouched), never resurrects a cancelled slot (cancelled is
    the audit-preserving deletion), never releases another conversation's
    hold, and never reopens a booked slot (that is cancellation's job).
    """
    try:
        slot = appointment_repository.get_slot_for_update(db, client_id, slot_id)
        if slot is None:
            db.rollback()
            return SlotActionResult(False, REASON_SLOT_MISSING)
        if slot.status != SlotStatus.BLOCKED:
            refused_status = slot.status  # closed vocabulary word only
            db.rollback()
            return SlotActionResult(
                False, REASON_SLOT_NOT_BLOCKED, detail=refused_status
            )
        slot.status = SlotStatus.AVAILABLE
        db.commit()
        return SlotActionResult(True, REASON_OK, slot=slot)
    except Exception:
        db.rollback()
        raise
