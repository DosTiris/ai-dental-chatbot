# calendar_tests/test_portal_schedule.py
#
# P4-A - Portal Slot Schedule Controls v1 (contract v1.2): the backend bite
# and regression suite for the five /portal/schedule endpoints, the
# advisory-lock serialization, DST wall-time classification, exact
# expansion, overlap refusal, the bulk sweep, and the patient-facing
# availability chain.
#
# BITE PROOF: every HTTP test here FAILS against untouched fd257005 - no
# /portal/schedule route exists there, so authenticated requests return 404
# where these tests demand 200/422/409 (and the service imports fail at
# collection). Proven by the delivered bite-proof run.
#
# Tokens are minted locally with PyJWT (HS256 test secret) - no network, no
# real Supabase project (the frozen test_portal_appointments.py pattern).
#
# CONCURRENCY (contract SS8.6): the threaded bites drive REAL parallel
# sessions (app.database.SessionLocal) against the throwaway PostgreSQL.
# They are skipped on any non-PostgreSQL TEST_DATABASE_URL because the
# advisory lock is PostgreSQL-native (the documented dialect seam).
# Sandbox runs are corroborating only (Rule 19): Kevin's Windows /
# PostgreSQL 17.10 run remains authoritative.
#
# Run (owner-local, PowerShell 5.1):
#   $env:ALLOW_DESTRUCTIVE_CALENDAR_TESTS="yes"
#   $env:TEST_DATABASE_URL="postgresql://postgres:test@localhost:5433/mia_calendar_test"
#   python -m pytest calendar_tests\test_portal_schedule.py -v

import os
import sys
import threading
import time as time_module
import uuid
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from calendar_tests.conftest import requires_db, TEST_DB_URL  # noqa: E402

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

# The COMPLETE approved field sets (leak-prevention pins - contract SS5-A/E).
APPROVED_SLOT_FIELDS = {
    "slot_id", "start_datetime", "end_datetime", "status",
    "provider_name", "service_key",
}
# Slice 4D-B envelope amendment: the schedule read carries the window's
# OPERATIONAL closed dates (from settings.calendar.closed_days only).
APPROVED_ENVELOPE_FIELDS = {"timezone_name", "start_day", "end_day", "slots",
                            "closed_days"}
APPROVED_BULK_FIELDS = {"day", "blocked_count", "booked_remaining"}
APPROVED_BOOKED_WINDOW_FIELDS = {"start_datetime", "end_datetime"}
# Markers that must NEVER appear anywhere in a schedule response body.
FORBIDDEN_BODY_MARKERS = [
    "client_id", "held_until", "held_by_conversation_id", "conversation_id",
    "patient_name", "patient_phone", "patient_email", "notify_error",
    "api_key", "client_key", '"settings"', "notification_email",
    "notification_phone",
]

requires_postgres = pytest.mark.skipif(
    not TEST_DB_URL.startswith("postgresql"),
    reason="advisory-lock concurrency bites are PostgreSQL-only "
           "(documented dialect seam)",
)


def _token(sub, *, secret=TEST_SECRET, aud=AUDIENCE, exp_delta=300,
           email="office@example.test"):
    """Mint a Supabase-shaped access token (frozen harness pattern)."""
    claims = {
        "sub": str(sub), "aud": aud,
        "exp": int(time_module.time()) + exp_delta,
        "email": email, "role": "authenticated", "iss": TEST_ISSUER,
    }
    return pyjwt.encode(claims, secret, algorithm="HS256")


# ---------------------------------------------------------------------------
# Fixtures (house harness, mirroring test_portal_appointments.py)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def office_users_table(engine):
    """Run the REAL migration 007 up before this module and down after."""
    import sqlalchemy

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
    from app.models import Client
    client = Client(
        id=uuid.uuid4(), practice_name="Other Dental",
        api_key=f"key-{uuid.uuid4()}", active=True,
        settings={"timezone": NY, "calendar": {"booking_enabled": True}},
    )
    db.add(client)
    db.commit()
    return client


def _bind_office_user(db, client, *, active=True):
    from app.portal_models import OfficeUser
    row = OfficeUser(auth_user_id=uuid.uuid4(), client_id=client.id,
                     active=active)
    db.add(row)
    db.commit()
    return row


def _make_slot(db, client, *, start_utc, end_utc=None, status="available",
               held_until=None, held_by=None, service_key=None,
               provider_name=None):
    from app.calendar_models import AppointmentSlot
    slot = AppointmentSlot(
        client_id=client.id, start_datetime=start_utc,
        end_datetime=end_utc or (start_utc + timedelta(hours=1)),
        status=status, held_until=held_until,
        held_by_conversation_id=held_by, service_key=service_key,
        provider_name=provider_name,
    )
    db.add(slot)
    db.commit()
    return slot


def _ny_instant(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=ZoneInfo(NY)).astimezone(UTC)


