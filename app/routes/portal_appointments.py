# app/routes/portal_appointments.py
#
# OWNER OF: the HTTP surface for the authenticated Office Portal READ-ONLY
# appointments slice (Portal Appointments v1). This file only binds transport
# inputs, delegates every query rule to the existing repository/service
# owners, and shapes the ONE approved response view - the same transport-only
# role app/routes/portal.py and app/routes/portal_leads.py already have
# (Rule 2/3). It performs no database write and defines no new query logic.
#
# Endpoint (GET, requires Authorization: Bearer <Supabase access token>,
# READ-ONLY):
#   GET /portal/appointments        the office's appointments in a local-day
#                                    range (default: today .. today+6, seven
#                                    inclusive local calendar days).
#
# TENANT BINDING: authentication and tenant resolution are REUSED, unchanged,
# from the frozen P2/P3-A/P3-B owners - require_portal_identity
# (app/routes/portal.py) -> portal_auth.authenticate_portal_request. The
# verified credential ALONE determines the tenant (identity.client). This
# endpoint declares NO client_id, client_key, or any other tenant selector;
# undeclared query parameters are ignored by FastAPI, so a stray
# ?client_id=... changes nothing (Rule 15). Everything queried is scoped by
# identity.client.id through the existing tenant-scoped repository read.
#
# LEAK PREVENTION: PortalAppointmentView below is the COMPLETE approved field
# set for this surface, and every instance is constructed explicitly field by
# field from the ORM row - never by splatting a row's __dict__. Deliberately
# EXCLUDED: client_id, slot_id, conversation_id, the RAW notify_error text,
# the per-channel office_sms_sent / office_email_sent / patient_sms_sent
# booleans (folded into a single safe derived outcome), created_at/updated_at,
# and every credential/settings/notification-recipient field. The
# leak-prevention tests pin this exact set.
#
# NOTIFICATION OUTCOME (MI-4/MI-5, determined from 0316b36c - not invented):
# the appointment row is the display truth. notification_service owns the
# projection that writes office_sms_sent / office_email_sent / notify_error
# from the notification_attempts ledger, and its closed notify_error
# vocabulary already ENCODES channel applicability: a "skipped (no ...
# configured)" entry means that channel is NOT applicable (no recipient), a
# "send_failed" entry means an applicable channel did not complete, and a
# True flag means an applicable channel was sent. derive_notification_outcome
# therefore reads ONLY the sanitized projection (Rule 3: sanitize is the
# single output-boundary owner) and never re-reads live client config and
# never returns raw text.

import uuid
from datetime import date, datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

# Reused P2/P3 owners: the per-request session factory and the ONE portal
# identity dependency. Importing the SAME callables (not copies) keeps this
# router covered by any dependency override applied to the portal router in
# tests, and keeps portal_auth the single authentication owner.
from app.routes.portal import get_db, require_portal_identity
from app.services.portal_auth import PortalIdentity
from app.repositories import appointment_repository
# Imported as a MODULE (not `from ... import client_now`) so that the route's
# reference to client_now / local_day_utc_window / etc. resolves through the
# settings-service module attribute at CALL TIME. This keeps the settings
# service the single live owner of "now" and the local-day window (Rule 3):
# a test (or any future seam) that substitutes
# calendar_settings_service.client_now is genuinely observed by the route,
# instead of the route holding an already-bound copy that a module-level
# monkeypatch could not reach.
from app.services import calendar_settings_service
# The single output-boundary owner of appointments.notify_error and the two
# vocabulary constants used to classify the SANITIZED value. Importing (not
# re-declaring) keeps notification_service the single owner of that
# vocabulary (Rule 3). Only the sanitized value is ever classified, so raw
# provider text can never reach a portal response.
from app.services.notification_service import (
    sanitize_stored_notify_error,
    NOTIFY_ERROR_WITHHELD,
    SEND_FAILED,
)

router = APIRouter(prefix="/portal", tags=["office-portal"])

# The user-facing default window is SEVEN inclusive local calendar days:
# today plus the next six. end_day = today + DEFAULT_RANGE_DAYS_INCLUSIVE
# where the offset is 6 (NOT 7 - that would be an 8-day range). Named here so
# the value is configurable in one reviewable place (Rule 4: no magic number).
DEFAULT_RANGE_DAYS_INCLUSIVE = 6

