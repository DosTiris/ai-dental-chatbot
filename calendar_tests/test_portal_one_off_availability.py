# calendar_tests/test_portal_one_off_availability.py
#
# PHASE 3A SLICE 4D-A - Calendar-native one-off availability + manual staff
# scheduling: the focused backend suite for the new
# POST /portal/schedule/slots/one-off surface and its service owner
# portal_schedule_service.create_one_off_slot.
#
# WHAT IS PROVEN HERE (the 4D-A contract's test requirements):
#   * caller-facing prevalidation speaks the caller's field vocabulary
#     (start_time / duration_minutes) through the SAME shared helpers and
#     named limits publish uses - pure, zero DB statements on refusal;
#   * the STRICTLY-FUTURE rule against the SERVER-authoritative office
#     clock (calendar_settings_service.client_now, resolved through the
#     module seam) - including the mandated start == now boundary refusal;
#   * the SAME-LOCAL-DAY rule (cross-midnight and exactly-at-midnight
#     windows refused loudly);
#   * DST handling delegates to the ONE SS3 owner: nonexistent and
#     ambiguous starts are refused with the frozen publish wording;
#   * tenant isolation for creation AND booking (the verified credential
#     alone selects the tenant; a smuggled body tenant is rejected 422 by
#     the strict transport model; tenants never collide or cross-read);
#   * duplicate/conflicting inventory follows the EXISTING overlap rule
#     under the EXISTING advisory day lock (409, zero inserts; cancelled
#     history never blocks; adjacent half-open windows are legal);
#   * the frozen publish surface is byte-behavior-unchanged (no past rule
#     leaked into /publish);
#   * the EMPTY-WEEK ACCEPTANCE CASE end to end over HTTP: empty future
#     week -> one-off creation -> the Open slot arrives through the normal
#     authoritative /portal/schedule read -> the EXISTING staff-booking
#     route books it by real server slot_id -> the slot is consumed
#     (booked) -> reloading reflects backend truth;
#   * concurrency: two competing one-off creations for the same tenant/
#     day/time produce exactly one row (advisory-lock serialization); two
#     different tenants never serialize against each other; and two
#     competing staff bookings of the SAME one-off-created slot can never
#     double-book (the frozen row-lock arbiter, preserved).
#
# Tokens are minted locally with PyJWT (HS256 test secret) - no network,
# no real Supabase project (the frozen test_portal_appointments.py
# pattern). Threaded bites drive REAL parallel sessions
# (app.database.SessionLocal) against the throwaway PostgreSQL and are
# skipped on any non-PostgreSQL TEST_DATABASE_URL (the documented dialect
# seam). Sandbox runs are corroborating only (Rule 19): Kevin's owner-local
# PostgreSQL run remains authoritative.
#
# Run (owner-local, PowerShell 5.1):
#   $env:ALLOW_DESTRUCTIVE_CALENDAR_TESTS="yes"
#   $env:TEST_DATABASE_URL="postgresql://postgres:test@localhost:5433/mia_calendar_test"
#   python -m pytest calendar_tests\test_portal_one_off_availability.py -v

import os
import sys
import threading
import time as time_module
import uuid
from datetime import date, datetime, timezone
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

# The COMPLETE approved slot field set (the P4-A leak-prevention pin).
APPROVED_SLOT_FIELDS = {
    "slot_id", "start_datetime", "end_datetime", "status",
    "provider_name", "service_key",
}
# Markers that must NEVER appear anywhere in a one-off response body.
FORBIDDEN_BODY_MARKERS = [
    "client_id", "held_until", "held_by_conversation_id", "conversation_id",
    "patient_name", "patient_phone", "patient_email", "notify_error",
    "api_key", "client_key", '"settings"', "notification_email",
    "notification_phone",
]

