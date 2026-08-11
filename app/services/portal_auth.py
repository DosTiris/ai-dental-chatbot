# app/services/portal_auth.py
#
# OWNER OF: Office Portal authentication and tenant binding (P2).
#
# This module is the SINGLE owner (Rule 3) of every rule that decides whether
# a portal request is authenticated and WHICH office it belongs to:
#
#   - server auth configuration reading + validation   (_load_config)
#   - Authorization: Bearer header parsing             (extract_bearer_token)
#   - Supabase JWT verification (HS256 secret or JWKS) (verify_portal_token)
#   - subject -> office_users -> clients resolution    (resolve_office_identity)
#   - user active / client active fail-closed checks   (resolve_office_identity)
#
# app/routes/portal.py contains ONLY transport wiring (Header binding and
# session injection); it repeats none of the logic above - the same split as
# calendar.py / calendar_admin_auth.py.
#
# IDENTITY MODEL (approved P2 design):
#   * Identities and passwords live in Supabase Auth. This backend NEVER sees
#     a password, stores no tokens, and holds no service-role key: it only
#     VERIFIES access tokens the browser obtained from Supabase directly.
#   * The verified "sub" claim is looked up in office_users (migration 007);
#     that row - never anything the browser sent - determines client_id.
#   * client_key stays a public widget identifier, ADMIN_API_KEY stays the
#     Dos Tiris operator key, mia_cal_ keys stay the Calendar staff API
#     credential. None of them can authenticate here: they are not JWTs
#     signed with the project key, so verification rejects them like any
#     other malformed token. This module imports none of those systems.
#
# FAILURE CONTRACT (mirrors calendar_admin_auth):
#   * EVERY credential failure - missing header, malformed header, malformed
#     token, wrong signature, expired, wrong audience, missing/invalid sub,
#     unknown subject, inactive binding, deactivated binding, inactive
#     client - returns EXACTLY 401 INVALID_PORTAL_CREDENTIALS_DETAIL, so
#     none of them can be told apart (no enumeration oracle).
#   * Missing/ambiguous SERVER configuration is 503 - an operator problem,
#     never blamed on the caller's credential, and never fail-open.
#   * Database errors roll the session back and propagate as server errors.

import os
import uuid
from dataclasses import dataclass
from typing import Optional, Tuple

import jwt as pyjwt
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models import Client
from app.portal_models import OfficeUser, OfficeUserRole

# --- Server configuration (named env vars - Rule 4, no magic strings) ------
# Exactly ONE of the two verification sources must be configured:
#   ENV_JWT_SECRET: the Supabase project "JWT Secret" (legacy/HS256 projects).
#   ENV_JWKS_URL:   https://<project-ref>.supabase.co/auth/v1/.well-known/jwks.json
#                   (projects migrated to asymmetric signing keys).
# Neither value is ever sent to a browser; the secret never leaves the server.
ENV_JWT_SECRET = "SUPABASE_JWT_SECRET"
ENV_JWKS_URL = "SUPABASE_JWKS_URL"
ENV_AUDIENCE = "PORTAL_JWT_AUDIENCE"          # optional override
# F-P2-2: the EXACT expected Supabase Auth issuer, e.g.
#   https://<project-ref>.supabase.co/auth/v1
# Always required: a signature alone proves "signed with our key", while the
# issuer pin also rejects tokens minted by any other environment that might
# ever share key material (staging vs production). Never inferred from the
# request - trust flows from configuration only.
ENV_ISSUER = "SUPABASE_AUTH_ISSUER"
DEFAULT_AUDIENCE = "authenticated"            # Supabase access-token default

HS256_ALGORITHMS = ["HS256"]
JWKS_ALGORITHMS = ["RS256", "ES256"]          # Supabase asymmetric options

INVALID_PORTAL_CREDENTIALS_DETAIL = "Invalid portal credentials."
AUTH_NOT_CONFIGURED_DETAIL = (
    "Portal authentication is not configured correctly on the server."
)

# Injection seam for the JWKS client so tests (and any future key-rotation
# tooling) can substitute the fetcher without network access. Cached per URL:
# PyJWKClient itself caches fetched keys.
_jwks_client_factory = pyjwt.PyJWKClient
_jwks_clients: dict = {}


def _invalid_credentials() -> HTTPException:
    """One constructor for the single indistinguishable credential failure."""
    return HTTPException(status_code=401,
                         detail=INVALID_PORTAL_CREDENTIALS_DETAIL)


@dataclass(frozen=True)
class PortalIdentity:
    """The authenticated result routes consume: the tenant row itself plus
    the binding row and the informational token email (verified-signature
    provenance, still only display data - never an authorization input)."""
    client: Client
    office_user: OfficeUser
    email: Optional[str]


def _load_config() -> Tuple[Optional[str], Optional[str], str]:
    """
    Purpose: Read and validate the server-side verification configuration.
    Returns: (jwt_secret, jwks_url, audience, issuer) with exactly one of
             the first two non-empty and issuer always non-empty.
    Failures: HTTPException 503 when NEITHER or BOTH key sources are
        configured, or when the expected issuer is missing (F-P2-2) -
        ambiguous/incomplete configuration is refused rather than guessed
        (Rule 4: no hidden behavior), and nothing ever fails open.
    """
    secret = (os.getenv(ENV_JWT_SECRET) or "").strip()
    jwks_url = (os.getenv(ENV_JWKS_URL) or "").strip()
    audience = (os.getenv(ENV_AUDIENCE) or "").strip() or DEFAULT_AUDIENCE
    issuer = (os.getenv(ENV_ISSUER) or "").strip()
    if bool(secret) == bool(jwks_url) or not issuer:
        raise HTTPException(status_code=503,
                            detail=AUTH_NOT_CONFIGURED_DETAIL)
    return (secret or None, jwks_url or None, audience, issuer)


