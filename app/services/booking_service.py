# app/services/booking_service.py
#
# OWNER OF: turning a held slot into a real appointment, confirming pending
# appointments (staff action — Patch 4, Senior Audit Critical #4), and
# cancelling appointments. Nothing else writes to the appointments table's
# lifecycle.
#
# The final booking sequence (Rule 15 — "final booking must recheck
# availability", and Rule 10 — no partial completion):
#   1. Lock the slot row.
#   2. Re-verify: still held, held by THIS conversation, hold not expired.
#   3. Guard: this conversation has not already booked (duplicate defense).
#   4. INSERT the appointment.
#   5. UPDATE the slot to booked.
#   6. COMMIT — steps 4 and 5 succeed or fail together.
# Notifications happen AFTER commit, in notification_service, precisely so a
# failed SMS can never roll back a real appointment (Rule 16: if the
# appointment saved but the SMS failed, we record exactly that).

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.calendar_models import Appointment, AppointmentStatus, SlotStatus
from app.repositories import appointment_repository
from app.services.availability_rules import evaluate_slot_policy
from app.services.calendar_settings_service import CalendarSettings, ensure_utc


@dataclass(frozen=True)
class BookingResult:
    """Explicit outcome — reason values are the complete failure vocabulary."""
    success: bool
    reason: str            # ok / hold_lost / hold_expired / slot_missing /
                           # already_booked_by_conversation /
                           # invalid_patient_data / slot_ineligible /
                           # already_cancelled / not_cancellable (cancel
                           # path; not_cancellable is PATCH 7) /
                           # PATCH 4: appointment_missing / already_confirmed
                           # (an idempotent SUCCESS) / not_confirmable
    appointment: Optional[Appointment] = None
    detail: Optional[str] = None   # For slot_ineligible: the exact policy
                                   # reason from evaluate_slot_policy.


# The two unique-violation sources this service is allowed to translate into
# booking outcomes. Defined once, matching migrations/002 and calendar_models
# exactly (Rule 3). Anything else re-raises (Rule 16 — failure must be visible).
_PG_UNIQUE_VIOLATION_SQLSTATE = "23505"
_CONVERSATION_UNIQUE_INDEX = "uq_active_appointment_per_conversation"
_SLOT_UNIQUE_INDEX = "uq_active_appointment_per_slot"


def _classify_booking_unique_violation(error: IntegrityError) -> Optional[str]:
    """
    Purpose: Decide whether an IntegrityError is one of the TWO known
             booking-race unique violations added by migration 002.
    Inputs:  the caught sqlalchemy.exc.IntegrityError.
    Returns: the violated index name (one of the two constants above), or
             None meaning "not ours — re-raise".
    Database effects: none (pure inspection of the driver error).
    Possible failures: none; unknown driver shapes simply return None.

    Deliberately strict (approved Patch 1 decision):
      - PostgreSQL ONLY: the SQLSTATE must be 23505 (unique_violation),
        read from the driver error's pgcode. SQLite IntegrityErrors carry
        no pgcode, fail this check, and are RE-RAISED — we never parse
        SQLite message strings. PostgreSQL is the production concurrency
        source of truth.
      - The violated constraint name must exactly match one of our two
        indexes (read from psycopg2's error diagnostics). A missing or
        unknown name means some OTHER integrity bug, which must surface
        loudly, not be absorbed into a polite booking reply.
    """
    driver_error = getattr(error, "orig", None)
    sqlstate = getattr(driver_error, "pgcode", None)
    if sqlstate != _PG_UNIQUE_VIOLATION_SQLSTATE:
        return None
    diagnostics = getattr(driver_error, "diag", None)
    constraint_name = getattr(diagnostics, "constraint_name", None)
    if constraint_name in (_CONVERSATION_UNIQUE_INDEX, _SLOT_UNIQUE_INDEX):
        return constraint_name
    return None


