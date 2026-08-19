# calendar_tests/test_portal_close_day.py
#
# PHASE 3A SLICE 4D-B - Calendar-native Close Day / Reopen Day: the focused
# backend suite for the OPERATIONAL closed-day authority
# (settings.calendar.closed_days, owned by app/services/closure_authority.py),
# the atomic close_day / reopen_day owners, the single under-lock creation
# gate in publish_day_slots, and the closed_days Calendar read slice.
#
# WHAT IS PROVEN HERE (the 4D-B GO's required matrix):
#   * closure_authority purity: tolerant reads, bounded idempotent add,
#     remove, recurring-membership informational read;
#   * ATOMIC Close Day: durable closed_days entry + available/held -> blocked
#     through the ONE shared bulk transition, booked rows byte-untouched,
#     ONE commit - and a FORCED FAILURE after the closure write rolls back
#     the COMBINED mutation (no partially closed state);
#   * idempotent duplicate Close (heals stray open rows); two simultaneous
#     Closes; Close vs Reopen; Close vs 4D-A one-off (full race AND the
#     deterministic lock-wait boundary: a creator that began before the
#     closure but obtains the day lock after it refuses with ZERO inserts);
#   * Close vs normal publish; Close vs recurring Apply (the honest
#     operationally_closed_skipped outcome); Close vs staff booking in BOTH
#     orders (a racing booking is never cancelled);
#   * office-local date semantics: today closable, past refused, the
#     NY-vs-UTC boundary, a DST-transition date;
#   * reload persistence; Reopen removes ONLY the live restriction
#     (provably zero row changes), reports the recurring-configured
#     informational flag, and the post-Reopen same-window one-off yields the
#     EXISTING overlap conflict (no fabricated recovery);
#   * surgical sibling preservation in BOTH directions (close/reopen never
#     clobber calendar.recurring / booking_enabled / unrelated siblings; the
#     recurring CAS write never clobbers closed_days);
#   * the preserved P4-B contract: a recurring closure SAVED but NOT applied
#     is never live state (no badge, publish and one-off still allowed);
#   * tenant isolation and the window-limited closed_days read slice.
#
# Sandbox runs are corroborating only (Rule 19). Threaded bites are
# PostgreSQL-only (real advisory/row locks, independent sessions).

import json
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

APPROVED_CLOSE_FIELDS = {"day", "already_closed", "blocked_count",
                         "booked_remaining"}
APPROVED_REOPEN_FIELDS = {"day", "was_closed", "recurring_configured"}
FORBIDDEN_BODY_MARKERS = [
    "client_id", "held_until", "held_by_conversation_id", "patient_name",
    "patient_phone", "patient_email", "api_key", '"settings"',
    "notification_email", "notification_phone", "schedule_config_updated_at",
]

requires_postgres = pytest.mark.skipif(
    not TEST_DB_URL.startswith("postgresql"),
    reason="advisory/row-lock concurrency bites are PostgreSQL-only",
)


def _token(sub, *, secret=TEST_SECRET, aud=AUDIENCE, exp_delta=300,
           email="office@example.test"):
    claims = {
        "sub": str(sub), "aud": aud,
        "exp": int(time_module.time()) + exp_delta,
        "email": email, "role": "authenticated", "iss": TEST_ISSUER,
    }
    return pyjwt.encode(claims, secret, algorithm="HS256")


# ---------------------------------------------------------------------------
# Fixtures (house harness)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def office_users_table(engine):
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
               held_until=None, held_by=None):
    from datetime import timedelta
    from app.calendar_models import AppointmentSlot
    slot = AppointmentSlot(
        client_id=client.id, start_datetime=start_utc,
        end_datetime=end_utc or (start_utc + timedelta(minutes=30)),
        status=status, held_until=held_until,
        held_by_conversation_id=held_by,
    )
    db.add(slot)
    db.commit()
    return slot


def _ny_instant(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=ZoneInfo(NY)).astimezone(UTC)


@pytest.fixture()
def portal_http(db, office_users_table, monkeypatch):
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
    from app.services import calendar_settings_service as css
    fixed = datetime(year, month, day, hour, minute, tzinfo=ZoneInfo(NY))
    monkeypatch.setattr(css, "client_now", lambda settings: fixed)


def _settings_for(client):
    from app.services.calendar_settings_service import load_calendar_settings
    return load_calendar_settings(client)


def _close(portal_http, token, day):
    return portal_http.post(f"/portal/schedule/days/{day}/close",
                            headers=_auth(token))


def _reopen(portal_http, token, day):
    return portal_http.post(f"/portal/schedule/days/{day}/reopen",
                            headers=_auth(token))


def _closed_days_in_db(db, client_id):
    from app.services import closure_authority
    return closure_authority.read_closed_days(
        closure_authority.read_settings_fresh(db, client_id))


def _fresh_session():
    from app.database import SessionLocal
    return SessionLocal()


