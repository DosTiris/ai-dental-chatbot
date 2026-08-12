# calendar_tests/test_portal_appointments.py
#
# Portal Appointments v1 (Office Portal read-only appointments slice):
# proves the security and behavior contract against the REAL
# portal_appointments router, over HTTP, with the REAL portal_auth tenant
# binding running.
#
# GROUPS:
#   * Pure tests (no database): derive_notification_outcome truth matrix,
#     including that a malformed legacy notify_error can only ever surface as
#     the safe "failed" outcome and never leaks raw text.
#   * HTTP tests (requires_db, house harness mirroring test_portal_leads.py):
#     the endpoint fails closed unauthenticated / inactive; an office reads
#     ONLY its own appointments (bidirectional isolation); a stray client_id
#     cannot override the token-bound tenant; the default range is exactly
#     seven inclusive local days; explicit ranges, an invalid range, an empty
#     range, and a DST-transition boundary all behave; the response contains
#     EXACTLY the approved fields; and no raw notify_error text ever appears.
#
# EVERY HTTP test here must FAIL against untouched 0316b36c (no
# /portal/appointments route exists there -> 404) and pass only against the
# implementation (bite proof).
#
# Tokens are minted locally with PyJWT (HS256 test secret) - no network, no
# real Supabase project, no provider is ever contacted.
#
# Run (owner-local, PowerShell 5.1):
#   $env:ALLOW_DESTRUCTIVE_CALENDAR_TESTS="yes"
#   $env:TEST_DATABASE_URL="postgresql://postgres:test@localhost:5433/mia_calendar_test"
#   python -m pytest calendar_tests\test_portal_appointments.py -v

import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from calendar_tests.conftest import requires_db  # noqa: E402  (env bootstrap)

# app.config needs DATABASE_URL at import; the pure tests never connect
# (the test_portal_leads.py pattern). setdefault never overrides the real
# test database when TEST_DATABASE_URL is set.
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://placeholder:placeholder@localhost:1/never_connected_placeholder",
)

import jwt as pyjwt  # noqa: E402

TEST_SECRET = "portal-test-secret-0123456789abcdef0123456789"
AUDIENCE = "authenticated"
TEST_ISSUER = "https://p2-test-project.supabase.co/auth/v1"

UTC = timezone.utc
NY = "America/New_York"

INVALID_DETAIL = "Invalid portal credentials."

# The COMPLETE approved appointment field set (leak-prevention pin). Any
# drift here means the response model and this contract diverged.
APPROVED_APPOINTMENT_FIELDS = {
    "appointment_id", "patient_name", "patient_phone", "patient_email",
    "new_or_returning", "reason", "urgency", "start_datetime",
    "end_datetime", "status", "confirmed_at", "source",
    "notification_outcome",
}
APPROVED_ENVELOPE_FIELDS = {
    "timezone_name", "start_day", "end_day", "appointments",
}
# Markers that must NEVER appear anywhere in a portal appointments response.
FORBIDDEN_BODY_MARKERS = [
    "client_id", "slot_id", "conversation_id", "notify_error",
    "office_sms_sent", "office_email_sent", "patient_sms_sent",
    "api_key", "client_key", "settings",
    "notification_email", "notification_phone", "created_at", "updated_at",
]


def _token(sub, *, secret=TEST_SECRET, aud=AUDIENCE, exp_delta=300,
           email="office@example.test"):
    """Mint a Supabase-shaped access token (test_portal_leads.py pattern)."""
    claims = {
        "sub": str(sub),
        "aud": aud,
        "exp": int(time.time()) + exp_delta,
        "email": email,
        "role": "authenticated",
        "iss": TEST_ISSUER,
    }
    return pyjwt.encode(claims, secret, algorithm="HS256")