# The safe derived notification outcome vocabulary - the ONLY values
# notification_outcome can carry (Rule 4/16 closed set). Raw notify_error text
# is never among them.
OUTCOME_SENT = "sent"          # Every applicable office channel completed.
OUTCOME_FAILED = "failed"      # An applicable office channel did not complete.
OUTCOME_PENDING = "pending"    # No notification outcome recorded yet.


class PortalAppointmentView(BaseModel):
    """The COMPLETE approved appointment shape for the office portal. Nothing
    sensitive belongs here BY CONSTRUCTION: no tenant identifiers, no slot or
    conversation ids, no raw notify_error, no per-channel send booleans, no
    credentials. Adding a field is a reviewed contract change. The
    leak-prevention test pins this exact field set."""
    appointment_id: uuid.UUID
    patient_name: str
    patient_phone: str
    patient_email: Optional[str]
    new_or_returning: Optional[str]
    reason: Optional[str]
    urgency: str
    # UTC ISO-8601 instants (aware). The FRONTEND renders these in
    # timezone_name (returned on the envelope), never in the device timezone.
    start_datetime: datetime
    end_datetime: datetime
    status: str
    # UTC instant of the first staff pending->confirmed action; null =
    # never staff-confirmed (includes auto-confirmed appointments). Display
    # only - the portal performs no confirmation (read-only v1).
    confirmed_at: Optional[datetime]
    source: str
    # The single safe derived outcome (sent | failed | pending). Never the
    # raw notify_error text and never the individual channel booleans.
    notification_outcome: str


class PortalAppointmentListView(BaseModel):
    """The list envelope: the office timezone the frontend must format times
    in, the echoed local-day bounds actually queried, and the page itself.
    There is deliberately no tenant field on this surface."""
    timezone_name: str
    start_day: date
    end_day: date
    appointments: List[PortalAppointmentView]


def derive_notification_outcome(sanitized_notify_error: Optional[str],
                                office_sms_sent: bool,
                                office_email_sent: bool) -> str:
    """
    Purpose: THE single mapping (Rule 3) from an appointment's SANITIZED
        notification projection to the safe closed outcome vocabulary. The
        appointment row is the notification display truth (MI-4): its
        office_*_sent flags and notify_error are recomputed by
        notification_service from the notification_attempts ledger.
    Inputs:
        sanitized_notify_error: the value AFTER sanitize_stored_notify_error -
            None (nothing recorded, or all channels succeeded), one of the
            approved closed-vocabulary entries, or the fixed withheld marker
            for a malformed legacy value. RAW provider text can never arrive
            here.
        office_sms_sent, office_email_sent: the monotonic per-channel sent
            flags from the same row.
    Returns (closed set OUTCOME_*):
        "sent"    - a channel completed AND no send-failure entry is recorded
                    (all APPLICABLE channels succeeded; a "skipped" channel is
                    not applicable and does not block "sent").
        "failed"  - a send_failure entry is recorded for an applicable
                    channel, OR the withheld marker is present (a malformed
                    legacy value - an honest non-success the office should
                    follow up on). Never leaks raw text.
        "pending" - nothing actionable recorded yet: no send-failure entry and
                    neither flag set.
    Applicability semantics (MI-5, from 0316b36c, NOT invented): the closed
        notify_error vocabulary already encodes applicability. A "skipped (no
        notification_phone/email configured)" entry marks a channel that is
        NOT applicable; it is not a failure. A skipped-only projection (no
        send_failed entry) therefore never yields "failed": it is "sent" when
        an applicable channel actually sent, else "pending".
    Database effects: none (pure). External effects: none (pure).
    """
    # No projection recorded at all: sent if a flag is set, else pending.
    if sanitized_notify_error is None:
        if office_sms_sent or office_email_sent:
            return OUTCOME_SENT
        return OUTCOME_PENDING

    # A malformed legacy value became the fixed withheld marker: surface as
    # failed WITHOUT leaking raw text (the marker is already safe wording).
    if sanitized_notify_error == NOTIFY_ERROR_WITHHELD:
        return OUTCOME_FAILED

    # An approved vocabulary value: a send_failure for an applicable channel
    # is a failure; a skipped-only value is not.
    entries = sanitized_notify_error.split("; ")
    has_send_failure = any(SEND_FAILED in entry for entry in entries)
    if has_send_failure:
        return OUTCOME_FAILED
    if office_sms_sent or office_email_sent:
        return OUTCOME_SENT
    return OUTCOME_PENDING


