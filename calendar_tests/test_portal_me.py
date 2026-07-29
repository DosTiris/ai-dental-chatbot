# calendar_tests/test_portal_me.py
#
# PORTAL MVP: GET /admin/calendar/me — the office-portal bootstrap endpoint.
#
# Proves, at the REAL HTTP layer wherever transport behavior is the claim:
#   - a valid active per-office credential bootstraps EXACTLY its own tenant,
#     and a stray client_id query parameter changes nothing (the endpoint
#     takes no such parameter: the credential alone decides the tenant);
#   - the response body contains EXACTLY the five approved safe fields —
#     no credential, hash, settings JSON, notification destination, or
#     foreign-tenant information can appear;
#   - timezone_name and today_local follow the OFFICE timezone (never server
#     time): two offices in always-different-date zones get different dates;
#   - booking_enabled is a REAL JSON boolean derived through the settings
#     owner's strict parsing (a string "true" must NOT read as enabled), and
#     booking_enabled=false still permits portal bootstrap;
#   - every credential failure (missing/empty/malformed/unknown/revoked/
#     inactive client) returns the identical 401 — a MISSING header is 401,
#     never 422 — and the global ADMIN_API_KEY is rejected;
#   - an authentication database failure fails CLOSED: the request surfaces
#     as a server failure (never 401, never authenticated), with the session
#     rolled back.
#
# FIXTURES: local to this file, mirroring calendar_tests/test_admin_auth.py
# (the shared db / client_row / engine fixtures from conftest.py are used
# UNCHANGED).
#
# SECRET HANDLING (same rule as test_admin_auth.py): raw credentials are
# generated in memory per test, never printed, and never placed directly
# inside assert expressions.

import sys
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from calendar_tests.conftest import requires_db  # noqa: E402

pytestmark = requires_db

UTC = ZoneInfo("UTC")

# The exact response contracts under test.
INVALID_DETAIL = "Invalid admin key."
ME_PATH = "/admin/calendar/me"

# conftest.py sets ADMIN_API_KEY=test-admin-key for app.config; it must be
# rejected here exactly as on every other Calendar route.
GLOBAL_ADMIN_KEY = "test-admin-key"

# The complete approved bootstrap contract — nothing more, nothing less.
APPROVED_FIELDS = {
    "client_id", "practice_name", "timezone_name", "today_local",
    "booking_enabled",
}


def _now():
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Local fixtures and helpers (mirroring test_admin_auth.py)
# ---------------------------------------------------------------------------

def _provision(db, client, label="pytest portal tool"):
    """Insert ONE credential the approved way: only the hash is persisted.
    Returns (raw_key, credential_row). The raw key exists only in memory."""
    from app.calendar_models import CalendarAdminCredential
    from app.services.calendar_admin_auth import generate_calendar_admin_key

    raw_key, key_hash = generate_calendar_admin_key()
    credential = CalendarAdminCredential(
        id=uuid.uuid4(), client_id=client.id, key_hash=key_hash, label=label
    )
    db.add(credential)
    db.commit()
    return raw_key, credential


def _make_office(db, practice_name, timezone_name=None, calendar_settings=None,
                 notification_email=None, notification_phone=None,
                 active=True):
    """One additional office with explicit settings for a specific proof."""
    from app.models import Client

    settings = {}
    if timezone_name is not None:
        settings["timezone"] = timezone_name
    if calendar_settings is not None:
        settings["calendar"] = calendar_settings
    client = Client(
        id=uuid.uuid4(),
        practice_name=practice_name,
        api_key=f"key-{uuid.uuid4()}",
        active=active,
        settings=settings or None,
        notification_email=notification_email,
        notification_phone=notification_phone,
    )
    db.add(client)
    db.commit()
    return client


@pytest.fixture()
def office_b(db):
    """A SECOND office whose information must never appear in Office A's
    bootstrap response."""
    return _make_office(db, "Other Dental")