def _run_pair(fn_a, fn_b):
    barrier = threading.Barrier(2)
    results = {}
    errors = []

    def runner(name, fn):
        try:
            barrier.wait(timeout=10)
            results[name] = fn()
        except Exception as exc:  # noqa: BLE001
            errors.append((name, exc))

    t_a = threading.Thread(target=runner, args=("a", fn_a))
    t_b = threading.Thread(target=runner, args=("b", fn_b))
    t_a.start(); t_b.start()
    t_a.join(timeout=30); t_b.join(timeout=30)
    assert not t_a.is_alive() and not t_b.is_alive(), "deadlocked pair"
    if errors:
        raise errors[0][1]
    return results["a"], results["b"]


def _statuses_on(db, client_id, day):
    from app.repositories import appointment_repository
    from app.services.calendar_settings_service import local_day_utc_window
    start_utc, end_utc = local_day_utc_window(day, NY)
    rows = appointment_repository.list_slots_between(
        db, client_id, start_utc, end_utc)
    return sorted((str(r.id), str(r.status)) for r in rows)


# ---------------------------------------------------------------------------
# closure_authority purity (no DB)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("settings,expected", [
    (None, []),
    ("garbage", []),
    ({"calendar": None}, []),
    ({"calendar": {"closed_days": "2026-09-04"}}, []),
    ({"calendar": {"closed_days": ["2026-09-04", "junk", 5, "2026-02-30",
                                   "2026-09-04", "2026-01-02"]}},
     [date(2026, 1, 2), date(2026, 9, 4)]),
])
def test_pure_read_closed_days_tolerant(settings, expected):
    from app.services import closure_authority
    assert closure_authority.read_closed_days(settings) == expected


def test_pure_add_is_bounded_idempotent_and_sorted():
    from app.services import closure_authority
    new, already = closure_authority.add_closed_day(
        [date(2026, 9, 5)], date(2026, 9, 4))
    assert new == [date(2026, 9, 4), date(2026, 9, 5)]
    assert already is False
    same, already = closure_authority.add_closed_day(new, date(2026, 9, 4))
    assert same == new and already is True
    # Build a full list of MAX distinct days and prove the cap is loud.
    from datetime import timedelta
    full = [date(2026, 1, 1) + timedelta(days=i)
            for i in range(closure_authority.MAX_CLOSED_DAYS)]
    with pytest.raises(closure_authority.ClosedDaysCapError) as exc:
        closure_authority.add_closed_day(full, date(2027, 1, 1))
    assert closure_authority.CLOSED_DAYS_CAP_DETAIL in str(exc.value)
    # Re-adding a PRESENT day at the cap stays idempotent (no error).
    _, already = closure_authority.add_closed_day(full, full[0])
    assert already is True


def test_pure_remove_is_idempotent():
    from app.services import closure_authority
    new, was = closure_authority.remove_closed_day(
        [date(2026, 9, 4), date(2026, 9, 5)], date(2026, 9, 4))
    assert new == [date(2026, 9, 5)] and was is True
    same, was = closure_authority.remove_closed_day(new, date(2026, 9, 4))
    assert same == new and was is False


@pytest.mark.parametrize("closures,day,expected", [
    ([{"date": "2026-09-04"}], date(2026, 9, 4), True),
    ([{"date": "2026-09-04"}], date(2026, 9, 5), False),
    ([{"start": "2026-09-01", "end": "2026-09-07"}], date(2026, 9, 4), True),
    ([{"start": "2026-09-01", "end": "2026-09-07"}], date(2026, 9, 1), True),
    ([{"start": "2026-09-01", "end": "2026-09-07"}], date(2026, 9, 7), True),
    ([{"start": "2026-09-01", "end": "2026-09-07"}], date(2026, 9, 8), False),
    ([{"bogus": True}, "junk", {"date": "not-a-date"}], date(2026, 9, 4),
     False),
    (None, date(2026, 9, 4), False),
])
def test_pure_recurring_membership_informational(closures, day, expected):
    from app.services import closure_authority
    settings = {"calendar": {"recurring": {"slot_minutes": 30,
                                           "closures": closures}}}
    if closures is None:
        settings = {"calendar": {}}
    assert closure_authority.date_in_recurring_closures(settings,
                                                        day) is expected


# ---------------------------------------------------------------------------
# Atomic Close Day
# ---------------------------------------------------------------------------

