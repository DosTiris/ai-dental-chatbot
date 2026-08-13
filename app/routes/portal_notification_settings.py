# app/routes/portal_notification_settings.py
#
# OWNER OF: the HTTP surface for Office Portal NOTIFICATION DESTINATION
# management (P6-A). Transport wiring only: it binds inputs, invokes the
# single rule owner (app/services/portal_notification_settings_service.py),
# and shapes the approved response - the same transport-only role
# app/routes/portal.py and app/routes/portal_leads.py have (Rule 2/3).
#
# Endpoints (both require Authorization: Bearer <Supabase access token>, both
# office_admin-guarded in the service):
#   GET /portal/notification-settings   read the office's own destinations
#   PUT /portal/notification-settings   replace both destinations (CAS)
#
# TENANT BINDING: authentication and tenant resolution are REUSED from the
# frozen P2/P3-A owners - require_portal_identity (app/routes/portal.py) ->
# portal_auth.authenticate_portal_request. The credential ALONE determines the
# tenant. No endpoint here declares a client_id, client_key, or any other
# tenant selector, and the PUT body FORBIDS unknown fields (extra="forbid"),
# so a smuggled tenant field is a 422, never a silent tenant change (Rule 15,
# owner decision C2). Stray undeclared query parameters are ignored by
# FastAPI and can never affect tenant selection.
#
# LEAK PREVENTION: the response model is the COMPLETE approved field set -
# exactly notification_email, notification_phone, and the concurrency token -
# built explicitly from the service result. Deliberately excluded: client_id,
# practice_name, settings, api_key, provider identifiers/credentials, the
# notification ledger, send booleans, and notify_error. The leak-prevention
# test pins this exact set.

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

# Reused P2/P3-A owners: the per-request session factory and the ONE portal
# identity dependency. Importing the SAME callables keeps this router covered
# by any dependency override applied to the portal router in tests, and keeps
# portal_auth the single authentication owner.
from app.routes.portal import get_db, require_portal_identity
from app.services import portal_notification_settings_service as settings_service
from app.services.portal_auth import PortalIdentity

router = APIRouter(prefix="/portal", tags=["office-portal"])


class NotificationSettingsView(BaseModel):
    """The COMPLETE approved response shape: the two destinations plus the
    server-owned concurrency token, and nothing else. The leak-prevention test
    pins this exact field set."""
    notification_email: Optional[str]
    notification_phone: Optional[str]
    notification_settings_updated_at: Optional[datetime]


class NotificationSettingsWriteBody(BaseModel):
    """PUT body: a CLOSED vocabulary of exactly three keys (owner decision
    C2). extra="forbid" makes any additional property - including a smuggled
    client_id/client_key/api_key - a 422 with no database write. All three
    fields are REQUIRED but nullable: the browser must always state each
    destination (null clears it) and which token it last saw (null = the
    office had never saved a destination when it loaded), reusing the P3-B2
    required-but-nullable convention."""
    model_config = ConfigDict(extra="forbid")

    notification_email: Optional[str] = Field(...)
    notification_phone: Optional[str] = Field(...)
    expected_notification_settings_updated_at: Optional[datetime] = Field(...)


def _view(result) -> NotificationSettingsView:
    """One mapping from the service result to the approved slice (explicit
    field-by-field: no model attribute can leak in by accident)."""
    return NotificationSettingsView(
        notification_email=result.notification_email,
        notification_phone=result.notification_phone,
        notification_settings_updated_at=result.notification_settings_updated_at,
    )


@router.get("/notification-settings", response_model=NotificationSettingsView)
def get_notification_settings(
    identity: PortalIdentity = Depends(require_portal_identity),
):
    """
    Purpose: Return the authenticated office's own notification destinations.
    Inputs:  only the Authorization header, consumed by the dependency.
    Returns: NotificationSettingsView (both destinations + the token).
    Possible failures: 401 for every credential failure; 403 for a non-admin
        role (D9/C5); 503 for missing server auth configuration.
    Database effects: none beyond the dependency's binding SELECT.
    """
    return _view(settings_service.get_notification_settings(identity))


@router.put("/notification-settings", response_model=NotificationSettingsView)
def put_notification_settings(
    body: NotificationSettingsWriteBody,
    identity: PortalIdentity = Depends(require_portal_identity),
    db: Session = Depends(get_db),
):
    """
    Purpose: Replace BOTH notification destinations under optimistic
        concurrency, refusing a result that leaves neither destination
        configured (owner decision D6).
    Inputs:  the closed three-key body; the authenticated identity; the
        request session.
    Returns: NotificationSettingsView built from the authoritative post-write
        re-read (never echoed request values).
    Possible failures: 401 credential; 403 non-admin (D9/C5); 422 malformed
        field, unknown body field, or both-empty result (no write); 409 stale
        token (no write); 503 server auth config; 500 if the authenticated
        client row vanished concurrently (fail closed, never a 404).
    Database effects: exactly the service's single atomic compare-and-set.
    """
    result = settings_service.set_notification_settings(
        db,
        identity,
        body.notification_email,
        body.notification_phone,
        body.expected_notification_settings_updated_at,
    )
    return _view(result)
