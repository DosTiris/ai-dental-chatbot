# app/routes/portal_appointment_actions.py
#
# OWNER OF: the HTTP surface for the authenticated Office Portal appointment
# ACTIONS slice (P5-A - Portal Appointment Actions v1): Confirm and Cancel.
# Like every other /portal route this file is transport/auth wiring ONLY - it
# validates the path input, binds the verified tenant, delegates the ENTIRE
# lifecycle rule to the frozen single owner (app.services.booking_service),
# maps that owner's BookingResult onto HTTP, and shapes the ONE approved
# response view. It contains no availability, slot, or state-transition rule
# of its own (Rule 2/3) and it never creates a second appointment state
# machine: the SAME booking_service.confirm_appointment /
# cancel_appointment the internal admin Calendar route calls are reused here,
# so office actions taken through the portal and through the admin surface
# share one lifecycle owner and one pessimistic FOR UPDATE serialization.
#
# Endpoints (POST, require Authorization: Bearer <Supabase access token>):
#   POST /portal/appointments/{appointment_id}/confirm   pending -> confirmed
#   POST /portal/appointments/{appointment_id}/cancel     -> cancelled + slot
#
# TENANT BINDING: authentication and tenant resolution are REUSED, unchanged,
# from the frozen P2/P3 owners - require_portal_identity (app/routes/portal.py)
# -> portal_auth.authenticate_portal_request. The verified credential ALONE
# determines the tenant (identity.client). This surface declares NO client_id,
# client_key, or any other tenant selector; an unknown or another office's
# appointment id is indistinguishable (404 "Appointment not found."), exactly
# as the lifecycle owner already enforces (Rule 15).
#
# RESPONSE PROJECTION: a successful action returns the SAME leak-safe
# PortalAppointmentView the read GET returns, built through the ONE public
# projection owner build_portal_appointment_view (app/routes/portal_appointments
# .py). No per-channel notification booleans, no raw notify_error, no slot,
# conversation, or tenant id ever leaves this surface.
#
# NOTIFICATIONS: none. booking_service.confirm_appointment and
# cancel_appointment perform NO office or patient notification on any path,
# and P5-A adds none. Patient SMS remains disabled.
#
# FAIL-CLOSED (guardrails G1/G2): route success is NOT a broad "anything
# else -> 200". Only the explicitly enumerated success reasons may reach the
# response projection, and a success result MUST carry an appointment row.
# Any other BookingResult - an unexpected reason, or an impossible
# success-without-appointment - fails closed with the established generic
# internal-error status (the demo route convention), never exposing the raw
# reason and never fabricating a malformed success body.

import uuid
from typing import Optional
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

# Reused P2/P3 owners: the per-request session factory and the ONE portal
# identity dependency. Importing the SAME callables (not copies) keeps this
# router covered by any dependency override applied to the portal router in
# tests, and keeps portal_auth the single authentication owner.
from app.routes.portal import get_db, require_portal_identity
from app.services.portal_auth import PortalIdentity
# The frozen appointment lifecycle owner. This route adds no rule of its own.
from app.services import appointment_note_service, booking_service
from app.services.appointment_note_service import INVALID_INTERNAL_NOTE_DETAIL
# The ONE public portal-appointment projection owner, shared with the read
# GET (Rule 3): a success response is built through the exact same field-by-
# field, leak-safe builder, so the action and read surfaces can never drift.
from app.routes.portal_appointments import (
    PortalAppointmentView,
    build_portal_appointment_view,
)

router = APIRouter(prefix="/portal", tags=["office-portal"])

# G1: the COMPLETE set of BookingResult.reason values that legitimately mean
# "the appointment is in the intended state" for each action - the ONLY
# reasons allowed to reach the success projection. Confirm is idempotent, so
# both a fresh "ok" and an already-CONFIRMED "already_confirmed" are success;
# Cancel's idempotent repeat is the frozen 409 already_cancelled refusal (D1),
# so ONLY "ok" is a cancel success. Anything outside these sets fails closed.
_CONFIRM_SUCCESS_REASONS = frozenset({"ok", "already_confirmed"})
_CANCEL_SUCCESS_REASONS = frozenset({"ok"})

