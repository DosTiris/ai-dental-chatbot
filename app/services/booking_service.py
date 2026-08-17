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
from app.services.availability_rules import evaluate_slot_policy, hold_is_active
# SLICE 4B1: the ONE internal-note normalization owner. Used by the STAFF
# booking path only; the frozen chatbot finalize_booking neither accepts nor
# stores notes. This import brings in NO notification, chat, or public-schema
# code (appointment_note_service imports the repository only).
from app.services.appointment_note_service import normalize_internal_note
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
                           # (an idempotent SUCCESS) / not_confirmable /
                           # PHASE 3A staff path: slot_taken / slot_blocked
                           # - the SAME words appointment_hold_service
                           # already uses for the SAME slot states, so the
                           # portal never learns a second vocabulary - plus
                           # slot_started, the one NEW reason (see
                           # finalize_staff_booking for why it cannot
                           # honestly reuse slot_ineligible/too_soon).
    appointment: Optional[Appointment] = None
    detail: Optional[str] = None   # For slot_ineligible: the exact policy
                                   # reason from evaluate_slot_policy.


# The two unique-violation sources this service is allowed to translate into
# booking outcomes. Defined once, matching migrations/002 and calendar_models
# exactly (Rule 3). Anything else re-raises (Rule 16 — failure must be visible).
# PHASE 3A (owner decision D6): the appointments.source value for a booking
# an authenticated office employee entered themselves. Declared HERE, in
# the service, and never accepted from a caller - a future route or browser
# cannot choose or spoof the provenance of an appointment. The column has
# no CHECK constraint (verified across migrations 001-010), so a typo would
# persist silently; a named constant is that typo resistance.
STAFF_BOOKING_SOURCE = "portal_staff"

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



