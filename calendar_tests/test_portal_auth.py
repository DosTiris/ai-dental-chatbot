# calendar_tests/test_portal_auth.py
#
# P2 (Office Portal auth foundation): proves the security invariants A-O of
# the P2 contract against the REAL portal router + portal_auth owner.
#
# GROUPS:
#   * Pure tests (no database): bearer parsing and the server-config gate.
#   * HTTP tests (requires_db, house harness): every credential failure is
#     the single indistinguishable 401; tenancy is resolved server-side from
#     office_users; client_key / ADMIN_API_KEY / mia_cal_ keys cannot
#     authenticate; a portal token cannot reach operator or Calendar admin
#     APIs; the response leaks no secrets.
#
# Tokens are minted locally with the SAME PyJWT library the verifier uses
# (HS256 with a test secret; one RS256/JWKS test with an in-memory keypair).
# No network, no real Supabase project, no provider is ever contacted.
#
# Run (owner-local, PowerShell 5.1):
#   $env:ALLOW_DESTRUCTIVE_CALENDAR_TESTS="yes"
#   $env:TEST_DATABASE_URL="postgresql://postgres:test@localhost:5433/mia_calendar_test"
#   python -m pytest calendar_tests\test_portal_auth.py -v

import os
import sys
import time
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from calendar_tests.conftest import requires_db  # noqa: E402  (env bootstrap)

# app.config needs DATABASE_URL at import; the pure tests never connect, so
# an unreachable placeholder keeps them runnable anywhere (SEC-1 pattern).
# setdefault never overrides the real test database when TEST_DATABASE_URL
# is set (conftest already exported it).
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://placeholder:placeholder@localhost:1/never_connected_placeholder",
)

import jwt as pyjwt  # noqa: E402

TEST_SECRET = "portal-test-secret-0123456789abcdef0123456789"  # >=32 bytes: HS256 guidance
OTHER_SECRET = "portal-wrong-secret-0123456789abcdef012345678"
AUDIENCE = "authenticated"
TEST_ISSUER = "https://p2-test-project.supabase.co/auth/v1"   # F-P2-2 issuer pin


def _token(sub, *, secret=TEST_SECRET, aud=AUDIENCE, exp_delta=300,
           email="office@example.test", algorithm="HS256", key=None,
           iss=TEST_ISSUER, is_anonymous=None, drop=()):
    """Mint a Supabase-shaped access token. `drop` removes claims to test
    the require-list; `key` overrides `secret` for asymmetric signing;
    `is_anonymous` mirrors Supabase anonymous-session tokens (F-P2-2)."""
    claims = {
        "sub": str(sub),
        "aud": aud,
        "exp": int(time.time()) + exp_delta,
        "email": email,
        "role": "authenticated",
        "iss": iss,
    }
    if is_anonymous is not None:
        claims["is_anonymous"] = is_anonymous
    for name in drop:
        claims.pop(name, None)
    return pyjwt.encode(claims, key if key is not None else secret,
                        algorithm=algorithm)


# ---------------------------------------------------------------------------
# Pure tests - no database, no HTTP.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("header", [
    None, "", "   ", "Bearer", "Bearer ", "Token abc",
    "abc.def.ghi", "Basic dXNlcjpwYXNz", "Bearer a b",
])
def test_extract_bearer_rejects_missing_and_malformed(header):
    from fastapi import HTTPException
    from app.services.portal_auth import (
        INVALID_PORTAL_CREDENTIALS_DETAIL, extract_bearer_token,
    )

    with pytest.raises(HTTPException) as excinfo:
        extract_bearer_token(header)
    assert excinfo.value.status_code == 401
    assert excinfo.value.detail == INVALID_PORTAL_CREDENTIALS_DETAIL


@pytest.mark.parametrize("secret,jwks,issuer", [
    ("", "", TEST_ISSUER),                 # no key source
    ("s", "https://x/jwks", TEST_ISSUER),  # ambiguous key sources
    (TEST_SECRET, "", ""),                 # F-P2-2: issuer not configured
])
def test_server_config_gate_fails_closed(monkeypatch, secret, jwks, issuer):
    """No key source, ambiguous key sources, or a MISSING expected issuer is
    a 503 server problem - never a 401 blamed on the caller, never open."""
    from fastapi import HTTPException
    from app.services import portal_auth

    monkeypatch.setenv(portal_auth.ENV_JWT_SECRET, secret)
    monkeypatch.setenv(portal_auth.ENV_JWKS_URL, jwks)
    monkeypatch.setenv(portal_auth.ENV_ISSUER, issuer)
    with pytest.raises(HTTPException) as excinfo:
        portal_auth.verify_portal_token("anything")
    assert excinfo.value.status_code == 503
    assert excinfo.value.detail == portal_auth.AUTH_NOT_CONFIGURED_DETAIL