# The single generic internal-error detail (the demo route convention - Rule 3
# for the internal-error boundary). It carries NO diagnostic vocabulary and
# never echoes an unexpected BookingResult.reason to the client (Rule 4/16).
UNEXPECTED_RESULT_DETAIL = "Unable to update appointment."


def _success_view(result, allowed_reasons):
    """
    Purpose: The single fail-closed gate (guardrails G1/G2) between a
        BookingResult that reached neither known refusal branch and the
        response projection. It is the ONLY place a 200 body is produced.
    Inputs:
        result: the BookingResult returned by the lifecycle owner after the
            route has already mapped every KNOWN refusal reason to its 404/409.
        allowed_reasons: the action's enumerated success reason set (G1).
    Returns: PortalAppointmentView built by the shared public projection owner.
    Failures: HTTPException(500, UNEXPECTED_RESULT_DETAIL) - fail closed -
        when the result is not an enumerated success (G1) or is an impossible
        success lacking an appointment (G2). The raw reason is never exposed.
    Database effects: none. External effects: none.
    """
    if not (result.success and result.reason in allowed_reasons):
        # G1: an unexpected reason/result is NEVER silently converted to a
        # success. Fail closed with the generic internal-error status.
        raise HTTPException(status_code=500, detail=UNEXPECTED_RESULT_DETAIL)
    if result.appointment is None:
        # G2: an allowed success MUST carry an appointment; an impossible
        # success-without-appointment must never produce a malformed body.
        raise HTTPException(status_code=500, detail=UNEXPECTED_RESULT_DETAIL)
    return build_portal_appointment_view(result.appointment)


@router.post("/appointments/{appointment_id}/confirm",
             response_model=PortalAppointmentView)
def portal_confirm_appointment(
    appointment_id: uuid.UUID,
    identity: PortalIdentity = Depends(require_portal_identity),
    db: Session = Depends(get_db),
):
    """
    Purpose: The authenticated office confirms ONE of its own appointments -
        the supported pending -> confirmed transition. The rule lives in the
        frozen lifecycle owner (booking_service.confirm_appointment); this
        route only binds the tenant, injects the confirmation instant, and
        maps the BookingResult onto HTTP.
    Inputs: the Authorization header (consumed by require_portal_identity) and
        the appointment id path segment. There is deliberately no tenant
        parameter to declare; the verified identity ALONE selects the tenant.
    Behavior: 200 for a fresh confirmation AND for re-confirming an
        already-confirmed appointment (idempotent success; confirmed_at is
        preserved by the owner). No notification is sent (office staff are
        acting; patient SMS stays disabled).
    Returns: PortalAppointmentView (the leak-safe shared projection).
    Database effects: exactly the lifecycle owner's one transaction (a single
        pending -> confirmed UPDATE on the allowed path; every other path
        rolls back). The slot is never touched.
    Possible failures: 404 "Appointment not found." for an unknown or
        cross-tenant id (indistinguishable, Rule 15); 409 when the appointment
        is cancelled/completed/no_show and cannot be confirmed (the owner's
        sanitized status word); 401/503 as on every portal endpoint;
        HTTPException(500) fail-closed for an unexpected lifecycle result
        (guardrails G1/G2); database errors propagate (fail closed, Rule 16).
    """
    result = booking_service.confirm_appointment(
        db, identity.client.id, appointment_id,
        now_utc=datetime.now(ZoneInfo("UTC")),
    )
    if result.reason == "appointment_missing":
        raise HTTPException(status_code=404, detail="Appointment not found.")
    if result.reason == "not_confirmable":
        raise HTTPException(
            status_code=409,
            detail=f"Appointment is {result.detail} and cannot be confirmed.",
        )
    return _success_view(result, _CONFIRM_SUCCESS_REASONS)