# ---------------------------------------------------------------------------
# Pure tests - no database, no HTTP: the notification-outcome truth matrix.
# ---------------------------------------------------------------------------
# The exact stored-vocabulary strings under test are imported from the single
# owner so the matrix cannot drift from production wording.
from app.services.notification_service import (  # noqa: E402
    OFFICE_SMS_SEND_FAILED,
    OFFICE_EMAIL_SEND_FAILED,
    OFFICE_SMS_SKIPPED,
    OFFICE_EMAIL_SKIPPED,
    NOTIFY_ERROR_WITHHELD,
)
from app.routes.portal_appointments import (  # noqa: E402
    derive_notification_outcome,
    OUTCOME_SENT,
    OUTCOME_FAILED,
    OUTCOME_PENDING,
)


@pytest.mark.parametrize("sanitized,sms,email,expected", [
    # Nothing recorded, no flags -> pending.
    (None, False, False, OUTCOME_PENDING),
    # Nothing recorded but a channel sent -> sent.
    (None, True, False, OUTCOME_SENT),
    (None, False, True, OUTCOME_SENT),
    (None, True, True, OUTCOME_SENT),
    # A send_failure for an applicable channel -> failed (regardless of the
    # other channel's flag).
    (OFFICE_SMS_SEND_FAILED, False, True, OUTCOME_FAILED),
    (OFFICE_EMAIL_SEND_FAILED, True, False, OUTCOME_FAILED),
    (f"{OFFICE_SMS_SEND_FAILED}; {OFFICE_EMAIL_SEND_FAILED}", False, False,
     OUTCOME_FAILED),
    (f"{OFFICE_SMS_SEND_FAILED}; {OFFICE_EMAIL_SKIPPED}", False, False,
     OUTCOME_FAILED),
    # Skipped-only (no applicable channel failed): sent if any flag set,
    # else pending. A skip is NOT a failure.
    (OFFICE_SMS_SKIPPED, False, True, OUTCOME_SENT),
    (OFFICE_EMAIL_SKIPPED, True, False, OUTCOME_SENT),
    (f"{OFFICE_SMS_SKIPPED}; {OFFICE_EMAIL_SKIPPED}", False, False,
     OUTCOME_PENDING),
    # The withheld marker (malformed legacy value) -> failed, never raw text.
    (NOTIFY_ERROR_WITHHELD, False, False, OUTCOME_FAILED),
    (NOTIFY_ERROR_WITHHELD, True, True, OUTCOME_FAILED),
])
def test_derive_notification_outcome_matrix(sanitized, sms, email, expected):
    assert derive_notification_outcome(sanitized, sms, email) == expected


def test_derive_outcome_returns_only_the_closed_vocabulary():
    """Every branch resolves to exactly one of the three safe values."""
    allowed = {OUTCOME_SENT, OUTCOME_FAILED, OUTCOME_PENDING}
    samples = [
        (None, False, False), (None, True, False),
        (OFFICE_SMS_SEND_FAILED, False, False),
        (OFFICE_SMS_SKIPPED, True, False),
        (NOTIFY_ERROR_WITHHELD, False, False),
    ]
    for sanitized, sms, email in samples:
        assert derive_notification_outcome(sanitized, sms, email) in allowed


# ---------------------------------------------------------------------------
# HTTP fixtures (house harness, mirroring test_portal_leads.py).
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def office_users_table(engine):
    """Run the REAL migration 007 (sole creation authority for office_users,
    F-P2-3) up before this module and down after it."""
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
    """A SECOND office whose appointments must never appear for Office A."""
    from app.models import Client

    client = Client(
        id=uuid.uuid4(),
        practice_name="Other Dental",
        api_key=f"key-{uuid.uuid4()}",
        active=True,
        settings={"timezone": NY, "calendar": {"booking_enabled": True}},
    )
    db.add(client)
    db.commit()
    return client


def _bind_office_user(db, client, *, active=True):
    """One Supabase-user -> office binding (migration 007 row)."""
    from app.portal_models import OfficeUser

    row = OfficeUser(auth_user_id=uuid.uuid4(), client_id=client.id,
                     active=active)
    db.add(row)
    db.commit()
    return row