# ---------------------------------------------------------------------------
# HTTP fixtures (house harness).
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def office_users_table(engine):
    """F-P2-3 makes migration 007 the SOLE creation authority: office_users
    is deliberately absent from Base.metadata, so create_all no longer makes
    it. The harness therefore runs the REAL 007 SQL (including F-P2-1 row
    level security) - which also proves the backend's direct SQL keeps
    working against the true production DDL. The harness superuser bypasses
    RLS the same way the owning backend role is exempt in production."""
    import sqlalchemy
    from calendar_tests.conftest import TEST_DB_URL

    migrations = Path(__file__).resolve().parents[1] / "migrations"
    raw = sqlalchemy.create_engine(TEST_DB_URL, isolation_level="AUTOCOMMIT")
    with raw.connect() as connection:
        connection.exec_driver_sql(
            (migrations / "007_office_users_up.sql").read_text())
    yield
    with raw.connect() as connection:
        connection.exec_driver_sql(
            (migrations / "007_office_users_down.sql").read_text())
    raw.dispose()


@pytest.fixture()
def office_b(db):
    """A SECOND office (test_admin_auth pattern)."""
    from app.models import Client

    client = Client(
        id=uuid.uuid4(),
        practice_name="Other Dental",
        api_key=f"key-{uuid.uuid4()}",
        active=True,
    )
    db.add(client)
    db.commit()
    return client


def _bind_office_user(db, client, *, active=True, deactivated_at=None):
    from app.portal_models import OfficeUser

    row = OfficeUser(
        auth_user_id=uuid.uuid4(),
        client_id=client.id,
        active=active,
        deactivated_at=deactivated_at,
    )
    db.add(row)
    db.commit()
    return row


