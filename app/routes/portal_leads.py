# app/routes/portal_leads.py
#
# OWNER OF: the HTTP surface for the Office Portal READ-ONLY data slice
# (P3-B1: Dashboard + Leads list + Lead detail). This file only binds
# transport inputs, delegates every query rule to
# app/services/portal_leads_service.py, and shapes the approved response
# views - the same transport-only role app/routes/portal.py has for the
# identity surface (Rule 2/3).
#
# Endpoints (all GET, all require Authorization: Bearer <Supabase access
# token>, all READ-ONLY - this router performs no database write):
#   GET /portal/dashboard          tenant-scoped counts + recent leads
#   GET /portal/leads              paginated/searchable/filterable leads
#   GET /portal/leads/{lead_id}    one lead + its message transcript
#
# TENANT BINDING: authentication and tenant resolution are REUSED from the
# frozen P2/P3-A owners - require_portal_identity (app/routes/portal.py) ->
# portal_auth.authenticate_portal_request. The credential ALONE determines
# the tenant. No endpoint here declares a client_id, client_key, or any
# other tenant selector; undeclared query parameters are ignored by
# FastAPI, so a stray ?client_id=... changes nothing (Rule 15).
#
# LEAK PREVENTION: every response model below is the COMPLETE approved
# field set for its surface, and every model instance is constructed
# explicitly field by field from the ORM row - never by splatting a row's
# __dict__. Deliberately excluded everywhere: client_id, api_key/client_key,
# settings, notification_email/notification_phone, visitor_id, the
# lead_*_source_text audit-evidence columns, booking_* state columns, and
# every credential of any kind. The leak-prevention tests pin these sets.

import uuid
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

# Reused P2/P3-A owners: the per-request session factory and the ONE portal
# identity dependency. Importing the SAME callables (not copies) keeps this
# router covered by any dependency override applied to the portal router in
# tests, and keeps portal_auth the single authentication owner.
from app.routes.portal import get_db, require_portal_identity
from app.services import portal_leads_service as leads_service
from app.services.portal_auth import PortalIdentity

router = APIRouter(prefix="/portal", tags=["office-portal"])


class PortalLeadSummaryView(BaseModel):
    """The COMPLETE approved lead summary shape (list rows, dashboard
    recent-lead rows, and the base of the detail view). The leak-prevention
    test pins this exact field set."""
    lead_id: uuid.UUID
    lead_name: Optional[str]
    lead_phone: Optional[str]
    lead_email: Optional[str]
    lead_reason: Optional[str]
    lead_status: Optional[str]
    lead_patient_type: Optional[str]
    lead_time_window: Optional[str]
    lead_is_emergency: bool
    lead_is_priority: bool
    lead_is_outside_hours: bool
    lead_outside_hours_note: Optional[str]
    lead_email_opt_out: bool
    last_lead_at: Optional[datetime]
    created_at: Optional[datetime]


class PortalLeadMessageView(BaseModel):
    """One transcript line: exactly who said what, when - nothing else."""
    role: str
    content: str
    created_at: Optional[datetime]


class PortalLeadDetailView(PortalLeadSummaryView):
    """The lead detail: the approved summary fields plus the transcript.
    The transcript is BOUNDED (audit finding A2): messages holds at most
    portal_leads_service.TRANSCRIPT_MESSAGE_LIMIT rows, messages_total is
    the true transcript length, and messages_truncated says whether the
    bound cut it - so the frontend can state a partial transcript honestly
    instead of silently showing one."""
    messages: List[PortalLeadMessageView]
    messages_total: int
    messages_truncated: bool
    # P3-B2: the office workflow slice (migration 008). office_status is
    # office-owned and lives beside - never inside - Mia's lead_status.
    # The *_updated_at values double as the optimistic-concurrency tokens
    # the two PUT writers require.
    office_status: Optional[str]
    office_status_updated_at: Optional[datetime]
    office_note: Optional[str]
    office_note_updated_at: Optional[datetime]


class PortalLeadListView(BaseModel):
    """The paginated list envelope: the filtered total (for honest
    pagination) plus the echoed page bounds and the page itself."""
    total: int
    limit: int
    offset: int
    leads: List[PortalLeadSummaryView]


class PortalDashboardView(BaseModel):
    """The dashboard: the verified practice name, the four schema-backed
    counts, and the RECENCY-ordered recent-lead strip. urgent_leads counts
    leads flagged emergency or priority - deliberately NOT a lead_status
    count, because "new" only means intake is unfinished and "completed"
    only means Mia finished capturing the request (audit finding A1)."""
    practice_name: str
    total_conversations: int
    total_leads: int
    urgent_leads: int
    leads_last_7_days: int
    recent_leads: List[PortalLeadSummaryView]