@requires_db
def test_close_day_atomic_success_shape_and_transitions(portal_http, db,
                                                        client_row,
                                                        monkeypatch):
    """One POST: durable closed_days entry + available->blocked +
    held->blocked, booked byte-untouched, blocked/cancelled untouched -
    exact response shape, leak pins, and DB truth."""
    from app.calendar_models import AppointmentSlot, SlotStatus
    from app.services.calendar_settings_service import ensure_utc

    user = _bind_office_user(db, client_row)
    _freeze_office_now(monkeypatch, 2026, 8, 21, 9, 0)
    D = date(2026, 8, 28)
    avail = _make_slot(db, client_row, start_utc=_ny_instant(2026, 8, 28, 9))
    held = _make_slot(db, client_row, start_utc=_ny_instant(2026, 8, 28, 10),
                      status="held",
                      held_until=datetime(2026, 8, 28, 15, tzinfo=UTC),
                      held_by=None)
    booked = _make_slot(db, client_row,
                        start_utc=_ny_instant(2026, 8, 28, 11),
                        status="booked")
    already_blocked = _make_slot(db, client_row,
                                 start_utc=_ny_instant(2026, 8, 28, 12),
                                 status="blocked")
    cancelled = _make_slot(db, client_row,
                           start_utc=_ny_instant(2026, 8, 28, 13),
                           status="cancelled")
    booked_updated_before = booked.updated_at if hasattr(
        booked, "updated_at") else None

    r = _close(portal_http, _token(user.auth_user_id), D.isoformat())
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == APPROVED_CLOSE_FIELDS
    assert body["day"] == D.isoformat()
    assert body["already_closed"] is False
    assert body["blocked_count"] == 2          # available + held only
    assert len(body["booked_remaining"]) == 1  # the booked window, times only
    raw = r.text
    for marker in FORBIDDEN_BODY_MARKERS:
        assert marker not in raw, f"leak: {marker}"

    assert _closed_days_in_db(db, client_row.id) == [D]
    db.expire_all()
    assert db.get(AppointmentSlot, avail.id).status == SlotStatus.BLOCKED
    assert db.get(AppointmentSlot, held.id).status == SlotStatus.BLOCKED
    booked_after = db.get(AppointmentSlot, booked.id)
    assert booked_after.status == SlotStatus.BOOKED
    assert ensure_utc(booked_after.start_datetime) == _ny_instant(
        2026, 8, 28, 11)
    if booked_updated_before is not None:
        assert booked_after.updated_at == booked_updated_before
    assert db.get(AppointmentSlot,
                  already_blocked.id).status == SlotStatus.BLOCKED
    assert db.get(AppointmentSlot, cancelled.id).status == SlotStatus.CANCELLED


@requires_db
def test_close_day_forced_failure_rolls_back_combined_mutation(db, client_row,
                                                               monkeypatch):
    """A failure AFTER the closure write but before completion rolls back
    closed_days AND every slot mutation - no partially closed state."""
    from app.services import portal_schedule_service as pss

    _freeze_office_now(monkeypatch, 2026, 8, 21, 9, 0)
    D = date(2026, 8, 28)
    _make_slot(db, client_row, start_utc=_ny_instant(2026, 8, 28, 9))
    settings = _settings_for(client_row)
    before_statuses = _statuses_on(db, client_row.id, D)
    before_settings = json.dumps(client_row.settings, sort_keys=True)

    def boom(*args, **kwargs):
        raise RuntimeError("forced 4D-B write-phase failure")

    monkeypatch.setattr(pss, "_block_all_open_locked", boom)
    with pytest.raises(RuntimeError, match="forced 4D-B"):
        pss.close_day(db, client_row.id, settings, D)

    assert _closed_days_in_db(db, client_row.id) == []          # rolled back
    assert _statuses_on(db, client_row.id, D) == before_statuses
    db.expire_all()
    assert json.dumps(db.get(type(client_row), client_row.id).settings,
                      sort_keys=True) == before_settings


@requires_db
def test_close_day_idempotent_duplicate_heals(portal_http, db, client_row,
                                              monkeypatch):
    user = _bind_office_user(db, client_row)
    _freeze_office_now(monkeypatch, 2026, 8, 21, 9, 0)
    token = _token(user.auth_user_id)
    D = "2026-08-28"
    assert _close(portal_http, token, D).status_code == 200
    r = _close(portal_http, token, D)
    assert r.status_code == 200
    assert r.json()["already_closed"] is True
    assert r.json()["blocked_count"] == 0
    assert _closed_days_in_db(db, client_row.id) == [date(2026, 8, 28)]
    # A stray open row (e.g. left by a failed earlier attempt or legacy
    # tooling) is HEALED by the idempotent re-close.
    _make_slot(db, client_row, start_utc=_ny_instant(2026, 8, 28, 14))
    r = _close(portal_http, token, D)
    assert r.status_code == 200
    assert r.json()["already_closed"] is True
    assert r.json()["blocked_count"] == 1


@requires_db
@pytest.mark.parametrize("day,expected_status", [
    ("2026-08-20", 422),   # yesterday (office-local) - refused
    ("2026-08-21", 200),   # today - allowed (blocks what remains)
    ("2026-08-22", 200),   # tomorrow
])
def test_close_day_past_rule_office_local(portal_http, db, client_row,
                                          monkeypatch, day, expected_status):
    from app.services import portal_schedule_service as pss
    user = _bind_office_user(db, client_row)
    _freeze_office_now(monkeypatch, 2026, 8, 21, 9, 0)
    r = _close(portal_http, _token(user.auth_user_id), day)
    assert r.status_code == expected_status
    if expected_status == 422:
        assert r.json()["detail"] == pss.CLOSE_DAY_PAST_DETAIL
        assert _closed_days_in_db(db, client_row.id) == []


