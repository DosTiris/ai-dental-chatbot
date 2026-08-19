# app/routes/portal_schedule.py
#
# OWNER OF: the HTTP surface for the authenticated Office Portal SCHEDULE
# slice (P4-A - Portal Slot Schedule Controls v1, contract v1.2). This file
# only binds transport inputs, delegates every rule to its single owner, and
# shapes the approved response views - the transport-only role portal.py /
# portal_leads.py / portal_appointments.py already have (Rule 2/3).
#
# Endpoints (all require Authorization: Bearer <Supabase access token>):
#   GET  /portal/schedule                              day-grid slot read
#   POST /portal/schedule/days/{day}/publish           publish a day's slots
#   POST /portal/schedule/slots/{slot_id}/block        block one slot
#   POST /portal/schedule/slots/{slot_id}/unblock      blocked -> available
#   POST /portal/schedule/days/{day}/block-all-open    bulk slot operation
#
# TENANT BINDING (Rule 15): authentication and tenant resolution are REUSED,
# unchanged, from the frozen P2 owners - require_portal_identity ->
# portal_auth.authenticate_portal_request. The verified credential ALONE
# determines the tenant (identity.client). NO endpoint here declares a
# client_id, client_key, or any other tenant selector; undeclared query
# parameters are ignored by FastAPI, so a stray ?client_id=... changes
# nothing.
#
# WORDING RULE (contract SS5-E / D3): the bulk operation is "block all open
# slots" - the words "close"/"closed"/"closure" are deliberately absent
# from this surface. Blocking the day's current open rows does NOT prevent
# later publication; durable closures are P4-B.
#
# LEAK PREVENTION: the view models below are the COMPLETE approved field
# sets, constructed explicitly field by field. Deliberately EXCLUDED:
# client_id, held_until, held_by_conversation_id, every patient /
# appointment / conversation / notification / credential field. The
# leak-prevention tests pin these exact sets.

import uuid
from datetime import date, datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

# Reused P2/P3 owners: the per-request session factory and the ONE portal
# identity dependency. Importing the SAME callables (not copies) keeps this
# router covered by any dependency override applied to the portal router in
# tests, and keeps portal_auth the single authentication owner.
from app.routes.portal import get_db, require_portal_identity
from app.services.portal_auth import PortalIdentity
from app.repositories import appointment_repository
# Imported as a MODULE so client_now / local_day_utc_window resolve through
# the settings-service attribute at CALL TIME (the frozen P3-C seam rule):
# a test substituting calendar_settings_service.client_now is genuinely
# observed by this route.
from app.services import calendar_settings_service
from app.services import portal_schedule_service
# Slice 4D-B: the operational closed-day read owner (closed_days slice for
# the schedule envelope; all mutation rules live in the service layer).
from app.services import closure_authority
from app.services import slot_management_service
from app.services.calendar_settings_service import ensure_utc

router = APIRouter(prefix="/portal", tags=["office-portal"])

# The user-facing default window is SEVEN inclusive local calendar days
# (today plus the next six) - the frozen portal_appointments convention.
DEFAULT_RANGE_DAYS_INCLUSIVE = 6
# Contract SS5-A: the maximum INCLUSIVE local-day span one read may cover
# (matches the availability preview's 31-day convention).
MAX_RANGE_DAYS_INCLUSIVE = 31

# Route-level wording, each in one reviewable place (Rule 4).
SLOT_NOT_FOUND_DETAIL = "Slot not found."
SLOT_BOOKED_DETAIL = (
    "Slot has a booked appointment. Cancel the appointment first."
)


class PortalScheduleSlotView(BaseModel):
    """The COMPLETE approved slot shape for the portal schedule surface.
    Mirrors the admin SlotView fields exactly (id renamed slot_id for the
    portal convention); adds nothing and never carries hold ownership,
    tenant, patient, or notification data. Adding a field is a reviewed
    contract change - the leak-prevention test pins this exact set."""
    slot_id: uuid.UUID
    # UTC ISO-8601 instants (aware). The FRONTEND renders these in
    # timezone_name (returned on the envelope), never in the device tz.
    start_datetime: datetime
    end_datetime: datetime
    status: str
    provider_name: Optional[str]
    service_key: Optional[str]