requires_postgres = pytest.mark.skipif(
    not TEST_DB_URL.startswith("postgresql"),
    reason="advisory-lock / row-lock concurrency bites are PostgreSQL-only "
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
# Fixtures (house harness, mirroring test_portal_schedule.py)
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
    from datetime import timedelta
    from app.calendar_models import AppointmentSlot
    slot = AppointmentSlot(
        client_id=client.id, start_datetime=start_utc,
        end_datetime=end_utc or (start_utc + timedelta(minutes=30)),
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
    """Real app containing the P2 identity router, the P4-A schedule router
    (which now carries the 4D-A one-off route), the Slice 2 staff-booking
    router, and the P3 appointments read - the complete surface the
    empty-week acceptance case exercises over HTTP. Only the session
    dependency is overridden (every router imports the SAME get_db
    callable); the REAL authorization owner runs."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.routes import portal as portal_routes
    from app.routes import portal_schedule as portal_schedule_routes
    from app.routes import portal_staff_booking as portal_staff_booking_routes
    from app.routes import portal_appointments as portal_appointments_routes
    from app.services import portal_auth

    monkeypatch.setenv(portal_auth.ENV_JWT_SECRET, TEST_SECRET)
    monkeypatch.delenv(portal_auth.ENV_JWKS_URL, raising=False)
    monkeypatch.setenv(portal_auth.ENV_ISSUER, TEST_ISSUER)
    portal_auth._jwks_clients.clear()

    app = FastAPI()
    app.include_router(portal_routes.router)
    app.include_router(portal_schedule_routes.router)
    app.include_router(portal_staff_booking_routes.router)
    app.include_router(portal_appointments_routes.router)
    app.dependency_overrides[portal_routes.get_db] = lambda: db
    with TestClient(app) as client:
        yield client


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _freeze_office_now(monkeypatch, year, month, day, hour=9, minute=0):
    """Freeze the office-local SERVER clock by patching the settings-service
    module attribute the 4D-A service calls THROUGH (the frozen P3-C seam).
    This is the ONLY clock authority on the path: no request value can move
    it."""
    from app.services import calendar_settings_service as css
    fixed = datetime(year, month, day, hour, minute, tzinfo=ZoneInfo(NY))
    monkeypatch.setattr(css, "client_now", lambda settings: fixed)


def _settings_for(client):
    from app.services.calendar_settings_service import load_calendar_settings
    return load_calendar_settings(client)


def _one_off(portal_http, token, day, start_time, duration_minutes,
             extra=None):
    body = {"day": day, "start_time": start_time,
            "duration_minutes": duration_minutes}
    if extra:
        body.update(extra)
    return portal_http.post("/portal/schedule/slots/one-off",
                            json=body, headers=_auth(token))


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


def _pure_settings(timezone_name=NY):
    """A CalendarSettings snapshot for the PURE refusal tests (no DB): the
    prevalidation paths under test return before any session use, proven by
    passing db=None - a DB touch would raise immediately."""
    from app.services.calendar_settings_service import CalendarSettings
    return CalendarSettings(
        booking_enabled=True, hold_minutes=5, minimum_notice_minutes=60,
        max_offered_slots=3, max_booking_days=30,
        require_staff_confirmation=True, timezone_name=timezone_name,
    )


# ---------------------------------------------------------------------------
# Pure prevalidation (zero DB statements on refusal - db=None proves it)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw_start", ["9am", "25:00", "0900", "", "12:60"])
def test_pure_start_time_malformed_refused(raw_start):
    from app.services import portal_schedule_service as pss
    result = pss.create_one_off_slot(
        None, uuid.uuid4(), _pure_settings(), date(2026, 8, 28),
        raw_start, 30)
    assert result.ok is False
    assert result.reason == pss.PUBLISH_INVALID
    assert result.detail == "start_time must be HH:MM (00:00-23:59)."


@pytest.mark.parametrize("bad_duration", ["30", True, None, 30.0])
def test_pure_duration_non_integer_refused(bad_duration):
    from app.services import portal_schedule_service as pss
    result = pss.create_one_off_slot(
        None, uuid.uuid4(), _pure_settings(), date(2026, 8, 28),
        "10:00", bad_duration)
    assert result.ok is False
    assert result.reason == pss.PUBLISH_INVALID
    assert result.detail == "duration_minutes must be an integer."


@pytest.mark.parametrize("out_of_range", [5, 9, 245, 1000])
def test_pure_duration_out_of_range_refused(out_of_range):
    from app.services import portal_schedule_service as pss
    result = pss.create_one_off_slot(
        None, uuid.uuid4(), _pure_settings(), date(2026, 8, 28),
        "10:00", out_of_range)
    assert result.ok is False
    assert result.reason == pss.PUBLISH_INVALID
    assert result.detail == (
        f"duration_minutes must be between {pss.SLOT_MINUTES_MIN} "
        f"and {pss.SLOT_MINUTES_MAX}.")


@pytest.mark.parametrize("off_step", [12, 33, 61])
def test_pure_duration_off_step_refused(off_step):
    from app.services import portal_schedule_service as pss
    result = pss.create_one_off_slot(
        None, uuid.uuid4(), _pure_settings(), date(2026, 8, 28),
        "10:00", off_step)
    assert result.ok is False
    assert result.reason == pss.PUBLISH_INVALID
    assert result.detail == (
        f"duration_minutes must be divisible by {pss.SLOT_MINUTES_STEP}.")


@pytest.mark.parametrize("start_time,duration", [
    ("23:30", 60),   # crosses local midnight
    ("23:30", 30),   # ends EXACTLY at midnight - unrepresentable close
    ("23:55", 10),   # crosses by five minutes
])
def test_pure_same_day_rule_refused(start_time, duration):
    from app.services import portal_schedule_service as pss
    result = pss.create_one_off_slot(
        None, uuid.uuid4(), _pure_settings(), date(2026, 8, 28),
        start_time, duration)
    assert result.ok is False
    assert result.reason == pss.PUBLISH_INVALID
    assert result.detail == pss.ONE_OFF_SAME_DAY_DETAIL


@pytest.mark.parametrize("day,start_time", [
    (date(2026, 8, 21), "08:00"),   # earlier the same office day
    (date(2026, 8, 21), "09:00"),   # start == now - the mandated boundary
    (date(2026, 8, 20), "10:00"),   # the previous office day entirely
])
def test_pure_strictly_future_rule_refused(monkeypatch, day, start_time):
    """The strictly-future rule fires BEFORE any DB statement (db=None) and
    reads its clock ONLY from the server-authoritative module seam."""
    from app.services import portal_schedule_service as pss
    _freeze_office_now(monkeypatch, 2026, 8, 21, 9, 0)
    result = pss.create_one_off_slot(
        None, uuid.uuid4(), _pure_settings(), day, start_time, 30)
    assert result.ok is False
    assert result.reason == pss.PUBLISH_INVALID
    assert result.detail == pss.ONE_OFF_PAST_DETAIL


@pytest.mark.parametrize("day,start_time,fragment", [
    (date(2027, 3, 14), "02:30", "does not exist"),   # spring-forward gap
    (date(2027, 11, 7), "01:30", "occurs twice"),     # fall-back repeat
])
def test_pure_dst_start_refusals_keep_publish_wording(monkeypatch, day,
                                                      start_time, fragment):
    """A nonexistent/ambiguous START is refused by the ONE SS3 owner via the
    publish delegation - the frozen wording, not a new vocabulary. Pure:
    publish's expansion refuses before any DB statement (db=None)."""
    from app.services import portal_schedule_service as pss
    _freeze_office_now(monkeypatch, 2026, 8, 21, 9, 0)
    result = pss.create_one_off_slot(
        None, uuid.uuid4(), _pure_settings(), day, start_time, 30)
    assert result.ok is False
    assert result.reason == pss.PUBLISH_INVALID
    assert fragment in result.detail


# ---------------------------------------------------------------------------
# HTTP contract: authentication, strict transport, shape, leak pins
# ---------------------------------------------------------------------------

@requires_db
def test_one_off_unauthenticated_fails_closed(portal_http):
    r = portal_http.post(
        "/portal/schedule/slots/one-off",
        json={"day": "2026-08-28", "start_time": "10:00",
              "duration_minutes": 30})
    assert r.status_code == 401
    r = portal_http.post(
        "/portal/schedule/slots/one-off",
        json={"day": "2026-08-28", "start_time": "10:00",
              "duration_minutes": 30},
        headers=_auth(_token(uuid.uuid4(), secret="wrong-secret-" + "x" * 24)))
    assert r.status_code == 401
    assert r.json()["detail"] == INVALID_DETAIL


@requires_db
@pytest.mark.parametrize("smuggled", [
    {"client_id": str(uuid.uuid4())},          # tenant selector
    {"status": "available"},                   # status authority
    {"now": "2020-01-01T00:00:00Z"},           # browser-supplied clock
    {"start_datetime": "2026-08-28T14:00:00Z"},  # raw datetime authority
])
def test_one_off_strict_body_rejects_smuggled_fields(portal_http, db,
                                                     client_row, monkeypatch,
                                                     smuggled):
    """The strict transport model rejects EVERY undeclared key with 422 -
    a tenant, a status, a raw datetime, or a client-supplied 'now' can never
    be silently ignored (constraints 1 and 3)."""
    user = _bind_office_user(db, client_row)
    _freeze_office_now(monkeypatch, 2026, 8, 21, 9, 0)
    r = _one_off(portal_http, _token(user.auth_user_id),
                 "2026-08-28", "10:00", 30, extra=smuggled)
    assert r.status_code == 422
    # Zero inserts happened.
    from app.repositories import appointment_repository
    from app.services.calendar_settings_service import local_day_utc_window
    day_start, day_end = local_day_utc_window(date(2026, 8, 28), NY)
    assert appointment_repository.list_slots_between(
        db, client_row.id, day_start, day_end) == []


@requires_db
def test_one_off_creates_open_slot_shape_and_leak_pin(portal_http, db,
                                                      client_row,
                                                      monkeypatch):
    """The success body is EXACTLY the approved SlotView; the committed row
    is available, generic (no provider/service), tenant-owned, and sits on
    the requested office-local instants."""
    from app.calendar_models import AppointmentSlot, SlotStatus
    from app.services.calendar_settings_service import ensure_utc

    user = _bind_office_user(db, client_row)
    _freeze_office_now(monkeypatch, 2026, 8, 21, 9, 0)
    r = _one_off(portal_http, _token(user.auth_user_id),
                 "2026-08-28", "10:00", 30)
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == APPROVED_SLOT_FIELDS
    assert body["status"] == "available"
    assert body["provider_name"] is None
    assert body["service_key"] is None
    raw = r.text
    for marker in FORBIDDEN_BODY_MARKERS:
        assert marker not in raw, f"leak: {marker}"

    row = db.get(AppointmentSlot, uuid.UUID(body["slot_id"]))
    assert row is not None
    assert row.client_id == client_row.id
    assert row.status == SlotStatus.AVAILABLE
    assert ensure_utc(row.start_datetime) == _ny_instant(2026, 8, 28, 10, 0)
    assert ensure_utc(row.end_datetime) == _ny_instant(2026, 8, 28, 10, 30)
    assert row.held_until is None
    assert row.held_by_conversation_id is None


@requires_db
@pytest.mark.parametrize("day,start_time", [
    ("2026-08-21", "08:00"),   # earlier the same office day
    ("2026-08-21", "09:00"),   # start == now - the mandated boundary
    ("2026-08-20", "10:00"),   # yesterday
])
def test_one_off_past_rejected_over_http(portal_http, db, client_row,
                                         monkeypatch, day, start_time):
    from app.services import portal_schedule_service as pss
    user = _bind_office_user(db, client_row)
    _freeze_office_now(monkeypatch, 2026, 8, 21, 9, 0)
    r = _one_off(portal_http, _token(user.auth_user_id), day, start_time, 30)
    assert r.status_code == 422
    assert r.json()["detail"] == pss.ONE_OFF_PAST_DETAIL


@requires_db
def test_one_off_strictly_future_same_day_accepted(portal_http, db,
                                                   client_row, monkeypatch):
    """Minutes into the future on the SAME office day is legal - the rule is
    strictly-future, not a notice window (staff open time for callers)."""
    user = _bind_office_user(db, client_row)
    _freeze_office_now(monkeypatch, 2026, 8, 21, 9, 0)
    r = _one_off(portal_http, _token(user.auth_user_id),
                 "2026-08-21", "09:05", 30)
    assert r.status_code == 200
    assert r.json()["status"] == "available"


@requires_db
@pytest.mark.parametrize("day,start_time,fragment", [
    ("2027-03-14", "02:30", "does not exist"),
    ("2027-11-07", "01:30", "occurs twice"),
])
def test_one_off_dst_refusals_over_http(portal_http, db, client_row,
                                        monkeypatch, day, start_time,
                                        fragment):
    user = _bind_office_user(db, client_row)
    _freeze_office_now(monkeypatch, 2026, 8, 21, 9, 0)
    r = _one_off(portal_http, _token(user.auth_user_id), day, start_time, 30)
    assert r.status_code == 422
    assert fragment in r.json()["detail"]


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------

@requires_db
def test_one_off_tenant_isolation_for_creation_and_reads(portal_http, db,
                                                         client_row, office_b,
                                                         monkeypatch):
    """Office A's one-off is invisible to office B; office B may open the
    SAME wall time without any collision (the overlap universe and the
    advisory lock are both tenant-scoped); each row belongs to its verified
    tenant only."""
    from app.calendar_models import AppointmentSlot

    user_a = _bind_office_user(db, client_row)
    user_b = _bind_office_user(db, office_b)
    _freeze_office_now(monkeypatch, 2026, 8, 21, 9, 0)

    r_a = _one_off(portal_http, _token(user_a.auth_user_id),
                   "2026-08-28", "10:00", 30)
    assert r_a.status_code == 200
    r_b_read = portal_http.get(
        "/portal/schedule",
        params={"start_day": "2026-08-28", "end_day": "2026-08-28"},
        headers=_auth(_token(user_b.auth_user_id)))
    assert r_b_read.status_code == 200
    assert r_b_read.json()["slots"] == []          # A's slot is invisible to B

    r_b = _one_off(portal_http, _token(user_b.auth_user_id),
                   "2026-08-28", "10:00", 30)
    assert r_b.status_code == 200                  # no cross-tenant collision

    row_a = db.get(AppointmentSlot, uuid.UUID(r_a.json()["slot_id"]))
    row_b = db.get(AppointmentSlot, uuid.UUID(r_b.json()["slot_id"]))
    assert row_a.client_id == client_row.id
    assert row_b.client_id == office_b.id

    r_a_read = portal_http.get(
        "/portal/schedule",
        params={"start_day": "2026-08-28", "end_day": "2026-08-28"},
        headers=_auth(_token(user_a.auth_user_id)))
    slots_a = r_a_read.json()["slots"]
    assert [s["slot_id"] for s in slots_a] == [r_a.json()["slot_id"]]


@requires_db
def test_one_off_cross_tenant_booking_stays_indistinguishable(portal_http,
                                                              db, client_row,
                                                              office_b,
                                                              monkeypatch):
    """Office B cannot book office A's one-off slot: the frozen staff-booking
    tenant-filtered locked read cannot see the foreign row (404 with the
    portal wording), and the row is untouched."""
    from app.calendar_models import AppointmentSlot, SlotStatus

    user_a = _bind_office_user(db, client_row)
    user_b = _bind_office_user(db, office_b)
    _freeze_office_now(monkeypatch, 2026, 8, 21, 9, 0)
    r_a = _one_off(portal_http, _token(user_a.auth_user_id),
                   "2026-08-28", "11:00", 30)
    assert r_a.status_code == 200
    slot_id = r_a.json()["slot_id"]

    r = portal_http.post(
        f"/portal/schedule/slots/{slot_id}/book",
        json={"patient_name": "Foreign Book", "patient_phone": "516-555-0000"},
        headers=_auth(_token(user_b.auth_user_id)))
    assert r.status_code == 404
    assert r.json()["detail"] == "Slot not found."
    db.rollback()
    row = db.get(AppointmentSlot, uuid.UUID(slot_id))
    assert row.status == SlotStatus.AVAILABLE


# ---------------------------------------------------------------------------
# Duplicate / conflicting inventory (the frozen overlap rule + lock)
# ---------------------------------------------------------------------------

@requires_db
def test_one_off_duplicate_and_overlap_409_zero_inserts(portal_http, db,
                                                        client_row,
                                                        monkeypatch):
    from app.services import portal_schedule_service as pss
    from app.repositories import appointment_repository
    from app.services.calendar_settings_service import local_day_utc_window

    user = _bind_office_user(db, client_row)
    _freeze_office_now(monkeypatch, 2026, 8, 21, 9, 0)
    token = _token(user.auth_user_id)

    assert _one_off(portal_http, token, "2026-08-28", "10:00",
                    30).status_code == 200
    # Exact duplicate.
    r = _one_off(portal_http, token, "2026-08-28", "10:00", 30)
    assert r.status_code == 409
    assert r.json()["detail"] == pss.OVERLAP_DETAIL
    # Partial overlap.
    r = _one_off(portal_http, token, "2026-08-28", "10:15", 30)
    assert r.status_code == 409
    # Zero inserts from both refusals.
    day_start, day_end = local_day_utc_window(date(2026, 8, 28), NY)
    rows = appointment_repository.list_slots_between(
        db, client_row.id, day_start, day_end)
    assert len(rows) == 1
    # Adjacent half-open window is LEGAL (end == next start).
    assert _one_off(portal_http, token, "2026-08-28", "10:30",
                    30).status_code == 200


@requires_db
def test_one_off_cancelled_history_never_blocks(portal_http, db, client_row,
                                                monkeypatch):
    """A CANCELLED row at the same time is audit history, not capacity (the
    frozen find_first_overlap rule) - the one-off succeeds, preserving the
    4C Open-slot + cancelled-history coexistence."""
    user = _bind_office_user(db, client_row)
    _freeze_office_now(monkeypatch, 2026, 8, 21, 9, 0)
    _make_slot(db, client_row, start_utc=_ny_instant(2026, 8, 28, 10),
               status="cancelled")
    r = _one_off(portal_http, _token(user.auth_user_id),
                 "2026-08-28", "10:00", 30)
    assert r.status_code == 200


@requires_db
@pytest.mark.parametrize("existing_status", ["blocked", "booked", "held"])
def test_one_off_every_live_status_blocks(portal_http, db, client_row,
                                          monkeypatch, existing_status):
    from app.services import portal_schedule_service as pss
    user = _bind_office_user(db, client_row)
    _freeze_office_now(monkeypatch, 2026, 8, 21, 9, 0)
    _make_slot(db, client_row, start_utc=_ny_instant(2026, 8, 28, 10),
               status=existing_status)
    r = _one_off(portal_http, _token(user.auth_user_id),
                 "2026-08-28", "10:00", 30)
    assert r.status_code == 409
    assert r.json()["detail"] == pss.OVERLAP_DETAIL


# ---------------------------------------------------------------------------
# The frozen publish surface is behavior-unchanged
# ---------------------------------------------------------------------------

@requires_db
def test_publish_endpoint_unchanged_no_past_rule_leaked(portal_http, db,
                                                        client_row,
                                                        monkeypatch):
    """/publish keeps its frozen P4-A semantics: publishing a day whose early
    windows have already passed is still accepted there (the strictly-future
    rule is a 4D-A ONE-OFF rule and must not silently change the Schedule
    page's contract)."""
    user = _bind_office_user(db, client_row)
    _freeze_office_now(monkeypatch, 2026, 8, 21, 9, 0)
    r = portal_http.post(
        "/portal/schedule/days/2026-08-21/publish",
        json={"open_time": "08:00", "close_time": "12:00",
              "slot_minutes": 60},
        headers=_auth(_token(user.auth_user_id)))
    assert r.status_code == 200
    assert len(r.json()) == 4     # 08-09 (already past) through 11-12


# ---------------------------------------------------------------------------
# THE EMPTY-WEEK ACCEPTANCE CASE (end to end over HTTP)
# ---------------------------------------------------------------------------

@requires_db
def test_empty_week_create_open_book_and_reload(portal_http, db, client_row,
                                                monkeypatch):
    """The 4D-A contract's exact scenario: an empty future week becomes
    bookable entirely through authoritative inventory - create a one-off,
    see it arrive through the normal /portal/schedule read, book it through
    the EXISTING staff-booking route by real server slot_id, watch the slot
    be consumed, and prove a reload reflects backend truth."""
    from app.calendar_models import SlotStatus

    user = _bind_office_user(db, client_row)
    _freeze_office_now(monkeypatch, 2026, 8, 21, 9, 0)
    token = _token(user.auth_user_id)
    week = {"start_day": "2026-08-24", "end_day": "2026-08-30"}

    # 1. The future week is EMPTY.
    r = portal_http.get("/portal/schedule", params=week, headers=_auth(token))
    assert r.status_code == 200
    assert r.json()["slots"] == []

    # 2. Staff creates a one-off future availability from Calendar.
    r = _one_off(portal_http, token, "2026-08-26", "14:00", 30)
    assert r.status_code == 200
    created_slot_id = r.json()["slot_id"]

    # 3. The Open slot arrives through the NORMAL authoritative data source.
    r = portal_http.get("/portal/schedule", params=week, headers=_auth(token))
    slots = r.json()["slots"]
    assert [s["slot_id"] for s in slots] == [created_slot_id]
    assert slots[0]["status"] == "available"

    # 4. Staff books that REAL server slot_id through the EXISTING flow.
    r = portal_http.post(
        f"/portal/schedule/slots/{created_slot_id}/book",
        json={"patient_name": "Walk-in Caller",
              "patient_phone": "516-555-7777",
              "reason": "one-off opening"},
        headers=_auth(token))
    assert r.status_code == 200
    appointment = r.json()
    assert appointment["status"] == "confirmed"
    assert appointment["patient_name"] == "Walk-in Caller"
    assert appointment["start_datetime"] == slots[0]["start_datetime"]
    appointment_id = appointment["appointment_id"]

    # 5. The slot is CONSUMED - no longer Open - and the appointment shows.
    db.rollback()
    r = portal_http.get("/portal/schedule", params=week, headers=_auth(token))
    slots_after = r.json()["slots"]
    assert [s["slot_id"] for s in slots_after] == [created_slot_id]
    assert slots_after[0]["status"] == SlotStatus.BOOKED
    r = portal_http.get("/portal/appointments", params=week,
                        headers=_auth(token))
    assert r.status_code == 200
    ids = [a["appointment_id"] for a in r.json()["appointments"]]
    assert appointment_id in ids

    # 6. Reload preserves the result from backend state (a second identical
    #    authoritative read - nothing was frontend-synthesized).
    r = portal_http.get("/portal/schedule", params=week, headers=_auth(token))
    assert r.json()["slots"][0]["status"] == SlotStatus.BOOKED


# ---------------------------------------------------------------------------
# Concurrency bites (PostgreSQL-only - the documented dialect seam)
# ---------------------------------------------------------------------------

@requires_db
@requires_postgres
def test_concurrent_one_off_same_time_exactly_one_wins(db, client_row,
                                                       monkeypatch):
    """Two competing one-off creations, same tenant/day/time, empty day at
    start: the advisory day lock serializes them - exactly one row exists,
    the loser receives the frozen overlap refusal, zero extra inserts."""
    from app.services import portal_schedule_service as pss
    from app.repositories import appointment_repository
    from app.services.calendar_settings_service import local_day_utc_window

    _freeze_office_now(monkeypatch, 2026, 8, 21, 9, 0)
    settings = _settings_for(client_row)
    client_id = client_row.id
    day = date(2026, 9, 15)

    def create():
        def call():
            session = _fresh_session()
            try:
                return pss.create_one_off_slot(
                    session, client_id, settings, day, "10:00", 30)
            finally:
                session.close()
        return call

    result_a, result_b = _run_pair(create(), create())
    outcomes = sorted([result_a.reason, result_b.reason])
    assert outcomes == [pss.PUBLISH_OK, pss.PUBLISH_OVERLAP], outcomes

    day_start, day_end = local_day_utc_window(day, NY)
    rows = appointment_repository.list_slots_between(
        db, client_id, day_start, day_end)
    assert len(rows) == 1


@requires_db
@requires_postgres
def test_concurrent_one_off_different_tenants_both_succeed(db, client_row,
                                                           office_b,
                                                           monkeypatch):
    """The advisory lock is TENANT-scoped: two offices opening the same wall
    time simultaneously never collide and each ends with its own row."""
    from app.services import portal_schedule_service as pss
    from app.repositories import appointment_repository
    from app.services.calendar_settings_service import local_day_utc_window

    _freeze_office_now(monkeypatch, 2026, 8, 21, 9, 0)
    day = date(2026, 9, 16)
    settings_a = _settings_for(client_row)
    settings_b = _settings_for(office_b)

    def create(client_id, settings):
        def call():
            session = _fresh_session()
            try:
                return pss.create_one_off_slot(
                    session, client_id, settings, day, "10:00", 30)
            finally:
                session.close()
        return call

    result_a, result_b = _run_pair(create(client_row.id, settings_a),
                                   create(office_b.id, settings_b))
    assert result_a.reason == pss.PUBLISH_OK
    assert result_b.reason == pss.PUBLISH_OK

    day_start, day_end = local_day_utc_window(day, NY)
    for owner in (client_row.id, office_b.id):
        rows = appointment_repository.list_slots_between(
            db, owner, day_start, day_end)
        assert len(rows) == 1
        assert rows[0].client_id == owner


@requires_db
@requires_postgres
def test_one_off_created_slot_cannot_be_double_booked(db, client_row,
                                                      monkeypatch):
    """The preserved race protection ON a 4D-A-created slot: two competing
    staff bookings behind one barrier, independent sessions - exactly one
    books, the other is refused by the frozen row-lock arbiter, exactly one
    committed appointment exists."""
    from app.services import portal_schedule_service as pss
    from app.services import booking_service
    from app.calendar_models import Appointment

    _freeze_office_now(monkeypatch, 2026, 8, 21, 9, 0)
    settings = _settings_for(client_row)
    client_id = client_row.id
    creation = pss.create_one_off_slot(
        db, client_id, settings, date(2026, 9, 17), "10:00", 30)
    assert creation.ok
    slot_id = creation.slots[0].id
    now_utc = datetime(2026, 8, 21, 13, 0, tzinfo=UTC)

    def book(name):
        def call():
            session = _fresh_session()
            try:
                return booking_service.finalize_staff_booking(
                    session, client_id, slot_id,
                    now_utc=now_utc,
                    patient_name=name, patient_phone="516-555-1111",
                    patient_email=None, new_or_returning=None,
                    reason=None, urgency="routine")
            finally:
                session.close()
        return call

    result_a, result_b = _run_pair(book("Racer A"), book("Racer B"))
    reasons = sorted([result_a.reason, result_b.reason])
    assert reasons == ["ok", "slot_taken"], reasons

    db.rollback()
    rows = (db.query(Appointment)
            .filter(Appointment.client_id == client_id)
            .filter(Appointment.slot_id == slot_id)
            .all())
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# v1.0.1 (audit F1) - the under-lock strictly-future boundary
# ---------------------------------------------------------------------------
# The pre-lock check is a FAST rejection only: a request can WAIT on the
# tenant/day advisory lock and cross its requested start while waiting. The
# corrected pathway re-judges strictly-future through publish_day_slots'
# under_lock_check seam - after the lock, before any overlap read or INSERT.
# These tests prove (1) the seam itself refuses with zero inserts, (2) the
# audit's exact lock-wait choreography refuses when the start is stale by
# lock-acquisition time, and (3) the complementary still-future wait
# succeeds. (2) and (3) are PostgreSQL-only (real advisory locks, real
# threads, independent sessions) and are made DETERMINISTIC by a mutable
# server-clock seam plus pg_locks polling for the ungranted advisory wait.


class _MutableClock:
    """A controllable server clock for the lock-wait choreography: the
    css.client_now seam reads .value at every call, so the test moves
    authoritative time between phase-1 (pre-lock) and phase-2 (under-lock)
    checks without any sleeping-based nondeterminism."""

    def __init__(self, initial):
        self.value = initial

    def now(self, settings):
        return self.value


@requires_db
def test_under_lock_seam_refusal_inserts_nothing(db, client_row, monkeypatch):
    """The seam contract, proven directly on publish_day_slots: a caller-
    supplied under-lock refusal aborts the WHOLE transaction - the exact
    PublishResult comes back, zero rows exist, and the lock is released
    (the same session can immediately run a normal publish)."""
    from app.services import portal_schedule_service as pss
    from app.repositories import appointment_repository
    from app.services.calendar_settings_service import local_day_utc_window

    _freeze_office_now(monkeypatch, 2026, 8, 21, 9, 0)
    settings = _settings_for(client_row)
    day = date(2026, 9, 21)
    refusal = pss.PublishResult(False, pss.PUBLISH_INVALID,
                                detail=pss.ONE_OFF_PAST_DETAIL)

    result = pss.publish_day_slots(
        db, client_row.id, settings, day, "10:00", "11:00", 30,
        under_lock_check=lambda: refusal)
    assert result is refusal
    day_start, day_end = local_day_utc_window(day, NY)
    assert appointment_repository.list_slots_between(
        db, client_row.id, day_start, day_end) == []

    # The rollback released the advisory lock: the same day publishes
    # normally afterwards (and a None-returning check changes nothing).
    result = pss.publish_day_slots(
        db, client_row.id, settings, day, "10:00", "11:00", 30,
        under_lock_check=lambda: None)
    assert result.ok is True
    assert len(result.slots) == 2


def _run_lock_wait_choreography(db, client_row, monkeypatch, *,
                                advance_to_minute):
    """The audit F1 choreography, shared by the refusal and success cases.

    1. Clock frozen at 09:00 office-local; requested start 09:10 (future).
    2. Thread A opens an independent session and takes the EXACT tenant/day
       advisory lock through the service's own acquire_schedule_day_lock.
    3. Thread B calls create_one_off_slot: phase-1 passes at 09:00, then B
       blocks waiting on A's lock (observed via pg_locks: an ungranted
       advisory wait - never a sleep-based guess).
    4. The clock seam is advanced to `advance_to_minute` past 09:00.
    5. A releases (rollback). 6. B acquires the lock and re-judges under it.
    Returns B's PublishResult."""
    from app.services import portal_schedule_service as pss
    from app.services import calendar_settings_service as css
    from sqlalchemy import text

    clock = _MutableClock(datetime(2026, 8, 21, 9, 0, tzinfo=ZoneInfo(NY)))
    monkeypatch.setattr(css, "client_now", clock.now)
    settings = _settings_for(client_row)
    client_id = client_row.id
    day = date(2026, 8, 21)

    a_locked = threading.Event()
    release_a = threading.Event()
    b_result = {}
    errors = []

    def thread_a():
        session = _fresh_session()
        try:
            pss.acquire_schedule_day_lock(session, client_id, day)
            a_locked.set()
            assert release_a.wait(timeout=30), "release signal never came"
            session.rollback()   # releases the advisory lock
        except Exception as exc:  # noqa: BLE001 - surfaced below
            errors.append(("a", exc))
            a_locked.set()
        finally:
            session.close()

    def thread_b():
        session = _fresh_session()
        try:
            b_result["value"] = pss.create_one_off_slot(
                session, client_id, settings, day, "09:10", 30)
        except Exception as exc:  # noqa: BLE001 - surfaced below
            errors.append(("b", exc))
        finally:
            session.close()

    t_a = threading.Thread(target=thread_a)
    t_a.start()
    assert a_locked.wait(timeout=30), "A never acquired the lock"
    assert not errors, errors

    t_b = threading.Thread(target=thread_b)
    t_b.start()

    # DETERMINISTIC wait detection: B's phase-1 already passed (clock still
    # 09:00) and B is now parked INSIDE the locked transaction waiting for
    # the advisory lock - visible as an ungranted advisory entry in
    # pg_locks. Poll with a hard timeout; no timing guesses.
    watcher = _fresh_session()
    try:
        deadline = time_module.time() + 20
        while True:
            waiting = watcher.execute(text(
                "SELECT count(*) FROM pg_locks "
                "WHERE locktype = 'advisory' AND NOT granted")).scalar()
            watcher.rollback()
            if waiting and waiting >= 1:
                break
            assert time_module.time() < deadline, \
                "B never appeared as an advisory-lock waiter"
            time_module.sleep(0.05)
    finally:
        watcher.close()

    # 4. Move authoritative time while B is provably still WAITING.
    clock.value = datetime(2026, 8, 21, 9, advance_to_minute,
                           tzinfo=ZoneInfo(NY))
    # 5. Release A; 6. B proceeds under the lock.
    release_a.set()
    t_a.join(timeout=30)
    t_b.join(timeout=30)
    assert not t_a.is_alive() and not t_b.is_alive(), "deadlocked threads"
    assert not errors, errors
    return b_result["value"]


@requires_db
@requires_postgres
def test_one_off_lock_wait_past_boundary_refused(db, client_row, monkeypatch):
    """AUDIT F1 REGRESSION: start 09:10 is strictly future at phase 1
    (09:00), B waits on the lock, and by lock-acquisition time the clock
    reads EXACTLY 09:10 (start == fresh now - the mandated boundary). The
    under-lock re-judgment must refuse with the one-off past detail and
    insert NOTHING."""
    from app.services import portal_schedule_service as pss
    from app.repositories import appointment_repository
    from app.services.calendar_settings_service import local_day_utc_window

    result = _run_lock_wait_choreography(db, client_row, monkeypatch,
                                         advance_to_minute=10)
    assert result.ok is False
    assert result.reason == pss.PUBLISH_INVALID
    assert result.detail == pss.ONE_OFF_PAST_DETAIL

    day_start, day_end = local_day_utc_window(date(2026, 8, 21), NY)
    assert appointment_repository.list_slots_between(
        db, client_row.id, day_start, day_end) == []


@requires_db
@requires_postgres
def test_one_off_lock_wait_still_future_succeeds(db, client_row, monkeypatch):
    """The complementary case: B waits on the same lock, the clock advances
    only to 09:05 (start 09:10 still strictly future), so after A releases,
    the under-lock re-judgment passes and creation succeeds - exactly one
    available row on the requested instants."""
    from app.services.calendar_settings_service import (ensure_utc,
                                                        local_day_utc_window)
    from app.repositories import appointment_repository

    result = _run_lock_wait_choreography(db, client_row, monkeypatch,
                                         advance_to_minute=5)
    assert result.ok is True
    assert len(result.slots) == 1

    day_start, day_end = local_day_utc_window(date(2026, 8, 21), NY)
    rows = appointment_repository.list_slots_between(
        db, client_row.id, day_start, day_end)
    assert len(rows) == 1
    assert ensure_utc(rows[0].start_datetime) == _ny_instant(2026, 8, 21, 9, 10)