@router.post("/appointments/{appointment_id}/cancel",
             response_model=PortalAppointmentView)
def portal_cancel_appointment(
    appointment_id: uuid.UUID,
    identity: PortalIdentity = Depends(require_portal_identity),
    db: Session = Depends(get_db),
):
    """
    Purpose: The authenticated office cancels ONE of its own appointments.
        The rule - including freeing the linked slot ONLY when it is currently
        booked, and leaving a drifted slot untouched - lives in the frozen
        lifecycle owner (booking_service.cancel_appointment); this route only
        binds the tenant and maps the BookingResult onto HTTP.
    Inputs: the Authorization header (dependency) and the appointment id path
        segment. The verified identity ALONE selects the tenant.
    Returns: PortalAppointmentView (the leak-safe shared projection).
    Database effects: exactly the lifecycle owner's one transaction (on the
        allowed path: appointment -> cancelled AND, when currently booked, its
        slot freed to available with hold fields cleared, committed together;
        every rejection path rolls back). No notification is sent.
    Possible failures: 404 "Appointment not found." for an unknown or
        cross-tenant id (indistinguishable, Rule 15); 409 "Appointment is
        already cancelled." (frozen idempotent refusal, decision D1); 409 for
        a completed/no_show terminal appointment (the owner's sanitized status
        word); 401/503 as on every portal endpoint; HTTPException(500)
        fail-closed for an unexpected lifecycle result (guardrails G1/G2);
        database errors propagate (fail closed, Rule 16).
    """
    result = booking_service.cancel_appointment(
        db, identity.client.id, appointment_id
    )
    if result.reason == "slot_missing":
        raise HTTPException(status_code=404, detail="Appointment not found.")
    if result.reason == "already_cancelled":
        raise HTTPException(status_code=409,
                            detail="Appointment is already cancelled.")
    if result.reason == "not_cancellable":
        raise HTTPException(
            status_code=409,
            detail=f"Appointment is {result.detail} and cannot be cancelled.",
        )
    return _success_view(result, _CANCEL_SUCCESS_REASONS)


# --------------------------------------------------------------------------
# PHASE 3A Slice 4B1: office-internal note edit/clear
# --------------------------------------------------------------------------

_NOTE_SUCCESS_REASONS = frozenset({"ok"})


class InternalNoteUpdateRequest(BaseModel):
    """The note-edit body - STRICT transport (the StaffBookingRequest /
    PublishDayRequest convention): any undeclared field is rejected with 422
    by pydantic itself, so a smuggled client_id/status/source/slot/timestamp
    key can never be silently ignored. Exactly ONE field is declared, and it
    is REQUIRED-BUT-NULLABLE (v1.0.1 audit correction F1): a string replaces
    the note, an EXPLICIT null clears it, and blank/whitespace normalizes to
    null (clears) - but an ABSENT field is a 422 and mutates NOTHING. An
    office note is stored data; erasing it must always be a stated intent,
    never the side effect of an accidentally empty PUT. Normalization and
    the 2000-character limit are owned by appointment_note_service - never
    restated here (Rule 3)."""
    model_config = ConfigDict(extra="forbid")
    internal_note: Optional[str]


@router.put("/appointments/{appointment_id}/internal-note",
            response_model=PortalAppointmentView)