@pytest.fixture()
def http(db):
    """A real FastAPI app containing the calendar router, driven over HTTP.
    Only get_db is overridden; the authorization dependency runs FOR REAL."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.routes import calendar as calendar_routes

    app = FastAPI()
    app.include_router(calendar_routes.router)
    app.dependency_overrides[calendar_routes.get_db] = lambda: db
    with TestClient(app) as client:
        yield client


def _get_me(http, raw_key, params=None):
    return http.get(ME_PATH, params=params, headers={"X-Admin-Key": raw_key})


def _expected_local_dates(timezone_name):
    """The office-local date immediately around 'now' — a request straddling
    local midnight may legitimately land on either side, so assertions accept
    a before/after pair computed at call time."""
    return {
        datetime.now(ZoneInfo(timezone_name)).date().isoformat(),
    }


# ---------------------------------------------------------------------------
# Happy path: the credential alone decides the tenant
# ---------------------------------------------------------------------------

def test_me_bootstraps_exactly_the_credential_tenant(http, db, client_row):
    """A valid active per-office credential returns ITS office's identity:
    client_id, practice_name, and (per the client_row fixture) a strict-true
    booking_enabled."""
    raw_key, _credential = _provision(db, client_row)
    response = _get_me(http, raw_key)
    assert response.status_code == 200
    body = response.json()
    assert body["client_id"] == str(client_row.id)
    assert body["practice_name"] == "Test Dental"
    assert body["booking_enabled"] is True


def test_me_response_contains_exactly_the_approved_fields(http, db, client_row):
    """The body's key set equals the approved contract EXACTLY — the shape
    check that makes any future accidental field addition a test failure."""
    raw_key, _credential = _provision(db, client_row)
    response = _get_me(http, raw_key)
    assert response.status_code == 200
    assert set(response.json().keys()) == APPROVED_FIELDS


def test_me_ignores_stray_client_id_parameter(http, db, client_row, office_b):
    """The endpoint takes NO client_id parameter: presenting Office B's id as
    a stray query parameter changes nothing — the response is still Office
    A's bootstrap, and nothing of Office B appears anywhere in the body."""
    raw_key, _credential = _provision(db, client_row)
    response = _get_me(http, raw_key, params={"client_id": str(office_b.id)})
    assert response.status_code == 200
    body = response.json()
    assert body["client_id"] == str(client_row.id)
    body_text = response.text
    foreign_id_leaked = str(office_b.id) in body_text
    foreign_name_leaked = "Other Dental" in body_text
    assert not foreign_id_leaked
    assert not foreign_name_leaked


# ---------------------------------------------------------------------------
# Office timezone drives timezone_name and today_local
# ---------------------------------------------------------------------------

def test_me_today_local_and_timezone_follow_the_office_timezone(http, db):
    """Two offices in zones whose local DATES always differ (UTC+14 vs
    UTC-11, 25 hours apart) each report their OWN configured zone and their
    OWN local date — so today_local provably comes from the office timezone,
    never from server time. Midnight straddle is tolerated by accepting the
    date computed just before or just after the request."""
    east_office = _make_office(db, "Eastmost Dental",
                               timezone_name="Pacific/Kiritimati")
    west_office = _make_office(db, "Westmost Dental",
                               timezone_name="Pacific/Pago_Pago")
    east_key, _c1 = _provision(db, east_office)
    west_key, _c2 = _provision(db, west_office)

    east_expected = _expected_local_dates("Pacific/Kiritimati")
    east_body = _get_me(http, east_key).json()
    east_expected |= _expected_local_dates("Pacific/Kiritimati")

    west_expected = _expected_local_dates("Pacific/Pago_Pago")
    west_body = _get_me(http, west_key).json()
    west_expected |= _expected_local_dates("Pacific/Pago_Pago")

    assert east_body["timezone_name"] == "Pacific/Kiritimati"
    assert west_body["timezone_name"] == "Pacific/Pago_Pago"
    assert east_body["today_local"] in east_expected
    assert west_body["today_local"] in west_expected
    # 25 hours apart: the two offices' local dates can never be equal.
    assert east_body["today_local"] != west_body["today_local"]


def test_me_today_local_is_iso_yyyy_mm_dd(http, db, client_row):
    """The wire form is exactly YYYY-MM-DD (a JSON string, parseable as an
    ISO date)."""
    from datetime import date as date_type

    raw_key, _credential = _provision(db, client_row)
    body = _get_me(http, raw_key).json()
    value = body["today_local"]
    assert isinstance(value, str)
    parsed = date_type.fromisoformat(value)   # raises on any other shape
    assert parsed.isoformat() == value


# ---------------------------------------------------------------------------
# booking_enabled: strict boolean, and false never blocks bootstrap
# ---------------------------------------------------------------------------

def test_me_booking_enabled_is_a_real_json_boolean(http, db, client_row):
    """The value arrives as JSON true/false — after parsing, a Python bool,
    never a string or number."""
    raw_key, _credential = _provision(db, client_row)
    body = _get_me(http, raw_key).json()
    assert isinstance(body["booking_enabled"], bool)


def test_me_booking_disabled_still_permits_bootstrap(http, db):
    """booking_enabled=false is informational: staff must still be able to
    review and confirm existing requests, so the bootstrap succeeds and
    reports false."""
    paused_office = _make_office(
        db, "Paused Dental", timezone_name="America/New_York",
        calendar_settings={"booking_enabled": False},
    )
    raw_key, _credential = _provision(db, paused_office)
    response = _get_me(http, raw_key)
    assert response.status_code == 200
    assert response.json()["booking_enabled"] is False


def test_me_booking_enabled_uses_the_settings_owner_strict_parse(http, db):
    """Derivation goes through load_calendar_settings (single owner): the
    string \"true\" is NOT a JSON boolean, so it must fall back to the
    fail-safe default False — bootstrap still succeeds."""
    sloppy_office = _make_office(
        db, "Sloppy Settings Dental", timezone_name="America/New_York",
        calendar_settings={"booking_enabled": "true"},
    )
    raw_key, _credential = _provision(db, sloppy_office)
    response = _get_me(http, raw_key)
    assert response.status_code == 200
    assert response.json()["booking_enabled"] is False


