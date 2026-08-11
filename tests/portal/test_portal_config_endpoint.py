# test_portal_config_endpoint.py - integration tests for GET /portal/config
# on the REAL application (MIA_P3A_PORTAL_AUTH_UI v1.0.1, F-P3A-1).
#
# Runs against the real app.main application, so it needs the same local
# environment the P2 portal suite needs (a valid local .env). Run it as a
# direct file invocation, separate from calendar_tests/:
#
#   python -m pytest tests\portal\test_portal_config_endpoint.py -v
#
# The two portal config variables are pinned to known-good TEST values
# BEFORE app import so these tests are deterministic regardless of the
# local .env contents; every other variable comes from the local .env
# exactly as in the existing P2 suite.

import os

# Pin the portal config env BEFORE importing the app (import-time reads in
# other modules are unaffected; portal_config_service reads at request
# time, but pinning early keeps the test hermetic either way).
_TEST_ISSUER = "https://testproject.supabase.co/auth/v1"
_TEST_PUBLISHABLE_KEY = "sb_publishable_endpoint_test_key"
os.environ["SUPABASE_AUTH_ISSUER"] = _TEST_ISSUER
os.environ["SUPABASE_PUBLISHABLE_KEY"] = _TEST_PUBLISHABLE_KEY

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

client = TestClient(app)

# Both P2 route shapes resolve to the same public path once included in the
# real app; the literal public path is the contract under test.
CONFIG_PATH = "/portal/config"
ME_PATH = "/portal/me"


def test_portal_config_works_without_authorization():
    """The endpoint is public configuration: no Authorization required."""
    response = client.get(CONFIG_PATH)
    assert response.status_code == 200


def test_portal_config_returns_exactly_two_public_fields():
    response = client.get(CONFIG_PATH)
    assert response.status_code == 200
    body = response.json()
    assert sorted(body.keys()) == [
        "supabase_publishable_key",
        "supabase_url",
    ]
    assert body["supabase_url"] == "https://testproject.supabase.co"
    assert body["supabase_publishable_key"] == _TEST_PUBLISHABLE_KEY


def test_portal_config_exposes_no_tenant_identity_or_secret():
    response = client.get(CONFIG_PATH)
    assert response.status_code == 200
    raw = response.text.lower()
    for forbidden in (
        "client_id",
        "tenant",
        "practice_name",
        "sb_secret",
        "service_role",
        "jwt_secret",
        "admin_api_key",
        "database_url",
        "mia_cal_",
    ):
        assert forbidden not in raw, forbidden


def test_portal_config_ignores_any_authorization_header():
    """A token neither helps nor changes the response: config only."""
    response = client.get(
        CONFIG_PATH, headers={"Authorization": "Bearer not-a-real-token"}
    )
    assert response.status_code == 200
    assert sorted(response.json().keys()) == [
        "supabase_publishable_key",
        "supabase_url",
    ]


def test_portal_me_remains_protected_without_token():
    """F-P3A-1 must not loosen /portal/me: unauthenticated is rejected."""
    response = client.get(ME_PATH)
    assert response.status_code in (401, 403)


def test_portal_me_remains_protected_with_garbage_token():
    response = client.get(
        ME_PATH, headers={"Authorization": "Bearer not-a-real-token"}
    )
    assert response.status_code in (401, 403, 404)
    # Never a success and never a tenant payload for a garbage token.
    if response.headers.get("content-type", "").startswith(
        "application/json"
    ):
        assert "practice_name" not in response.text
