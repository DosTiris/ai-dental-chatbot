# test_portal_config_service.py - unit tests for the PUBLIC portal browser
# configuration service (MIA_P3A_PORTAL_AUTH_UI v1.0.1, F-P3A-1).
#
# These tests import ONLY app.services.portal_config_service. They do not
# import the FastAPI app, portal_auth, or any database code, so they run in
# any environment with fastapi installed:
#
#   python -m pytest tests\portal\test_portal_config_service.py -v

import base64
import json
import logging

import pytest
from fastapi import HTTPException

from app.services.portal_config_service import (
    ISSUER_ENV_VAR,
    PUBLISHABLE_KEY_ENV_VAR,
    REQUIRED_PUBLISHABLE_KEY_PREFIX,
    build_portal_public_config,
    derive_supabase_url,
    validate_publishable_key,
)


def make_legacy_service_role_jwt():
    """
    Build a REALISTIC legacy service_role JWT test value (F-P3A-3): the
    base64url-encoded payload decodes to {"role":"service_role"} but the
    raw token does NOT contain the plaintext substring "service_role" -
    exactly the shape a substring denylist would wrongly accept.
    """
    header = base64.urlsafe_b64encode(
        json.dumps({"alg": "HS256", "typ": "JWT"}).encode()
    ).rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(
        json.dumps({"role": "service_role"}).encode()
    ).rstrip(b"=").decode()
    token = header + "." + payload + ".fake-signature-material"
    # Prove the trap: role decodes from the payload, plaintext is absent.
    padded = payload + "=" * (-len(payload) % 4)
    assert json.loads(base64.urlsafe_b64decode(padded))["role"] == "service_role"
    assert "service_role" not in token
    return token

VALID_ISSUER = "https://abcproject.supabase.co/auth/v1"
VALID_KEY = "sb_publishable_test_value_123"