def finalize_staff_booking(
    db: Session,
    client_id: uuid.UUID,
    slot_id: uuid.UUID,
    *,
    now_utc: datetime,
    patient_name: str,
    patient_phone: str,
    patient_email: Optional[str],
    new_or_returning: Optional[str],
    reason: Optional[str],
    urgency: str,
    internal_note: Optional[str] = None,
) -> BookingResult:
    """
    Purpose: PHASE 3A Slice 1 - book ONE existing authoritative slot on behalf
             of an authenticated office employee, with no chatbot
             conversation and no conversational hold.

    WHY A SEPARATE FUNCTION (locked architecture decision): finalize_booking
    is the frozen chatbot path and its signature, semantics, hold-ownership
    rules, public policy behaviour and status selection are unchanged by this
    slice. Staff booking differs from it in exactly four ways - no
    conversation, no hold to verify, no public policy, and a fixed CONFIRMED
    status - and every one of those is a REMOVAL. Threading four flags through
    the frozen function would have put those removals inside the path that
    protects patients. They live here instead.

    WHAT IS DELIBERATELY SHARED (this is what makes the split safe - the two
    paths must never become two different ways to claim a slot):
      - appointment_repository.get_slot_for_update: the SAME SELECT ... FOR
        UPDATE row lock, with the same populate_existing identity-map
        correction, and the same (client_id, slot_id) tenant filter.
      - appointment_repository.create_appointment_from_slot: the SAME INSERT
        primitive, so start/end/client_id are copied off the LOCKED row and
        the same required-field validation applies.
      - availability_rules.hold_is_active: the SAME single definition of an
        active hold (D4) - this function does not restate what "expired"
        means.
      - _classify_booking_unique_violation and uq_active_appointment_per_slot:
        the SAME final database arbiter for a concurrent claim.
    There is no second lock, no second INSERT, and no second expiry rule.

    Inputs: ids, then KEYWORD-ONLY context following the Patch 2C convention
        (no permissive defaults). now_utc is INJECTED - this function never
        reads the clock - so tests stay deterministic.
        Deliberately NOT accepted, because all of them are server-owned and a
        caller must not be able to influence them: conversation_id (always
        NULL), source (always STAFF_BOOKING_SOURCE), status (always
        CONFIRMED), start/end datetimes, client_id inside patient data,
        provider_name and service_key (properties of the authoritative slot).
        There is also NO settings parameter: the public policy rules staff
        bypass are the only thing settings carried into the booking decision,
        and the CONFIRMED status is fixed rather than derived from
        require_staff_confirmation (D5). A staff booking therefore cannot be
        altered by a settings edit racing the request.

    Owner product decisions implemented here:
        D1 - the public minimum-notice rule is NOT applied. The only time
             rule that survives is that a slot which has already started
             cannot be booked; a past slot is refused as slot_started.
        D2 - the public maximum-horizon rule is NOT applied. Staff may book
             any existing future slot, and cannot manufacture one: the slot
             must already exist for this tenant.
        D3 - patient time-preference and service filtering are NOT applied,
             and no preference is accepted. Provider/service remain whatever
             the authoritative slot already carries.
        D4 - slot state, tenant, and concurrency rules are MANDATORY and are
             all evaluated under the row lock.
        D5 - the appointment is created CONFIRMED. confirmed_at stays NULL:
             booking_service.confirm_appointment remains its only writer, and
             NULL continues to mean "never staff-confirmed via that action",
             exactly as it already does for auto-confirmed appointments.
        D6 - source is STAFF_BOOKING_SOURCE.
        D7 - NO notification of any kind. This function calls no notification
             code, and the module imports none.

    Returns: BookingResult. The reason vocabulary reuses the existing words
        wherever they are honestly equivalent:
        ok               - one CONFIRMED appointment exists and the slot is
                           BOOKED, committed together.
        slot_missing     - no such slot FOR THIS CLIENT. An unknown id and
                           another office's id are deliberately
                           indistinguishable (Rule 15) because the repository
                           filters on both columns.
        slot_taken       - the slot is already BOOKED, or an ACTIVE hold
                           belongs to a conversation. Staff never steals a
                           live hold. ALSO returned when
                           uq_active_appointment_per_slot rejects a
                           concurrent insert: to the office it is the same
                           outcome, so it reuses the same word.
        slot_blocked     - staff blocked or cancelled the slot.
        slot_started     - the slot's start time is not in the future. This
                           is the ONE new reason. It could not honestly reuse
                           slot_ineligible/too_soon: that reason means "inside
                           the configured minimum notice", which is precisely
                           the rule staff bypass. Reporting a past slot with
                           the name of a bypassed rule would be a misleading
                           map of a materially different condition.
        invalid_patient_data - missing name/phone, surfaced loudly by the
                           shared INSERT primitive rather than stored
                           half-empty. patient_email is optional and a blank
                           one is normalized to NULL by that same primitive;
                           nothing here requires an email.
        invalid_internal_note - SLICE 4B1: the optional office-internal note
                           exceeds 2000 characters after trimming. Judged by
                           the ONE normalization owner
                           (appointment_note_service.normalize_internal_note)
                           BEFORE the INSERT, inside this same transaction,
                           so the appointment and its note are created
                           atomically or not at all - there is no
                           book-first / note-second path, and a refusal
                           creates nothing. Blank/whitespace notes normalize
                           to NULL (no note). The parameter defaults to
                           None (the contract-specified shape): unlike the
                           injected CONTEXT parameters above, an absent note
                           is a true value - "no note" - not missing context,
                           so the default is honest rather than permissive,
                           and every pre-4B1 caller (including the frozen
                           Slice 1/2 tests) remains valid unchanged. The note
                           never influences any slot/status/source/policy
                           decision above or below this point.

    Database effects: ONE transaction. On success: appointment INSERT + slot
        UPDATE to booked with the hold columns cleared, committed together.
        EVERY refusal path rolls back before returning, so no refusal can
        leave a partial mutation, a half-cleared hold, or an open transaction
        holding the slot lock. Unlike the chatbot path there is no committed
        hold-release branch, because this path never owns a hold.
    External effects: none.
    """
    try:
        # THE lock. Everything below is judged on the locked row, never on
        # anything the caller displayed or believed earlier (Rule 15).
        slot = appointment_repository.get_slot_for_update(db, client_id, slot_id)
        if slot is None:
            db.rollback()
            return BookingResult(False, "slot_missing")

        if slot.status == SlotStatus.BOOKED:
            db.rollback()
            return BookingResult(False, "slot_taken")

        if slot.status in (SlotStatus.BLOCKED, SlotStatus.CANCELLED):
            db.rollback()
            return BookingResult(False, "slot_blocked")

        # D4: a hold that is STILL ACTIVE belongs to a patient conversation
        # mid-booking and may not be taken. hold_is_active is the single
        # owner of that definition; an EXPIRED hold is treated as available
        # here for exactly the same reason place_hold already re-takes one -
        # lazy reclaim is the established production rule, and inventing a
        # second meaning of "expired" for staff would be a Rule 3 violation.
        if hold_is_active(slot, now_utc):
            db.rollback()
            return BookingResult(False, "slot_taken")

        # D1/D2: notice and horizon are bypassed, but the past is not
        # bookable. Both sides are normalized to aware UTC by the single
        # owner before comparison, so this is never a naive/aware mix.
        if ensure_utc(slot.start_datetime) <= ensure_utc(now_utc):
            db.rollback()
            return BookingResult(False, "slot_started")

        # SLICE 4B1: normalize the optional office-internal note UNDER the
        # same row lock and transaction as the INSERT it rides with. An
        # over-limit note refuses the WHOLE booking (rollback, nothing
        # persisted) - atomicity is the contract, and silent truncation is
        # forbidden.
        try:
            normalized_internal_note = normalize_internal_note(internal_note)
        except ValueError:
            db.rollback()
            return BookingResult(False, "invalid_internal_note")

        try:
            appointment = appointment_repository.create_appointment_from_slot(
                db,
                slot=slot,
                # THE conversation invariant: a staff appointment has no
                # conversation. NULL is exempt from
                # uq_active_appointment_per_conversation (a partial index on
                # conversation_id IS NOT NULL) while remaining fully bound by
                # uq_active_appointment_per_slot, which is the index that
                # actually arbitrates a slot race.
                conversation_id=None,
                patient_name=patient_name,
                patient_phone=patient_phone,
                patient_email=patient_email,
                new_or_returning=new_or_returning,
                reason=reason,
                urgency=urgency,
                status=AppointmentStatus.CONFIRMED,   # D5
                source=STAFF_BOOKING_SOURCE,          # D6
                internal_note=normalized_internal_note,  # 4B1: pre-normalized
            )
        except ValueError:
            db.rollback()
            return BookingResult(False, "invalid_patient_data")

        slot.status = SlotStatus.BOOKED
        # Clear any EXPIRED hold bookkeeping so a booked slot never carries a
        # stale owner. An ACTIVE hold could not have reached this line.
        slot.held_until = None
        slot.held_by_conversation_id = None

        db.commit()
        return BookingResult(True, "ok", appointment=appointment)
    except IntegrityError as integrity_error:
        # The database refused the insert/commit. EXPECTED in exactly one
        # situation: a concurrent claim raced past the checks above and the
        # migration-002 partial unique index rejected the loser. Map ONLY
        # that; anything else is a real bug and must propagate (Rule 16).
        db.rollback()  # Releases the slot lock; nothing was persisted.
        violated_index = _classify_booking_unique_violation(integrity_error)

        if violated_index == _SLOT_UNIQUE_INDEX:
            # Another appointment - staff or patient - owns this slot now.
            return BookingResult(False, "slot_taken")

        # uq_active_appointment_per_conversation cannot be violated by this
        # path: it is partial on conversation_id IS NOT NULL and this path
        # always inserts NULL. Reaching it would mean the invariant above was
        # broken, so it must surface rather than be absorbed.
        raise
    except Exception:
        db.rollback()
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


