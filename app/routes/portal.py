# app/routes/portal.py
#
# OWNER OF: the HTTP surface for the authenticated Office Portal (P2
# foundation). This file only binds transport inputs and delegates - every
# authentication and tenant-binding rule lives in
# app/services/portal_auth.py (Rule 2/3, the calendar.py split).
#
# Endpoints (all require Authorization: Bearer <Supabase access token>):
#   GET /portal/me    authenticated office identity bootstrap
#
# The credential ALONE determines the tenant. No endpoint here takes a
# client_id, client_key, or any other tenant selector, and undeclared query
# parameters are ignored (FastAPI default) - so nothing on this surface can
# be pointed at another office (Rule 15).

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.services.portal_auth import (
    PortalIdentity,
    authenticate_portal_request,
)

router = APIRouter(prefix="/portal", tags=["office-portal"])


def get_db():
    """Standard per-request session, mirroring the existing routes."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def require_portal_identity(
    authorization: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> PortalIdentity:
    """
    Purpose: Transport wiring ONLY. Binds the OPTIONAL Authorization header -
        optional at the FastAPI validation layer so a MISSING header yields
        the same 401 as every other credential failure, never a 422 (the
        Patch 5 convention) - and the request session, then delegates every
        rule to the single owner: portal_auth.authenticate_portal_request.
    Returns: PortalIdentity - the ONE office this request may act for.
    Failures: 401 "Invalid portal credentials." for every credential
        failure; 503 when server auth configuration is absent/ambiguous;
        database errors propagate (fail closed).
    """
    return authenticate_portal_request(db, authorization)


class PortalMeView(BaseModel):
    """The COMPLETE approved response shape. Deliberately excludes api_key,
    settings, notification contacts, and every credential/secret - the
    leak-prevention test pins this exact field set."""
    client_id: uuid.UUID
    practice_name: str
    role: str
    email: Optional[str]


@router.get("/me", response_model=PortalMeView)
def portal_me(
    identity: PortalIdentity = Depends(require_portal_identity),
):
    """
    Purpose: Office Portal bootstrap - proves WHICH office the presented
        token belongs to, so the portal never asks the browser for (or
        trusts it about) a client_id.
    Inputs: only the Authorization header, consumed by the dependency.
    Returns: PortalMeView (client_id, practice_name, role, email).
    Database effects: none beyond the dependency's single binding SELECT.
    Possible failures: 401 for every credential failure (indistinguishable
        by design); 503 for missing server auth configuration.
    """
    return PortalMeView(
        client_id=identity.client.id,
        practice_name=identity.client.practice_name,
        role=identity.office_user.role,
        email=identity.email,
    )