def finalize_booking(
    db: Session,
    client_id: uuid.UUID,
    slot_id: uuid.UUID,
    conversation_id: uuid.UUID,
    *,
    settings: CalendarSettings,
    now_utc: datetime,
    time_preference: str,
    service_key: Optional[str],
    patient_name: str,
    patient_phone: str,
    patient_email: Optional[str],
    new_or_returning: Optional[str],
    reason: Optional[str],
    urgency: str,
) -> BookingResult:
    """
    Purpose: Create the appointment for a slot this conversation holds.
    Inputs:  ids, then KEYWORD-ONLY context (Patch 2C — no permissive
             defaults): settings, aware-UTC now, the EFFECTIVE time
             preference the offer was filtered with, the same service value
             display filtering uses, and the patient details captured by
             Mia's EXISTING intake (this module never re-collects them —
             Rule 3: intake has one owner, and it is not the calendar).
             Settings are a request-level snapshot loaded at the beginning
             of the current patient message; this function does not lock the
             client row or guarantee visibility of an admin edit occurring
             after that read but before the slot-row lock.
    Returns: BookingResult with the appointment on success.
    Database effects: one transaction — appointment INSERT + slot UPDATE to
        booked, committed together; any failure rolls back both. EXCEPTION
        (Patch 2C): the slot_ineligible path COMMITS a release of this
        conversation's own verified hold (slot -> available) in the same
        transaction instead of leaving it to time out; no appointment is
        inserted on that path.
    Possible failures (each mapped to safe patient wording by the caller):
        slot_missing / hold_lost / hold_expired — the recheck failed; the
            patient is shown fresh availability.
        slot_ineligible — the slot no longer satisfies CURRENT booking
            policy (Patch 2C — Critical #8): notice/horizon/preference/
            service are re-judged here, under the lock, by the single pure
            owner, even though the hold itself is still valid. detail
            carries the exact policy reason. No appointment is created and
            the owned hold is released atomically.
        already_booked_by_conversation — duplicate confirmation (double-send,
            page refresh); the EXISTING appointment is returned so Mia can
            restate it instead of booking twice. ALSO returned when the
            database's uq_active_appointment_per_conversation index rejects
            a concurrent duplicate that slipped past the pre-check (two
            requests, two different slots, same conversation) — see the
            IntegrityError handler below.
        hold_lost — additionally returned when uq_active_appointment_per_slot
            rejects a concurrent insert for the same slot: to the patient it
            is the same outcome ("that time was just taken"), so it reuses
            the existing vocabulary and the caller's existing re-offer path.
        invalid_patient_data — missing name/phone; a bug upstream, surfaced
            loudly rather than stored half-empty.
    """
    try:
        # Duplicate defense FIRST: if this conversation already produced an
        # appointment, return it — do not create a second one (Rule 10).
        existing = appointment_repository.get_appointment_by_conversation(
            db, client_id, conversation_id
        )
        if existing is not None:
            db.rollback()
            return BookingResult(False, "already_booked_by_conversation", appointment=existing)

        slot = appointment_repository.get_slot_for_update(db, client_id, slot_id)
        if slot is None:
            db.rollback()
            return BookingResult(False, "slot_missing")

        # Recheck the hold INSIDE the lock — never trust the earlier display.
        if slot.status != SlotStatus.HELD or slot.held_by_conversation_id != conversation_id:
            db.rollback()
            return BookingResult(False, "hold_lost")
        if slot.held_until is None or ensure_utc(slot.held_until) < now_utc:
            db.rollback()
            return BookingResult(False, "hold_expired")

        # PATCH 2C (Critical #8): the hold is valid, but the WORLD may have
        # changed since the slot was displayed — revalidate CURRENT policy
        # under this same lock via the single pure owner. On failure,
        # release OUR hold (ownership verified two checks above) in this
        # SAME transaction rather than leaving the slot held until timeout,
        # and create NO appointment.
        policy = evaluate_slot_policy(
            slot,
            now_utc=now_utc,
            settings=settings,
            time_preference=time_preference,
            service_key=service_key,
        )
        if not policy.eligible:
            slot.status = SlotStatus.AVAILABLE
            slot.held_until = None
            slot.held_by_conversation_id = None
            db.commit()  # Atomic hold release; nothing else changed.
            return BookingResult(False, "slot_ineligible", detail=policy.reason)

        # Early rollout: appointments start as "pending" so the office
        # confirms manually; flip the setting to auto-confirm later.
        status = (
            AppointmentStatus.PENDING
            if settings.require_staff_confirmation
            else AppointmentStatus.CONFIRMED
        )

        try:
            appointment = appointment_repository.create_appointment_from_slot(
                db,
                slot=slot,
                conversation_id=conversation_id,
                patient_name=patient_name,
                patient_phone=patient_phone,
                patient_email=patient_email,
                new_or_returning=new_or_returning,
                reason=reason,
                urgency=urgency,
                status=status,
            )
        except ValueError:
            db.rollback()
            return BookingResult(False, "invalid_patient_data")

        slot.status = SlotStatus.BOOKED
        slot.held_until = None
        slot.held_by_conversation_id = None

        db.commit()
        return BookingResult(True, "ok", appointment=appointment)
    except IntegrityError as integrity_error:
        # The database refused the insert/commit. This is EXPECTED in exactly
        # one situation: two concurrent finalize requests raced past the
        # pre-check above, and one of the migration-002 partial unique
        # indexes rejected the loser. Map ONLY that situation to a calm,
        # deterministic booking outcome; anything else is a real bug and
        # must propagate (Rule 16 — no hidden failures).
        db.rollback()  # Releases the slot lock; nothing was persisted.
        violated_index = _classify_booking_unique_violation(integrity_error)

        if violated_index == _CONVERSATION_UNIQUE_INDEX:
            # This conversation already has an active appointment — the other
            # request won. Re-query (fresh read, post-rollback) so Mia can
            # restate the WINNING appointment instead of booking twice.
            winner = appointment_repository.get_appointment_by_conversation(
                db, client_id, conversation_id
            )
            return BookingResult(
                False, "already_booked_by_conversation", appointment=winner
            )

        if violated_index == _SLOT_UNIQUE_INDEX:
            # Another conversation's appointment owns this slot. Same patient
            # outcome as losing the hold: the caller re-offers fresh slots.
            return BookingResult(False, "hold_lost")

        raise  # Unknown constraint, non-PostgreSQL, or non-23505: surface it.
    except Exception:
        db.rollback()  # No partial completion, ever (Rule 16).
        raise