# ===========================================================================
# PHASE 3A Slice 4C: cancelled-appointment recovery and rescheduling.
#
# Two NEW office-staff lifecycle actions, added to THIS module because it is
# the single appointment-lifecycle owner (Rule 3) - restore and reschedule
# are state transitions plus slot-inventory moves, exactly the concerns this
# file already owns for finalize/confirm/cancel. Nothing existing above this
# line changed; both functions REUSE the established primitives:
#   - appointment_repository.get_appointment_for_update /
#     get_slot_for_update: the SAME tenant-filtered FOR UPDATE row locks
#     (with the V4.2 populate_existing correction) every other lifecycle
#     action uses;
#   - availability_rules.hold_is_active: the ONE definition of an active
#     hold (an expired hold is lazily reclaimed, the established production
#     rule - D4);
#   - _classify_booking_unique_violation + the migration-002 partial unique
#     indexes: the SAME final database arbiter for a concurrent claim.
# There is no second lock, no second expiry rule, and no second state
# machine.
#
# LOCK ORDER (deadlock discipline): the appointment row is ALWAYS locked
# first, then slot rows. When ONE action must lock TWO slots (an active
# reschedule locks the old slot and the target slot), they are locked in
# ascending UUID order - a single global slot order, so two concurrent
# reschedules moving between the same pair of slots in opposite directions
# request the locks in the SAME sequence and can never form a cycle. The
# existing cancel path (appointment -> its one slot) is a one-slot special
# case of this order, so no existing path conflicts with it.
#
# NOTIFICATIONS: none, on any path. These functions call no notification
# code and this module continues to import none - office staff are the ones
# acting, and patient SMS remains disabled (the confirm/cancel policy,
# unchanged).
# ===========================================================================

# Slice 4C: the ONLY appointment statuses each new action may start from.
# Rejection is the default (Rule 4): a completed / no_show / malformed /
# future status is refused without mutation, exactly the _CANCELLABLE_
# STATUSES pattern above.
_RESTORABLE_STATUSES = frozenset({AppointmentStatus.CANCELLED})
_RESCHEDULABLE_ACTIVE_STATUSES = frozenset({
    AppointmentStatus.PENDING,
    AppointmentStatus.CONFIRMED,
})