def extract_bearer_token(authorization: Optional[str]) -> str:
    """
    Purpose: Parse the Authorization header into the raw bearer token.
    Inputs:  the header value; None when absent (optional at the FastAPI
             layer so a MISSING header reaches the single 401, never a 422 -
             the calendar transport convention).
    Returns: the token string.
    Failures: 401 for missing/blank headers, non-Bearer schemes, and
        malformed values - all indistinguishable.
    """
    value = (authorization or "").strip()
    if not value:
        raise _invalid_credentials()
    parts = value.split()
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
        raise _invalid_credentials()
    return parts[1]


def verify_portal_token(raw_token: str) -> dict:
    """
    Purpose: Verify a Supabase access token and return its claims.
    Inputs:  the raw JWT from extract_bearer_token.
    Returns: the verified claims dict.
    Verification: signature (HS256 project secret OR JWKS asymmetric key),
        exp (required, enforced), aud == configured audience, iss == the
        configured Supabase Auth issuer (required - missing or wrong issuer
        is a 401, F-P2-2), sub required, and is_anonymous must be absent or
        false: the Office Portal is for INVITED permanent identities, so an
        otherwise-valid Supabase anonymous session can never authenticate.
        No other claim is trusted for authorization; in particular any role
        claim inside the token is IGNORED - the application role comes from
        office_users only (audit-preserved).
    Failures: 503 for server misconfiguration (_load_config); 401 for every
        token problem, including JWKS retrieval errors - a key-fetch failure
        must never fail open, and surfacing it separately would create an
        infrastructure oracle.
    """
    secret, jwks_url, audience, issuer = _load_config()
    try:
        if secret is not None:
            key = secret
            algorithms = HS256_ALGORITHMS
        else:
            client = _jwks_clients.get(jwks_url)
            if client is None:
                client = _jwks_client_factory(jwks_url)
                _jwks_clients[jwks_url] = client
            key = client.get_signing_key_from_jwt(raw_token).key
            algorithms = JWKS_ALGORITHMS
        claims = pyjwt.decode(
            raw_token,
            key,
            algorithms=algorithms,
            audience=audience,
            issuer=issuer,
            options={"require": ["exp", "sub", "iss"]},
        )
    except HTTPException:
        raise
    except Exception:
        # Every JWT/JWKS failure collapses to the one credential 401.
        raise _invalid_credentials()
    # F-P2-2: reject Supabase anonymous sessions outright. Checked AFTER
    # signature verification so the claim cannot be spoofed, and treated as
    # the same indistinguishable credential failure as every other reject.
    if claims.get("is_anonymous"):
        raise _invalid_credentials()
    return claims


def resolve_office_identity(db: Session, claims: dict) -> PortalIdentity:
    """
    Purpose: Map verified claims to the ONE office this user may act for.
    Inputs:  request session; verified claims (verify_portal_token output).
    Returns: PortalIdentity - routes must use ONLY identity.client and must
             never accept a caller-supplied tenant.
    Database effects: one SELECT on office_users (unique auth_user_id index)
        joined to clients. No writes.
    Failures: 401 INVALID_PORTAL_CREDENTIALS_DETAIL for: non-UUID sub,
        unknown subject, binding not active, binding deactivated_at set
        (checked INDEPENDENTLY of active, failing closed against corrupted
        rows exactly like calendar_admin_auth), unknown role value outside
        the closed vocabulary, and inactive client. Database errors roll
        back and propagate (never converted to 401, never fail open).
    """
    try:
        subject = uuid.UUID(str(claims.get("sub")))
    except (TypeError, ValueError):
        raise _invalid_credentials()

    try:
        row = (
            db.query(OfficeUser, Client)
            .join(Client, OfficeUser.client_id == Client.id)
            .filter(OfficeUser.auth_user_id == subject)
            .first()
        )
    except Exception:
        db.rollback()
        raise

    if row is None:
        raise _invalid_credentials()

    office_user, client = row

    if office_user.active is not True:
        raise _invalid_credentials()
    if office_user.deactivated_at is not None:
        raise _invalid_credentials()
    if office_user.role not in OfficeUserRole.ALL:
        raise _invalid_credentials()          # closed vocabulary (Rule 16)
    if client.active is not True:
        raise _invalid_credentials()

    email = claims.get("email")
    return PortalIdentity(client=client, office_user=office_user,
                          email=email if isinstance(email, str) else None)


def authenticate_portal_request(db: Session,
                                authorization: Optional[str]) -> PortalIdentity:
    """The one entry point transport code calls: header -> token -> claims ->
    tenant-bound identity. Composition only; every rule lives above."""
    token = extract_bearer_token(authorization)
    claims = verify_portal_token(token)
    return resolve_office_identity(db, claims)