@pytest.fixture()
def portal_http(db, office_users_table, monkeypatch):
    """Real app containing the portal + operator-admin + calendar routers so
    cross-surface invariants (K/L/M) are proven over HTTP. Only the two
    session dependencies are overridden; BOTH authorization owners run for
    real. HS256 verification is configured with the test secret."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.routes import admin as admin_routes
    from app.routes import calendar as calendar_routes
    from app.routes import portal as portal_routes
    from app.services import portal_auth

    monkeypatch.setenv(portal_auth.ENV_JWT_SECRET, TEST_SECRET)
    monkeypatch.delenv(portal_auth.ENV_JWKS_URL, raising=False)
    monkeypatch.setenv(portal_auth.ENV_ISSUER, TEST_ISSUER)   # F-P2-2
    portal_auth._jwks_clients.clear()

    app = FastAPI()
    app.include_router(portal_routes.router)
    app.include_router(admin_routes.router)
    app.include_router(calendar_routes.router)
    app.dependency_overrides[portal_routes.get_db] = lambda: db
    app.dependency_overrides[calendar_routes.get_db] = lambda: db
    with TestClient(app) as client:
        yield client


def _me(portal_http, token=None, headers=None, query=""):
    hdrs = dict(headers or {})
    if token is not None:
        hdrs["Authorization"] = f"Bearer {token}"
    return portal_http.get(f"/portal/me{query}", headers=hdrs)


# ---------------------------------------------------------------------------
# A/B/C + G/H/I - every credential failure is the ONE indistinguishable 401.
# ---------------------------------------------------------------------------

@requires_db
def test_missing_header_fails_closed(db, portal_http, client_row):
    _bind_office_user(db, client_row)
    response = portal_http.get("/portal/me")
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid portal credentials."


@requires_db
@pytest.mark.parametrize("case", [
    "garbage", "wrong_secret", "expired", "wrong_audience",
    "missing_sub", "non_uuid_sub", "missing_exp",
    "missing_issuer", "wrong_issuer",
])
def test_bad_tokens_fail_closed_indistinguishably(db, portal_http,
                                                  client_row, case):
    user = _bind_office_user(db, client_row)
    token = {
        "garbage": "not.a.jwt",
        "wrong_secret": _token(user.auth_user_id, secret=OTHER_SECRET),
        "expired": _token(user.auth_user_id, exp_delta=-60),
        "wrong_audience": _token(user.auth_user_id, aud="anon"),
        "missing_sub": _token(uuid.uuid4(), drop=("sub",)),
        "non_uuid_sub": _token("not-a-uuid"),
        "missing_exp": _token(user.auth_user_id, drop=("exp",)),
        "missing_issuer": _token(user.auth_user_id, drop=("iss",)),
        "wrong_issuer": _token(user.auth_user_id,
                               iss="https://attacker.example/auth/v1"),
    }[case]
    response = _me(portal_http, token)
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid portal credentials."


@requires_db
def test_anonymous_supabase_session_cannot_authenticate(db, portal_http,
                                                        client_row):
    """F-P2-2: an OTHERWISE-VALID Supabase token (right signature, issuer,
    audience, exp, bound subject) carrying is_anonymous=true must never
    authenticate - the Office Portal is for invited permanent identities.
    The same token with is_anonymous=false authenticates normally, proving
    the rejection keys on exactly that claim."""
    user = _bind_office_user(db, client_row)
    anonymous = _token(user.auth_user_id, is_anonymous=True)
    response = _me(portal_http, anonymous)
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid portal credentials."
    assert _me(portal_http,
               _token(user.auth_user_id, is_anonymous=False)).status_code == 200


@requires_db
def test_unknown_subject_fails_closed(db, portal_http, client_row):
    _bind_office_user(db, client_row)                 # some OTHER user exists
    response = _me(portal_http, _token(uuid.uuid4()))  # sub with no binding
    assert response.status_code == 401


@requires_db
def test_inactive_office_user_fails_closed(db, portal_http, client_row):
    user = _bind_office_user(db, client_row, active=False)
    assert _me(portal_http, _token(user.auth_user_id)).status_code == 401


@requires_db
def test_deactivated_office_user_fails_closed(db, portal_http, client_row):
    from datetime import datetime, timezone
    user = _bind_office_user(db, client_row, active=False,
                             deactivated_at=datetime.now(timezone.utc))
    assert _me(portal_http, _token(user.auth_user_id)).status_code == 401


@requires_db
def test_inactive_client_fails_closed(db, portal_http, client_row):
    user = _bind_office_user(db, client_row)
    client_row.active = False
    db.add(client_row)
    db.commit()
    assert _me(portal_http, _token(user.auth_user_id)).status_code == 401


# ---------------------------------------------------------------------------
# D/E/F - server-authoritative tenancy.
# ---------------------------------------------------------------------------

@requires_db
def test_valid_token_resolves_exactly_the_bound_office(db, portal_http,
                                                       client_row):
    user = _bind_office_user(db, client_row)
    response = _me(portal_http, _token(user.auth_user_id))
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "client_id": str(client_row.id),
        "practice_name": client_row.practice_name,
        "role": "office_admin",
        "email": "office@example.test",
    }


@requires_db
def test_stray_client_id_parameter_cannot_override_tenancy(db, portal_http,
                                                           client_row,
                                                           office_b):
    """Invariant E/F: supplying Office B's id in the query changes NOTHING -
    the endpoint declares no tenant parameter, so it is ignored and the
    authenticated binding still wins."""
    user = _bind_office_user(db, client_row)
    response = _me(portal_http, _token(user.auth_user_id),
                   query=f"?client_id={office_b.id}&client_key=anything")
    assert response.status_code == 200
    assert response.json()["client_id"] == str(client_row.id)


@requires_db
def test_two_offices_resolve_independently(db, portal_http, client_row,
                                           office_b):
    user_a = _bind_office_user(db, client_row)
    user_b = _bind_office_user(db, office_b)
    assert _me(portal_http, _token(user_a.auth_user_id)).json()["client_id"] \
        == str(client_row.id)
    assert _me(portal_http, _token(user_b.auth_user_id)).json()["client_id"] \
        == str(office_b.id)


# ---------------------------------------------------------------------------
# J/K/L - the three existing credential systems cannot authenticate here.
# ---------------------------------------------------------------------------

@requires_db
def test_client_key_cannot_authenticate_to_portal(db, portal_http,
                                                  client_row):
    _bind_office_user(db, client_row)
    assert _me(portal_http, client_row.api_key).status_code == 401


@requires_db
def test_global_admin_key_cannot_authenticate_to_portal(db, portal_http,
                                                        client_row):
    from app.config import ADMIN_API_KEY
    _bind_office_user(db, client_row)
    assert _me(portal_http, ADMIN_API_KEY).status_code == 401          # bearer
    response = portal_http.get("/portal/me",
                               headers={"X-Admin-Key": ADMIN_API_KEY})
    assert response.status_code == 401                                 # header


@requires_db
def test_calendar_key_cannot_authenticate_to_portal(db, portal_http,
                                                    client_row):
    """A REAL, active per-office Calendar credential (stored hash) still
    cannot become a portal identity: it is not a JWT signed with the
    project key (invariant L)."""
    from app.calendar_models import CalendarAdminCredential
    from app.services.calendar_admin_auth import generate_calendar_admin_key

    raw_key, key_hash = generate_calendar_admin_key()
    db.add(CalendarAdminCredential(client_id=client_row.id,
                                   key_hash=key_hash, label="p2-test"))
    db.commit()
    _bind_office_user(db, client_row)
    assert _me(portal_http, raw_key).status_code == 401


# ---------------------------------------------------------------------------
# M - a portal token opens NOTHING on the operator or Calendar surfaces.
# ---------------------------------------------------------------------------

@requires_db
def test_portal_token_cannot_reach_operator_or_calendar_apis(db, portal_http,
                                                             client_row):
    user = _bind_office_user(db, client_row)
    bearer = {"Authorization": f"Bearer {_token(user.auth_user_id)}"}
    assert portal_http.get("/admin/health", headers=bearer).status_code == 401
    assert portal_http.get("/admin/calendar/me",
                           headers=bearer).status_code == 401


# ---------------------------------------------------------------------------
# N - no secret material in the portal response.
# ---------------------------------------------------------------------------

@requires_db
def test_me_leaks_no_secrets_or_settings(db, portal_http, client_row):
    client_row.settings = {"calendar": {"booking_enabled": True},
                           "booking_url": "https://external.example"}
    db.add(client_row)
    db.commit()
    user = _bind_office_user(db, client_row)
    response = _me(portal_http, _token(user.auth_user_id))
    assert response.status_code == 200
    assert set(response.json().keys()) == {
        "client_id", "practice_name", "role", "email",
    }
    text = response.text
    assert client_row.api_key not in text
    assert "settings" not in text
    assert "booking_url" not in text
    assert TEST_SECRET not in text


# ---------------------------------------------------------------------------
# Server configuration + JWKS branch.
# ---------------------------------------------------------------------------

def test_office_users_is_not_registered_on_the_startup_base():
    """F-P2-3 regression: importing every portal module must NOT register
    office_users on app.database.Base - otherwise startup
    Base.metadata.create_all() would auto-create the table before migration
    007 (schema drift: no RLS, no CHECKs, wrong index names) and 007 would
    later fail. The table lives only on PortalBase; 007 is the sole creation
    authority."""
    import app.portal_models as portal_models
    import app.routes.portal  # noqa: F401  (full portal import chain)
    from app.database import Base

    assert "office_users" not in Base.metadata.tables
    assert "office_users" in portal_models.PortalBase.metadata.tables


@requires_db
def test_unconfigured_server_returns_503_over_http(db, office_users_table,
                                                   client_row, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.routes import portal as portal_routes
    from app.services import portal_auth

    monkeypatch.delenv(portal_auth.ENV_JWT_SECRET, raising=False)
    monkeypatch.delenv(portal_auth.ENV_JWKS_URL, raising=False)
    monkeypatch.delenv(portal_auth.ENV_ISSUER, raising=False)
    user = _bind_office_user(db, client_row)

    app = FastAPI()
    app.include_router(portal_routes.router)
    app.dependency_overrides[portal_routes.get_db] = lambda: db
    with TestClient(app) as http:
        response = http.get("/portal/me", headers={
            "Authorization": f"Bearer {_token(user.auth_user_id)}"})
    assert response.status_code == 503


@requires_db
def test_jwks_asymmetric_branch_verifies_and_rejects(db, portal_http,
                                                     client_row, monkeypatch):
    """RS256 via the JWKS seam: a token signed with the matching private key
    authenticates; a tampered token stays a 401. No network is used - the
    factory seam returns an in-memory key."""
    from cryptography.hazmat.primitives.asymmetric import rsa
    from app.services import portal_auth

    private_key = rsa.generate_private_key(public_exponent=65537,
                                           key_size=2048)

    class _FakeSigningKey:
        key = private_key.public_key()

    class _FakeJwksClient:
        def __init__(self, url):
            self.url = url

        def get_signing_key_from_jwt(self, token):
            return _FakeSigningKey()

    monkeypatch.setenv(portal_auth.ENV_JWKS_URL, "https://example.test/jwks")
    monkeypatch.delenv(portal_auth.ENV_JWT_SECRET, raising=False)
    monkeypatch.setattr(portal_auth, "_jwks_client_factory", _FakeJwksClient)
    portal_auth._jwks_clients.clear()

    user = _bind_office_user(db, client_row)
    good = _token(user.auth_user_id, algorithm="RS256", key=private_key)
    assert _me(portal_http, good).status_code == 200
    assert _me(portal_http, good[:-3] + "AAA").status_code == 401
    portal_auth._jwks_clients.clear()