@requires_db
def test_close_day_ny_vs_utc_boundary(portal_http, db, client_row,
                                      monkeypatch):
    """23:30 office-local: UTC has already rolled to the next date. The rule
    is OFFICE-local: today (NY) closes; the NY-yesterday refuses - never the
    UTC calendar."""
    user = _bind_office_user(db, client_row)
    _freeze_office_now(monkeypatch, 2026, 8, 21, 23, 30)   # 03:30Z Aug 22
    token = _token(user.auth_user_id)
    assert _close(portal_http, token, "2026-08-21").status_code == 200
    assert _close(portal_http, token, "2026-08-20").status_code == 422


@requires_db
def test_close_day_dst_transition_date(portal_http, db, client_row,
                                       monkeypatch):
    """Closing the spring-forward date uses the established office-timezone
    window machinery: the 23-hour local day still blocks its inventory."""
    from app.calendar_models import AppointmentSlot, SlotStatus
    user = _bind_office_user(db, client_row)
    _freeze_office_now(monkeypatch, 2026, 8, 21, 9, 0)
    slot = _make_slot(db, client_row, start_utc=_ny_instant(2027, 3, 14, 9))
    r = _close(portal_http, _token(user.auth_user_id), "2027-03-14")
    assert r.status_code == 200
    assert r.json()["blocked_count"] == 1
    db.expire_all()
    assert db.get(AppointmentSlot, slot.id).status == SlotStatus.BLOCKED


@requires_db
def test_close_day_unauthenticated_fails_closed(portal_http):
    assert portal_http.post(
        "/portal/schedule/days/2026-08-28/close").status_code == 401
    assert portal_http.post(
        "/portal/schedule/days/2026-08-28/reopen").status_code == 401
    r = portal_http.post("/portal/schedule/days/not-a-date/close",
                         headers={"Authorization": "Bearer junk"})
    assert r.status_code in (401, 422)   # both fail closed


# ---------------------------------------------------------------------------
# The single under-lock creation gate
# ---------------------------------------------------------------------------

@requires_db
def test_closed_day_refuses_publish_and_one_off(portal_http, db, client_row,
                                                monkeypatch):
    from app.services import portal_schedule_service as pss
    from app.repositories import appointment_repository
    from app.services.calendar_settings_service import local_day_utc_window

    user = _bind_office_user(db, client_row)
    _freeze_office_now(monkeypatch, 2026, 8, 21, 9, 0)
    token = _token(user.auth_user_id)
    assert _close(portal_http, token, "2026-08-28").status_code == 200

    r = portal_http.post(
        "/portal/schedule/days/2026-08-28/publish",
        json={"open_time": "09:00", "close_time": "12:00",
              "slot_minutes": 60},
        headers=_auth(token))
    assert r.status_code == 409
    assert r.json()["detail"] == pss.CLOSED_DAY_DETAIL

    r = portal_http.post(
        "/portal/schedule/slots/one-off",
        json={"day": "2026-08-28", "start_time": "10:00",
              "duration_minutes": 30},
        headers=_auth(token))
    assert r.status_code == 409
    assert r.json()["detail"] == pss.CLOSED_DAY_DETAIL

    day_start, day_end = local_day_utc_window(date(2026, 8, 28), NY)
    assert appointment_repository.list_slots_between(
        db, client_row.id, day_start, day_end) == []       # zero inserts


@requires_db
@requires_postgres
def test_creator_waiting_on_lock_sees_committed_closure(db, client_row,
                                                        monkeypatch):
    """THE 4D-B lock-wait boundary: a 4D-A one-off begins BEFORE any closure
    exists, waits on the tenant/day advisory lock, a closure for that day is
    COMMITTED meanwhile, and on acquiring the lock the creator's FRESH
    closed-day judgment refuses with zero inserts."""
    from app.services import portal_schedule_service as pss
    from app.services import closure_authority
    from app.repositories import appointment_repository
    from app.services.calendar_settings_service import local_day_utc_window
    from sqlalchemy import text

    _freeze_office_now(monkeypatch, 2026, 8, 21, 9, 0)
    settings = _settings_for(client_row)
    client_id = client_row.id
    D = date(2026, 9, 18)

    a_locked = threading.Event()
    release_a = threading.Event()
    b_result = {}
    errors = []

    def thread_a():
        session = _fresh_session()
        try:
            pss.acquire_schedule_day_lock(session, client_id, D)
            a_locked.set()
            assert release_a.wait(timeout=30)
            session.rollback()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc); a_locked.set()
        finally:
            session.close()

    def thread_b():
        session = _fresh_session()
        try:
            b_result["value"] = pss.create_one_off_slot(
                session, client_id, settings, D, "10:00", 30)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            session.close()

    t_a = threading.Thread(target=thread_a); t_a.start()
    assert a_locked.wait(timeout=30) and not errors
    t_b = threading.Thread(target=thread_b); t_b.start()

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
            assert time_module.time() < deadline, "B never waited"
            time_module.sleep(0.05)
    finally:
        watcher.close()

    # While B provably WAITS: commit the closure (a completed Close Day's
    # durable half) in an independent session.
    committer = _fresh_session()
    try:
        fresh = closure_authority.read_settings_fresh(committer, client_id)
        closed, _ = closure_authority.add_closed_day(
            closure_authority.read_closed_days(fresh), D)
        closure_authority.write_closed_days_locked(committer, client_id,
                                                   closed)
        committer.commit()
    finally:
        committer.close()

    release_a.set()
    t_a.join(timeout=30); t_b.join(timeout=30)
    assert not errors, errors
    result = b_result["value"]
    assert result.ok is False
    assert result.reason == pss.PUBLISH_CLOSED_DAY
    assert result.detail == pss.CLOSED_DAY_DETAIL

    day_start, day_end = local_day_utc_window(D, NY)
    assert appointment_repository.list_slots_between(
        db, client_id, day_start, day_end) == []