def _sanitize_status_detail(status):
    """
    Purpose: Slice 4C extraction of the PATCH 8 / correction-pass-1 rule the
        confirm and cancel paths already state inline: the appointments
        status column has no CHECK constraint, so a stored value is
        untrusted at a refusal boundary. Only controlled AppointmentStatus
        vocabulary may leave through BookingResult.detail; anything else is
        represented ONLY as the fixed sentinel "unsupported". The stored
        value itself is never repaired or rewritten (Rule 4).
    Inputs: the raw stored status string.
    Returns: the status itself when it is a member of AppointmentStatus.ALL,
        otherwise "unsupported".
    Database effects: none. External effects: none.
    """
    return status if status in AppointmentStatus.ALL else "unsupported"


def _judge_slot_bookable(slot, now_utc: datetime) -> Optional[str]:
    """
    Purpose: Slice 4C - judge ONE locked slot row against the staff
        slot-state rules, returning the refusal reason or None (bookable).
        This is the finalize_staff_booking D1/D2/D4 judgement extracted so
        restore and reschedule apply the IDENTICAL rules in the IDENTICAL
        order (Rule 3: the portal must never grow two meanings of "this
        slot can be taken by staff"):
          - BOOKED, or an ACTIVE hold        -> "slot_taken" (staff never
            steal a live patient hold; hold_is_active is the single owner
            of "active", and an EXPIRED hold is lazily reclaimed - D4);
          - BLOCKED or CANCELLED             -> "slot_blocked";
          - start not strictly in the future -> "slot_started" (notice and
            horizon are bypassed for staff - D1/D2 - but the past is not
            bookable; both sides normalized to aware UTC by the single
            owner, never a naive/aware mix).
    Inputs: the LOCKED slot row and the injected aware-UTC now.
    Returns: None when the slot may be taken, else the refusal reason word.
    Database effects: none (pure judgement of the locked row's values).
    External effects: none.
    """
    if slot.status == SlotStatus.BOOKED:
        return "slot_taken"
    if slot.status in (SlotStatus.BLOCKED, SlotStatus.CANCELLED):
        return "slot_blocked"
    if hold_is_active(slot, now_utc):
        return "slot_taken"
    if ensure_utc(slot.start_datetime) <= ensure_utc(now_utc):
        return "slot_started"
    return None