class PortalScheduleListView(BaseModel):
    """The GET /portal/schedule envelope (P3-C shape + the 4D-B addition):
    closed_days is the OPERATIONAL closure slice for the requested window -
    derived ONLY from settings.calendar.closed_days (never from blocked
    rows, empty days, or recurring configuration), so the Calendar renders
    "Office closed" exclusively from authoritative backend state."""
    timezone_name: str
    start_day: date
    end_day: date
    slots: List[PortalScheduleSlotView]
    closed_days: List[date]


class PublishDayRequest(BaseModel):
    """The publish body - STRICT transport (contract SS5-B / Correction C5):
    any undeclared field is rejected with 422 by pydantic itself, so a
    misspelled or smuggled key can never be silently ignored."""
    model_config = ConfigDict(extra="forbid")
    open_time: str
    close_time: str
    slot_minutes: int


class BookedWindowView(BaseModel):
    """One still-booked window on a bulk-blocked day - times ONLY (Rule 16
    visibility without any patient data)."""
    start_datetime: datetime
    end_datetime: datetime


class BlockAllOpenResultView(BaseModel):
    """The bulk-operation result. Field names deliberately describe a SLOT
    operation, never a closure (contract SS5-E / D3)."""
    day: date
    blocked_count: int
    booked_remaining: List[BookedWindowView]


def _slot_view(slot) -> PortalScheduleSlotView:
    """Explicit field-by-field construction - never a __dict__ splat."""
    return PortalScheduleSlotView(
        slot_id=slot.id,
        start_datetime=ensure_utc(slot.start_datetime),
        end_datetime=ensure_utc(slot.end_datetime),
        status=slot.status,
        provider_name=slot.provider_name,
        service_key=slot.service_key,
    )


@router.get("/schedule", response_model=PortalScheduleListView)
def portal_get_schedule(
    start_day: Optional[date] = Query(default=None),
    end_day: Optional[date] = Query(default=None),
    identity: PortalIdentity = Depends(require_portal_identity),
    db: Session = Depends(get_db),
):
    """
    Purpose: The authenticated office's slot day-grid (ALL statuses, so
        staff sees held/booked/blocked rows, not just available ones) for a
        local-day range - contract SS5-A.
    Range semantics (frozen portal_appointments rules + the 31-day cap):
        both omitted -> today .. today+6 (office-local "today" via
        client_now, never server/browser time); either supplied -> both
        required (422 on a partial range); end_day < start_day -> 422;
        inclusive span > 31 local days -> 422. Boundaries are DST-safe:
        every UTC window edge comes from the single owner
        local_day_utc_window.
    Database effects: ONE tenant-scoped SELECT via
        appointment_repository.list_slots_between. No write.
    Possible failures: 422 partial/reversed/over-cap range; 401 every
        credential failure (indistinguishable); 503 unconfigured server
        auth; database errors propagate (fail closed).
    """
    settings = calendar_settings_service.load_calendar_settings(identity.client)

    if (start_day is None) != (end_day is None):
        raise HTTPException(
            status_code=422,
            detail="start_day and end_day must be supplied together.",
        )
    if start_day is None:
        today_local = calendar_settings_service.client_now(settings).date()
        start_day = today_local
        end_day = today_local + timedelta(days=DEFAULT_RANGE_DAYS_INCLUSIVE)
    elif end_day < start_day:
        raise HTTPException(
            status_code=422, detail="end_day is before start_day.")
    if (end_day - start_day).days + 1 > MAX_RANGE_DAYS_INCLUSIVE:
        raise HTTPException(
            status_code=422,
            detail=f"The range may cover at most "
                   f"{MAX_RANGE_DAYS_INCLUSIVE} days.",
        )

    start_utc, _ = calendar_settings_service.local_day_utc_window(
        start_day, settings.timezone_name)
    _, end_utc = calendar_settings_service.local_day_utc_window(
        end_day, settings.timezone_name)
    rows = appointment_repository.list_slots_between(
        db, identity.client.id, start_utc, end_utc)
    return PortalScheduleListView(
        timezone_name=settings.timezone_name,
        start_day=start_day,
        end_day=end_day,
        slots=[_slot_view(s) for s in rows],
        # 4D-B: the operational closed dates INSIDE the requested window,
        # from the live authority only (a read snapshot is fine for a GET -
        # mutation-side judgments re-read fresh under the day lock).
        closed_days=closure_authority.closed_days_in_range(
            identity.client.settings, start_day, end_day),
    )