def build_portal_appointment_view(a) -> PortalAppointmentView:
    """
    Purpose: The ONE public mapping from an Appointment ORM row to the
        approved portal appointment surface, SHARED (Rule 3) by the read GET
        below and the P5-A Confirm/Cancel action routes
        (app/routes/portal_appointment_actions.py) so the two surfaces can
        never drift. Explicit field-by-field construction so a
        new model column can never leak into a portal response by accident
        (the portal_leads _summary_view convention). The notify_error column
        is sanitized at the single output boundary and then reduced to the
        safe derived outcome - the raw value never enters the response.
    Database effects: none (attribute reads on an already-loaded row).
    """
    sanitized = sanitize_stored_notify_error(a.notify_error)
    return PortalAppointmentView(
        appointment_id=a.id,
        patient_name=a.patient_name,
        patient_phone=a.patient_phone,
        patient_email=a.patient_email,
        new_or_returning=a.new_or_returning,
        reason=a.reason,
        urgency=a.urgency,
        start_datetime=calendar_settings_service.ensure_utc(a.start_datetime),
        end_datetime=calendar_settings_service.ensure_utc(a.end_datetime),
        status=a.status,
        confirmed_at=(calendar_settings_service.ensure_utc(a.confirmed_at)
                      if a.confirmed_at is not None else None),
        source=a.source,
        notification_outcome=derive_notification_outcome(
            sanitized,
            bool(a.office_sms_sent),
            bool(a.office_email_sent),
        ),
    )


@router.get("/appointments", response_model=PortalAppointmentListView)
def portal_list_appointments(
    start_day: Optional[date] = Query(default=None),
    end_day: Optional[date] = Query(default=None),
    identity: PortalIdentity = Depends(require_portal_identity),
    db: Session = Depends(get_db),
):
    """
    Purpose: The authenticated office's READ-ONLY appointments for a local-day
        range - the smallest sell-critical portal view that lets an office
        operate Mia (see who booked, when, and whether the office was
        notified).
    Inputs: the Authorization header (consumed by require_portal_identity) and
        the two OPTIONAL local-day bounds. There is deliberately no tenant
        parameter to declare, so none can be honored: the verified identity
        ALONE selects the tenant (Rule 15).
    Range semantics:
        - Both omitted -> today .. today+6 (SEVEN inclusive local calendar
          days), where "today" is the office's CURRENT local date computed
          through client_now in the OFFICE timezone (never server or browser
          time).
        - Either supplied -> both must be supplied together (supplying only
          one is a 422; a partial range would be ambiguous - Rule 4, no
          guessing). end_day < start_day is a 422.
        - Boundaries are DST-safe: every UTC window boundary comes from the
          single owner local_day_utc_window (Rule 3 / Rule 9). end_day's
          window END is used, so the entire final local day is included.
    Returns: PortalAppointmentListView (timezone_name + echoed bounds + page).
    Database effects: ONE tenant-scoped SELECT via
        appointment_repository.list_appointments_between. No write.
    Possible failures: 422 for a partial or reversed range; 401 for every
        credential failure (indistinguishable by design); 503 for missing
        server auth configuration; database errors propagate (fail closed).
    """
    settings = calendar_settings_service.load_calendar_settings(identity.client)

    # Resolve the range. Partial supply is refused rather than guessed.
    if (start_day is None) != (end_day is None):
        raise HTTPException(
            status_code=422,
            detail="start_day and end_day must be supplied together.",
        )
    if start_day is None:
        # Default: today .. today+6 in the OFFICE timezone.
        today_local = calendar_settings_service.client_now(settings).date()
        start_day = today_local
        end_day = today_local + timedelta(days=DEFAULT_RANGE_DAYS_INCLUSIVE)
    elif end_day < start_day:
        raise HTTPException(
            status_code=422,
            detail="end_day is before start_day.",
        )

    # DST-safe multi-day range (Rule 3 / Rule 9): start of start_day and end
    # of end_day, each from the single window owner. The end window is the
    # NEXT local midnight after end_day, so the entire final local day is
    # included (half-open start_utc <= t < end_utc).
    start_utc, _ = calendar_settings_service.local_day_utc_window(start_day, settings.timezone_name)
    _, end_utc = calendar_settings_service.local_day_utc_window(end_day, settings.timezone_name)

    rows = appointment_repository.list_appointments_between(
        db, identity.client.id, start_utc, end_utc
    )
    return PortalAppointmentListView(
        timezone_name=settings.timezone_name,
        start_day=start_day,
        end_day=end_day,
        appointments=[build_portal_appointment_view(a) for a in rows],
    )