# ---------------------------------------------------------------------------
# Credential failures: the identical indistinguishable 401
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "mode", ["missing", "empty", "malformed", "unknown", "revoked",
             "inactive_client"]
)
def test_me_credential_failure_401(http, db, client_row, mode):
    """Missing (explicitly NOT 422), empty, malformed, unknown, revoked, and
    inactive-client credentials all get status 401 with the EXACT detail
    'Invalid admin key.' on /me — indistinguishable from one another."""
    from app.services.calendar_admin_auth import generate_calendar_admin_key

    headers = {}
    if mode == "empty":
        headers = {"X-Admin-Key": ""}
    elif mode == "malformed":
        headers = {"X-Admin-Key": "not-a-calendar-admin-key"}
    elif mode == "unknown":
        raw_key, _unused_hash = generate_calendar_admin_key()  # never inserted
        headers = {"X-Admin-Key": raw_key}
    elif mode == "revoked":
        raw_key, credential = _provision(db, client_row)
        credential.active = False
        credential.revoked_at = _now()
        db.commit()
        headers = {"X-Admin-Key": raw_key}
    elif mode == "inactive_client":
        dormant = _make_office(db, "Dormant Dental", active=False)
        raw_key, _credential = _provision(db, dormant)
        headers = {"X-Admin-Key": raw_key}

    response = http.get(ME_PATH, headers=headers)
    assert response.status_code == 401
    assert response.status_code != 422       # esp. the missing-header case
    assert response.json()["detail"] == INVALID_DETAIL


def test_me_global_admin_key_401(http, db, client_row):
    """The configured global ADMIN_API_KEY receives the identical 401 on the
    bootstrap endpoint, exactly as on every other Calendar route."""
    response = http.get(ME_PATH, headers={"X-Admin-Key": GLOBAL_ADMIN_KEY})
    assert response.status_code == 401
    assert response.json()["detail"] == INVALID_DETAIL


# ---------------------------------------------------------------------------
# The bootstrap leaks nothing sensitive
# ---------------------------------------------------------------------------

def test_me_leaks_no_credential_settings_or_notification_data(
    http, db, office_b
):
    """The raw response text contains none of: the raw key, its stored hash,
    any settings-JSON internals (hold_minutes as the sentinel), notification
    recipients, or another tenant's identity."""
    secretive_office = _make_office(
        db, "Leak Probe Dental", timezone_name="America/New_York",
        calendar_settings={"booking_enabled": True, "hold_minutes": 7},
        notification_email="frontdesk@leakprobe.example",
        notification_phone="516-555-7777",
    )
    raw_key, credential = _provision(db, secretive_office)
    response = _get_me(http, raw_key)
    assert response.status_code == 200
    body_text = response.text

    raw_key_leaked = raw_key in body_text
    key_hash_leaked = credential.key_hash in body_text
    settings_json_leaked = "hold_minutes" in body_text
    email_leaked = "frontdesk@leakprobe.example" in body_text
    phone_leaked = "516-555-7777" in body_text
    foreign_tenant_leaked = (str(office_b.id) in body_text
                             or "Other Dental" in body_text)
    assert not raw_key_leaked
    assert not key_hash_leaked
    assert not settings_json_leaked
    assert not email_leaked
    assert not phone_leaked
    assert not foreign_tenant_leaked


# ---------------------------------------------------------------------------
# Authentication infrastructure failures fail closed
# ---------------------------------------------------------------------------

def test_me_auth_database_error_fails_closed(db, client_row):
    """A database exception during credential lookup surfaces as a SERVER
    failure (500) — never a 401, never an authenticated bootstrap — with the
    session rolled back exactly once (the existing fail-closed contract,
    exercised on the new endpoint)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.routes import calendar as calendar_routes

    raw_key, _credential = _provision(db, client_row)

    class ExplodingSession:
        """Delegates everything to the real session EXCEPT query(), which
        simulates an infrastructure failure during the credential lookup."""

        def __init__(self, real_session):
            self._real_session = real_session
            self.rollback_calls = 0
            self.query_attempts = 0

        def query(self, *args, **kwargs):
            self.query_attempts += 1
            raise RuntimeError(
                "simulated database failure during credential lookup"
            )

        def rollback(self):
            self.rollback_calls += 1
            return self._real_session.rollback()

        def __getattr__(self, name):
            return getattr(self._real_session, name)

    app = FastAPI()
    app.include_router(calendar_routes.router)
    exploding = ExplodingSession(db)
    app.dependency_overrides[calendar_routes.get_db] = lambda: exploding
    with TestClient(app, raise_server_exceptions=False) as http_client:
        response = http_client.get(ME_PATH, headers={"X-Admin-Key": raw_key})
    assert response.status_code == 500
    assert response.status_code != 401
    assert exploding.query_attempts == 1
    assert exploding.rollback_calls == 1