@requires_db
@requires_postgres
def test_close_vs_one_off_full_race_is_coherent(db, client_row, monkeypatch):
    """Both orders are safe: either the one-off refused (closure first), or
    it landed and the close blocked it (creation first). Never an open slot
    on a closed day, never a lost closure."""
    from app.services import portal_schedule_service as pss
    from app.repositories import appointment_repository
    from app.services.calendar_settings_service import local_day_utc_window
    from app.calendar_models import SlotStatus

    _freeze_office_now(monkeypatch, 2026, 8, 21, 9, 0)
    settings = _settings_for(client_row)
    client_id = client_row.id
    D = date(2026, 9, 19)

    def close_call():
        session = _fresh_session()
        try:
            return pss.close_day(session, client_id, settings, D)
        finally:
            session.close()

    def one_off_call():
        session = _fresh_session()
        try:
            return pss.create_one_off_slot(
                session, client_id, settings, D, "10:00", 30)
        finally:
            session.close()

    close_result, one_off_result = _run_pair(close_call, one_off_call)
    assert close_result.ok is True
    assert _closed_days_in_db(db, client_id) == [D]

    day_start, day_end = local_day_utc_window(D, NY)
    rows = appointment_repository.list_slots_between(
        db, client_id, day_start, day_end)
    if one_off_result.ok:
        # Creation won the lock first; the close then blocked it.
        assert [r.status for r in rows] == [SlotStatus.BLOCKED]
        assert close_result.blocked_count == 1
    else:
        assert one_off_result.reason == pss.PUBLISH_CLOSED_DAY
        assert rows == []


@requires_db
@requires_postgres
def test_two_simultaneous_closes_exactly_one_first(db, client_row,
                                                   monkeypatch):
    from app.services import portal_schedule_service as pss
    _freeze_office_now(monkeypatch, 2026, 8, 21, 9, 0)
    settings = _settings_for(client_row)
    client_id = client_row.id
    D = date(2026, 9, 20)
    _make_slot(db, client_row, start_utc=_ny_instant(2026, 9, 20, 10))

    def close_call():
        session = _fresh_session()
        try:
            return pss.close_day(session, client_id, settings, D)
        finally:
            session.close()

    result_a, result_b = _run_pair(close_call, close_call)
    assert result_a.ok and result_b.ok
    assert sorted([result_a.already_closed, result_b.already_closed]) == [
        False, True]
    assert result_a.blocked_count + result_b.blocked_count == 1
    assert _closed_days_in_db(db, client_id) == [D]


@requires_db
@requires_postgres
def test_close_vs_reopen_race_is_serialized_and_coherent(db, client_row,
                                                         monkeypatch):
    from app.services import portal_schedule_service as pss
    _freeze_office_now(monkeypatch, 2026, 8, 21, 9, 0)
    settings = _settings_for(client_row)
    client_id = client_row.id
    D = date(2026, 9, 21)

    def close_call():
        session = _fresh_session()
        try:
            return pss.close_day(session, client_id, settings, D)
        finally:
            session.close()

    def reopen_call():
        session = _fresh_session()
        try:
            return pss.reopen_day(session, client_id, settings, D)
        finally:
            session.close()

    close_result, reopen_result = _run_pair(close_call, reopen_call)
    assert close_result.ok and reopen_result.ok
    final = _closed_days_in_db(db, client_id)
    if reopen_result.was_closed:
        assert final == []          # reopen ran second and removed it
    else:
        assert final == [D]         # reopen ran first against nothing


