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
#   GET    /admin/calendar/me                    office portal bootstrap
#   POST   /admin/calendar/slots                 publish bookable slots
#   GET    /admin/calendar/slots                 list slots for a local day
#   POST   /admin/calendar/slots/{id}/block      remove a slot from booking
#   GET    /admin/calendar/appointments          list appointments in a range
#   POST   /admin/calendar/appointments/{id}/confirm  pending -> confirmed
#   POST   /admin/calendar/appointments/{id}/cancel   cancel + free the slot
#   GET    /admin/calendar/availability-preview  read-only picker preview (B2)
#
# Times in requests/responses are ISO-8601. Requests may send local times
# WITH an offset ("2026-07-16T13:30:00-04:00") or UTC ("...T17:30:00Z");
# naive datetimes are REJECTED rather than guessed at (Rule 4).

import uuid
from datetime import date, datetime
from typing import List, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field, ValidationError
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
# B2 (availability preview): the route wires FOUR existing owners and adds
# no rule of its own (Rule 2) - the B1 request/response contract
# (app.schemas), the B1 preview builder, the shared enabled-service owner
# (extracted from chat.py in this same patch), and the master-key ->
# Calendar-policy translation owner.
from app.schemas import (
    AvailabilityPreviewRequest,
    AvailabilityPreviewResponse,
)
from app.services.availability_preview_service import (
    build_availability_preview,
)
from app.services.mia_service_library import (
    get_client_enabled_service_keys,
)
from app.services.service_policy_mapping import (
    calendar_policy_value_for_master_service,
)
from app.services import booking_service
# P4-A (Correction B): the single shared owner of the per-slot block
# mutation rule - this route delegates to it and maps outcomes only.
from app.services import slot_management_service
from app.services.calendar_settings_service import (
    client_now,
    ensure_utc,
    load_calendar_settings,
    local_day_utc_window,
)

router = APIRouter(prefix="/admin/calendar", tags=["calendar-admin"])

# B2 (availability preview): the ONE stable 422 detail for every rejected
# service_key - blank, unknown, case-mismatched, admin_other,
# tenant-disabled, unmapped, or a direct internal policy value. One wording
# BY DESIGN (Rule 4: named, not buried): the response never reveals WHICH
# gate rejected the key, so callers cannot probe the tenant's enabled
# services or the mapping vocabulary through error differences.
SERVICE_KEY_NOT_AVAILABLE_DETAIL = "service_key is not available for preview"


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


class PortalBootstrapView(BaseModel):
    """The office portal's bootstrap payload (Portal MVP) — the ONLY
    fields the portal needs before listing appointments. Nothing
    sensitive belongs here BY CONSTRUCTION: no credentials or hashes,
    no settings JSON, no notification recipients, no database metadata.
    Adding a field to this model is a reviewed contract change."""
    client_id: uuid.UUID
    practice_name: str
    timezone_name: str
    # A date serializes as YYYY-MM-DD — exactly the approved wire form.
    today_local: date
    booking_enabled: bool


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