@router.post("/schedule/days/{day}/publish",
             response_model=List[PortalScheduleSlotView])
def portal_publish_day(
    day: date,
    body: PublishDayRequest,
    identity: PortalIdentity = Depends(require_portal_identity),
    db: Session = Depends(get_db),
):
    """
    Purpose: Publish one local day's bookable slots (contract SS5-B). Every
        rule lives in portal_schedule_service (advisory-lock serialization,
        DST classification, exact expansion, overlap refusal); this route
        only maps the closed outcome vocabulary to HTTP.
    Failures: 422 for every PUBLISH_INVALID (including any undeclared body
        field via the strict request model, and malformed {day} via
        FastAPI's date parsing); 409 PUBLISH_OVERLAP with zero inserts;
        401/503 as on every portal endpoint.
    Database effects: on success, N slot INSERTs committed together; on any
        refusal, nothing (the service rolls back).
    """
    settings = calendar_settings_service.load_calendar_settings(identity.client)
    result = portal_schedule_service.publish_day_slots(
        db, identity.client.id, settings, day,
        body.open_time, body.close_time, body.slot_minutes,
    )
    if not result.ok:
        if result.reason == portal_schedule_service.PUBLISH_OVERLAP:
            raise HTTPException(status_code=409, detail=result.detail)
        if result.reason == portal_schedule_service.PUBLISH_CLOSED_DAY:
            # 4D-B: the day is operationally closed - a state conflict
            # (409), zero inserts, judged fresh under the day lock.
            raise HTTPException(status_code=409, detail=result.detail)
        raise HTTPException(status_code=422, detail=result.detail)
    return [_slot_view(s) for s in result.slots]


@router.post("/schedule/slots/{slot_id}/block",
             response_model=PortalScheduleSlotView)
def portal_block_slot(
    slot_id: uuid.UUID,
    identity: PortalIdentity = Depends(require_portal_identity),
    db: Session = Depends(get_db),
):
    """
    Purpose: Staff removes ONE slot from booking (partial-day control) -
        contract SS5-C. Delegates to the shared owner (Correction B), so
        the portal and the admin surface apply ONE rule text.
    Failures: 404 tenant-opaque (unknown and foreign indistinguishable);
        409 when a booked appointment occupies the slot - a patient's
        booking never silently vanishes; 401/503 as on every portal
        endpoint.
    """
    result = slot_management_service.block_slot(db, identity.client.id, slot_id)
    if result.reason == slot_management_service.REASON_SLOT_MISSING:
        raise HTTPException(status_code=404, detail=SLOT_NOT_FOUND_DETAIL)
    if result.reason == slot_management_service.REASON_SLOT_BOOKED:
        raise HTTPException(status_code=409, detail=SLOT_BOOKED_DETAIL)
    return _slot_view(result.slot)


@router.post("/schedule/slots/{slot_id}/unblock",
             response_model=PortalScheduleSlotView)
def portal_unblock_slot(
    slot_id: uuid.UUID,
    identity: PortalIdentity = Depends(require_portal_identity),
    db: Session = Depends(get_db),
):
    """
    Purpose: Staff returns ONE blocked slot to booking - contract SS5-D.
        The shared owner enforces blocked -> available ONLY; every other
        state is rejected, never coerced.
    Failures: 404 tenant-opaque; 409 whose detail carries only the closed
        SlotStatus word of the refused state; 401/503 as on every portal
        endpoint.
    """
    result = slot_management_service.unblock_slot(db, identity.client.id, slot_id)
    if result.reason == slot_management_service.REASON_SLOT_MISSING:
        raise HTTPException(status_code=404, detail=SLOT_NOT_FOUND_DETAIL)
    if result.reason == slot_management_service.REASON_SLOT_NOT_BLOCKED:
        raise HTTPException(
            status_code=409,
            detail=f"Slot is {result.detail} and cannot be unblocked.",
        )
    return _slot_view(result.slot)


@router.post("/schedule/days/{day}/block-all-open",
             response_model=BlockAllOpenResultView)
