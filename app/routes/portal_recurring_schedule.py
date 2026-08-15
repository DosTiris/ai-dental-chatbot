# app/routes/portal_recurring_schedule.py
#
# OWNER OF: the HTTP surface for Office Portal RECURRING SCHEDULE management
# (P4-B). Transport wiring ONLY (Rule 2/3): it binds inputs, invokes the single
# rule owner (app/services/portal_recurring_schedule_service.py), and shapes the
# approved response - the same transport-only role portal.py / portal_leads.py /
# portal_notification_settings.py have. It repeats none of the service's rules.
#
# Endpoints (all require Authorization: Bearer <Supabase access token>, all
# office_admin-guarded IN THE SERVICE, prefix /portal):
#   GET  /portal/schedule/recurring          surface the current config
#   PUT  /portal/schedule/recurring          save config only (atomic CAS)
#   POST /portal/schedule/recurring/preview  advisory read-only snapshot (body {})
#   POST /portal/schedule/recurring/apply    materialize the horizon
#
# TENANT BINDING is REUSED from the frozen P2/P3-A owners - require_portal_identity
# (app/routes/portal.py) -> portal_auth. The credential ALONE selects the tenant.
# No endpoint declares a client id; the write bodies FORBID unknown fields
# (extra="forbid"), so a smuggled tenant key is a 422, never a silent tenant
# change (Rule 15). The opaque config token is a STRING on the wire (A1 form);
# the browser echoes it verbatim and never parses it.

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

# Reused P2/P3-A owners: same session factory and identity dependency, so this
# router is covered by any portal dependency override in tests and portal_auth
# stays the single authentication owner.
from app.routes.portal import get_db, require_portal_identity
from app.services import portal_recurring_schedule_service as recurring_service
from app.services.portal_auth import PortalIdentity

router = APIRouter(prefix="/portal", tags=["office-portal"])


class RecurringConfigView(BaseModel):
    """The COMPLETE approved response slice. schedule_config_updated_at is the
    opaque token rendered in the A1 wire form (str|null) by the service."""
    weekly_hours: Dict[str, Any]
    slot_minutes: int
    closures: List[Any]
    schedule_config_updated_at: Optional[str]


class RecurringConfigWriteBody(BaseModel):
    """PUT body: a CLOSED vocabulary. extra="forbid" makes any additional
    property (including a smuggled client id) a 422 with no write. All fields
    are REQUIRED; weekly_hours/slot_minutes/closures are validated by the
    service (so the caller-safe INVALID_CONFIG wording is single-owned there),
    and the token is validated by the service's A1 parser."""
    model_config = ConfigDict(extra="forbid")

    weekly_hours: Any = Field(...)
    slot_minutes: Any = Field(...)
    closures: Any = Field(...)
    expected_schedule_config_updated_at: Any = Field(...)


class PreviewBody(BaseModel):
    """Preview body must be EXACTLY {} (F2): no fields, unknown keys forbidden."""
    model_config = ConfigDict(extra="forbid")


class ApplyBody(BaseModel):
    """Apply body: exactly the required (nullable) expected token."""
    model_config = ConfigDict(extra="forbid")

    expected_schedule_config_updated_at: Any = Field(...)


def _view(result) -> RecurringConfigView:
    """One explicit mapping from the service view to the approved slice."""
    return RecurringConfigView(
        weekly_hours=result.weekly_hours,
        slot_minutes=result.slot_minutes,
        closures=result.closures,
        schedule_config_updated_at=result.schedule_config_updated_at,
    )


@router.get("/schedule/recurring", response_model=RecurringConfigView)
def get_recurring(
    identity: PortalIdentity = Depends(require_portal_identity),
):
    """Return the office's current recurring config (first-writer safety).
    Failures: 401; 403 non-admin; 422 MALFORMED_STORED_CONFIG; 503."""
    return _view(recurring_service.get_recurring_config(identity))


@router.put("/schedule/recurring", response_model=RecurringConfigView)
def put_recurring(
    body: RecurringConfigWriteBody,
    identity: PortalIdentity = Depends(require_portal_identity),
    db: Session = Depends(get_db),
):
    """Save config only under optimistic concurrency (never materializes slots).
    Failures: 401; 403; 422 INVALID_CONFIG; 409 STALE_CONFIG; 500; 503."""
    result = recurring_service.put_recurring_config(
        db, identity,
        body.weekly_hours, body.slot_minutes, body.closures,
        body.expected_schedule_config_updated_at,
    )
    return _view(result)


@router.post("/schedule/recurring/preview")
def preview_recurring(
    body: PreviewBody,
    identity: PortalIdentity = Depends(require_portal_identity),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Advisory read-only snapshot over the Apply horizon (F2). No mutation.
    Failures: 401; 403; 422 (INVALID_CONFIG pre-Save geometry / MALFORMED_STORED_
    CONFIG); 503."""
    return recurring_service.preview_recurring_config(db, identity)


@router.post("/schedule/recurring/apply")
def apply_recurring(
    body: ApplyBody,
    identity: PortalIdentity = Depends(require_portal_identity),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Materialize the horizon (per-day commits; delegates to frozen P4-A).
    Failures: 401; 403; 409 CONFIG_NOT_SAVED/STALE_CONFIG; 422 MALFORMED_STORED_
    CONFIG; 500; 503. Returns 200 even when days are skipped/blocked."""
    return recurring_service.apply_recurring_config(
        db, identity, body.expected_schedule_config_updated_at)
