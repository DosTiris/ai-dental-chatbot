# app/routes/portal_staff_booking.py
#
# OWNER OF: the HTTP surface for the authenticated Office Portal STAFF
# BOOKING slice (Phase 3A Slice 2). This file is transport/auth wiring ONLY -
# it binds the verified tenant, validates the strict request body at the
# transport layer, injects the booking instant, delegates the ENTIRE booking
# rule to the frozen Slice 1 single owner
# (app.services.booking_service.finalize_staff_booking), maps that owner's
# BookingResult onto HTTP, and shapes the ONE approved response view. It
# contains no slot, hold, policy, or state rule of its own (Rule 2/3) and it
# never creates a second booking pathway: the SAME row lock, INSERT
# primitive, hold definition, and unique-index arbiter the chatbot path uses
# are reached through that one service function.
#
# Endpoint (POST, requires Authorization: Bearer <Supabase access token>):
#   POST /portal/schedule/slots/{slot_id}/book   book ONE authoritative slot
#                                                 on behalf of office staff
#
# TENANT BINDING (Rule 15): authentication and tenant resolution are REUSED,
# unchanged, from the frozen P2 owners - require_portal_identity
# (app/routes/portal.py) -> portal_auth.authenticate_portal_request. The
# verified credential ALONE determines the tenant (identity.client). This
# surface declares NO client_id, client_key, or any other tenant selector;
# the strict request model below additionally REJECTS any undeclared body
# field with 422, so a smuggled client_id/status/source/provider/service/
# datetime/urgency key can never be silently ignored (the PublishDayRequest /
# contract SS5-B convention). An unknown slot id and another office's slot id
# are indistinguishable (404 with the EXACT portal-schedule wording), because
# the service's tenant-filtered locked read cannot see foreign rows.
#
# BOOKING AUTHORITY: the browser supplies ONLY the slot_id path segment plus
# patient-entered contact fields. Everything else is server-owned inside the
# frozen service: conversation_id (always NULL), source (always
# "portal_staff"), status (always CONFIRMED), start/end datetimes (copied off
# the LOCKED authoritative row), and client_id (the verified identity),
# plus - at THIS layer - the appointment's urgency (STAFF_DEFAULT_URGENCY):
# the browser has no urgency authority (v1.0.1 audit correction F1). A
# datetime is never accepted as an alternative booking authority.
#
# RESPONSE PROJECTION: a successful booking returns the SAME leak-safe
# PortalAppointmentView the portal read GET and the P5-A action routes
# return, built through the ONE public projection owner
# build_portal_appointment_view (app/routes/portal_appointments.py, Rule 3).
# No slot, conversation, tenant id, raw notify_error, or per-channel boolean
# ever leaves this surface.
#
# NOTIFICATIONS: none. finalize_staff_booking performs NO office or patient
# notification on any path (Slice 1 owner decision D7), and this route adds
# none: it imports no notification code. Patient SMS remains disabled.
#
# FAIL-CLOSED (the P5-A guardrails G1/G2): route success is NOT a broad
# "anything else -> 200". Only the explicitly enumerated success reason may
# reach the response projection, and a success result MUST carry an
# appointment row. Any other BookingResult - an unexpected reason, or an
# impossible success-without-appointment - fails closed with the established
# generic internal-error status, never exposing the raw reason and never
# fabricating a malformed success body.

import uuid
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

# Reused P2 owners: the per-request session factory and the ONE portal
# identity dependency. Importing the SAME callables (not copies) keeps this
# router covered by any dependency override applied to the portal router in
# tests, and keeps portal_auth the single authentication owner.
from app.routes.portal import get_db, require_portal_identity
from app.services.portal_auth import PortalIdentity
# The frozen Slice 1 booking owner. This route adds no rule of its own.
from app.services import booking_service
# The ONE public portal-appointment projection owner, shared with the read
# GET and the P5-A action routes (Rule 3): a success response is built
# through the exact same field-by-field, leak-safe builder, so the booking,
# action, and read surfaces can never drift.
from app.routes.portal_appointments import (
    PortalAppointmentView,
    build_portal_appointment_view,
)

router = APIRouter(prefix="/portal", tags=["office-portal"])