def portal_set_internal_note(
    appointment_id: uuid.UUID,
    payload: InternalNoteUpdateRequest,
    identity: PortalIdentity = Depends(require_portal_identity),
    db: Session = Depends(get_db),
):
    """
    Purpose (Slice 4B1): the authenticated office replaces or clears the
        office-internal note on ONE of its own appointments. The rule lives
        in the note owner (appointment_note_service.
        set_appointment_internal_note); this route only binds the tenant and
        maps the result onto HTTP - the confirm/cancel wiring convention.
    Inputs: the Authorization header (dependency), the appointment id path
        segment, and the strict one-field body. The verified identity ALONE
        selects the tenant; there is no tenant parameter to declare, and the
        strict model rejects a smuggled one with 422.
    Behavior: works for ANY appointment the office can see - any source
        (a Mia-created appointment may carry a private office note) and any
        status (a cancelled historical appointment may legitimately retain
        one). Editing a note changes NOTHING else: no status, slot, source,
        confirmed_at, notification state, hold, or availability - and no
        notification is sent (the owner imports no notification code).
    Returns: PortalAppointmentView (the leak-safe shared projection), so the
        caller sees the stored note exactly as normalized.
    Database effects: exactly the note owner's one transaction (a
        tenant-scoped SELECT ... FOR UPDATE - the same lock the cancel path
        uses, so a note edit and a concurrent lifecycle action serialize -
        then a single-column UPDATE, committed; every refusal rolls back).
    Possible failures: 404 "Appointment not found." for an unknown or
        cross-tenant id (indistinguishable, Rule 15 - the existing portal
        convention word-for-word); 422 with the note owner's single refusal
        sentence for an over-limit note (nothing mutated); 401/503 as on
        every portal endpoint; HTTPException(500) fail-closed for an
        unexpected result (guardrails G1/G2); database errors propagate
        (fail closed, Rule 16).
    """
    result = appointment_note_service.set_appointment_internal_note(
        db, identity.client.id, appointment_id,
        internal_note=payload.internal_note,
    )
    if result.reason == "appointment_missing":
        raise HTTPException(status_code=404, detail="Appointment not found.")
    if result.reason == "invalid_internal_note":
        raise HTTPException(status_code=422,
                            detail=INVALID_INTERNAL_NOTE_DETAIL)
    return _success_view(result, _NOTE_SUCCESS_REASONS)


# --------------------------------------------------------------------------
# PHASE 3A Slice 4C: cancelled-appointment recovery and rescheduling
# --------------------------------------------------------------------------
# Transport/auth wiring ONLY, exactly the confirm/cancel convention above:
# the verified identity alone binds the tenant, the ENTIRE lifecycle rule
# lives in the single lifecycle owner (booking_service.restore_appointment /
# reschedule_appointment), and a success is projected through the ONE shared
# leak-safe builder. The browser's ONLY scheduling authority on this surface
# is a real server slot_id (reschedule); restore accepts NO body at all -
# the original slot is read off the locked appointment row by the owner.

# The slot-refusal wording is IMPORTED from the staff-booking surface rather
# than restated (Rule 3): the target of a reschedule is judged by the SAME
# service rules as a staff booking, so the office reads the SAME sentences
# for the SAME slot states and the portal never learns a second vocabulary.
from app.routes.portal_staff_booking import (
    SLOT_NOT_FOUND_DETAIL,
    SLOT_TAKEN_DETAIL,
    SLOT_BLOCKED_DETAIL,
    SLOT_STARTED_DETAIL,
)

_RESTORE_SUCCESS_REASONS = frozenset({"ok"})
_RESCHEDULE_SUCCESS_REASONS = frozenset({"ok"})

# Route-level refusal wording, each in one reviewable place (Rule 4).
# RESTORE deliberately collapses every original-slot refusal - taken,
# blocked, started, and a vanished slot row - into ONE sentence: the office
# clicked "Restore original time", not a picked slot, so the only actionable
# fact is that the original time cannot be had and Choose Another Time is
# the path forward. Distinct wording would leak slot-state distinctions
# (including that a patient conversation holds it) with no office benefit.
RESTORE_SLOT_UNAVAILABLE_DETAIL = "Original time is no longer available."
CONVERSATION_CONFLICT_DETAIL = (
    "The patient's chat conversation already has an active appointment."
)
SAME_SLOT_DETAIL = "Appointment already has this time."