def confirm_appointment(
    db: Session,
    client_id: uuid.UUID,
    appointment_id: uuid.UUID,
    *,
    now_utc: datetime,
) -> BookingResult:
    """
    Purpose: Staff confirmation (admin route) — the ONLY supported
             pending -> confirmed transition (Patch 4, Senior Audit
             Critical #4). Before this function existed, appointments booked
             with require_staff_confirmation enabled stayed PENDING forever.
    Inputs:  ids, then KEYWORD-ONLY aware-UTC now (Patch 2C convention).
             now_utc is injected by the caller and normalized through
             ensure_utc before storing — this function NEVER reads the real
             clock itself, so tests stay deterministic.
    Returns: BookingResult:
        ok                  — was PENDING; now CONFIRMED with confirmed_at =
                              the normalized now_utc (first staff
                              confirmation).
        already_confirmed   — idempotent SUCCESS (success=True): already
                              CONFIRMED; NOTHING is written, and the original
                              confirmed_at is preserved byte-for-byte. That
                              original value is NULL when the appointment was
                              created directly as CONFIRMED
                              (require_staff_confirmation=false) — approved
                              semantics: confirmed_at records STAFF
                              confirmations only.
        appointment_missing — no appointment with this id FOR THIS CLIENT.
                              Unknown ids and another office's ids are
                              deliberately indistinguishable (tenant
                              isolation, Rule 15).
        not_confirmable     — status is cancelled / completed / no_show, or
                              ANY value outside AppointmentStatus.ALL (the
                              status column has no CHECK constraint, so a
                              malformed / legacy / manually edited /
                              mixed-version row is possible). detail is
                              SANITIZED at this boundary (PATCH 8, the
                              mirror of the cancel path's correction
                              pass 1): a member of AppointmentStatus.ALL
                              passes through exactly; anything else is
                              represented ONLY as the fixed sentinel
                              "unsupported". The raw stored value is never
                              echoed through detail and never repaired or
                              rewritten. Nothing is mutated, including any
                              confirmed_at recorded by an earlier
                              confirmation.
    Database effects: one transaction. On the PENDING path ONLY: appointment
        status -> confirmed and confirmed_at set, committed ONCE. Every other
        path rolls back having written nothing (the rollback also releases
        the row lock). The slot row is never read, locked, or changed — it is
        and stays BOOKED. Notification flags and notify_error are never
        touched.
    External effects: NONE. No office SMS/email — authorized office staff are
        the ones performing this action — and no patient message of any kind
        (Patch 2D policy: patient SMS remains disabled).
    Concurrency: get_appointment_for_update serializes concurrent confirms
        and confirm-vs-cancel on the same appointment row. The loser of a
        confirm/confirm race takes the idempotent already_confirmed path, so
        confirmed_at is written exactly once. The pending -> confirmed UPDATE
        cannot violate the migration-002 partial unique indexes (the indexed
        columns are unchanged and the row stays inside the
        status <> 'cancelled' predicates), so no IntegrityError
        classification is needed here — an unexpected exception rolls back
        and propagates (Rule 16).
    """
    try:
        appointment = appointment_repository.get_appointment_for_update(
            db, client_id, appointment_id
        )
        if appointment is None:
            db.rollback()
            return BookingResult(False, "appointment_missing")
        if appointment.status == AppointmentStatus.CONFIRMED:
            # Idempotent success: repeated confirmation (double-click, retry)
            # must have NO duplicate effects. Nothing to write; the rollback
            # releases the row lock without touching the row.
            db.rollback()
            return BookingResult(True, "already_confirmed", appointment=appointment)
        if appointment.status != AppointmentStatus.PENDING:
            # cancelled / completed / no_show are not confirmable — a
            # finished or dead appointment must never come back to life via
            # this endpoint (Rule 14: no jumping to unrelated states).
            db.rollback()
            # PATCH 8 (inline mirror of the cancel path's correction pass 1
            # below): the status column has no CHECK constraint, so the
            # stored value is untrusted at this boundary. Only controlled
            # AppointmentStatus vocabulary may leave through detail;
            # anything else is represented ONLY as the fixed sentinel
            # "unsupported". The stored value itself is NOT repaired or
            # rewritten (no hidden data mutation — Rule 4).
            safe_detail = (
                appointment.status
                if appointment.status in AppointmentStatus.ALL
                else "unsupported"
            )
            return BookingResult(
                False, "not_confirmable",
                appointment=appointment, detail=safe_detail,
            )

        # The single supported transition: pending -> confirmed, with the
        # first-staff-confirmation audit instant, committed once.
        appointment.status = AppointmentStatus.CONFIRMED
        appointment.confirmed_at = ensure_utc(now_utc)
        db.commit()
        return BookingResult(True, "ok", appointment=appointment)
    except Exception:
        db.rollback()  # No partial completion, ever (Rule 16).
        raise