def restore_appointment(
    db: Session,
    client_id: uuid.UUID,
    appointment_id: uuid.UUID,
    *,
    now_utc: datetime,
) -> BookingResult:
    """
    Purpose: PHASE 3A Slice 4C - the office restores ONE of its own
        CANCELLED appointments back onto its ORIGINAL slot, because the
        patient called back and wants the appointment again. This is the
        ONLY supported cancelled -> confirmed transition, and it is valid
        ONLY when the original slot is re-verified as bookable UNDER THE
        ROW LOCK at mutation time (Rule 15: the final booking must recheck
        availability; a cancelled appointment is never blindly flipped
        back to life).

    Inputs: ids, then KEYWORD-ONLY aware-UTC now (the Patch 2C convention -
        injected, never read from the clock, so tests stay deterministic).
        Deliberately NOT accepted: a slot id (the original slot is read off
        the locked appointment row itself - the browser has no time
        authority on this action), datetimes, status, source, patient data,
        or any tenant selector.

    Status / timestamp semantics (recon-anchored, not invented):
        - The restored appointment becomes CONFIRMED, not PENDING: an
          authorized office employee restoring an appointment is an
          explicit confirmation action, exactly the D5 rationale that makes
          a staff BOOKING confirmed.
        - confirmed_at is NEVER written here. booking_service.
          confirm_appointment remains its ONLY writer (the D5 invariant);
          whatever value the appointment carried at cancellation - NULL or
          a prior staff-confirmation instant - is preserved byte-for-byte.
        - start/end datetimes are RE-COPIED from the LOCKED slot row, so
          the appointment's justified copies always match the slot it
          occupies again.
        - patient fields, new_or_returning, reason, urgency, source,
          conversation_id, internal_note, and every notification field are
          untouched.

    Returns: BookingResult. Reason vocabulary (existing words wherever they
        are honestly equivalent; see each):
        ok                  - the appointment is CONFIRMED and its original
                              slot is BOOKED again, committed together.
        appointment_missing - no appointment with this id FOR THIS CLIENT
                              (unknown and cross-tenant indistinguishable,
                              Rule 15 - the confirm-path word).
        not_restorable      - status is not CANCELLED. detail carries the
                              SANITIZED current status (correction pass 1),
                              so a live, completed, or malformed row is
                              refused without mutation.
        conversation_conflict - Slice 4C's ONE new refusal family: the
                              appointment was created by a chat
                              conversation, and that conversation has since
                              produced ANOTHER active appointment, so
                              restoring this one would give the same
                              conversation two active appointments.
                              Pre-checked through the existing
                              get_appointment_by_conversation read and
                              backstopped by uq_active_appointment_per_
                              conversation at commit (restoring re-enters
                              that partial index's predicate).
        slot_missing        - the original slot row no longer exists for
                              this client (fail closed - nothing is
                              restored onto a vanished slot).
        slot_taken / slot_blocked / slot_started - the original slot is no
                              longer bookable, judged by _judge_slot_
                              bookable under the lock: someone else booked
                              or actively holds it, staff blocked or
                              cancelled it, or its start time has passed.
                              The office is directed to Choose Another
                              Time instead; nothing is mutated.

    Database effects: ONE transaction. On success: appointment status ->
        confirmed with start/end re-copied, AND the original slot -> booked
        with hold bookkeeping cleared (an ACTIVE hold cannot reach that
        line; only expired-hold residue is cleared), committed together.
        EVERY refusal path rolls back before returning - no partial
        restore, no half-cleared hold, no open transaction holding locks.
    External effects: none (no notification of any kind - D7 unchanged).
    Concurrency: get_appointment_for_update serializes concurrent restores
        and restore-vs-cancel/confirm on the appointment row; the slot lock
        serializes restore against every other slot claimant (staff
        booking, chatbot hold/finalize, block). Lock order appointment ->
        slot is the cancel path's order. If a concurrent claimant slips
        past the pre-checks, uq_active_appointment_per_slot rejects the
        loser at commit and the IntegrityError maps to slot_taken - at most
        one active appointment ever occupies the slot.
    """
    try:
        appointment = appointment_repository.get_appointment_for_update(
            db, client_id, appointment_id
        )
        if appointment is None:
            db.rollback()
            return BookingResult(False, "appointment_missing")
        if appointment.status not in _RESTORABLE_STATUSES:
            # Only a CANCELLED appointment may be restored; a live row must
            # never be "restored" over itself and a terminal row must never
            # come back to life through this action (Rule 14).
            db.rollback()
            return BookingResult(
                False, "not_restorable",
                appointment=appointment,
                detail=_sanitize_status_detail(appointment.status),
            )

        if appointment.conversation_id is not None:
            # Pre-check the conversation invariant with the EXISTING owner
            # read (fast path); the migration-002 partial unique index
            # remains the guarantee at commit. Our own row cannot match:
            # it is CANCELLED and the read filters status != cancelled.
            existing = appointment_repository.get_appointment_by_conversation(
                db, client_id, appointment.conversation_id
            )
            if existing is not None:
                db.rollback()
                return BookingResult(False, "conversation_conflict")

        slot = appointment_repository.get_slot_for_update(
            db, client_id, appointment.slot_id
        )
        if slot is None:
            db.rollback()
            return BookingResult(False, "slot_missing")

        refusal = _judge_slot_bookable(slot, now_utc)
        if refusal is not None:
            db.rollback()
            return BookingResult(False, refusal)

        # The single supported recovery transition: cancelled -> confirmed,
        # with the appointment's justified time copies re-synced to the
        # LOCKED slot row it occupies again. confirmed_at is deliberately
        # NOT written (confirm_appointment stays its only writer - D5).
        appointment.status = AppointmentStatus.CONFIRMED
        appointment.start_datetime = slot.start_datetime
        appointment.end_datetime = slot.end_datetime

        slot.status = SlotStatus.BOOKED
        # Clear any EXPIRED hold bookkeeping so a booked slot never carries
        # a stale owner. An ACTIVE hold could not have reached this line.
        slot.held_until = None
        slot.held_by_conversation_id = None

        db.commit()
        return BookingResult(True, "ok", appointment=appointment)
    except IntegrityError as integrity_error:
        # EXPECTED in exactly one family of situations: a concurrent claim
        # raced past the pre-checks and a migration-002 partial unique index
        # rejected the loser at commit. Map ONLY those; anything else is a
        # real bug and must propagate (Rule 16).
        db.rollback()  # Releases the row locks; nothing was persisted.
        violated_index = _classify_booking_unique_violation(integrity_error)
        if violated_index == _SLOT_UNIQUE_INDEX:
            # Another active appointment owns the original slot now.
            return BookingResult(False, "slot_taken")
        if violated_index == _CONVERSATION_UNIQUE_INDEX:
            # The originating conversation re-booked concurrently; restoring
            # would give it two active appointments. Same refusal as the
            # pre-check - the database simply arbitrated the race.
            return BookingResult(False, "conversation_conflict")
        raise
    except Exception:
        db.rollback()  # No partial completion, ever (Rule 16).
        raise


