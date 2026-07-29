# app/routes/calendar.py
#
# OWNER OF: the HTTP surface for staff calendar management. This file only
# validates input, checks authorization, and delegates — every rule lives in
# the services/repositories (Rule 2: no layered wiring in routes).
#
# Endpoints (all require the X-Admin-Key header carrying a PER-OFFICE
# Calendar admin key — Patch 5, Senior Audit Critical #2. The credential
# determines the authenticated tenant; the request's client_id must equal it,
# and the global ADMIN_API_KEY has no access here. Staff tooling, never the
# public widget):
#   POST   /admin/calendar/slots                 publish bookable slots
#   GET    /admin/calendar/slots                 list slots for a local day
#   POST   /admin/calendar/slots/{id}/block      remove a slot from booking
#   GET    /admin/calendar/appointments          list appointments in a range
#   POST   /admin/calendar/appointments/{id}/confirm  pending -> confirmed
#   POST   /admin/calendar/appointments/{id}/cancel   cancel + free the slot
#
# Times in requests/responses are ISO-8601. Requests may send local times
# WITH an offset ("2026-07-16T13:30:00-04:00") or UTC ("...T17:30:00Z");
# naive datetimes are REJECTED rather than guessed at (Rule 4).

import uuid
from datetime import date, datetime
from typing import List, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import Client
from app.calendar_models import SlotStatus
from app.services.calendar_admin_auth import authenticate_calendar_admin
# PATCH 6 (Senior Audit Recommended #7): notification_service is the single
# owner of the notify_error vocabulary; this route only applies its output
# gate so AppointmentView can never return an arbitrary stored value.
from app.services.notification_service import sanitize_stored_notify_error
from app.repositories import appointment_repository
from app.services import booking_service
from app.services.calendar_settings_service import (
    ensure_utc,
    load_calendar_settings,
    local_day_utc_window,
)

router = APIRouter(prefix="/admin/calendar", tags=["calendar-admin"])