def _make_appointment(db, client, *, start_utc, end_utc,
                      name="Kevin Alvarado", phone="516-555-1234",
                      email=None, new_or_returning="new", reason="cleaning",
                      urgency="routine", status="pending",
                      office_sms_sent=False, office_email_sent=False,
                      notify_error=None):
    """Seed one appointment row directly (bypassing the booking pipeline) so
    times and the notification projection are set EXACTLY for the case under
    test. A throwaway slot is created first because appointments.slot_id is
    NOT NULL (schema fact), but the slot itself is never asserted on."""
    from app.calendar_models import AppointmentSlot, Appointment

    slot = AppointmentSlot(
        client_id=client.id,
        start_datetime=start_utc,
        end_datetime=end_utc,
        status="booked",
    )
    db.add(slot)
    db.flush()  # assign slot.id
    appointment = Appointment(
        client_id=client.id,
        slot_id=slot.id,
        conversation_id=None,
        patient_name=name,
        patient_phone=phone,
        patient_email=email,
        new_or_returning=new_or_returning,
        reason=reason,
        urgency=urgency,
        start_datetime=start_utc,
        end_datetime=end_utc,
        status=status,
        source="mia_widget",
        office_sms_sent=office_sms_sent,
        office_email_sent=office_email_sent,
        notify_error=notify_error,
    )
    db.add(appointment)
    db.commit()
    return appointment