# G1: the COMPLETE set of BookingResult.reason values that legitimately mean
# "the staff booking exists" - the ONLY reason allowed to reach the success
# projection. finalize_staff_booking has no idempotent-success second word
# (unlike confirm's already_confirmed), so the set is exactly {"ok"}.
# Anything outside it fails closed.
_BOOK_SUCCESS_REASONS = frozenset({"ok"})

# The single generic internal-error detail (the P5-A / demo route convention
# - Rule 3 for the internal-error boundary). It carries NO diagnostic
# vocabulary and never echoes an unexpected BookingResult.reason to the
# client (Rule 4/16).
UNEXPECTED_RESULT_DETAIL = "Unable to book slot."

# Route-level refusal wording, each in one reviewable place (Rule 4).
# SLOT_NOT_FOUND_DETAIL deliberately repeats the portal_schedule surface's
# EXACT words so the portal never learns a second not-found vocabulary, and
# so an unknown id and a foreign office's id stay indistinguishable.
SLOT_NOT_FOUND_DETAIL = "Slot not found."
# slot_taken deliberately covers booked, actively held, AND a lost concurrent
# race with ONE sentence: the service already collapses them (to the office
# they are the same outcome), and distinct wording would recreate the
# distinction - including leaking that a patient conversation is mid-booking.
SLOT_TAKEN_DETAIL = "Slot is no longer available."
SLOT_BLOCKED_DETAIL = "Slot is blocked and cannot be booked."
SLOT_STARTED_DETAIL = "Slot has already started and cannot be booked."
INVALID_PATIENT_DATA_DETAIL = "patient_name and patient_phone are required."

# The ONLY urgency this surface ever writes (v1.0.1 audit correction F1):
# the browser has NO urgency authority - the field is not declared on the
# strict request model, so a supplied one is rejected with 422 like every
# other undeclared key. "routine" is the word three existing owners already
# use (the appointments column default, the shared INSERT primitive's
# normalization, and the chatbot path's non-priority default); no
# request-validation urgency vocabulary exists yet, and this route does not
# invent one. finalize_staff_booking requires a str, so the route supplies
# exactly this server-owned value on every call (Rule 4: named once here).
STAFF_DEFAULT_URGENCY = "routine"


class StaffBookingRequest(BaseModel):
    """The staff-booking body - STRICT transport (the PublishDayRequest /
    contract SS5-B convention): any undeclared field is rejected with 422 by
    pydantic itself, so a misspelled or smuggled key (client_id, status,
    source, provider_*, service_*, start/end datetimes, conversation_id,
    confirmed_at, urgency, ...) can never be silently ignored. Only
    patient-entered
    contact fields are declared; blank-vs-valid name/phone is judged by the
    single validation owner (the shared INSERT primitive), never restated
    here (Rule 3). urgency is deliberately NOT declared (audit correction
    F1): it is server-owned - see STAFF_DEFAULT_URGENCY."""
    model_config = ConfigDict(extra="forbid")
    patient_name: str
    patient_phone: str
    patient_email: Optional[str] = None
    new_or_returning: Optional[str] = None
    reason: Optional[str] = None


def _success_view(result) -> PortalAppointmentView:
    """
    Purpose: The single fail-closed gate (guardrails G1/G2) between a
        BookingResult that reached none of the known refusal branches and the
        response projection. It is the ONLY place a 200 body is produced.
    Inputs: the BookingResult returned by finalize_staff_booking after the
        route has already mapped every KNOWN refusal reason to its 404/409/422.
    Returns: PortalAppointmentView built by the shared public projection owner.
    Failures: HTTPException(500, UNEXPECTED_RESULT_DETAIL) - fail closed -
        when the result is not the enumerated success (G1) or is an
        impossible success lacking an appointment (G2). The raw reason is
        never exposed.
    Database effects: none. External effects: none.
    """
    if not (result.success and result.reason in _BOOK_SUCCESS_REASONS):
        # G1: an unexpected reason/result is NEVER silently converted to a
        # success. Fail closed with the generic internal-error status.
        raise HTTPException(status_code=500, detail=UNEXPECTED_RESULT_DETAIL)
    if result.appointment is None:
        # G2: an allowed success MUST carry an appointment; an impossible
        # success-without-appointment must never produce a malformed body.
        raise HTTPException(status_code=500, detail=UNEXPECTED_RESULT_DETAIL)
    return build_portal_appointment_view(result.appointment)