@pytest.fixture()
def portal_http(db, office_users_table, monkeypatch):
    """Real app containing the P2 identity router AND the P4-A schedule
    router, driven over HTTP. Only the session dependency is overridden
    (portal_schedule imports the SAME get_db callable); the REAL
    authorization owner runs."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.routes import portal as portal_routes
    from app.routes import portal_schedule as portal_schedule_routes
    from app.services import portal_auth

    monkeypatch.setenv(portal_auth.ENV_JWT_SECRET, TEST_SECRET)
    monkeypatch.delenv(portal_auth.ENV_JWKS_URL, raising=False)
    monkeypatch.setenv(portal_auth.ENV_ISSUER, TEST_ISSUER)
    portal_auth._jwks_clients.clear()

    app = FastAPI()
    app.include_router(portal_routes.router)
    app.include_router(portal_schedule_routes.router)
    app.dependency_overrides[portal_routes.get_db] = lambda: db
    with TestClient(app) as client:
        yield client


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _freeze_office_today(monkeypatch, year, month, day):
    """Freeze the office-local 'now' at 09:00 on the given NY date by
    patching the settings-service module attribute the route calls THROUGH
    (the frozen P3-C seam)."""
    from app.services import calendar_settings_service as css
    fixed = datetime(year, month, day, 9, 0, tzinfo=ZoneInfo(NY))
    monkeypatch.setattr(css, "client_now", lambda settings: fixed)


# ---------------------------------------------------------------------------
# SS8.1 - routes + shapes + leak pins
# ---------------------------------------------------------------------------

@requires_db
def test_schedule_read_shape_and_leak_pin(portal_http, db, client_row,
                                          monkeypatch):
    user = _bind_office_user(db, client_row)
    _make_slot(db, client_row, start_utc=_ny_instant(2026, 8, 21, 9),
               status="available")
    _make_slot(db, client_row, start_utc=_ny_instant(2026, 8, 21, 10),
               status="blocked")
    r = portal_http.get(
        "/portal/schedule",
        params={"start_day": "2026-08-21", "end_day": "2026-08-21"},
        headers=_auth(_token(user.auth_user_id)))
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == APPROVED_ENVELOPE_FIELDS
    assert body["timezone_name"] == NY
    assert len(body["slots"]) == 2  # ALL statuses are visible to staff
    for slot in body["slots"]:
        assert set(slot.keys()) == APPROVED_SLOT_FIELDS
    raw = r.text
    for marker in FORBIDDEN_BODY_MARKERS:
        assert marker not in raw, f"leak: {marker}"


@requires_db
def test_schedule_default_range_is_seven_days(portal_http, db, client_row,
                                              monkeypatch):
    user = _bind_office_user(db, client_row)
    _freeze_office_today(monkeypatch, 2026, 7, 16)
    r = portal_http.get("/portal/schedule",
                        headers=_auth(_token(user.auth_user_id)))
    assert r.status_code == 200
    body = r.json()
    assert body["start_day"] == "2026-07-16"
    assert body["end_day"] == "2026-07-22"


@requires_db
def test_schedule_unauthenticated_fails_closed(portal_http):
    for path, method in [
        ("/portal/schedule", "get"),
        ("/portal/schedule/days/2026-08-21/publish", "post"),
        (f"/portal/schedule/slots/{uuid.uuid4()}/block", "post"),
        (f"/portal/schedule/slots/{uuid.uuid4()}/unblock", "post"),
        ("/portal/schedule/days/2026-08-21/block-all-open", "post"),
    ]:
        kwargs = {}
        if path.endswith("/publish"):
            kwargs["json"] = {"open_time": "09:00", "close_time": "17:00",
                              "slot_minutes": 60}
        response = getattr(portal_http, method)(path, **kwargs)
        assert response.status_code == 401, path
        assert response.json()["detail"] == INVALID_DETAIL


# ---------------------------------------------------------------------------
# SS8.13 - range rules
# ---------------------------------------------------------------------------

@requires_db
@pytest.mark.parametrize("params,detail_piece", [
    ({"start_day": "2026-08-21"}, "supplied together"),
    ({"end_day": "2026-08-21"}, "supplied together"),
    ({"start_day": "2026-08-22", "end_day": "2026-08-21"}, "before"),
    ({"start_day": "2026-08-01", "end_day": "2026-09-01"}, "at most 31"),
])
def test_schedule_range_refusals(portal_http, db, client_row, params,
                                 detail_piece):
    user = _bind_office_user(db, client_row)
    r = portal_http.get("/portal/schedule", params=params,
                        headers=_auth(_token(user.auth_user_id)))
    assert r.status_code == 422
    assert detail_piece in r.json()["detail"]


@requires_db
def test_schedule_range_31_days_is_accepted(portal_http, db, client_row):
    user = _bind_office_user(db, client_row)
    r = portal_http.get(
        "/portal/schedule",
        params={"start_day": "2026-08-01", "end_day": "2026-08-31"},
        headers=_auth(_token(user.auth_user_id)))
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# SS8.2/8.3 - publish expansion, exact rules, strict transport
# ---------------------------------------------------------------------------

@requires_db
def test_publish_9_to_17_at_60_creates_8_correct_utc_rows(portal_http, db,
                                                          client_row):
    user = _bind_office_user(db, client_row)
    r = portal_http.post(
        "/portal/schedule/days/2026-08-21/publish",
        json={"open_time": "09:00", "close_time": "17:00", "slot_minutes": 60},
        headers=_auth(_token(user.auth_user_id)))
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 8
    for slot in body:
        assert set(slot.keys()) == APPROVED_SLOT_FIELDS
        assert slot["status"] == "available"
        assert slot["provider_name"] is None    # D5: generic slots
        assert slot["service_key"] is None
    starts = [datetime.fromisoformat(s["start_datetime"].replace("Z", "+00:00"))
              for s in body]
    assert starts[0] == _ny_instant(2026, 8, 21, 9)
    assert starts[-1] == _ny_instant(2026, 8, 21, 16)


@requires_db
def test_publish_at_30_creates_16_rows(portal_http, db, client_row):
    user = _bind_office_user(db, client_row)
    r = portal_http.post(
        "/portal/schedule/days/2026-08-21/publish",
        json={"open_time": "09:00", "close_time": "17:00", "slot_minutes": 30},
        headers=_auth(_token(user.auth_user_id)))
    assert r.status_code == 200 and len(r.json()) == 16


@requires_db
@pytest.mark.parametrize("body_json,piece", [
    # remainder refused loudly (Correction E)
    ({"open_time": "09:00", "close_time": "17:30", "slot_minutes": 60},
     "exact multiple"),
    ({"open_time": "09:00", "close_time": "15:45", "slot_minutes": 30},
     "exact multiple"),
    # slot_minutes bounds and step
    ({"open_time": "09:00", "close_time": "10:06", "slot_minutes": 33},
     "divisible by 5"),
    ({"open_time": "09:00", "close_time": "10:00", "slot_minutes": 8},
     "between 10 and 240"),
    ({"open_time": "09:00", "close_time": "17:00", "slot_minutes": 245},
     "between 10 and 240"),
    # ordering + malformed times
    ({"open_time": "17:00", "close_time": "09:00", "slot_minutes": 60},
     "after open_time"),
    ({"open_time": "9:00", "close_time": "17:00", "slot_minutes": 60},
     "open_time must be HH:MM"),
    ({"open_time": "09:00", "close_time": "25:00", "slot_minutes": 60},
     "close_time must be HH:MM"),
    # >100 generated slots (00:00-20:00 at 10 = 120)
    ({"open_time": "00:00", "close_time": "20:00", "slot_minutes": 10},
     "maximum per publish is 100"),
])
def test_publish_422_matrix_zero_inserts(portal_http, db, client_row,
                                         body_json, piece):
    from app.repositories import appointment_repository
    from app.services.calendar_settings_service import local_day_utc_window
    user = _bind_office_user(db, client_row)
    r = portal_http.post("/portal/schedule/days/2026-08-21/publish",
                         json=body_json,
                         headers=_auth(_token(user.auth_user_id)))
    assert r.status_code == 422
    assert piece in r.json()["detail"]
    day_start, day_end = local_day_utc_window(date(2026, 8, 21), NY)
    assert appointment_repository.list_slots_between(
        db, client_row.id, day_start, day_end) == []


@requires_db
def test_publish_strict_model_rejects_undeclared_field(portal_http, db,
                                                       client_row):
    """Correction C5 bite: extra='forbid' - a smuggled key is 422, never
    silently ignored."""
    user = _bind_office_user(db, client_row)
    r = portal_http.post(
        "/portal/schedule/days/2026-08-21/publish",
        json={"open_time": "09:00", "close_time": "17:00",
              "slot_minutes": 60, "provider": "smuggled"},
        headers=_auth(_token(user.auth_user_id)))
    assert r.status_code == 422


@requires_db
def test_publish_45_minute_slots_exact_span(portal_http, db, client_row):
    user = _bind_office_user(db, client_row)
    r = portal_http.post(
        "/portal/schedule/days/2026-08-21/publish",
        json={"open_time": "09:00", "close_time": "15:45", "slot_minutes": 45},
        headers=_auth(_token(user.auth_user_id)))
    assert r.status_code == 200 and len(r.json()) == 9


# ---------------------------------------------------------------------------
# SS8.4 - DST bites (Correction C2 / D9)
# ---------------------------------------------------------------------------

@requires_db
def test_publish_spring_forward_gap_is_nonexistent_422(portal_http, db,
                                                       client_row):
    """2027-03-14 02:00-03:00 NY does not exist. A boundary in the gap
    refuses the WHOLE request with zero inserts."""
    from app.repositories import appointment_repository
    from app.services.calendar_settings_service import local_day_utc_window
    user = _bind_office_user(db, client_row)
    r = portal_http.post(
        "/portal/schedule/days/2027-03-14/publish",
        json={"open_time": "02:00", "close_time": "03:00", "slot_minutes": 30},
        headers=_auth(_token(user.auth_user_id)))
    assert r.status_code == 422
    assert "does not exist" in r.json()["detail"]
    day_start, day_end = local_day_utc_window(date(2027, 3, 14), NY)
    assert appointment_repository.list_slots_between(
        db, client_row.id, day_start, day_end) == []


@requires_db
def test_publish_fall_back_ambiguous_interval_422(portal_http, db,
                                                  client_row):
    """2026-11-01 01:00-01:59 NY occurs twice. Any boundary inside the
    repeated interval refuses the WHOLE request (fold never resolves)."""
    from app.repositories import appointment_repository
    from app.services.calendar_settings_service import local_day_utc_window
    user = _bind_office_user(db, client_row)
    r = portal_http.post(
        "/portal/schedule/days/2026-11-01/publish",
        json={"open_time": "01:00", "close_time": "02:00", "slot_minutes": 30},
        headers=_auth(_token(user.auth_user_id)))
    assert r.status_code == 422
    assert "occurs twice" in r.json()["detail"]
    day_start, day_end = local_day_utc_window(date(2026, 11, 1), NY)
    assert appointment_repository.list_slots_between(
        db, client_row.id, day_start, day_end) == []


@requires_db
def test_publish_on_fall_back_day_outside_ambiguity_succeeds(portal_http, db,
                                                             client_row):
    """09:00-17:00 on 2026-11-01 is entirely outside the repeated interval:
    exactly one valid round-trip candidate per boundary -> valid."""
    user = _bind_office_user(db, client_row)
    r = portal_http.post(
        "/portal/schedule/days/2026-11-01/publish",
        json={"open_time": "09:00", "close_time": "17:00", "slot_minutes": 60},
        headers=_auth(_token(user.auth_user_id)))
    assert r.status_code == 200 and len(r.json()) == 8
    first = datetime.fromisoformat(
        r.json()[0]["start_datetime"].replace("Z", "+00:00"))
    # 09:00 EST (post-transition) = 14:00 UTC.
    assert first == datetime(2026, 11, 1, 14, 0, tzinfo=UTC)


@requires_db
def test_block_all_open_sweeps_full_25_hour_local_day(portal_http, db,
                                                      client_row):
    """The 2026-11-01 NY local day spans 25 hours in UTC. Slots in BOTH
    offset segments (EDT 01:30 first pass = 05:30Z; EST 22:00 = 03:00Z next
    UTC day) are swept by one block-all-open."""
    user = _bind_office_user(db, client_row)
    early = _make_slot(db, client_row,
                       start_utc=datetime(2026, 11, 1, 5, 30, tzinfo=UTC),
                       end_utc=datetime(2026, 11, 1, 6, 0, tzinfo=UTC))
    late = _make_slot(db, client_row,
                      start_utc=datetime(2026, 11, 2, 3, 0, tzinfo=UTC),
                      end_utc=datetime(2026, 11, 2, 3, 30, tzinfo=UTC))
    r = portal_http.post("/portal/schedule/days/2026-11-01/block-all-open",
                         headers=_auth(_token(user.auth_user_id)))
    assert r.status_code == 200
    assert r.json()["blocked_count"] == 2
    db.refresh(early)
    db.refresh(late)
    assert early.status == "blocked" and late.status == "blocked"


# ---------------------------------------------------------------------------
# SS8.5 - overlap refusal
# ---------------------------------------------------------------------------

@requires_db
@pytest.mark.parametrize("existing_status", ["available", "held", "booked",
                                             "blocked"])
def test_publish_overlap_409_zero_inserts(portal_http, db, client_row,
                                          existing_status):
    from app.repositories import appointment_repository
    from app.services.calendar_settings_service import local_day_utc_window
    user = _bind_office_user(db, client_row)
    held_until = (datetime.now(UTC) + timedelta(minutes=5)
                  if existing_status == "held" else None)
    _make_slot(db, client_row, start_utc=_ny_instant(2026, 8, 21, 10),
               end_utc=_ny_instant(2026, 8, 21, 11), status=existing_status,
               held_until=held_until,
               held_by=uuid.uuid4() if held_until else None)
    r = portal_http.post(
        "/portal/schedule/days/2026-08-21/publish",
        json={"open_time": "09:00", "close_time": "17:00", "slot_minutes": 60},
        headers=_auth(_token(user.auth_user_id)))
    assert r.status_code == 409
    assert r.json()["detail"] == (
        "One or more requested slots overlap existing slots on that day.")
    day_start, day_end = local_day_utc_window(date(2026, 8, 21), NY)
    rows = appointment_repository.list_slots_between(
        db, client_row.id, day_start, day_end)
    assert len(rows) == 1  # only the pre-existing row; ZERO inserts


@requires_db
def test_publish_overlapping_only_cancelled_succeeds(portal_http, db,
                                                     client_row):
    user = _bind_office_user(db, client_row)
    _make_slot(db, client_row, start_utc=_ny_instant(2026, 8, 21, 10),
               end_utc=_ny_instant(2026, 8, 21, 11), status="cancelled")
    r = portal_http.post(
        "/portal/schedule/days/2026-08-21/publish",
        json={"open_time": "09:00", "close_time": "17:00", "slot_minutes": 60},
        headers=_auth(_token(user.auth_user_id)))
    assert r.status_code == 200 and len(r.json()) == 8


# ---------------------------------------------------------------------------
# SS8.8 - unblock over HTTP
# ---------------------------------------------------------------------------

@requires_db
def test_unblock_http_contract(portal_http, db, client_row, office_b):
    user = _bind_office_user(db, client_row)
    headers = _auth(_token(user.auth_user_id))

    blocked = _make_slot(db, client_row,
                         start_utc=_ny_instant(2026, 8, 21, 9),
                         status="blocked")
    r = portal_http.post(f"/portal/schedule/slots/{blocked.id}/unblock",
                         headers=headers)
    assert r.status_code == 200
    assert r.json()["status"] == "available"
    assert set(r.json().keys()) == APPROVED_SLOT_FIELDS

    booked = _make_slot(db, client_row,
                        start_utc=_ny_instant(2026, 8, 21, 10),
                        status="booked")
    r = portal_http.post(f"/portal/schedule/slots/{booked.id}/unblock",
                         headers=headers)
    assert r.status_code == 409
    assert r.json()["detail"] == "Slot is booked and cannot be unblocked."
    db.refresh(booked)
    assert booked.status == "booked"

    foreign = _make_slot(db, office_b, start_utc=_ny_instant(2026, 8, 21, 11),
                         status="blocked")
    r_foreign = portal_http.post(
        f"/portal/schedule/slots/{foreign.id}/unblock", headers=headers)
    r_missing = portal_http.post(
        f"/portal/schedule/slots/{uuid.uuid4()}/unblock", headers=headers)
    assert r_foreign.status_code == r_missing.status_code == 404
    assert r_foreign.json() == r_missing.json()  # indistinguishable
    db.refresh(foreign)
    assert foreign.status == "blocked"


# ---------------------------------------------------------------------------
# SS8.9/8.10 - bulk sweep + booked protection
# ---------------------------------------------------------------------------

@requires_db
def test_block_all_open_mixed_seed(portal_http, db, client_row):
    """The contract's mixed seed: exactly the three OPEN rows become
    blocked with holds cleared; both booked rows (pending AND confirmed
    appointments look identical at slot level: status booked) plus their
    windows are reported; blocked/cancelled rows byte-untouched; repeat is
    idempotent."""
    user = _bind_office_user(db, client_row)
    headers = _auth(_token(user.auth_user_id))
    day = date(2026, 8, 21)

    available = _make_slot(db, client_row, start_utc=_ny_instant(2026, 8, 21, 9))
    held_active = _make_slot(
        db, client_row, start_utc=_ny_instant(2026, 8, 21, 10), status="held",
        held_until=datetime.now(UTC) + timedelta(minutes=5),
        held_by=uuid.uuid4())
    held_expired = _make_slot(
        db, client_row, start_utc=_ny_instant(2026, 8, 21, 11), status="held",
        held_until=datetime.now(UTC) - timedelta(minutes=5),
        held_by=uuid.uuid4())
    booked_1 = _make_slot(db, client_row,
                          start_utc=_ny_instant(2026, 8, 21, 12),
                          status="booked")
    booked_2 = _make_slot(db, client_row,
                          start_utc=_ny_instant(2026, 8, 21, 13),
                          status="booked")
    already_blocked = _make_slot(db, client_row,
                                 start_utc=_ny_instant(2026, 8, 21, 14),
                                 status="blocked")
    cancelled = _make_slot(db, client_row,
                           start_utc=_ny_instant(2026, 8, 21, 15),
                           status="cancelled")

    r = portal_http.post(f"/portal/schedule/days/{day}/block-all-open",
                         headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == APPROVED_BULK_FIELDS
    assert body["blocked_count"] == 3
    assert len(body["booked_remaining"]) == 2
    for window in body["booked_remaining"]:
        assert set(window.keys()) == APPROVED_BOOKED_WINDOW_FIELDS
    raw = r.text
    for marker in FORBIDDEN_BODY_MARKERS:
        assert marker not in raw, f"leak: {marker}"
    # Wording pin (SS5-E / D3): a SLOT operation, never a closure.
    assert "close" not in raw.lower()

    for row in (available, held_active, held_expired):
        db.refresh(row)
        assert row.status == "blocked"
        assert row.held_until is None and row.held_by_conversation_id is None
    for row, expected in ((booked_1, "booked"), (booked_2, "booked"),
                          (already_blocked, "blocked"),
                          (cancelled, "cancelled")):
        db.refresh(row)
        assert row.status == expected

    # Idempotent repeat.
    r2 = portal_http.post(f"/portal/schedule/days/{day}/block-all-open",
                          headers=headers)
    assert r2.status_code == 200
    assert r2.json()["blocked_count"] == 0
    assert len(r2.json()["booked_remaining"]) == 2


@requires_db
def test_portal_block_booked_refused(portal_http, db, client_row):
    user = _bind_office_user(db, client_row)
    booked = _make_slot(db, client_row, start_utc=_ny_instant(2026, 8, 21, 9),
                        status="booked")
    r = portal_http.post(f"/portal/schedule/slots/{booked.id}/block",
                         headers=_auth(_token(user.auth_user_id)))
    assert r.status_code == 409
    assert r.json()["detail"] == (
        "Slot has a booked appointment. Cancel the appointment first.")
    db.refresh(booked)
    assert booked.status == "booked"


# ---------------------------------------------------------------------------
# SS8.11 - Mia availability reflects changes (D12 byte-verified expectations)
# ---------------------------------------------------------------------------

@requires_db
def test_availability_chain_after_bulk_block(portal_http, db, client_row,
                                             monkeypatch):
    """After block-all-open: get_available_slots is empty; the preview day
    is FULL when a policy-eligible booked row remains and UNAVAILABLE when
    only blocked rows remain (the D12 byte-verified distinction);
    place_hold on a blocked slot -> slot_blocked; a swept held slot ->
    hold_lost at finalize; unblock reopens the day."""
    from app.repositories import appointment_repository
    from app.schemas import AvailabilityPreviewRequest
    from app.services import booking_service
    from app.services.appointment_hold_service import place_hold
    from app.services.appointment_intent import PREF_ANY
    from app.services.availability_preview_service import (
        build_availability_preview,
    )
    from app.services.availability_service import get_available_slots
    from app.services.calendar_settings_service import load_calendar_settings

    user = _bind_office_user(db, client_row)
    headers = _auth(_token(user.auth_user_id))
    settings = load_calendar_settings(client_row)
    now_utc = datetime(2026, 8, 12, 16, 0, tzinfo=UTC)  # NY noon Aug 12

    # --- Day A (Aug 21): available + held + BOOKED -> post-sweep FULL.
    day_a = date(2026, 8, 21)
    open_a = _make_slot(db, client_row, start_utc=_ny_instant(2026, 8, 21, 9))
    # Seeded available; the REAL hold pipeline turns it into a held slot
    # below, so the sweep races an authentic mid-flow patient state.
    held_a = _make_slot(db, client_row, start_utc=_ny_instant(2026, 8, 21, 10))
    _make_slot(db, client_row, start_utc=_ny_instant(2026, 8, 21, 12),
               status="booked")
    # --- Day B (Aug 22): ONLY open rows -> post-sweep UNAVAILABLE.
    day_b = date(2026, 8, 22)
    open_b = _make_slot(db, client_row, start_utc=_ny_instant(2026, 8, 22, 9))

    assert len(get_available_slots(db, client_row.id, settings, day_a,
                                   PREF_ANY, now_utc)) == 2
    # A patient held held_a mid-flow before the sweep (real hold pipeline).
    conversation_id = uuid.uuid4()
    hold = place_hold(db, client_row.id, held_a.id, conversation_id,
                      settings=settings, time_preference=PREF_ANY,
                      service_key=None, now_utc=now_utc)
    assert hold.success

    for day in (day_a, day_b):
        r = portal_http.post(f"/portal/schedule/days/{day}/block-all-open",
                             headers=headers)
        assert r.status_code == 200

    # Patient-facing offers: nothing bookable on either day.
    assert get_available_slots(db, client_row.id, settings, day_a, PREF_ANY,
                               now_utc) == []
    assert get_available_slots(db, client_row.id, settings, day_b, PREF_ANY,
                               now_utc) == []

    # D12: preview FULL (eligible booked capacity remains) vs UNAVAILABLE
    # (only blocked rows remain). Vocabulary and implementation frozen.
    preview = build_availability_preview(
        db, client_row,
        AvailabilityPreviewRequest(start_day="2026-08-21",
                                   end_day="2026-08-22"),
        now_utc)
    states = {day.local_date.isoformat(): day.state for day in preview.days}
    assert states["2026-08-21"] == "full"
    assert states["2026-08-22"] == "unavailable"

    # place_hold on a blocked slot -> slot_blocked (frozen path).
    retry = place_hold(db, client_row.id, open_a.id, uuid.uuid4(),
                       settings=settings, time_preference=PREF_ANY,
                       service_key=None, now_utc=now_utc)
    assert not retry.success and retry.reason == "slot_blocked"

    # The swept mid-flow patient: finalize fails safe as hold_lost (D4).
    finalize = booking_service.finalize_booking(
        db, client_row.id, held_a.id, conversation_id,
        settings=settings, now_utc=now_utc, time_preference=PREF_ANY,
        service_key=None, patient_name="Kevin Alvarado",
        patient_phone="516-555-1234", patient_email=None,
        new_or_returning="new", reason="cleaning", urgency="routine")
    assert not finalize.success and finalize.reason == "hold_lost"

    # Unblock reopens the day for patients.
    r = portal_http.post(f"/portal/schedule/slots/{open_b.id}/unblock",
                         headers=headers)
    assert r.status_code == 200
    assert len(get_available_slots(db, client_row.id, settings, day_b,
                                   PREF_ANY, now_utc)) == 1
    preview_after = build_availability_preview(
        db, client_row,
        AvailabilityPreviewRequest(start_day="2026-08-22",
                                   end_day="2026-08-22"),
        now_utc)
    assert preview_after.days[0].state == "open"


# ---------------------------------------------------------------------------
# SS8.12 - tenant isolation
# ---------------------------------------------------------------------------

@requires_db
def test_two_office_isolation_all_mutations(portal_http, db, client_row,
                                            office_b):
    """A's read shows only A rows; A's publish/block/unblock/bulk never
    mutates a B row (B asserted identical before/after); a stray
    ?client_id=<B> is inert; foreign slot ids are tenant-opaque 404s."""
    user_a = _bind_office_user(db, client_row)
    headers = _auth(_token(user_a.auth_user_id))

    b_available = _make_slot(db, office_b,
                             start_utc=_ny_instant(2026, 8, 21, 9))
    b_blocked = _make_slot(db, office_b,
                           start_utc=_ny_instant(2026, 8, 21, 10),
                           status="blocked")
    b_before = {
        b_available.id: (b_available.status, b_available.start_datetime,
                         b_available.end_datetime),
        b_blocked.id: (b_blocked.status, b_blocked.start_datetime,
                       b_blocked.end_datetime),
    }

    # Read: only A's rows (none yet), even with a smuggled client_id.
    r = portal_http.get(
        "/portal/schedule",
        params={"start_day": "2026-08-21", "end_day": "2026-08-21",
                "client_id": str(office_b.id)},
        headers=headers)
    assert r.status_code == 200 and r.json()["slots"] == []

    # Publish overlapping B's 09:00-10:00 window: succeeds for A (the
    # overlap universe is tenant-scoped) and creates rows ONLY for A.
    r = portal_http.post(
        "/portal/schedule/days/2026-08-21/publish",
        json={"open_time": "09:00", "close_time": "11:00", "slot_minutes": 60},
        headers=headers)
    assert r.status_code == 200 and len(r.json()) == 2

    # Bulk sweep with a smuggled client_id: blocks ONLY A's rows.
    r = portal_http.post(
        "/portal/schedule/days/2026-08-21/block-all-open",
        params={"client_id": str(office_b.id)}, headers=headers)
    assert r.status_code == 200 and r.json()["blocked_count"] == 2

    # Foreign per-slot actions: tenant-opaque 404s.
    for action in ("block", "unblock"):
        r = portal_http.post(
            f"/portal/schedule/slots/{b_available.id}/{action}",
            headers=headers)
        assert r.status_code == 404
        assert r.json()["detail"] == "Slot not found."

    # B's rows byte-identical afterwards.
    db.expire_all()
    for slot_id, (status, start, end) in b_before.items():
        from app.calendar_models import AppointmentSlot
        row = db.get(AppointmentSlot, slot_id)
        assert (row.status, row.start_datetime, row.end_datetime) == (
            status, start, end)


# ---------------------------------------------------------------------------
# SS8.6 - PostgreSQL concurrency bites (advisory-lock serialization)
# ---------------------------------------------------------------------------

def _fresh_session():
    from app.database import SessionLocal
    return SessionLocal()


def _run_pair(fn_a, fn_b):
    """Run two callables in real threads behind one barrier; re-raise the
    first captured error; return (result_a, result_b)."""
    barrier = threading.Barrier(2)
    results = {}
    errors = []

    def runner(name, fn):
        try:
            barrier.wait(timeout=10)
            results[name] = fn()
        except Exception as exc:  # noqa: BLE001 - surfaced below
            errors.append((name, exc))

    t_a = threading.Thread(target=runner, args=("a", fn_a))
    t_b = threading.Thread(target=runner, args=("b", fn_b))
    t_a.start()
    t_b.start()
    t_a.join(timeout=30)
    t_b.join(timeout=30)
    assert not t_a.is_alive() and not t_b.is_alive(), "deadlocked pair"
    if errors:
        raise errors[0][1]
    return results["a"], results["b"]


@requires_db
@requires_postgres
def test_concurrent_publish_vs_publish_exactly_one_wins(db, client_row,
                                                        engine):
    """SS8.6a: two concurrent publishes, same tenant/day, overlapping
    windows, EMPTY day at start - the exact v1.0 defect scenario. Exactly
    one creates rows; the other serializes behind the advisory lock and
    receives the overlap refusal; the final table has no overlapping rows."""
    from app.services import portal_schedule_service as pss
    from app.services.calendar_settings_service import (
        load_calendar_settings, local_day_utc_window)
    from app.repositories import appointment_repository

    settings = load_calendar_settings(client_row)
    day = date(2026, 9, 10)
    client_id = client_row.id

    def publish(open_time, close_time):
        def call():
            session = _fresh_session()
            try:
                return pss.publish_day_slots(
                    session, client_id, settings, day,
                    open_time, close_time, 60)
            finally:
                session.close()
        return call

    result_a, result_b = _run_pair(publish("09:00", "17:00"),
                                   publish("10:00", "14:00"))
    outcomes = sorted([result_a.reason, result_b.reason])
    assert outcomes == [pss.PUBLISH_OK, pss.PUBLISH_OVERLAP], outcomes

    day_start, day_end = local_day_utc_window(day, NY)
    rows = appointment_repository.list_slots_between(
        db, client_id, day_start, day_end)
    winner = result_a if result_a.ok else result_b
    assert len(rows) == len(winner.slots)
    # No two rows overlap (half-open geometry).
    spans = sorted((r.start_datetime, r.end_datetime) for r in rows)
    for (s1, e1), (s2, e2) in zip(spans, spans[1:]):
        assert e1 <= s2


@requires_db
@requires_postgres
def test_concurrent_publish_vs_block_all_open_never_a_mix(db, client_row):
    """SS8.6b: the final state is one of the two serialized orders and
    never a mix - either EVERY published slot ended blocked (sweep ran
    second) or EVERY published slot ended available (publish ran second)."""
    from app.services import portal_schedule_service as pss
    from app.services.calendar_settings_service import (
        load_calendar_settings, local_day_utc_window)
    from app.repositories import appointment_repository

    settings = load_calendar_settings(client_row)
    day = date(2026, 9, 11)
    client_id = client_row.id

    def do_publish():
        session = _fresh_session()
        try:
            return pss.publish_day_slots(session, client_id, settings, day,
                                         "09:00", "17:00", 60)
        finally:
            session.close()

    def do_sweep():
        session = _fresh_session()
        try:
            return pss.block_all_open(session, client_id, settings, day)
        finally:
            session.close()

    publish_result, sweep_result = _run_pair(do_publish, do_sweep)
    assert publish_result.ok and len(publish_result.slots) == 8

    day_start, day_end = local_day_utc_window(day, NY)
    statuses = {r.status for r in appointment_repository.list_slots_between(
        db, client_id, day_start, day_end)}
    # Serialized orders only: all blocked (sweep second, blocked_count 8)
    # or all available (publish second, blocked_count 0). NEVER a mix.
    if sweep_result.blocked_count == 8:
        assert statuses == {"blocked"}
    else:
        assert sweep_result.blocked_count == 0
        assert statuses == {"available"}


@requires_db
@requires_postgres
def test_concurrent_sweep_vs_sweep_counts_sum(db, client_row):
    """SS8.6c: both bulk sweeps succeed; blocked counts sum to the day's
    open-slot count - no error, no double effect."""
    from app.services import portal_schedule_service as pss
    from app.services.calendar_settings_service import load_calendar_settings

    settings = load_calendar_settings(client_row)
    day = date(2026, 9, 12)
    for hour in (9, 10, 11, 12, 13):
        _make_slot(db, client_row, start_utc=_ny_instant(2026, 9, 12, hour))
    client_id = client_row.id

    def do_sweep():
        session = _fresh_session()
        try:
            return pss.block_all_open(session, client_id, settings, day)
        finally:
            session.close()

    result_a, result_b = _run_pair(do_sweep, do_sweep)
    assert result_a.blocked_count + result_b.blocked_count == 5


@requires_db
@requires_postgres
def test_lock_material_isolates_tenants_and_days(db, client_row, office_b):
    """SS8.6d (deterministic, no timing): while tenant A holds the day lock
    for day X inside an open transaction, pg_try_advisory_xact_lock proves
    (A, X) is contended while (B, X) and (A, Y) are free - the canonical
    material separates tenants and days."""
    from sqlalchemy import text as sql_text
    from app.services import portal_schedule_service as pss

    day_x, day_y = date(2026, 9, 13), date(2026, 9, 14)
    holder = _fresh_session()
    probe = _fresh_session()
    try:
        pss.acquire_schedule_day_lock(holder, client_row.id, day_x)

        def try_lock(client_id, day):
            material = pss.build_schedule_lock_material(client_id, day)
            got = probe.execute(
                sql_text("SELECT pg_try_advisory_xact_lock("
                         "hashtextextended(:m, 0))"),
                {"m": material}).scalar()
            return bool(got)

        assert try_lock(client_row.id, day_x) is False   # contended
        assert try_lock(office_b.id, day_x) is True      # other tenant: free
        assert try_lock(client_row.id, day_y) is True    # other day: free
    finally:
        probe.rollback()
        probe.close()
        holder.rollback()
        holder.close()


@requires_db
@requires_postgres
def test_concurrent_sweep_vs_place_hold_impossible_state_never_exists(
        db, client_row):
    """SS8.6e (Correction C6): block-all-open racing place_hold on the same
    available slot serializes through the existing row lock. Either
    legitimate order is accepted (hold-then-swept, or sweep-first ->
    slot_blocked); the invariant is the final row is NEVER status=blocked
    with an active hold, and the hold outcome is in the closed set."""
    from app.services import portal_schedule_service as pss
    from app.services.appointment_hold_service import place_hold
    from app.services.appointment_intent import PREF_ANY
    from app.services.calendar_settings_service import load_calendar_settings
    from app.calendar_models import AppointmentSlot

    settings = load_calendar_settings(client_row)
    day = date(2026, 9, 15)
    slot = _make_slot(db, client_row, start_utc=_ny_instant(2026, 9, 15, 10))
    client_id, slot_id = client_row.id, slot.id
    now_utc = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)

    def do_sweep():
        session = _fresh_session()
        try:
            return pss.block_all_open(session, client_id, settings, day)
        finally:
            session.close()

    def do_hold():
        session = _fresh_session()
        try:
            return place_hold(session, client_id, slot_id, uuid.uuid4(),
                              settings=settings, time_preference=PREF_ANY,
                              service_key=None, now_utc=now_utc)
        finally:
            session.close()

    sweep_result, hold_result = _run_pair(do_sweep, do_hold)

    # Closed outcome set: hold succeeded first (then swept) or was refused.
    assert (hold_result.success
            or hold_result.reason in ("slot_blocked", "slot_taken"))
    assert sweep_result.blocked_count in (0, 1)

    db.expire_all()
    final = db.get(AppointmentSlot, slot_id)
    # The impossible combination must NEVER exist (Correction C6).
    active_hold = (final.held_until is not None
                   and final.held_until.replace(tzinfo=UTC) >= now_utc)
    assert not (final.status == "blocked" and active_hold)
    assert final.status in ("blocked", "held")
    if final.status == "blocked":
        assert final.held_until is None
        assert final.held_by_conversation_id is None