def portal_block_all_open(
    day: date,
    identity: PortalIdentity = Depends(require_portal_identity),
    db: Session = Depends(get_db),
):
    """
    Purpose: Block every currently OPEN slot (available, or held - the bulk
        block outranks holds) on one local day - contract SS5-E. A SLOT
        operation: it does not prevent later publication and is never
        described as closing the day (durable closures are P4-B).
    Returns: blocked_count plus the still-booked windows (times only), so
        staff visibly sees remaining appointments (Rule 16). Idempotent.
    Database effects: one serialized, locked transaction (advisory day lock
        + row locks) committed once; rollback on error.
    Failures: 422 malformed {day} (FastAPI date parsing); 401/503 as on
        every portal endpoint; database errors propagate.
    """
    settings = calendar_settings_service.load_calendar_settings(identity.client)
    result = portal_schedule_service.block_all_open(
        db, identity.client.id, settings, day)
    return BlockAllOpenResultView(
        day=day,
        blocked_count=result.blocked_count,
        booked_remaining=[
            BookedWindowView(start_datetime=start, end_datetime=end)
            for start, end in result.booked_remaining
        ],
    )


# ---------------------------------------------------------------------------
# PHASE 3A SLICE 4D-A - Calendar-native ONE-OFF availability (transport only)
# ---------------------------------------------------------------------------

class OneOffAvailabilityRequest(BaseModel):
    """The one-off availability body - STRICT transport (the
    PublishDayRequest / contract SS5-B convention): any undeclared field is
    rejected with 422 by pydantic itself, so a misspelled or SMUGGLED key -
    client_id, status, provider, service, a raw datetime, a browser-supplied
    "now" - can never be silently ignored. Tenant identity comes ONLY from
    the verified portal credential; time authority comes ONLY from the
    server clock inside the service. day parses as a strict ISO calendar
    date; start_time is the office-local "HH:MM" wall time; validity,
    timezone normalization, DST refusal, the strictly-future rule, overlap,
    and tenancy are ALL server-enforced in the service owner - browser-side
    validation is assistance, never eligibility."""
    model_config = ConfigDict(extra="forbid")
    day: date
    start_time: str
    duration_minutes: int


# Fail-closed wording for an impossible service result (the staff-booking
# route's UNEXPECTED_RESULT_DETAIL convention - Rule 4/16: named once,
# generic, never echoing internals).
UNEXPECTED_ONE_OFF_RESULT_DETAIL = "Unable to create availability."


@router.post("/schedule/slots/one-off",
             response_model=PortalScheduleSlotView)
def portal_create_one_off_availability(
    body: OneOffAvailabilityRequest,
    identity: PortalIdentity = Depends(require_portal_identity),
    db: Session = Depends(get_db),
):
    """
    Purpose: Slice 4D-A - create ONE one-off available slot from the
        Calendar page, so a receptionist can open a single future time and
        then book it through the EXISTING staff-booking flow. Every rule
        lives in portal_schedule_service.create_one_off_slot (shared
        prevalidation helpers, the strictly-future rule against the
        server-authoritative office clock, the same-local-day rule, then
        wholesale delegation to the frozen publish transaction - advisory
        lock, DST classification, overlap refusal, repository insert); this
        route only binds the verified tenant and maps the closed outcome
        vocabulary to HTTP.
    Tenant binding (Rule 15): identity.client from the verified credential
        ALONE. The strict body above cannot carry a tenant selector.
    Failures: 422 for every PUBLISH_INVALID (including any undeclared body
        field via the strict request model); 409 PUBLISH_OVERLAP with zero
        inserts (the frozen OVERLAP_DETAIL wording); 401 every credential
        failure (indistinguishable); 503 unconfigured server auth; 500
        fail-closed if the service ever returned success without exactly
        one row (impossible by construction - never guessed around);
        database errors propagate (fail closed).
    Database effects: on success, exactly ONE slot INSERT committed by the
        service; on any refusal, nothing.
    """
    settings = calendar_settings_service.load_calendar_settings(identity.client)
    result = portal_schedule_service.create_one_off_slot(
        db, identity.client.id, settings, body.day,
        body.start_time, body.duration_minutes,
    )
    if not result.ok:
        if result.reason == portal_schedule_service.PUBLISH_OVERLAP:
            raise HTTPException(status_code=409, detail=result.detail)
        if result.reason == portal_schedule_service.PUBLISH_CLOSED_DAY:
            # 4D-B: creation on an operationally closed day is a state
            # conflict (409) - the single under-lock gate refused it.
            raise HTTPException(status_code=409, detail=result.detail)
        raise HTTPException(status_code=422, detail=result.detail)
    # G1/G2 fail-closed (the staff-booking convention): only a success
    # carrying EXACTLY ONE created row may reach the response projection.
    if len(result.slots) != 1:
        raise HTTPException(status_code=500,
                            detail=UNEXPECTED_ONE_OFF_RESULT_DETAIL)
    return _slot_view(result.slots[0])