@requires_db
def test_close_vs_recurring_apply_operationally_closed_skipped(db, client_row,
                                                               monkeypatch):
    """A live closed_days date OUTRANKS recurring materialization: Apply
    reports the honest new outcome and creates ZERO inventory there, while
    other open days still publish."""
    from app.services import portal_schedule_service as pss
    from app.services import portal_recurring_schedule_service as prs
    from app.repositories import appointment_repository
    from app.services.calendar_settings_service import local_day_utc_window
    from app.portal_models import OfficeUser, OfficeUserRole
    from sqlalchemy import text
    import json as _json

    _freeze_office_now(monkeypatch, 2026, 8, 21, 9, 0)
    settings = _settings_for(client_row)
    client_id = client_row.id

    # Save a canonical recurring config (weekly: Friday open 09:00-11:00,
    # everything else closed; no recurring closures) - the REAL CAS write.
    weekly = {d: {"open": False} for d in
              ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]}
    weekly["fri"] = {"open": True, "start": "09:00", "end": "11:00"}
    token_value = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
    db.execute(text(prs._CAS_UPDATE_SQL.text), {
        "office_hours_json": _json.dumps(weekly),
        "recurring_json": _json.dumps({"slot_minutes": 60, "closures": []}),
        "new_token": token_value, "client_id": str(client_id),
        "expected_token": None})
    db.commit(); db.refresh(client_row)

    # Operationally close the FIRST Friday in the horizon.
    first_friday = date(2026, 8, 28)
    close_result = pss.close_day(db, client_id, settings, first_friday)
    assert close_result.ok

    identity = type("I", (), {})()
    identity.client = client_row
    identity.office_user = OfficeUser(auth_user_id=uuid.uuid4(),
                                      client_id=client_id,
                                      role=OfficeUserRole.OFFICE_ADMIN,
                                      active=True)
    token_wire = token_value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    result = prs.apply_recurring_config(db, identity, token_wire)

    day_row = [d for d in result["days"]
               if d["day"] == first_friday.isoformat()][0]
    assert day_row["outcome"] == "operationally_closed_skipped"
    assert result["totals"]["operationally_closed_days"] == 1
    assert result["totals"]["published_days"] >= 1     # other Fridays did

    day_start, day_end = local_day_utc_window(first_friday, NY)
    assert appointment_repository.list_slots_between(
        db, client_id, day_start, day_end) == []


@requires_db
def test_close_vs_staff_booking_both_orders(db, client_row, monkeypatch):
    """Order A: a booking that won first is preserved and REPORTED, never
    cancelled. Order B: after close, booking the blocked slot refuses."""
    from app.services import portal_schedule_service as pss
    from app.services import booking_service
    from app.calendar_models import Appointment, SlotStatus

    _freeze_office_now(monkeypatch, 2026, 8, 21, 9, 0)
    settings = _settings_for(client_row)
    client_id = client_row.id
    now_utc = datetime(2026, 8, 21, 13, 5, tzinfo=UTC)

    # Order A: book, then close.
    creation = pss.create_one_off_slot(db, client_id, settings,
                                       date(2026, 9, 22), "10:00", 30)
    assert creation.ok
    slot_a = creation.slots[0].id
    booked = booking_service.finalize_staff_booking(
        db, client_id, slot_a, now_utc=now_utc,
        patient_name="Race Winner", patient_phone="516-555-2222",
        patient_email=None, new_or_returning=None, reason=None,
        urgency="routine")
    assert booked.reason == "ok"
    close_result = pss.close_day(db, client_id, settings, date(2026, 9, 22))
    assert close_result.ok
    assert close_result.blocked_count == 0
    assert len(close_result.booked_remaining) == 1
    db.expire_all()
    appointment = (db.query(Appointment)
                   .filter(Appointment.client_id == client_id,
                           Appointment.slot_id == slot_a).one())
    assert str(appointment.status) == "confirmed"

    # Order B: close first, then try to book the (now blocked) slot.
    creation = pss.create_one_off_slot(db, client_id, settings,
                                       date(2026, 9, 23), "10:00", 30)
    assert creation.ok
    slot_b = creation.slots[0].id
    assert pss.close_day(db, client_id, settings, date(2026, 9, 23)).ok
    refused = booking_service.finalize_staff_booking(
        db, client_id, slot_b, now_utc=now_utc,
        patient_name="Too Late", patient_phone="516-555-3333",
        patient_email=None, new_or_returning=None, reason=None,
        urgency="routine")
    assert refused.reason == "slot_blocked"
    from app.calendar_models import AppointmentSlot
    db.rollback()
    assert db.get(AppointmentSlot, slot_b).status == SlotStatus.BLOCKED


# ---------------------------------------------------------------------------
# Reopen Day
# ---------------------------------------------------------------------------