class PortalLeadStatusWriteBody(BaseModel):
    """PUT /portal/leads/{id}/status body: ONLY the requested value and the
    concurrency token. Both fields are REQUIRED but nullable - the browser
    must always state which office_status_updated_at it last saw (null =
    "the office had never set a status when I loaded"). There is
    deliberately no tenant field; extra JSON keys are ignored by pydantic,
    so a smuggled client_id changes nothing (Rule 15)."""
    office_status: Optional[str] = Field(...)
    expected_office_status_updated_at: Optional[datetime] = Field(...)


class PortalLeadNoteWriteBody(BaseModel):
    """PUT /portal/leads/{id}/note body: the note text (null = explicit
    clear) and the office_note_updated_at token last observed. Same
    required-but-nullable and no-tenant-field rules as the status body."""
    office_note: Optional[str] = Field(...)
    expected_office_note_updated_at: Optional[datetime] = Field(...)


class PortalLeadWorkflowView(BaseModel):
    """Both writers' response: the COMPLETE office workflow slice as
    persisted - enough safe current state for the frontend to update its
    tokens without a refetch, and nothing else (no system fields, no
    tenant identifiers)."""
    lead_id: uuid.UUID
    office_status: Optional[str]
    office_status_updated_at: Optional[datetime]
    office_note: Optional[str]
    office_note_updated_at: Optional[datetime]


def _summary_view(conversation) -> PortalLeadSummaryView:
    """
    Purpose: The ONE mapping from a Conversation ORM row to the approved
        portal lead surface. Explicit field-by-field construction: a new
        model column can never leak into a portal response by accident.
    Database effects: none (attribute reads on an already-loaded row).
    """
    return PortalLeadSummaryView(
        lead_id=conversation.id,
        lead_name=conversation.lead_name,
        lead_phone=conversation.lead_phone,
        lead_email=conversation.lead_email,
        lead_reason=conversation.lead_reason,
        lead_status=conversation.lead_status,
        lead_patient_type=leads_service.derive_patient_type(conversation),
        lead_time_window=conversation.lead_time_window,
        lead_is_emergency=bool(conversation.lead_is_emergency),
        lead_is_priority=bool(conversation.lead_is_priority),
        lead_is_outside_hours=bool(conversation.lead_is_outside_hours),
        lead_outside_hours_note=conversation.lead_outside_hours_note,
        lead_email_opt_out=bool(conversation.lead_email_opt_out),
        last_lead_at=conversation.last_lead_at,
        created_at=conversation.created_at,
    )


@router.get("/dashboard", response_model=PortalDashboardView)
def portal_dashboard(
    identity: PortalIdentity = Depends(require_portal_identity),
    db: Session = Depends(get_db),
):
    """
    Purpose: The authenticated office's read-only landing data: WHO they
        are (server-verified practice name), the four trustworthy counts,
        and their most recent leads.
    Inputs: only the Authorization header, consumed by the dependency.
    Returns: PortalDashboardView.
    Database effects: two SELECTs (one aggregate, one recent-lead page).
    Possible failures: 401 for every credential failure (indistinguishable
        by design); 503 for missing server auth configuration; database
        errors propagate (fail closed).
    """
    counts = leads_service.get_dashboard_counts(db, identity.client)
    recent = leads_service.get_recent_leads(db, identity.client)
    return PortalDashboardView(
        practice_name=identity.client.practice_name,
        total_conversations=counts["total_conversations"],
        total_leads=counts["total_leads"],
        urgent_leads=counts["urgent_leads"],
        leads_last_7_days=counts["leads_last_7_days"],
        recent_leads=[_summary_view(row) for row in recent],
    )


@router.get("/leads", response_model=PortalLeadListView)
def portal_list_leads(
    status: Optional[str] = None,
    q: Optional[str] = None,
    days: Optional[int] = None,
    limit: int = leads_service.LIST_LIMIT_DEFAULT,
    offset: int = leads_service.OFFSET_MIN,
    identity: PortalIdentity = Depends(require_portal_identity),
    db: Session = Depends(get_db),
):
    """
    Purpose: The authenticated office's paginated lead list with optional
        status filter, literal substring search, and recency window.
    Inputs: the Authorization header (dependency) plus the declared filter
        parameters above - and ONLY those: there is deliberately no tenant
        parameter to declare, so none can be honored.
    Returns: PortalLeadListView.
    Database effects: two SELECTs (count + page) via the service owner.
    Possible failures: 400 with an explicit reason for any invalid filter
        (closed status vocabulary, oversized search, out-of-range
        days/limit/offset); 401/503 as on every portal endpoint; database
        errors propagate (fail closed).
    """
    total, rows = leads_service.list_leads(
        db,
        identity.client,
        status=status,
        q=q,
        days=days,
        limit=limit,
        offset=offset,
    )
    # Echo the SAME normalized bounds the query actually used so the
    # frontend paginates against reality, not against its own input.
    return PortalLeadListView(
        total=total,
        limit=limit,
        offset=offset,
        leads=[_summary_view(row) for row in rows],
    )