# ---------------------------------------------------------------------------
# PHASE 3A SLICE 4D-B - Calendar-native CLOSE DAY / REOPEN DAY (transport)
# ---------------------------------------------------------------------------

class CloseDayView(BaseModel):
    """The close_day response: whether the day was ALREADY closed
    (idempotent), how many open/held rows THIS call blocked, and the day's
    still-booked windows soonest-first (times only, never patient data) -
    the explicit Rule 16 "closing will not cancel these" evidence."""
    day: date
    already_closed: bool
    blocked_count: int
    booked_remaining: List[BookedWindowView]


class ReopenDayView(BaseModel):
    """The reopen_day response: whether the date was actually closed
    (idempotent remove), plus the informational recurring flag - True when
    the SAME date is also configured in the recurring closures list, so the
    office learns a future Recurring Apply may close it again. Reopen never
    unblocks any slot; the wording for that lives in the frontend."""
    day: date
    was_closed: bool
    recurring_configured: bool


@router.post("/schedule/days/{day}/close", response_model=CloseDayView)
def portal_close_day(
    day: date,
    identity: PortalIdentity = Depends(require_portal_identity),
    db: Session = Depends(get_db),
):
    """
    Purpose: Slice 4D-B - mark an office-local date operationally closed
        from the Calendar, WITHOUT a fake appointment: one atomic
        transaction persists the closed_days entry AND blocks the day's
        current open/held inventory through the one shared bulk transition;
        booked appointments are untouched and returned for the staff
        warning. Every rule lives in portal_schedule_service.close_day.
    Authorization: require_portal_identity - the SAME availability-control
        permission publish/block-all/one-off use (approved 4D-B ruling; no
        new role model). Tenant binding from the verified credential ONLY.
    Failures: 422 CLOSE_INVALID (past office-local date; closed-days cap) -
        zero mutation; 401 every credential failure (indistinguishable);
        503 unconfigured server auth; database errors propagate after the
        combined rollback (fail closed - no partially closed state).
    Database effects: on success, the surgical closed_days write plus the
        day's slot transitions, committed ONCE; on any refusal, nothing.
    """
    settings = calendar_settings_service.load_calendar_settings(identity.client)
    result = portal_schedule_service.close_day(
        db, identity.client.id, settings, day)
    if not result.ok:
        raise HTTPException(status_code=422, detail=result.detail)
    return CloseDayView(
        day=day,
        already_closed=result.already_closed,
        blocked_count=result.blocked_count,
        booked_remaining=[
            BookedWindowView(start_datetime=start, end_datetime=end)
            for start, end in result.booked_remaining
        ],
    )


@router.post("/schedule/days/{day}/reopen", response_model=ReopenDayView)
def portal_reopen_day(
    day: date,
    identity: PortalIdentity = Depends(require_portal_identity),
    db: Session = Depends(get_db),
):
    """
    Purpose: Slice 4D-B - remove the operational closed_days restriction
        for an office-local date. NOTHING else changes: no slot is read,
        locked, or unblocked (blocked rows carry no provenance - the
        approved Reopen ruling), and the recurring configuration is never
        modified. Idempotent: reopening a not-closed date reports
        was_closed=false.
    Authorization: require_portal_identity (same as close).
    Failures: 401/503 as on every portal endpoint; database errors
        propagate after rollback.
    Database effects: at most one surgical closed_days write, ONE commit;
        ZERO appointment_slots statements.
    """
    settings = calendar_settings_service.load_calendar_settings(identity.client)
    result = portal_schedule_service.reopen_day(
        db, identity.client.id, settings, day)
    if not result.ok:
        raise HTTPException(status_code=422, detail=result.detail)
    return ReopenDayView(
        day=day,
        was_closed=result.was_closed,
        recurring_configured=result.recurring_configured,
    )