@requires_db
def test_reopen_removes_only_the_live_restriction(portal_http, db, client_row,
                                                  monkeypatch):
    """Reopen: was_closed reporting, ZERO row changes (statuses byte-equal
    before/after), creation eligibility restored, idempotent second call."""
    user = _bind_office_user(db, client_row)
    _freeze_office_now(monkeypatch, 2026, 8, 21, 9, 0)
    token = _token(user.auth_user_id)
    D = "2026-08-28"
    _make_slot(db, client_row, start_utc=_ny_instant(2026, 8, 28, 9))
    assert _close(portal_http, token, D).status_code == 200
    statuses_before = _statuses_on(db, client_row.id, date(2026, 8, 28))

    r = _reopen(portal_http, token, D)
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == APPROVED_REOPEN_FIELDS
    assert body["was_closed"] is True
    assert body["recurring_configured"] is False
    assert _closed_days_in_db(db, client_row.id) == []
    assert _statuses_on(db, client_row.id,
                        date(2026, 8, 28)) == statuses_before   # zero unblocks

    # Creation eligibility is restored (a NON-conflicting window publishes).
    r = portal_http.post(
        "/portal/schedule/slots/one-off",
        json={"day": D, "start_time": "15:00", "duration_minutes": 30},
        headers=_auth(token))
    assert r.status_code == 200

    r = _reopen(portal_http, token, D)
    assert r.status_code == 200
    assert r.json()["was_closed"] is False       # idempotent


@requires_db
def test_post_reopen_same_window_gets_existing_overlap_conflict(portal_http,
                                                                db,
                                                                client_row,
                                                                monkeypatch):
    """The honest consequence: the closure-blocked row still OCCUPIES its
    window, so re-adding availability at the same time yields the EXISTING
    overlap 409 - no fabricated recovery, no silent unblock."""
    from app.services import portal_schedule_service as pss
    user = _bind_office_user(db, client_row)
    _freeze_office_now(monkeypatch, 2026, 8, 21, 9, 0)
    token = _token(user.auth_user_id)
    r = portal_http.post(
        "/portal/schedule/slots/one-off",
        json={"day": "2026-08-28", "start_time": "10:00",
              "duration_minutes": 30},
        headers=_auth(token))
    assert r.status_code == 200
    assert _close(portal_http, token, "2026-08-28").status_code == 200
    assert _reopen(portal_http, token, "2026-08-28").status_code == 200
    r = portal_http.post(
        "/portal/schedule/slots/one-off",
        json={"day": "2026-08-28", "start_time": "10:00",
              "duration_minutes": 30},
        headers=_auth(token))
    assert r.status_code == 409
    assert r.json()["detail"] == pss.OVERLAP_DETAIL


@requires_db
@pytest.mark.parametrize("closures", [
    [{"date": "2026-08-28"}],
    [{"start": "2026-08-24", "end": "2026-08-30"}],
])
def test_reopen_reports_recurring_configured_and_never_edits_it(portal_http,
                                                                db,
                                                                client_row,
                                                                monkeypatch,
                                                                closures):
    user = _bind_office_user(db, client_row)
    _freeze_office_now(monkeypatch, 2026, 8, 21, 9, 0)
    token = _token(user.auth_user_id)
    client_row.settings = {
        "timezone": NY,
        "calendar": {"booking_enabled": True,
                     "recurring": {"slot_minutes": 30, "closures": closures}},
    }
    db.commit()
    assert _close(portal_http, token, "2026-08-28").status_code == 200
    r = _reopen(portal_http, token, "2026-08-28")
    assert r.status_code == 200
    assert r.json()["was_closed"] is True
    assert r.json()["recurring_configured"] is True
    db.expire_all()
    fresh = db.get(type(client_row), client_row.id)
    assert fresh.settings["calendar"]["recurring"]["closures"] == closures
    assert fresh.settings["calendar"].get("closed_days") == []


# ---------------------------------------------------------------------------
# Sibling preservation + the preserved P4-B contract
# ---------------------------------------------------------------------------

@requires_db
def test_close_and_reopen_preserve_every_settings_sibling(portal_http, db,
                                                          client_row,
                                                          monkeypatch):
    from sqlalchemy import text
    user = _bind_office_user(db, client_row)
    _freeze_office_now(monkeypatch, 2026, 8, 21, 9, 0)
    token = _token(user.auth_user_id)
    client_row.settings = {
        "timezone": NY,
        "unrelated_top": {"keep": 1},
        "calendar": {"booking_enabled": True,
                     "hold_minutes": 7,
                     "recurring": {"slot_minutes": 30,
                                   "closures": [{"date": "2026-12-24"}]}},
    }
    db.commit()
    assert _close(portal_http, token, "2026-08-28").status_code == 200
    db.expire_all()
    s = db.get(type(client_row), client_row.id).settings
    assert s["unrelated_top"] == {"keep": 1}
    assert s["calendar"]["booking_enabled"] is True
    assert s["calendar"]["hold_minutes"] == 7
    assert s["calendar"]["recurring"] == {"slot_minutes": 30,
                                          "closures": [{"date": "2026-12-24"}]}
    assert s["calendar"]["closed_days"] == ["2026-08-28"]

    # And the OTHER direction: the recurring CAS write preserves closed_days.
    from app.services import portal_recurring_schedule_service as prs
    import json as _json
    db.execute(text(prs._CAS_UPDATE_SQL.text), {
        "office_hours_json": _json.dumps(
            {d: {"open": False} for d in
             ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]}),
        "recurring_json": _json.dumps({"slot_minutes": 45, "closures": []}),
        "new_token": datetime(2026, 8, 21, 13, 0, tzinfo=UTC),
        "client_id": str(client_row.id), "expected_token": None})
    db.commit(); db.expire_all()
    s = db.get(type(client_row), client_row.id).settings
    assert s["calendar"]["closed_days"] == ["2026-08-28"]
    assert s["calendar"]["recurring"] == {"slot_minutes": 45, "closures": []}

    assert _reopen(portal_http, token, "2026-08-28").status_code == 200
    db.expire_all()
    s = db.get(type(client_row), client_row.id).settings
    assert s["calendar"]["closed_days"] == []
    assert s["unrelated_top"] == {"keep": 1}