class RescheduleRequest(BaseModel):
    """The reschedule body - STRICT transport (the StaffBookingRequest /
    InternalNoteUpdateRequest convention): any undeclared field is rejected
    with 422 by pydantic itself, so a smuggled datetime / client_id /
    status / source / provider / service / urgency / patient key can never
    be silently ignored. Exactly ONE field is declared: the chosen REAL
    server slot_id - the only scheduling authority this surface accepts.
    Pixels and typed datetimes never become booking times."""
    model_config = ConfigDict(extra="forbid")
    slot_id: uuid.UUID


@router.post("/appointments/{appointment_id}/restore",
             response_model=PortalAppointmentView)
def portal_restore_appointment(
    appointment_id: uuid.UUID,
    identity: PortalIdentity = Depends(require_portal_identity),
    db: Session = Depends(get_db),
):
    """
    Purpose (Slice 4C): the authenticated office restores ONE of its own
        CANCELLED appointments back onto its ORIGINAL slot - the patient
        called back. The rule - including the mandatory re-verification of
        the original slot under the row locks - lives in the single
        lifecycle owner (booking_service.restore_appointment); this route
        only binds the tenant, injects the instant, and maps the
        BookingResult onto HTTP.
    Inputs: the Authorization header (dependency) and the appointment id
        path segment. NO body: the original slot is server-known, so the
        browser supplies no scheduling value of any kind. The verified
        identity ALONE selects the tenant (Rule 15).
    Returns: PortalAppointmentView (the leak-safe shared projection) - the
        restored appointment, now confirmed.
    Database effects: exactly the lifecycle owner's one transaction (on the
        allowed path: appointment cancelled -> confirmed with times
        re-synced AND its original slot -> booked, committed together;
        every refusal rolls back). No notification is sent on any path.
    Possible failures: 404 "Appointment not found." for an unknown or
        cross-tenant id (indistinguishable, Rule 15); 409 with the owner's
        sanitized status word when the appointment is not cancelled; 409
        RESTORE_SLOT_UNAVAILABLE_DETAIL when the original slot is taken,
        actively held, blocked, vanished, or already started - restoration
        refuses cleanly and the office chooses another time instead; 409
        CONVERSATION_CONFLICT_DETAIL when the originating chat conversation
        already has another active appointment; 401/503 as on every portal
        endpoint; HTTPException(500) fail-closed for an unexpected result
        (guardrails G1/G2); database errors propagate (Rule 16).
    """
    result = booking_service.restore_appointment(
        db, identity.client.id, appointment_id,
        now_utc=datetime.now(ZoneInfo("UTC")),
    )
    if result.reason == "appointment_missing":
        raise HTTPException(status_code=404, detail="Appointment not found.")
    if result.reason == "not_restorable":
        raise HTTPException(
            status_code=409,
            detail=f"Appointment is {result.detail} and cannot be restored.",
        )
    if result.reason == "conversation_conflict":
        raise HTTPException(status_code=409,
                            detail=CONVERSATION_CONFLICT_DETAIL)
    if result.reason in ("slot_missing", "slot_taken", "slot_blocked",
                         "slot_started"):
        raise HTTPException(status_code=409,
                            detail=RESTORE_SLOT_UNAVAILABLE_DETAIL)
    return _success_view(result, _RESTORE_SUCCESS_REASONS)


@router.post("/appointments/{appointment_id}/reschedule",
             response_model=PortalAppointmentView)