def _move_appointment_to_slot(
    db: Session,
    client_id: uuid.UUID,
    appointment_id: uuid.UUID,
    target_slot_id: uuid.UUID,
    *,
    now_utc: datetime,
    cancelled_mode: bool,
) -> BookingResult:
    """
    Purpose: PHASE 3A Slice 4C - the SHARED move engine behind the two
        MODE-PINNED public commands (v1.0.1 correction F1:
        reschedule_appointment and restore_appointment_to_slot below).
        The office moves ONE of its own appointments onto a DIFFERENT
        real authoritative slot, in ONE atomic operation:
          - an ACTIVE appointment (pending or confirmed): "Change time" -
            the old slot is released, the target slot is claimed, and the
            appointment's STATUS IS PRESERVED (a pending appointment stays
            pending; rescheduling is not a confirmation).
          - a CANCELLED appointment: "Choose another time" - the
            appointment is restored AND moved in the SAME transaction,
            ending CONFIRMED (the restore_appointment rationale: an office
            employee explicitly bringing it back is a confirmation
            action). The old slot is NOT touched: cancellation already
            released it, and whatever happened to it since is not this
            action's business.
        There is never a book-first / release-old-second pair of requests:
        the whole move commits or nothing does.

    MODE PIN (v1.0.1 correction F1): which of the two behaviors runs is
        chosen by the CALLER-OWNED cancelled_mode flag - the server-side
        identity of the command the user actually issued - and is NEVER
        re-derived from the appointment's current status. The
        starting-status requirement is enforced AFTER the appointment row
        lock is acquired:
          - cancelled_mode=False ("Change time"): legal starting status
            is pending or confirmed ONLY. A row found CANCELLED under the
            lock is refused not_reschedulable - a stale Change-time
            request must NEVER resurrect a cancellation that committed
            first.
          - cancelled_mode=True ("Choose another time"): legal starting
            status is CANCELLED only. A row found active or terminal
            under the lock is refused not_restorable - a stale
            cancelled-recovery request must never silently become an
            ordinary move, nor touch a finished row.

    Inputs: ids (the appointment and the CHOSEN target slot - the ONLY
        scheduling authority a caller may supply), then KEYWORD-ONLY
        aware-UTC now (Patch 2C convention). Deliberately NOT accepted:
        datetimes, status, source, provider, service, urgency, patient
        data, or any tenant selector - all server-owned. The target slot
        must already exist FOR THIS TENANT; a cross-tenant or unknown id is
        slot_missing (indistinguishable, Rule 15).

    Provider/service compatibility (recon conclusion, stated rather than
        invented): appointments carry NO provider/service binding column,
        and the staff-booking owner decision D3 already books ANY of the
        tenant's slots without provider/service filtering - provider_name /
        service_key remain whatever the authoritative TARGET slot carries.
        Reschedule inherits exactly those rules; no new compatibility rule
        exists in the current data model to enforce, and none is invented
        here. The appointment's justified start/end copies are re-copied
        from the LOCKED target row.

    Returns: BookingResult:
        ok                  - the appointment occupies the target slot
                              (status per the table above), the target is
                              BOOKED, and - on the active path - the old
                              slot was released, committed together.
        appointment_missing - no appointment with this id FOR THIS CLIENT.
        not_reschedulable   - active mode only: the LOCKED status is not
                              pending/confirmed (a cancelled, completed,
                              no_show, or malformed row - the F1 stale-
                              command refusal included). detail carries
                              the SANITIZED status.
        not_restorable      - cancelled mode only: the LOCKED status is
                              not CANCELLED (the restore_appointment
                              refusal word - same meaning, same route
                              mapping). detail carries the SANITIZED
                              status.
        same_slot           - an ACTIVE appointment's target IS its current
                              slot; there is nothing to move, and falling
                              through would dishonestly report the slot
                              "taken" by the appointment itself. (A
                              CANCELLED appointment choosing its own
                              original slot is NOT this refusal - that is
                              simply a restore, and the general path
                              handles it.)
        conversation_conflict - cancelled mode only: the originating chat
                              conversation has since produced another
                              active appointment (see restore_appointment;
                              same pre-check, same index backstop).
        slot_missing        - no target slot with this id FOR THIS CLIENT.
        slot_taken / slot_blocked / slot_started - the target slot is not
                              bookable, judged by _judge_slot_bookable
                              UNDER THE LOCK at mutation time; a slot that
                              became occupied between UI selection and
                              submit is refused safely with NO partial
                              move (the appointment stays exactly where -
                              and as - it was).

    Database effects: ONE transaction. On success (active path): old slot
        -> available with hold fields cleared WHEN it is currently booked
        (a DRIFTED old slot is left exactly as-is - the cancel path's C7
        pin), appointment slot_id/start/end re-pointed to the LOCKED
        target row, target slot -> booked with hold bookkeeping cleared -
        all committed together. On success (cancelled path): the same,
        minus any old-slot touch, plus status -> confirmed (confirmed_at
        untouched; confirm_appointment stays its only writer). EVERY
        refusal path rolls back before returning.
    External effects: none (no notification of any kind).
    Concurrency: the appointment row lock serializes reschedule against
        confirm/cancel/restore/reschedule on the same appointment. Slot
        locks: the active path locks the OLD and TARGET slots in ascending
        UUID order (the module lock-order rule above) so opposite-direction
        moves over the same pair cannot deadlock; the cancelled path locks
        only the target. Two concurrent reschedules racing for the SAME
        target serialize on its row lock; the loser observes BOOKED and is
        refused slot_taken - and uq_active_appointment_per_slot remains
        the final arbiter for any interleaving the pre-checks cannot see,
        so at most one winner ever holds the target.
    """
    try:
        appointment = appointment_repository.get_appointment_for_update(
            db, client_id, appointment_id
        )
        if appointment is None:
            db.rollback()
            return BookingResult(False, "appointment_missing")

        # --- MODE PIN (v1.0.1 F1): enforce the command's legal starting
        # status UNDER THE ROW LOCK. The mode is the caller-owned identity
        # of the command the user issued; the locked status either matches
        # it, or the stale command is refused - the engine never
        # reinterprets one command as the other.
        if cancelled_mode:
            if appointment.status != AppointmentStatus.CANCELLED:
                # Stale Choose-another-time: the row was restored,
                # confirmed, or finished after the user last saw it. No
                # second move, no touch of a live or finished row.
                db.rollback()
                return BookingResult(
                    False, "not_restorable",
                    appointment=appointment,
                    detail=_sanitize_status_detail(appointment.status),
                )
        elif appointment.status not in _RESCHEDULABLE_ACTIVE_STATUSES:
            # Stale (or illegal) Change-time: cancelled, completed,
            # no_show, or malformed. A finished appointment must never be
            # rewritten (Rule 14), rejection is the default for unknown
            # statuses (Rule 4), and a CANCELLED row must never be
            # resurrected by a Change-time command that lost the race to
            # a concurrent Cancel.
            db.rollback()
            return BookingResult(
                False, "not_reschedulable",
                appointment=appointment,
                detail=_sanitize_status_detail(appointment.status),
            )
        if not cancelled_mode and target_slot_id == appointment.slot_id:
            # Moving an active appointment onto its own slot is not a move.
            # Refusing here is honest; the general checks below would see
            # the slot BOOKED (by this very appointment) and claim it
            # "taken", which would be a misleading map of the condition.
            db.rollback()
            return BookingResult(
                False, "same_slot", appointment=appointment
            )

        if cancelled_mode and appointment.conversation_id is not None:
            # The cancelled path re-enters uq_active_appointment_per_
            # conversation's predicate; pre-check with the existing owner
            # read (the restore_appointment rule - one rationale, stated
            # there). The index remains the guarantee at commit.
            existing = appointment_repository.get_appointment_by_conversation(
                db, client_id, appointment.conversation_id
            )
            if existing is not None:
                db.rollback()
                return BookingResult(False, "conversation_conflict")

        # --- slot locks, in the module's deterministic order -------------
        old_slot = None
        if cancelled_mode:
            # Cancellation already released the original slot; whatever
            # state it is in now is deliberately not touched or judged.
            target_slot = appointment_repository.get_slot_for_update(
                db, client_id, target_slot_id
            )
        else:
            # Active move: BOTH slots are locked, ascending UUID order
            # (see the module lock-order comment). same_slot was refused
            # above, so the two ids are distinct here.
            first_id, second_id = sorted([appointment.slot_id,
                                          target_slot_id])
            first = appointment_repository.get_slot_for_update(
                db, client_id, first_id
            )
            second = appointment_repository.get_slot_for_update(
                db, client_id, second_id
            )
            if first_id == target_slot_id:
                target_slot, old_slot = first, second
            else:
                target_slot, old_slot = second, first

        if target_slot is None:
            db.rollback()
            return BookingResult(False, "slot_missing")

        refusal = _judge_slot_bookable(target_slot, now_utc)
        if refusal is not None:
            db.rollback()
            return BookingResult(False, refusal)

        # --- the atomic move ---------------------------------------------
        if old_slot is not None and old_slot.status == SlotStatus.BOOKED:
            # Release the vacated slot EXACTLY as cancellation does: only
            # when it is currently booked; a drifted slot (blocked,
            # cancelled, or already re-purposed) is left untouched (C7 -
            # no repair, no coercion), and the appointment still moves.
            old_slot.status = SlotStatus.AVAILABLE
            old_slot.held_until = None
            old_slot.held_by_conversation_id = None

        appointment.slot_id = target_slot.id
        # Re-sync the justified time copies to the LOCKED target row.
        appointment.start_datetime = target_slot.start_datetime
        appointment.end_datetime = target_slot.end_datetime
        if cancelled_mode:
            # Choose Another Time restores as part of the SAME atomic
            # operation - the restore_appointment status rationale.
            # confirmed_at is deliberately not written (D5 invariant).
            appointment.status = AppointmentStatus.CONFIRMED

        target_slot.status = SlotStatus.BOOKED
        target_slot.held_until = None
        target_slot.held_by_conversation_id = None

        db.commit()
        return BookingResult(True, "ok", appointment=appointment)
    except IntegrityError as integrity_error:
        db.rollback()  # Releases the row locks; nothing was persisted.
        violated_index = _classify_booking_unique_violation(integrity_error)
        if violated_index == _SLOT_UNIQUE_INDEX:
            # A concurrent claimant won the target slot at commit time.
            return BookingResult(False, "slot_taken")
        if violated_index == _CONVERSATION_UNIQUE_INDEX and cancelled_mode:
            # Only the cancelled path re-enters that index's predicate; the
            # database arbitrated the same race the pre-check guards.
            return BookingResult(False, "conversation_conflict")
        # An active-path conversation violation (the row already counted
        # once and neither conversation_id nor status changed) - or any
        # unknown constraint - would mean a broken invariant: surface it
        # loudly rather than absorbing it into a polite refusal (Rule 16).
        raise
    except Exception:
        db.rollback()
        raise