def _set_env(monkeypatch, issuer, key):
    """Set or delete both portal config env vars for one test case."""
    if issuer is None:
        monkeypatch.delenv(ISSUER_ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(ISSUER_ENV_VAR, issuer)
    if key is None:
        monkeypatch.delenv(PUBLISHABLE_KEY_ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(PUBLISHABLE_KEY_ENV_VAR, key)


# ---------------------------------------------------------------------------
# URL derivation from the existing SUPABASE_AUTH_ISSUER
# ---------------------------------------------------------------------------

def test_url_derives_from_issuer_by_stripping_auth_v1():
    assert (
        derive_supabase_url(VALID_ISSUER)
        == "https://abcproject.supabase.co"
    )


def test_url_derivation_accepts_one_trailing_slash():
    assert (
        derive_supabase_url(VALID_ISSUER + "/")
        == "https://abcproject.supabase.co"
    )


@pytest.mark.parametrize(
    "bad_issuer",
    [
        None,
        "",
        "   ",
        "http://abcproject.supabase.co/auth/v1",  # plaintext scheme
        "https://abcproject.supabase.co",  # suffix missing
        "https://abcproject.supabase.co/auth/v2",  # wrong version
        "https://abcproject.supabase.co/auth/v1/extra",  # extra path
        "https:///auth/v1",  # empty host
        "https://host/auth/v1 https://other/auth/v1",  # embedded space
    ],
)
def test_url_derivation_rejects_unexpected_issuer_forms(bad_issuer):
    assert derive_supabase_url(bad_issuer) is None


# ---------------------------------------------------------------------------
# Publishable-key validation
# ---------------------------------------------------------------------------

def test_publishable_key_valid_modern_value_is_returned_stripped():
    assert VALID_KEY.startswith(REQUIRED_PUBLISHABLE_KEY_PREFIX)
    assert validate_publishable_key("  " + VALID_KEY + "  ") == VALID_KEY


@pytest.mark.parametrize("bad_key", [None, "", "   "])
def test_publishable_key_missing_or_empty_is_rejected(bad_key):
    assert validate_publishable_key(bad_key) is None


@pytest.mark.parametrize(
    "non_publishable",
    [
        "sb_secret_abc123",                                  # secret key
        "sb_publishable",                                    # bare, no material
        "sb_publishable_",                                   # prefix only
        "SB_PUBLISHABLE_UPPERCASED",                         # wrong case
        "arbitrary_string_key",                              # arbitrary string
        "eyJhbGciOiJIUzI1NiJ9.eyJmb28iOiJiYXIifQ.sig",       # JWT-shaped
        "eyJhbGciOiJIUzI1NiJ9.eyJyb2xlIjoiYW5vbiJ9.sig",     # legacy anon JWT
    ],
)
def test_publishable_key_rejects_every_non_publishable_form(non_publishable):
    # F-P3A-3: allowlist - ONLY modern sb_publishable_ keys are served.
    assert validate_publishable_key(non_publishable) is None


def test_publishable_key_rejects_encoded_legacy_service_role_jwt():
    # The critical F-P3A-3 case: the role claim is base64url-ENCODED, so
    # the raw token carries no plaintext "service_role" - a substring
    # denylist would accept it; the allowlist must reject it.
    token = make_legacy_service_role_jwt()
    assert validate_publishable_key(token) is None


# ---------------------------------------------------------------------------
# build_portal_public_config: exact response and fail-closed 503s
# ---------------------------------------------------------------------------

def test_valid_env_returns_exactly_two_public_fields(monkeypatch):
    _set_env(monkeypatch, VALID_ISSUER, VALID_KEY)
    body = build_portal_public_config()
    assert body == {
        "supabase_url": "https://abcproject.supabase.co",
        "supabase_publishable_key": VALID_KEY,
    }
    # EXACT two keys - no tenant identity, no client_id, no extras.
    assert sorted(body.keys()) == [
        "supabase_publishable_key",
        "supabase_url",
    ]


def test_missing_issuer_fails_closed_503(monkeypatch):
    _set_env(monkeypatch, None, VALID_KEY)
    with pytest.raises(HTTPException) as excinfo:
        build_portal_public_config()
    assert excinfo.value.status_code == 503


def test_malformed_issuer_fails_closed_503(monkeypatch):
    _set_env(monkeypatch, "https://abcproject.supabase.co", VALID_KEY)
    with pytest.raises(HTTPException) as excinfo:
        build_portal_public_config()
    assert excinfo.value.status_code == 503


def test_missing_publishable_key_fails_closed_503(monkeypatch):
    _set_env(monkeypatch, VALID_ISSUER, None)
    with pytest.raises(HTTPException) as excinfo:
        build_portal_public_config()
    assert excinfo.value.status_code == 503


def test_non_publishable_key_fails_closed_503(monkeypatch):
    _set_env(monkeypatch, VALID_ISSUER, "sb_secret_do_not_serve")
    with pytest.raises(HTTPException) as excinfo:
        build_portal_public_config()
    assert excinfo.value.status_code == 503


def test_legacy_service_role_jwt_fails_closed_503(monkeypatch):
    _set_env(monkeypatch, VALID_ISSUER, make_legacy_service_role_jwt())
    with pytest.raises(HTTPException) as excinfo:
        build_portal_public_config()
    assert excinfo.value.status_code == 503


def test_failure_log_never_contains_supplied_key(monkeypatch, caplog):
    # F-P3A-3: the supplied value must never be logged, even on rejection.
    rejected_value = make_legacy_service_role_jwt()
    _set_env(monkeypatch, VALID_ISSUER, rejected_value)
    with caplog.at_level(logging.DEBUG, logger="portal_config"):
        with pytest.raises(HTTPException):
            build_portal_public_config()
    assert rejected_value not in caplog.text
    for piece in rejected_value.split("."):
        assert piece not in caplog.text


def test_503_detail_is_generic_and_names_no_variable(monkeypatch):
    # The client-facing failure must not disclose which variable failed or
    # any environment detail; the specific cause is logged server-side.
    _set_env(monkeypatch, None, None)
    with pytest.raises(HTTPException) as excinfo:
        build_portal_public_config()
    detail = str(excinfo.value.detail)
    assert detail == "portal configuration unavailable"
    assert ISSUER_ENV_VAR not in detail
    assert PUBLISHABLE_KEY_ENV_VAR not in detail


def test_response_values_carry_no_secret_markers(monkeypatch):
    _set_env(monkeypatch, VALID_ISSUER, VALID_KEY)
    body = build_portal_public_config()
    joined = " ".join(str(v) for v in body.values()).lower()
    assert "sb_secret" not in joined
    assert "service_role" not in joined
    assert "client_id" not in joined
