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
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

# Reused P2/P3 owners: the per-request session factory and the ONE portal
# identity dependency. Importing the SAME callables (not copies) keeps this
# router covered by any dependency override applied to the portal router in
# tests, and keeps portal_auth the single authentication owner.
from app.routes.portal import get_db, require_portal_identity
from app.services.portal_auth import PortalIdentity
# The frozen appointment lifecycle owner. This route adds no rule of its own.
from app.services import booking_service
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