def portal_reschedule_appointment(
    appointment_id: uuid.UUID,
    payload: RescheduleRequest,
    identity: PortalIdentity = Depends(require_portal_identity),
    db: Session = Depends(get_db),
):
    """
    Purpose (Slice 4C, v1.0.1 mode pin F1): "Change time" - the
        authenticated office moves ONE of its own ACTIVE (pending or
        confirmed) appointments onto a DIFFERENT real authoritative slot
        in ONE atomic operation, preserving its status. This route is the
        ACTIVE-ONLY command: an appointment found CANCELLED under the row
        lock (a concurrent Cancel committed first) is refused 409 with
        the sanitized status word, so a stale Change-time request can
        NEVER resurrect a cancellation. Cancelled recovery is a DIFFERENT
        server-owned command with its own route (/restore-to-slot below);
        the browser never chooses a transition by sending a status - it
        only picks which command it issues. Both routes delegate into the
        SAME lifecycle engine (booking_service._move_appointment_to_slot)
        so the mode pin, deterministic slot lock order, the target
        re-check under lock, the guarded old-slot release, and the
        unique-index arbitration exist exactly once (Rule 3).
    Inputs: the Authorization header (dependency), the appointment id path
        segment, and the STRICT one-field body carrying only the chosen
        real target slot_id. The verified identity ALONE selects the
        tenant; the strict model rejects any smuggled datetime / tenant /
        status / source / provider / service / urgency / patient key with
        422 before any code here runs.
    Returns: PortalAppointmentView - the moved appointment at its new time.
        Patient fields, source, urgency, and internal_note are unchanged by
        construction (the owner never touches them).
    Database effects: exactly the lifecycle owner's one transaction; every
        refusal rolls back with the appointment exactly where - and as -
        it was (no partial move, ever).
    Possible failures: 404 "Appointment not found." (unknown/cross-tenant
        appointment); 404 SLOT_NOT_FOUND_DETAIL (unknown/cross-tenant
        target slot - the staff-booking wording verbatim); 409 with the
        owner's sanitized status word for a cancelled/completed/no_show/
        malformed appointment (the v1.0.1 mode pin - "cancelled" here
        means a stale Change-time request was refused, never converted);
        409 SAME_SLOT_DETAIL when an active appointment's
        target is its current slot; 409 SLOT_TAKEN_DETAIL /
        SLOT_BLOCKED_DETAIL / SLOT_STARTED_DETAIL when the target is not
        bookable (the staff-booking sentences verbatim - same rules, same
        words); 409 CONVERSATION_CONFLICT_DETAIL retained as a defensive
        mapping (the active mode cannot produce it; mapped rather than
        500 if it ever appears);
        422 for a malformed/extra-field body (pydantic, nothing mutated);
        401/503 as on every portal endpoint; HTTPException(500) fail-closed
        for an unexpected result (G1/G2); database errors propagate.
    """
    result = booking_service.reschedule_appointment(
        db, identity.client.id, appointment_id, payload.slot_id,
        now_utc=datetime.now(ZoneInfo("UTC")),
    )
    if result.reason == "appointment_missing":
        raise HTTPException(status_code=404, detail="Appointment not found.")
    if result.reason == "not_reschedulable":
        raise HTTPException(
            status_code=409,
            detail=(f"Appointment is {result.detail} "
                    "and cannot be rescheduled."),
        )
    if result.reason == "same_slot":
        raise HTTPException(status_code=409, detail=SAME_SLOT_DETAIL)
    if result.reason == "conversation_conflict":
        raise HTTPException(status_code=409,
                            detail=CONVERSATION_CONFLICT_DETAIL)
    if result.reason == "slot_missing":
        raise HTTPException(status_code=404, detail=SLOT_NOT_FOUND_DETAIL)
    if result.reason == "slot_taken":
        raise HTTPException(status_code=409, detail=SLOT_TAKEN_DETAIL)
    if result.reason == "slot_blocked":
        raise HTTPException(status_code=409, detail=SLOT_BLOCKED_DETAIL)
    if result.reason == "slot_started":
        raise HTTPException(status_code=409, detail=SLOT_STARTED_DETAIL)
    return _success_view(result, _RESCHEDULE_SUCCESS_REASONS)


# v1.0.1 (F1): the cancelled-recovery move is its OWN server-owned command.
_RESTORE_TO_SLOT_SUCCESS_REASONS = frozenset({"ok"})


@router.post("/appointments/{appointment_id}/restore-to-slot",
             response_model=PortalAppointmentView)