# PATCH 7 (Senior Audit Recommended #6): the complete cancellation
# allow-list. ONLY these statuses may proceed to the cancelled mutation;
# every other status — current (completed / no_show) or any future one —
# is rejected by default with reason "not_cancellable" (Rule 4: rejection
# is the default, permission is explicit; Rule 14: no leaving a terminal
# state). Defined once here, in the single lifecycle owner (Rule 3).
_CANCELLABLE_STATUSES = frozenset({
    AppointmentStatus.PENDING,
    AppointmentStatus.CONFIRMED,
})


def cancel_appointment(
    db: Session,
    client_id: uuid.UUID,
    appointment_id: uuid.UUID,
) -> BookingResult:
    """
    Purpose: Staff-initiated cancellation (admin route). Patient-initiated
             cancellation through chat is a later approved phase (Rule 17);
             for the MVP the office handles patient requests manually.
             PATCH 7 (Senior Audit Recommended #6): cancellation follows an
             explicit lifecycle allow-list instead of cancelling anything
             not already cancelled.
    Lifecycle contract (the complete transition table for this operation):
        pending   -> cancelled   (slot released)
        confirmed -> cancelled   (slot released; confirmed_at PRESERVED for
                                  the audit trail — this function writes
                                  status only)
        cancelled -> rejected    reason already_cancelled — idempotent and
                                  mutation-free: nothing is rewritten, no
                                  slot change, no side effects (approved
                                  decision D1: reported as success=False).
        completed -> rejected    reason not_cancellable, detail "completed"
        no_show   -> rejected    reason not_cancellable, detail "no_show"
        any other -> rejected    not_cancellable. Statuses outside the
                                  allow-list are rejected by DEFAULT, so a
                                  future or malformed status cannot
                                  silently become cancellable. The status
                                  column has NO database CHECK constraint,
                                  so a malformed / legacy / manually
                                  edited / mixed-version row may hold a
                                  value outside AppointmentStatus.ALL.
                                  detail is therefore SANITIZED at this
                                  boundary (correction pass 1): a member
                                  of AppointmentStatus.ALL passes through
                                  exactly; anything else is represented
                                  ONLY as the fixed sentinel
                                  "unsupported". The raw stored value is
                                  never echoed through detail and never
                                  repaired or rewritten.
    Returns: BookingResult (reason: ok / slot_missing when appointment not
             found FOR THIS CLIENT — unknown ids and another office's ids
             are deliberately indistinguishable (Rule 15) / already_cancelled
             / not_cancellable with detail = current status).
    Database effects: one transaction. On the allowed path ONLY: appointment
        status -> cancelled AND its slot freed back to available (hold
        fields cleared), committed together. EVERY rejection path rolls back
        having written nothing — the rollback also releases the row lock.
        A completed/no_show appointment's historical slot is therefore never
        reopened (the audit's stated harm).
    External effects: NONE. No office SMS/email (authorized staff are the
        ones acting) and no patient message of any kind (Patch 2D policy:
        patient SMS remains disabled). Notification flags and notify_error
        are never touched.
    Concurrency: get_appointment_for_update serializes concurrent cancels
        and cancel-vs-confirm on the same row (lock order appointment ->
        slot; no other path takes those locks in the opposite order). The
        loser of a cancel/cancel race deterministically observes CANCELLED
        and takes the already_cancelled path.
    """
    try:
        appointment = appointment_repository.get_appointment_for_update(
            db, client_id, appointment_id
        )
        if appointment is None:
            db.rollback()
            return BookingResult(False, "slot_missing")
        if appointment.status == AppointmentStatus.CANCELLED:
            db.rollback()
            return BookingResult(False, "already_cancelled", appointment=appointment)
        if appointment.status not in _CANCELLABLE_STATUSES:
            # PATCH 7: completed / no_show (and any future status) are
            # terminal for cancellation — a finished appointment must never
            # be rewritten and its historical slot must never be reopened
            # (Senior Audit Recommended #6). Mutation-free: this guard runs
            # BEFORE any state changes, and the rollback releases the row
            # lock without touching the row or its slot.
            db.rollback()
            # Correction pass 1: the status column has no CHECK constraint,
            # so the stored value is untrusted at this boundary. Only
            # controlled AppointmentStatus vocabulary may leave through
            # detail; anything else is represented as the fixed sentinel
            # "unsupported". The stored value itself is NOT repaired or
            # rewritten (no hidden data mutation — Rule 4).
            safe_detail = (
                appointment.status
                if appointment.status in AppointmentStatus.ALL
                else "unsupported"
            )
            return BookingResult(
                False, "not_cancellable",
                appointment=appointment, detail=safe_detail,
            )

        appointment.status = AppointmentStatus.CANCELLED

        # Free the slot so it can be rebooked. Locked to serialize with any
        # concurrent hold attempt on the same row.
        slot = appointment_repository.get_slot_for_update(
            db, client_id, appointment.slot_id
        )
        if slot is not None and slot.status == SlotStatus.BOOKED:
            slot.status = SlotStatus.AVAILABLE
            slot.held_until = None
            slot.held_by_conversation_id = None

        db.commit()
        return BookingResult(True, "ok", appointment=appointment)
    except Exception:
        db.rollback()
        raise