@router.post("/schedule/slots/{slot_id}/book",
             response_model=PortalAppointmentView)
def portal_staff_book_slot(
    slot_id: uuid.UUID,
    body: StaffBookingRequest,
    identity: PortalIdentity = Depends(require_portal_identity),
    db: Session = Depends(get_db),
):
    """
    Purpose: An authenticated office employee books EXACTLY the authoritative
        slot named by slot_id for a patient who contacted the office directly.
        Every booking rule - the row lock, tenant-filtered read, status and
        active-hold refusals, the not-in-the-past rule, the shared INSERT,
        the CONFIRMED status, the server-owned "portal_staff" source, the
        NULL conversation, and the unique-index race arbiter - lives in the
        frozen Slice 1 owner (booking_service.finalize_staff_booking); this
        route only binds the tenant, injects the booking instant, and maps
        the BookingResult onto HTTP.
    Inputs: the Authorization header (consumed by require_portal_identity),
        the slot id path segment, and the strict patient-entered body. There
        is deliberately no tenant, provider, service, datetime, status,
        source, or urgency parameter to declare, so none can be honored: the verified
        identity ALONE selects the tenant, and the LOCKED slot row ALONE
        supplies the appointment's provider/service/times (Rule 15).
    Staff policy (Slice 1 owner decisions, unchanged): public minimum notice
        and maximum horizon are bypassed; a slot that has already started is
        refused; an ACTIVE chatbot hold is never stolen; an EXPIRED hold is
        lazily reclaimed exactly as the chatbot path already reclaims one.
    Returns: PortalAppointmentView (the leak-safe shared projection).
    Database effects: exactly the owner's ONE transaction (appointment INSERT
        + slot UPDATE to booked committed together on success; every refusal
        rolls back before returning). External effects: none - NO office or
        patient notification of any kind (D7).
    Possible failures: 404 SLOT_NOT_FOUND_DETAIL for an unknown or
        cross-tenant slot id (indistinguishable, Rule 15); 409 for a booked/
        actively-held slot (SLOT_TAKEN_DETAIL - including a lost concurrent
        race), a staff-blocked/cancelled slot (SLOT_BLOCKED_DETAIL), or a
        slot whose start time is not in the future (SLOT_STARTED_DETAIL);
        422 for blank patient name/phone (INVALID_PATIENT_DATA_DETAIL, judged
        by the single validation owner) and - via the strict model - for any
        undeclared body field; 401/503 as on every portal endpoint;
        HTTPException(500) fail-closed for an unexpected booking result
        (guardrails G1/G2); database errors propagate (fail closed, Rule 16).
    """
    result = booking_service.finalize_staff_booking(
        db, identity.client.id, slot_id,
        # The injected booking instant (the P5-A route convention): the
        # frozen service never reads the clock, so the transport layer
        # supplies the one real "now" used for the active-hold and
        # already-started judgments.
        now_utc=datetime.now(ZoneInfo("UTC")),
        patient_name=body.patient_name,
        patient_phone=body.patient_phone,
        patient_email=body.patient_email,
        new_or_returning=body.new_or_returning,
        reason=body.reason,
        # Server-owned, NEVER derived from the browser (v1.0.1 audit
        # correction F1) - the one established word (STAFF_DEFAULT_URGENCY).
        urgency=STAFF_DEFAULT_URGENCY,
    )
    if result.reason == "slot_missing":
        raise HTTPException(status_code=404, detail=SLOT_NOT_FOUND_DETAIL)
    if result.reason == "slot_taken":
        raise HTTPException(status_code=409, detail=SLOT_TAKEN_DETAIL)
    if result.reason == "slot_blocked":
        raise HTTPException(status_code=409, detail=SLOT_BLOCKED_DETAIL)
    if result.reason == "slot_started":
        raise HTTPException(status_code=409, detail=SLOT_STARTED_DETAIL)
    if result.reason == "invalid_patient_data":
        raise HTTPException(status_code=422,
                            detail=INVALID_PATIENT_DATA_DETAIL)
    return _success_view(result)