def get_db():
    """Standard per-request session, mirroring the existing chat route."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def require_calendar_admin(
    x_admin_key: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> Client:
    """
    Purpose: Transport wiring ONLY (Patch 5). Binds the OPTIONAL X-Admin-Key
        header — optional at the FastAPI validation layer so a MISSING header
        yields the same 401 as every other credential failure, never a 422 —
        and the request session, then delegates every authorization rule to
        the single owner (Rule 3): calendar_admin_auth.authenticate_calendar_admin.
    Returns: the authenticated Client — the ONE tenant this request may manage.
    Failures: 401 "Invalid admin key." for every credential failure
        (missing/empty/malformed/unknown/revoked/inactive client);
        infrastructure errors propagate as server failures (fail closed,
        Rule 16 — the global ADMIN_API_KEY is never consulted here).
    """
    return authenticate_calendar_admin(db, x_admin_key)


def require_tenant_match(
    requested_client_id: uuid.UUID, authenticated_client: Client
) -> Client:
    """
    Purpose: The single per-request tenant gate (Rule 15). Every endpoint
        compares the caller-supplied client_id to the AUTHENTICATED tenant
        FIRST — before any parameter semantics and before any query that
        could touch the supplied id.
    Returns: the authenticated Client (already loaded and active-checked by
        the authorization owner), so downstream code uses ONLY it.
    Failures: 404 "Client not found." on mismatch — the exact pre-Patch-5
        wording, and deliberately indistinguishable (in response AND in
        database activity: the foreign id is never queried) from a client id
        that does not exist at all.
    """
    if requested_client_id != authenticated_client.id:
        raise HTTPException(status_code=404, detail="Client not found.")
    return authenticated_client


# ---------------------------------------------------------------------------
# Request/response schemas
# ---------------------------------------------------------------------------

class SlotCreate(BaseModel):
    start_datetime: datetime          # Must include a timezone offset.
    end_datetime: datetime            # Must include a timezone offset.
    provider_name: Optional[str] = None
    service_key: Optional[str] = None


class SlotsCreateRequest(BaseModel):
    client_id: uuid.UUID
    slots: List[SlotCreate] = Field(min_length=1, max_length=100)


class SlotView(BaseModel):
    id: uuid.UUID
    start_datetime: datetime
    end_datetime: datetime
    status: str
    provider_name: Optional[str]
    service_key: Optional[str]


class AppointmentView(BaseModel):
    id: uuid.UUID
    patient_name: str
    patient_phone: str
    patient_email: Optional[str]
    new_or_returning: Optional[str]
    reason: Optional[str]
    urgency: str
    start_datetime: datetime
    end_datetime: datetime
    status: str
    # PATCH 4: UTC instant of the FIRST staff pending->confirmed action;
    # null = never staff-confirmed (includes auto-confirmed appointments).
    confirmed_at: Optional[datetime]
    source: str
    office_sms_sent: bool
    office_email_sent: bool
    patient_sms_sent: bool
    notify_error: Optional[str]


def _require_aware(dt: datetime, field_name: str) -> datetime:
    """Reject naive datetimes loudly instead of guessing a timezone."""
    if dt.tzinfo is None:
        raise HTTPException(
            status_code=422,
            detail=f"{field_name} must include a timezone offset "
                   f"(e.g. 2026-07-16T13:30:00-04:00).",
        )
    return dt.astimezone(ZoneInfo("UTC"))


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/slots", response_model=List[SlotView])
def create_slots(
    body: SlotsCreateRequest,
    db: Session = Depends(get_db),
    authenticated_client: Client = Depends(require_calendar_admin),
):
    """
    Purpose: Staff publishes bookable slots (the whole 'Model B' calendar).
    Database effects: INSERTs, committed together — an invalid slot in the
        batch rejects the WHOLE batch so staff never half-publish a day.
    Failures: 404 tenant mismatch (Patch 5 — indistinguishable from a
        nonexistent client); 422 naive datetimes or end<=start.
    """
    client = require_tenant_match(body.client_id, authenticated_client)
    created = []
    try:
        for item in body.slots:
            start_utc = _require_aware(item.start_datetime, "start_datetime")
            end_utc = _require_aware(item.end_datetime, "end_datetime")
            try:
                slot = appointment_repository.create_slot(
                    db, client.id, start_utc, end_utc,
                    provider_name=item.provider_name,
                    service_key=item.service_key,
                )
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc))
            created.append(slot)
        db.commit()
    except HTTPException:
        db.rollback()  # All-or-nothing batch; see docstring.
        raise
    return [_slot_view(s) for s in created]


@router.get("/slots", response_model=List[SlotView])
def list_slots(
    client_id: uuid.UUID = Query(...),
    day: date = Query(..., description="Local calendar day, e.g. 2026-07-16"),
    db: Session = Depends(get_db),
    authenticated_client: Client = Depends(require_calendar_admin),
):
    """
    Purpose: Staff daily view — ALL statuses, so held/booked/blocked slots
        are visible, not just available ones.
    Database effects: SELECT only. The local day is converted using the
        client's configured timezone (Rule 9: timezone boundaries).
    Failures: 404 tenant mismatch (Patch 5).
    """
    client = require_tenant_match(client_id, authenticated_client)
    settings = load_calendar_settings(client)
    # DST-safe local-day window (Patch 2B): both boundaries from the single
    # owner, never start + 24h — so the staff daily view matches exactly
    # what patients can be offered for that local date.
    day_start, day_end = local_day_utc_window(day, settings.timezone_name)
    rows = appointment_repository.list_slots_between(
        db, client.id, day_start, day_end
    )
    return [_slot_view(s) for s in rows]


@router.post("/slots/{slot_id}/block", response_model=SlotView)
def block_slot(
    slot_id: uuid.UUID,
    client_id: uuid.UUID = Query(...),
    db: Session = Depends(get_db),
    authenticated_client: Client = Depends(require_calendar_admin),
):
    """
    Purpose: Staff removes a slot from booking (meeting, lunch, closure).
    Database effects: one locked transaction; the slot becomes 'blocked'.
    Failures: 404 tenant mismatch (Patch 5) or unknown slot for this client;
        409 when the slot is already BOOKED — staff must cancel the
        appointment instead, so a patient's booking can never silently
        vanish (Rule 4 / Rule 16).
    """
    client = require_tenant_match(client_id, authenticated_client)
    slot = appointment_repository.get_slot_for_update(db, client.id, slot_id)
    if slot is None:
        db.rollback()
        raise HTTPException(status_code=404, detail="Slot not found.")
    if slot.status == SlotStatus.BOOKED:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Slot has a booked appointment. Cancel the appointment first.",
        )
    slot.status = SlotStatus.BLOCKED
    slot.held_until = None
    slot.held_by_conversation_id = None
    db.commit()
    return _slot_view(slot)


@router.get("/appointments", response_model=List[AppointmentView])
def list_appointments(
    client_id: uuid.UUID = Query(...),
    start_day: date = Query(...),
    end_day: date = Query(...),
    db: Session = Depends(get_db),
    authenticated_client: Client = Depends(require_calendar_admin),
):
    """Purpose: staff appointment list for a local-day range. SELECT only.
    The tenant gate runs FIRST (Patch 5): a mismatched caller gets 404
    before any parameter semantics are revealed (so mismatch + bad dates is
    404, not 422)."""
    client = require_tenant_match(client_id, authenticated_client)
    if end_day < start_day:
        raise HTTPException(status_code=422, detail="end_day is before start_day.")
    settings = load_calendar_settings(client)
    # DST-safe multi-day range (Patch 2B): start of start_day and end of
    # end_day each come from the single window owner. The old form added
    # 24h AFTER converting end_day's midnight, which was wrong whenever
    # end_day -> end_day+1 crossed an offset transition.
    start_utc, _ = local_day_utc_window(start_day, settings.timezone_name)
    _, end_utc = local_day_utc_window(end_day, settings.timezone_name)
    rows = appointment_repository.list_appointments_between(db, client.id, start_utc, end_utc)
    return [_appointment_view(a) for a in rows]


@router.post("/appointments/{appointment_id}/confirm", response_model=AppointmentView)
def confirm_appointment(
    appointment_id: uuid.UUID,
    client_id: uuid.UUID = Query(...),
    db: Session = Depends(get_db),
    authenticated_client: Client = Depends(require_calendar_admin),
):
    """
    Purpose: Staff confirmation — the supported pending -> confirmed
        transition (Patch 4, Senior Audit Critical #4). booking_service owns
        the rule; this route only maps outcomes.
    Behavior: 200 for a fresh confirmation AND for re-confirming an
        already-confirmed appointment (idempotent success — confirmed_at is
        preserved byte-for-byte, and stays null for appointments that were
        created directly as confirmed). NO notification is sent: authorized
        office staff are performing the action, and patient messaging remains
        disabled (Patch 2D policy).
    Failures: 404 tenant mismatch (Patch 5) or appointment not found for
        this tenant — unknown and cross-tenant appointment ids remain
        indistinguishable, with the same wording as cancel (Rule 15);
        409 when the appointment is cancelled/completed/no_show and cannot
        be confirmed. Unexpected database errors roll back inside
        booking_service and propagate (Rule 16).
    """
    client = require_tenant_match(client_id, authenticated_client)
    result = booking_service.confirm_appointment(
        db, client.id, appointment_id,
        now_utc=datetime.now(ZoneInfo("UTC")),
    )
    if result.reason == "appointment_missing":
        raise HTTPException(status_code=404, detail="Appointment not found.")
    if result.reason == "not_confirmable":
        raise HTTPException(
            status_code=409,
            detail=f"Appointment is {result.detail} and cannot be confirmed.",
        )
    return _appointment_view(result.appointment)


@router.post("/appointments/{appointment_id}/cancel", response_model=AppointmentView)
def cancel_appointment(
    appointment_id: uuid.UUID,
    client_id: uuid.UUID = Query(...),
    db: Session = Depends(get_db),
    authenticated_client: Client = Depends(require_calendar_admin),
):
    """
    Purpose: Staff cancellation. Frees the underlying slot in the same
        transaction (booking_service owns that rule). PATCH 7 (Senior Audit
        Recommended #6): only pending and confirmed appointments are
        cancellable; booking_service owns the allow-list, this route only
        maps outcomes.
    Failures: 404 tenant mismatch (Patch 5) or appointment not found for
        this tenant — unknown and cross-tenant appointment ids remain
        indistinguishable, with the same wording as confirm (Rule 15);
        409 already cancelled (mutation-free, approved decision D1);
        409 when the appointment is completed/no_show — finished
        appointments must never be rewritten and their historical slots
        never reopened. The 409 detail carries only a controlled
        AppointmentStatus word (never tenant, patient, slot, provider, or
        database information). No notification is sent on any path.
    """
    client = require_tenant_match(client_id, authenticated_client)
    result = booking_service.cancel_appointment(db, client.id, appointment_id)
    if result.reason == "slot_missing":
        raise HTTPException(status_code=404, detail="Appointment not found.")
    if result.reason == "already_cancelled":
        raise HTTPException(status_code=409, detail="Appointment is already cancelled.")
    if result.reason == "not_cancellable":
        # PATCH 7: completed / no_show (terminal statuses). Wording mirrors
        # the confirm route's 409 exactly; result.detail is a controlled
        # AppointmentStatus value supplied by the lifecycle owner.
        raise HTTPException(
            status_code=409,
            detail=f"Appointment is {result.detail} and cannot be cancelled.",
        )
    return _appointment_view(result.appointment)


# ---------------------------------------------------------------------------
# View mappers (pure)
# ---------------------------------------------------------------------------

def _slot_view(slot) -> SlotView:
    return SlotView(
        id=slot.id,
        start_datetime=ensure_utc(slot.start_datetime),
        end_datetime=ensure_utc(slot.end_datetime),
        status=slot.status,
        provider_name=slot.provider_name,
        service_key=slot.service_key,
    )


def _appointment_view(a) -> AppointmentView:
    return AppointmentView(
        id=a.id,
        patient_name=a.patient_name,
        patient_phone=a.patient_phone,
        patient_email=a.patient_email,
        new_or_returning=a.new_or_returning,
        reason=a.reason,
        urgency=a.urgency,
        start_datetime=ensure_utc(a.start_datetime),
        end_datetime=ensure_utc(a.end_datetime),
        status=a.status,
        confirmed_at=(ensure_utc(a.confirmed_at)
                      if a.confirmed_at is not None else None),
        source=a.source,
        office_sms_sent=a.office_sms_sent,
        office_email_sent=a.office_email_sent,
        patient_sms_sent=a.patient_sms_sent,
        # PATCH 6: only the approved closed vocabulary passes through; any
        # legacy/arbitrary stored value returns the fixed withheld marker.
        notify_error=sanitize_stored_notify_error(a.notify_error),
    )