@requires_db
def test_saved_but_unapplied_recurring_closure_is_never_live(portal_http, db,
                                                             client_row,
                                                             monkeypatch):
    """THE PRESERVED P4-B CONTRACT (4D-B GO): saving a recurring closure has
    NO operational effect - no closed_days badge, publish allowed, one-off
    allowed. Apply remains the materialization boundary."""
    user = _bind_office_user(db, client_row)
    _freeze_office_now(monkeypatch, 2026, 8, 21, 9, 0)
    token = _token(user.auth_user_id)
    client_row.settings = {
        "timezone": NY,
        "calendar": {"booking_enabled": True,
                     "recurring": {"slot_minutes": 30,
                                   "closures": [{"date": "2026-08-28"}]}},
    }
    db.commit()

    r = portal_http.get("/portal/schedule",
                        params={"start_day": "2026-08-24",
                                "end_day": "2026-08-30"},
                        headers=_auth(token))
    assert r.status_code == 200
    assert r.json()["closed_days"] == []                 # NOT live state

    r = portal_http.post(
        "/portal/schedule/days/2026-08-28/publish",
        json={"open_time": "09:00", "close_time": "10:00",
              "slot_minutes": 30},
        headers=_auth(token))
    assert r.status_code == 200                          # publish allowed

    r = portal_http.post(
        "/portal/schedule/slots/one-off",
        json={"day": "2026-08-28", "start_time": "14:00",
              "duration_minutes": 30},
        headers=_auth(token))
    assert r.status_code == 200                          # one-off allowed


# ---------------------------------------------------------------------------
# Read model + isolation + persistence
# ---------------------------------------------------------------------------

@requires_db
def test_schedule_read_exposes_window_limited_closed_days(portal_http, db,
                                                          client_row,
                                                          monkeypatch):
    user = _bind_office_user(db, client_row)
    _freeze_office_now(monkeypatch, 2026, 8, 21, 9, 0)
    token = _token(user.auth_user_id)
    assert _close(portal_http, token, "2026-08-28").status_code == 200
    assert _close(portal_http, token, "2026-10-09").status_code == 200

    r = portal_http.get("/portal/schedule",
                        params={"start_day": "2026-08-24",
                                "end_day": "2026-08-30"},
                        headers=_auth(token))
    body = r.json()
    assert set(body.keys()) == {"timezone_name", "start_day", "end_day",
                                "slots", "closed_days"}
    assert body["closed_days"] == ["2026-08-28"]     # window-limited
    for marker in FORBIDDEN_BODY_MARKERS:
        assert marker not in r.text

    # Reload persistence: a second identical authoritative read agrees.
    r = portal_http.get("/portal/schedule",
                        params={"start_day": "2026-10-05",
                                "end_day": "2026-10-11"},
                        headers=_auth(token))
    assert r.json()["closed_days"] == ["2026-10-09"]


@requires_db
def test_close_day_tenant_isolation(portal_http, db, client_row, office_b,
                                    monkeypatch):
    from app.services import portal_schedule_service as pss
    user_a = _bind_office_user(db, client_row)
    user_b = _bind_office_user(db, office_b)
    _freeze_office_now(monkeypatch, 2026, 8, 21, 9, 0)
    assert _close(portal_http, _token(user_a.auth_user_id),
                  "2026-08-28").status_code == 200

    r = portal_http.get("/portal/schedule",
                        params={"start_day": "2026-08-24",
                                "end_day": "2026-08-30"},
                        headers=_auth(_token(user_b.auth_user_id)))
    assert r.json()["closed_days"] == []             # invisible to B

    r = portal_http.post(                            # B's day is NOT closed
        "/portal/schedule/days/2026-08-28/publish",
        json={"open_time": "09:00", "close_time": "10:00",
              "slot_minutes": 30},
        headers=_auth(_token(user_b.auth_user_id)))
    assert r.status_code == 200

    assert _close(portal_http, _token(user_b.auth_user_id),
                  "2026-08-28").status_code == 200   # independent closure
    assert _closed_days_in_db(db, client_row.id) == [date(2026, 8, 28)]
    assert _closed_days_in_db(db, office_b.id) == [date(2026, 8, 28)]
    assert _reopen(portal_http, _token(user_a.auth_user_id),
                   "2026-08-28").status_code == 200
    assert _closed_days_in_db(db, office_b.id) == [date(2026, 8, 28)]