@router.get("/leads/{lead_id}", response_model=PortalLeadDetailView)
def portal_lead_detail(
    lead_id: uuid.UUID,
    identity: PortalIdentity = Depends(require_portal_identity),
    db: Session = Depends(get_db),
):
    """
    Purpose: One of the authenticated office's leads with its full message
        transcript.
    Inputs: the Authorization header (dependency) and the lead id path
        segment (a UUID by declaration; malformed ids are a validation
        error before any query runs).
    Returns: PortalLeadDetailView.
    Database effects: three SELECTs (lead row + transcript count + bounded
        transcript page) via the service.
    Possible failures: 404 "Lead not found." - identical for a foreign
        office's lead, a nonexistent id, and a non-lead conversation
        (Rule 15); 401/503 as on every portal endpoint; database errors
        propagate (fail closed).
    """
    conversation, messages, messages_total, messages_truncated = (
        leads_service.get_lead_detail(db, identity.client, lead_id)
    )
    summary = _summary_view(conversation)
    return PortalLeadDetailView(
        **summary.model_dump(),
        messages=[
            PortalLeadMessageView(
                role=message.role,
                content=message.content,
                created_at=message.created_at,
            )
            for message in messages
        ],
        messages_total=messages_total,
        messages_truncated=messages_truncated,
        office_status=conversation.office_status,
        office_status_updated_at=conversation.office_status_updated_at,
        office_note=conversation.office_note,
        office_note_updated_at=conversation.office_note_updated_at,
    )


def _workflow_view(conversation) -> PortalLeadWorkflowView:
    """The ONE mapping from a Conversation row to the office workflow
    response slice (explicit field-by-field, same leak rule as
    _summary_view)."""
    return PortalLeadWorkflowView(
        lead_id=conversation.id,
        office_status=conversation.office_status,
        office_status_updated_at=conversation.office_status_updated_at,
        office_note=conversation.office_note,
        office_note_updated_at=conversation.office_note_updated_at,
    )


@router.put("/leads/{lead_id}/status", response_model=PortalLeadWorkflowView)
def portal_write_lead_status(
    lead_id: uuid.UUID,
    body: PortalLeadStatusWriteBody,
    identity: PortalIdentity = Depends(require_portal_identity),
    db: Session = Depends(get_db),
):
    """
    Purpose: The office sets or clears its workflow status on ONE of its
        own leads - the first portal write path.
    Inputs: the Authorization header (dependency), the lead id path
        segment, and the two-field body above. The verified identity ALONE
        selects the tenant.
    Returns: PortalLeadWorkflowView (persisted state incl. fresh tokens).
    Database effects: one compare-and-set UPDATE + one re-read via the
        service owner; lead_status and note fields untouched.
    Possible failures: 400 (closed vocabulary; "new" is refused - portal
        clearing is null); 404 tenant-opaque; 409 stale token; 401/503 as
        on every portal endpoint; database errors propagate (fail closed).
    """
    conversation = leads_service.set_office_status(
        db,
        identity.client,
        lead_id,
        body.office_status,
        body.expected_office_status_updated_at,
    )
    return _workflow_view(conversation)


@router.put("/leads/{lead_id}/note", response_model=PortalLeadWorkflowView)
def portal_write_lead_note(
    lead_id: uuid.UUID,
    body: PortalLeadNoteWriteBody,
    identity: PortalIdentity = Depends(require_portal_identity),
    db: Session = Depends(get_db),
):
    """
    Purpose: The office saves or clears its ONE current note on ONE of its
        own leads (V1: no note history).
    Inputs: the Authorization header (dependency), the lead id path
        segment, and the two-field body above.
    Returns: PortalLeadWorkflowView (persisted state incl. fresh tokens).
    Database effects: one compare-and-set UPDATE + one re-read via the
        service owner; status fields untouched.
    Possible failures: 400 (whitespace-only or >2000 chars after trim);
        404 tenant-opaque; 409 stale token; 401/503 as on every portal
        endpoint; database errors propagate (fail closed).
    """
    conversation = leads_service.set_office_note(
        db,
        identity.client,
        lead_id,
        body.office_note,
        body.expected_office_note_updated_at,
    )
    return _workflow_view(conversation)