def portal_restore_appointment_to_slot(
    appointment_id: uuid.UUID,
    payload: RescheduleRequest,
    identity: PortalIdentity = Depends(require_portal_identity),
    db: Session = Depends(get_db),
):
    """
    Purpose (Slice 4C, v1.0.1 mode pin F1): "Choose another time" - the
        authenticated office restores ONE of its own CANCELLED
        appointments AND moves it onto a chosen DIFFERENT real slot in
        ONE atomic operation, ending CONFIRMED (confirmed_at never
        written). This route is the CANCELLED-ONLY command: an
        appointment found NOT cancelled under the row lock (a concurrent
        Restore Original Time or Confirm committed first) is refused 409
        with the sanitized status word, so a stale recovery request never
        silently becomes an ordinary move. The active move has its OWN
        route (/reschedule above); both delegate into the SAME lifecycle
        engine (booking_service._move_appointment_to_slot) so locking,
        slot judgement, unique-index arbitration, and transaction rules
        exist exactly once (Rule 3).
    Inputs: the Authorization header (dependency), the appointment id
        path segment, and the STRICT one-field body carrying only the
        chosen real target slot_id (the SAME RescheduleRequest model:
        extra="forbid" rejects every smuggled datetime / tenant / status /
        source / provider / service / urgency / patient key with 422
        before any code here runs; the browser owns nothing but which
        real slot it picked).
    Returns: PortalAppointmentView - the recovered appointment, CONFIRMED
        at its new time. Patient fields, source, urgency, internal_note,
        and confirmed_at are unchanged by construction.
    Database effects: exactly the lifecycle owner's one transaction;
        every refusal rolls back with the appointment exactly where - and
        as - it was (no partial recovery, ever).
    Possible failures: 404 "Appointment not found." (unknown/cross-tenant
        appointment); 404 SLOT_NOT_FOUND_DETAIL (unknown/cross-tenant
        target slot - the staff-booking wording verbatim); 409 with the
        owner's sanitized status word when the row is not cancelled under
        the lock (the mode pin refusal - a stale request against a
        restored, confirmed, or finished row); 409 SLOT_TAKEN_DETAIL /
        SLOT_BLOCKED_DETAIL / SLOT_STARTED_DETAIL when the target is not
        bookable (staff-booking sentences verbatim); 409
        CONVERSATION_CONFLICT_DETAIL when the originating chat
        conversation already has an active appointment; 422 for a
        malformed/extra-field body (pydantic, nothing mutated); 401/503
        as on every portal endpoint; HTTPException(500) fail-closed for
        an unexpected result (guardrails G1/G2); database errors
        propagate (Rule 16).
    """
    result = booking_service.restore_appointment_to_slot(
        db, identity.client.id, appointment_id, payload.slot_id,
        now_utc=datetime.now(ZoneInfo("UTC")),
    )
    if result.reason == "appointment_missing":
        raise HTTPException(status_code=404, detail="Appointment not found.")
    if result.reason == "not_restorable":
        raise HTTPException(
            status_code=409,
            detail=f"Appointment is {result.detail} and cannot be restored.",
        )
    if result.reason == "conversation_conflict":
        raise HTTPException(status_code=409,
                            detail=CONVERSATION_CONFLICT_DETAIL)
    if result.reason == "slot_missing":
        raise HTTPException(status_code=404, detail=SLOT_NOT_FOUND_DETAIL)
    if result.reason == "slot_taken":
        raise HTTPException(status_code=409, detail=SLOT_TAKEN_DETAIL)
    if result.reason == "slot_blocked":
        raise HTTPException(status_code=409, detail=SLOT_BLOCKED_DETAIL)
    if result.reason == "slot_started":
        raise HTTPException(status_code=409, detail=SLOT_STARTED_DETAIL)
    return _success_view(result, _RESTORE_TO_SLOT_SUCCESS_REASONS)
