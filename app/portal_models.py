# app/portal_models.py
#
# OWNER OF: Office Portal data structures (tables, role names, no logic).
# This file defines WHAT the portal identity layer stores. It contains no
# authentication or authorization logic - that single owner is
# app/services/portal_auth.py (Rule 3), mirroring how calendar_models.py
# owns shape while calendar_admin_auth.py owns the rules.
#
# Passwords, reset tokens, sessions, and provider tokens are DELIBERATELY
# not representable here: Supabase Auth owns credentials end to end, so a
# database compromise of the Mia schema cannot leak a portal password.

import uuid

from sqlalchemy import Boolean, Column, DateTime, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import declarative_base

# F-P2-3: office_users deliberately lives on its OWN declarative base, NOT on
# app.database.Base. Startup runs Base.metadata.create_all(bind=engine); if
# this table were registered there, an app-first deployment would auto-create
# office_users BEFORE migration 007, without the migration's RLS, CHECKs, and
# named indexes - silent schema drift, and 007 would then fail loudly later.
# Keeping the mapping off Base makes migration 007 the SOLE creation
# authority (rollout order: migrate first, deploy second - see the 007
# header and docs/PORTAL_AUTH_SETUP.md). Sessions do not care which base a
# mapped class came from, so ORM queries work unchanged.
PortalBase = declarative_base()


# ---------------------------------------------------------------------------
# Portal roles - the ONLY valid values for office_users.role (Rule 4/16
# closed vocabulary; the database CHECK in migration 007 enforces the same
# set, and the application refuses anything outside ALL independently).
# ---------------------------------------------------------------------------
class OfficeUserRole:
    OFFICE_ADMIN = "office_admin"   # V1: one administrator per office.

    ALL = {OFFICE_ADMIN}


class OfficeUser(PortalBase):
    """
    One Supabase-Auth-user -> one-office binding (migration 007).

    Rows are created only through the operator provisioning runbook
    (docs/PORTAL_AUTH_SETUP.md) - there is no self-registration path in the
    application, which is exactly how unrestricted public signup stays
    impossible at the data layer.
    """

    __tablename__ = "office_users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # The verified JWT "sub" claim (Supabase auth.users.id). Unique in V1:
    # one auth user can never silently belong to two offices.
    auth_user_id = Column(UUID(as_uuid=True), nullable=False, unique=True,
                          index=True)

    # Client isolation (Rule 15): every portal query resolves the tenant
    # FROM THIS ROW, never from anything the browser supplied.
    # NO SQLAlchemy ForeignKey object ON PURPOSE: the real FK to clients(id)
    # is enforced by the DATABASE via migration 007. An ORM-level FK cannot
    # resolve across declarative bases (clients lives on app.database.Base),
    # and portal_auth joins with an explicit onclause, so nothing needs it.
    client_id = Column(UUID(as_uuid=True), nullable=False, index=True)

    role = Column(String, nullable=False,
                  server_default=OfficeUserRole.OFFICE_ADMIN,
                  default=OfficeUserRole.OFFICE_ADMIN)

    # Deactivation mirrors calendar_admin_credentials (005): active flips
    # False, deactivated_at records when. The CHECK forbids the inconsistent
    # combination; portal_auth ALSO rejects both independently (fail closed
    # against manual corruption).
    active = Column(Boolean, nullable=False, server_default=text("true"),
                    default=True)
    created_at = Column(DateTime(timezone=True), server_default=text("now()"),
                        nullable=False)
    deactivated_at = Column(DateTime(timezone=True), nullable=True)