@pytest.fixture()
def portal_http(db, office_users_table, monkeypatch):
    """Real app containing the P2 identity router AND the Portal Appointments
    router, driven over HTTP. Only the session dependency is overridden
    (portal_appointments imports the SAME get_db callable, so one override
    covers both routers); the REAL authorization owner runs."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.routes import portal as portal_routes
    from app.routes import portal_appointments as portal_appt_routes
    from app.services import portal_auth

    monkeypatch.setenv(portal_auth.ENV_JWT_SECRET, TEST_SECRET)
    monkeypatch.delenv(portal_auth.ENV_JWKS_URL, raising=False)
    monkeypatch.setenv(portal_auth.ENV_ISSUER, TEST_ISSUER)
    portal_auth._jwks_clients.clear()

    app = FastAPI()
    app.include_router(portal_routes.router)
    app.include_router(portal_appt_routes.router)
    app.dependency_overrides[portal_routes.get_db] = lambda: db
    with TestClient(app) as client:
        yield client


def _get(portal_http, path, token=None):
    headers = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return portal_http.get(path, headers=headers)


def _local_noon_utc(local_date_str):
    """A UTC instant that is unambiguously inside the given NY local date
    (noon local -> safe away from either midnight boundary)."""
    from zoneinfo import ZoneInfo
    y, m, d = (int(part) for part in local_date_str.split("-"))
    local = datetime(y, m, d, 12, 0, tzinfo=ZoneInfo(NY))
    return local.astimezone(UTC)


def _freeze_office_today(monkeypatch, year, month, day):
    """Freeze the office's local 'now' at 09:00 on the given NY date by
    patching the settings-service module attribute the route calls THROUGH
    (calendar_settings_service.client_now). This is the single seam for
    'today'; the route qualifies the call via the module (F1), so this patch
    is genuinely observed. Returns nothing - it installs the patch."""
    from app.services import calendar_settings_service as css
    from zoneinfo import ZoneInfo
    fixed = datetime(year, month, day, 9, 0, tzinfo=ZoneInfo(NY))
    monkeypatch.setattr(css, "client_now", lambda settings: fixed)


# ---------------------------------------------------------------------------
# Authentication / fail-closed (mirrors the frozen portal_auth contract).
# ---------------------------------------------------------------------------

@requires_db
def test_missing_token_is_401(portal_http):
    res = _get(portal_http, "/portal/appointments")
    assert res.status_code == 401
    assert res.json()["detail"] == INVALID_DETAIL


@requires_db
def test_malformed_token_is_401(portal_http):
    res = _get(portal_http, "/portal/appointments", "not-a-jwt")
    assert res.status_code == 401
    assert res.json()["detail"] == INVALID_DETAIL


@requires_db
def test_unknown_subject_is_401(portal_http):
    # A validly-signed token whose sub is bound to no office_users row.
    res = _get(portal_http, "/portal/appointments", _token(uuid.uuid4()))
    assert res.status_code == 401
    assert res.json()["detail"] == INVALID_DETAIL


@requires_db
def test_inactive_binding_is_401(portal_http, db, client_row):
    user = _bind_office_user(db, client_row, active=False)
    res = _get(portal_http, "/portal/appointments",
               _token(user.auth_user_id))
    assert res.status_code == 401
    assert res.json()["detail"] == INVALID_DETAIL


# ---------------------------------------------------------------------------
# Tenant isolation (the highest-risk contract).
# ---------------------------------------------------------------------------

@requires_db
def test_office_sees_only_its_own_appointments(
        portal_http, db, client_row, office_b):
    day = "2026-07-16"
    _make_appointment(db, client_row, name="A Patient",
                      start_utc=_local_noon_utc(day),
                      end_utc=_local_noon_utc(day) + timedelta(minutes=45))
    _make_appointment(db, office_b, name="B Patient",
                      start_utc=_local_noon_utc(day),
                      end_utc=_local_noon_utc(day) + timedelta(minutes=45))
    user_a = _bind_office_user(db, client_row)

    body = _get(portal_http,
                f"/portal/appointments?start_day={day}&end_day={day}",
                _token(user_a.auth_user_id)).json()
    names = [a["patient_name"] for a in body["appointments"]]
    assert names == ["A Patient"]
    assert "B Patient" not in names


@requires_db
def test_isolation_is_bidirectional(
        portal_http, db, client_row, office_b):
    day = "2026-07-16"
    _make_appointment(db, client_row, name="A Patient",
                      start_utc=_local_noon_utc(day),
                      end_utc=_local_noon_utc(day) + timedelta(minutes=45))
    _make_appointment(db, office_b, name="B Patient",
                      start_utc=_local_noon_utc(day),
                      end_utc=_local_noon_utc(day) + timedelta(minutes=45))
    user_a = _bind_office_user(db, client_row)
    user_b = _bind_office_user(db, office_b)

    a_names = [a["patient_name"] for a in _get(
        portal_http, f"/portal/appointments?start_day={day}&end_day={day}",
        _token(user_a.auth_user_id)).json()["appointments"]]
    b_names = [a["patient_name"] for a in _get(
        portal_http, f"/portal/appointments?start_day={day}&end_day={day}",
        _token(user_b.auth_user_id)).json()["appointments"]]
    assert a_names == ["A Patient"]
    assert b_names == ["B Patient"]


@requires_db
def test_stray_client_id_cannot_override_the_bound_tenant(
        portal_http, db, client_row, office_b):
    day = "2026-07-16"
    _make_appointment(db, office_b, name="B Patient",
                      start_utc=_local_noon_utc(day),
                      end_utc=_local_noon_utc(day) + timedelta(minutes=45))
    user_a = _bind_office_user(db, client_row)

    # A smuggled ?client_id pointing at office B is an undeclared parameter;
    # FastAPI ignores it and the query stays scoped to A -> zero rows.
    res = _get(
        portal_http,
        f"/portal/appointments?start_day={day}&end_day={day}"
        f"&client_id={office_b.id}",
        _token(user_a.auth_user_id))
    assert res.status_code == 200
    assert res.json()["appointments"] == []


# ---------------------------------------------------------------------------
# Range semantics.
# ---------------------------------------------------------------------------

@requires_db
def test_default_range_is_seven_inclusive_local_days(
        portal_http, db, client_row, monkeypatch):
    # Pin "today" so the default window is deterministic. The route calls
    # calendar_settings_service.client_now THROUGH the module, so patching
    # the module attribute is genuinely observed by the route (F1 fix); a
    # bare `from ... import client_now` in the route would NOT be reachable
    # this way, which is exactly why the route imports the module.
    _freeze_office_today(monkeypatch, 2026, 7, 16)

    user = _bind_office_user(db, client_row)
    res = _get(portal_http, "/portal/appointments", _token(user.auth_user_id))
    assert res.status_code == 200
    body = res.json()
    # today .. today+6 == 2026-07-16 .. 2026-07-22 (seven inclusive days).
    assert body["start_day"] == "2026-07-16"
    assert body["end_day"] == "2026-07-22"


@requires_db
def test_default_range_seam_bite_route_observes_the_patched_clock(
        portal_http, db, client_row, monkeypatch):
    """F1 BITE: prove the route actually READS the patched settings-service
    clock. The frozen date is a fixed, non-current day; the asserted window
    can ONLY be produced if calendar_settings_service.client_now is the live
    seam the route consults. Against the pre-fix code (which bound its own
    client_now at import), this monkeypatch would NOT reach the route and the
    window would be anchored on the real current date instead - so this test
    fails on the pre-fix route and passes only on the fixed route."""
    _freeze_office_today(monkeypatch, 2031, 2, 3)  # a fixed, non-today date
    user = _bind_office_user(db, client_row)
    body = _get(portal_http, "/portal/appointments",
                _token(user.auth_user_id)).json()
    assert body["start_day"] == "2031-02-03"
    assert body["end_day"] == "2031-02-09"   # today + 6, inclusive


@requires_db
def test_default_range_end_day_is_inclusive_and_not_eight_days(
        portal_http, db, client_row, monkeypatch):
    """An appointment on the SEVENTH local day (today+6) is inside the
    default window; one on the eighth (today+7) is not."""
    _freeze_office_today(monkeypatch, 2026, 7, 16)

    seventh = "2026-07-22"   # today + 6
    eighth = "2026-07-23"    # today + 7
    _make_appointment(db, client_row, name="Seventh Day",
                      start_utc=_local_noon_utc(seventh),
                      end_utc=_local_noon_utc(seventh) + timedelta(minutes=30))
    _make_appointment(db, client_row, name="Eighth Day",
                      start_utc=_local_noon_utc(eighth),
                      end_utc=_local_noon_utc(eighth) + timedelta(minutes=30))
    user = _bind_office_user(db, client_row)

    names = [a["patient_name"] for a in _get(
        portal_http, "/portal/appointments",
        _token(user.auth_user_id)).json()["appointments"]]
    assert "Seventh Day" in names
    assert "Eighth Day" not in names


@requires_db
def test_explicit_range_filters_to_its_bounds(
        portal_http, db, client_row):
    inside = "2026-07-16"
    outside = "2026-07-20"
    _make_appointment(db, client_row, name="Inside",
                      start_utc=_local_noon_utc(inside),
                      end_utc=_local_noon_utc(inside) + timedelta(minutes=30))
    _make_appointment(db, client_row, name="Outside",
                      start_utc=_local_noon_utc(outside),
                      end_utc=_local_noon_utc(outside) + timedelta(minutes=30))
    user = _bind_office_user(db, client_row)

    names = [a["patient_name"] for a in _get(
        portal_http,
        f"/portal/appointments?start_day={inside}&end_day={inside}",
        _token(user.auth_user_id)).json()["appointments"]]
    assert names == ["Inside"]


@requires_db
def test_reversed_range_is_422(portal_http, db, client_row):
    user = _bind_office_user(db, client_row)
    res = _get(
        portal_http,
        "/portal/appointments?start_day=2026-07-20&end_day=2026-07-16",
        _token(user.auth_user_id))
    assert res.status_code == 422


@requires_db
def test_partial_range_is_422(portal_http, db, client_row):
    user = _bind_office_user(db, client_row)
    res = _get(portal_http, "/portal/appointments?start_day=2026-07-16",
               _token(user.auth_user_id))
    assert res.status_code == 422


@requires_db
def test_empty_range_is_200_empty_list(
        portal_http, db, client_row):
    user = _bind_office_user(db, client_row)
    res = _get(
        portal_http,
        "/portal/appointments?start_day=2026-07-16&end_day=2026-07-16",
        _token(user.auth_user_id))
    assert res.status_code == 200
    assert res.json()["appointments"] == []


@requires_db
def test_dst_fall_back_day_includes_its_full_25_hours(
        portal_http, db, client_row):
    """2026-11-01 is a 25-hour local day in America/New_York (fall back). An
    appointment late on that local date must be inside a single-day window
    for that date - proving the DST-safe local_day_utc_window is used, not a
    naive +24h form that would drop the final local hour."""
    from zoneinfo import ZoneInfo
    dst_day = "2026-11-01"
    # 23:30 local on the 25-hour day: past a naive +24h end, inside the real
    # local-day window.
    late_local = datetime(2026, 11, 1, 23, 30, tzinfo=ZoneInfo(NY))
    late_utc = late_local.astimezone(UTC)
    _make_appointment(db, client_row, name="Late DST",
                      start_utc=late_utc,
                      end_utc=late_utc + timedelta(minutes=15))
    user = _bind_office_user(db, client_row)

    names = [a["patient_name"] for a in _get(
        portal_http,
        f"/portal/appointments?start_day={dst_day}&end_day={dst_day}",
        _token(user.auth_user_id)).json()["appointments"]]
    assert names == ["Late DST"]


# ---------------------------------------------------------------------------
# Response shape / leak prevention.
# ---------------------------------------------------------------------------

@requires_db
def test_response_shape_is_exactly_the_approved_fields(
        portal_http, db, client_row):
    day = "2026-07-16"
    _make_appointment(db, client_row, name="Shape Patient", email="p@x.test",
                      start_utc=_local_noon_utc(day),
                      end_utc=_local_noon_utc(day) + timedelta(minutes=45))
    user = _bind_office_user(db, client_row)

    body = _get(
        portal_http,
        f"/portal/appointments?start_day={day}&end_day={day}",
        _token(user.auth_user_id)).json()
    assert set(body.keys()) == APPROVED_ENVELOPE_FIELDS
    assert len(body["appointments"]) == 1
    assert set(body["appointments"][0].keys()) == APPROVED_APPOINTMENT_FIELDS


@requires_db
def test_no_forbidden_markers_in_response_body(
        portal_http, db, client_row):
    day = "2026-07-16"
    _make_appointment(db, client_row, name="Marker Patient",
                      start_utc=_local_noon_utc(day),
                      end_utc=_local_noon_utc(day) + timedelta(minutes=45),
                      office_sms_sent=True)
    user = _bind_office_user(db, client_row)

    raw = _get(
        portal_http,
        f"/portal/appointments?start_day={day}&end_day={day}",
        _token(user.auth_user_id)).text
    for marker in FORBIDDEN_BODY_MARKERS:
        assert marker not in raw, f"forbidden marker leaked: {marker}"


@requires_db
def test_malformed_legacy_notify_error_never_leaks_raw_text(
        portal_http, db, client_row):
    """A legacy raw provider exception stored in notify_error must surface as
    the safe derived outcome 'failed' with NONE of its raw text in the body
    (the sanitize boundary + derived-outcome reduction working together)."""
    day = "2026-07-16"
    raw_secret = "Twilio 500: https://api.twilio.com/leaked?token=SECRET123"
    _make_appointment(db, client_row, name="Legacy Patient",
                      start_utc=_local_noon_utc(day),
                      end_utc=_local_noon_utc(day) + timedelta(minutes=45),
                      notify_error=raw_secret)
    user = _bind_office_user(db, client_row)

    res = _get(
        portal_http,
        f"/portal/appointments?start_day={day}&end_day={day}",
        _token(user.auth_user_id))
    body = res.json()
    appt = body["appointments"][0]
    assert appt["notification_outcome"] == "failed"
    assert "SECRET123" not in res.text
    assert "twilio.com" not in res.text.lower()


@requires_db
def test_times_are_utc_and_status_passthrough(
        portal_http, db, client_row):
    day = "2026-07-16"
    start = _local_noon_utc(day)
    _make_appointment(db, client_row, name="TZ Patient",
                      start_utc=start,
                      end_utc=start + timedelta(minutes=45),
                      status="confirmed")
    user = _bind_office_user(db, client_row)

    body = _get(
        portal_http,
        f"/portal/appointments?start_day={day}&end_day={day}",
        _token(user.auth_user_id)).json()
    appt = body["appointments"][0]
    # The backend returns UTC instants (aware); the frontend renders local.
    assert appt["start_datetime"].endswith("+00:00") or \
        appt["start_datetime"].endswith("Z")
    assert appt["status"] == "confirmed"
    assert body["timezone_name"] == NY