def reschedule_appointment(
    db: Session,
    client_id: uuid.UUID,
    appointment_id: uuid.UUID,
    target_slot_id: uuid.UUID,
    *,
    now_utc: datetime,
) -> BookingResult:
    """
    Purpose: "Change time" - move an ACTIVE (pending or confirmed)
        appointment onto a different real slot, preserving its status.
        MODE-PINNED public command (v1.0.1 correction F1): this entry
        point can ONLY perform the active move. If the appointment turns
        out to be CANCELLED under the row lock - a concurrent Cancel
        committed after the user last saw the row - the stale command is
        refused not_reschedulable and NOTHING is resurrected. Cancelled
        recovery is a DIFFERENT command (restore_appointment_to_slot).

    Inputs / Returns / Database effects / Concurrency: the shared
        engine's contract (_move_appointment_to_slot, cancelled_mode=
        False) - one engine, so locking, slot judgement, unique-index
        arbitration, and transaction rules exist exactly once (Rule 3).
    External effects: none (no notification of any kind).
    """
    return _move_appointment_to_slot(
        db, client_id, appointment_id, target_slot_id,
        now_utc=now_utc, cancelled_mode=False,
    )


def restore_appointment_to_slot(
    db: Session,
    client_id: uuid.UUID,
    appointment_id: uuid.UUID,
    target_slot_id: uuid.UUID,
    *,
    now_utc: datetime,
) -> BookingResult:
    """
    Purpose: "Choose another time" - restore a CANCELLED appointment AND
        move it onto a chosen different real slot in ONE atomic
        transaction, ending CONFIRMED (confirmed_at is never written;
        confirm_appointment stays its only writer). MODE-PINNED public
        command (v1.0.1 correction F1): this entry point can ONLY perform
        the cancelled recovery. If the appointment turns out NOT to be
        cancelled under the row lock - a concurrent Restore Original Time
        (or Confirm) committed first - the stale command is refused
        not_restorable and no second move ever happens.

    Inputs / Returns / Database effects / Concurrency: the shared
        engine's contract (_move_appointment_to_slot, cancelled_mode=
        True) - one engine, so locking, slot judgement, unique-index
        arbitration, and transaction rules exist exactly once (Rule 3).
    External effects: none (no notification of any kind).
    """
    return _move_appointment_to_slot(
        db, client_id, appointment_id, target_slot_id,
        now_utc=now_utc, cancelled_mode=True,
    )