@router.get("/me", response_model=PortalBootstrapView)
def portal_me(
    authenticated_client: Client = Depends(require_calendar_admin),
):
    """
    Purpose: Office-portal bootstrap (Portal MVP). Identifies WHICH office
        a presented per-office key belongs to, so the portal never asks
        staff for a client_id. Deliberately takes NO client_id parameter:
        the credential ALONE determines the tenant (Rule 15 — nothing on
        this endpoint can be pointed at another office).
    Inputs: only the X-Admin-Key header, consumed by the existing
        require_calendar_admin dependency (authorization stays with its
        single owner, calendar_admin_auth — nothing is duplicated here).
    Returns: PortalBootstrapView — client_id, practice_name,
        timezone_name, today_local (the office's CURRENT local calendar
        date, computed through client_now in the OFFICE timezone — never
        server time and never browser time), and booking_enabled as a
        strict boolean from the settings owner (load_calendar_settings).
        booking_enabled=false is INFORMATIONAL: the endpoint still
        succeeds, so staff can review and confirm existing requests
        while online booking stays paused.
    Database effects: none beyond the authorization dependency's
        credential SELECT (read-only endpoint).
    Possible failures: 401 "Invalid admin key." for every credential
        failure — missing/empty/malformed/unknown/revoked/inactive
        client — indistinguishable by design (single owner's rule).
    """
    settings = load_calendar_settings(authenticated_client)
    return PortalBootstrapView(
        client_id=authenticated_client.id,
        practice_name=authenticated_client.practice_name,
        timezone_name=settings.timezone_name,
        today_local=client_now(settings).date(),
        booking_enabled=settings.booking_enabled,
    )


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
    Purpose: Staff removes a slot from booking (meeting, lunch, blocked
        period).
    Database effects: one locked transaction; the slot becomes 'blocked'.
        P4-A (contract v1.2, Correction B): the mutation rule itself moved
        to its single shared owner, slot_management_service.block_slot, so
        the admin surface and the portal apply ONE rule text. This route's
        observable behavior is byte-equivalent to before the extraction
        (pinned by calendar_tests/test_slot_management_owner.py).
    Failures: 404 tenant mismatch (Patch 5) or unknown slot for this client;
        409 when the slot is already BOOKED — staff must cancel the
        appointment instead, so a patient's booking can never silently
        vanish (Rule 4 / Rule 16).
    """
    client = require_tenant_match(client_id, authenticated_client)
    result = slot_management_service.block_slot(db, client.id, slot_id)
    if result.reason == slot_management_service.REASON_SLOT_MISSING:
        raise HTTPException(status_code=404, detail="Slot not found.")
    if result.reason == slot_management_service.REASON_SLOT_BOOKED:
        raise HTTPException(
            status_code=409,
            detail="Slot has a booked appointment. Cancel the appointment first.",
        )
    return _slot_view(result.slot)


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
# B2 - read-only availability preview (Prototype B)
# ---------------------------------------------------------------------------

def _resolve_preview_service_policy(
    client: Client, service_key: Optional[str]
) -> Optional[str]:
    """
    Purpose: The B2 route's ONLY service-key gate. Translates one OPTIONAL
        browser-supplied master-library key into the existing Calendar-
        policy vocabulary, enforcing the locked order: trim, then the
        tenant-enabled check (shared owner get_client_enabled_service_keys),
        then the mapping owner's translation. Runs AFTER authentication and
        tenant matching only - the caller guarantees that ordering.
    Inputs:
        client: the AUTHENTICATED tenant (never the raw requested id).
        service_key: the raw optional query value; None means the caller
            requested a generic preview.
    Returns: None for a generic preview (mapping owner NOT invoked - locked
        contract), otherwise the translated existing policy value
        (e.g. "cleaning/checkup") to hand to B1 unchanged.
    Database effects: none (both owners consulted are pure).
    Possible failures: HTTPException 422 with the single
        SERVICE_KEY_NOT_AVAILABLE_DETAIL wording for blank, unknown,
        case-mismatched, admin_other, tenant-disabled, unmapped, or direct
        internal-policy-value input. The chat fallback
        ("appointment" + " request") is DELIBERATELY never applied here:
        B2 rejects unmapped keys instead of inheriting a generic bucket.
    """
    if service_key is None:
        return None

    trimmed = service_key.strip()
    if not trimmed:
        # Blank/whitespace-only is a rejected SUPPLIED key, never silently
        # downgraded to the generic mode (Rule 4 - no hidden behavior).
        raise HTTPException(
            status_code=422, detail=SERVICE_KEY_NOT_AVAILABLE_DETAIL
        )

    # Tenant-enabled gate FIRST (locked order): a real master key the
    # office has not enabled must be indistinguishable from an unknown
    # key. Matching is case-sensitive by design - the shared owner
    # returns canonical keys and no normalization is performed here.
    if trimmed not in get_client_enabled_service_keys(client):
        raise HTTPException(
            status_code=422, detail=SERVICE_KEY_NOT_AVAILABLE_DETAIL
        )

    # Translation through the single mapping owner. None covers unknown,
    # admin_other, case-mismatched, unmapped, and direct internal policy
    # values (those are never master keys, so they cannot map).
    policy_value = calendar_policy_value_for_master_service(trimmed)
    if policy_value is None:
        raise HTTPException(
            status_code=422, detail=SERVICE_KEY_NOT_AVAILABLE_DETAIL
        )
    return policy_value


def _preview_request_error_detail(exc: ValidationError) -> str:
    """
    Purpose: Render the B1 request-model's ValidationError as a compact,
        input-describing 422 detail. Only pydantic's field locations and
        rule messages are surfaced - never tracebacks, internal types, or
        tenant data (the messages describe the CALLER'S own input).
    Inputs:  exc - the ValidationError raised while constructing
        AvailabilityPreviewRequest from the raw query strings.
    Returns: "field: message; field: message" in pydantic's deterministic
        error order.
    Database effects: none. External effects: none.
    """
    parts = []
    for error in exc.errors():
        loc = ".".join(str(item) for item in error.get("loc", ()))
        msg = str(error.get("msg", "invalid value"))
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "; ".join(parts)


@router.get("/availability-preview", response_model=AvailabilityPreviewResponse)
def availability_preview(
    client_id: uuid.UUID = Query(...),
    start_day: str = Query(
        ..., description="Raw office-local day, e.g. 2026-07-16"
    ),
    end_day: str = Query(
        ..., description="Raw office-local day, e.g. 2026-07-22"
    ),
    selected_day: Optional[str] = Query(
        default=None, description="Raw office-local day inside the range"
    ),
    service_key: Optional[str] = Query(
        default=None, description="Mia master-library service key"
    ),
    db: Session = Depends(get_db),
    authenticated_client: Client = Depends(require_calendar_admin),
):
    """
    Purpose: B2 - the authenticated, STRICTLY READ-ONLY transport for the
        B1 availability preview (the visual picker's calendar grid and
        selected-day slot list). This route only orders the gates and
        delegates; every rule lives in its existing owner (Rule 2).
    Locked processing order:
        1. require_calendar_admin (dependency - runs before this body)
        2. require_tenant_match
        3. optional master-service-key validation/translation
           (_resolve_preview_service_policy)
        4. existing B1 AvailabilityPreviewRequest construction
        5. existing B1 build_availability_preview call
        6. existing B1 AvailabilityPreviewResponse returned unchanged
    Inputs: X-Admin-Key header (per-office credential); client_id (UUID);
        start_day / end_day / selected_day as RAW office-local YYYY-MM-DD
        STRINGS - deliberately NOT FastAPI date parameters, so date
        semantics are revealed only AFTER authentication and tenant
        matching (a foreign tenant with malformed dates gets the existing
        404, never a 422); service_key as an OPTIONAL master-library key
        (absent = generic preview; the mapping owner is not consulted).
    Returns: the B1 AvailabilityPreviewResponse unchanged - day states
        only from the locked past/open/full/unavailable vocabulary, no
        slot_id, no daily counts, no patient/hold/conversation/
        notification data. booking_enabled=false is INFORMATIONAL: the
        preview still renders and the tenant setting is never altered.
    Database effects: SELECT only - the authorization owner's credential
        SELECT plus exactly ONE appointment_slots range SELECT inside the
        B1 service. No INSERT/UPDATE/DELETE, no SELECT FOR UPDATE, no
        commit/flush, no hold placement or release (an expired hold may be
        INTERPRETED as available, but its row is left byte-untouched).
    Possible failures: 401 "Invalid admin key." for every credential
        failure (single owner's rule); 404 "Client not found." on tenant
        mismatch - indistinguishable from a nonexistent client and issued
        BEFORE any parameter semantics; 422 for invalid dates/ranges/
        selected_day (B1 model rules, surfaced verbatim) and for every
        rejected service_key (single SERVICE_KEY_NOT_AVAILABLE_DETAIL
        wording). Database errors propagate (Rule 16 - fail visibly).
    """
    client = require_tenant_match(client_id, authenticated_client)
    policy_value = _resolve_preview_service_policy(client, service_key)
    try:
        # The B1 model owns EVERY date rule (valid ISO date, ordering, the
        # 31-day cap, selected_day membership) - nothing is re-implemented
        # here, and raw strings enter validation only past the two gates.
        request = AvailabilityPreviewRequest(
            start_day=start_day,
            end_day=end_day,
            selected_day=selected_day,
            service_key=policy_value,
        )
    except ValidationError as exc:
        raise HTTPException(
            status_code=422, detail=_preview_request_error_detail(exc)
        )
    return build_availability_preview(
        db, client, request, datetime.now(ZoneInfo("UTC"))
    )


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
