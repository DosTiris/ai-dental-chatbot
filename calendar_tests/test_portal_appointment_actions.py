# calendar_tests/test_portal_appointment_actions.py
#
# P5-A - Portal Appointment Actions v1 (authenticated Office Portal Confirm /
# Cancel): proves the transport, fail-closed, and lifecycle contract of the
# NEW app/routes/portal_appointment_actions.py router, which reuses the frozen
# booking_service lifecycle owner unchanged.
#
# TWO GROUPS (deliberately split so most bites run without a database):
#
#   GROUP A - route wiring / mapping / fail-closed (NO database). The REAL
#     action router runs over HTTP; the portal identity is overridden and the
#     frozen booking_service is monkeypatched to return each BookingResult,
#     so these bites prove ONLY the route's own responsibilities: the two
#     POST routes exist and require auth; every BookingResult.reason maps to
#     the exact status + wording; the guardrails fail closed (G1: only the
#     enumerated success reasons reach a 200, an unexpected reason is a
#     generic 500 that never echoes the raw reason; G2: a success lacking an
#     appointment is a generic 500, never a malformed body); a 200 body is
#     EXACTLY the 13 approved fields with none of the forbidden markers; and
#     the projection + lifecycle owners are shared, not copied (Rule 3).
#
#   GROUP B - real lifecycle over Postgres (requires_db, owner-local PG17).
#     The REAL booking_service runs against real rows: Confirm and Cancel
#     transitions, the freed-slot side effect, the drifted-slot pin (C7),
#     availability reflecting a released slot, two-office tenant isolation,
#     unknown-vs-foreign 404 opacity, the idempotent-repeat refusals, and the
#     Confirm-vs-Cancel serialized concurrency outcomes (C1). Without
#     TEST_DATABASE_URL every Group B test SKIPS visibly (never a silent pass).
#
# BITE PROOF: every Group A route bite and every Group B behavior bite FAILS
# against untouched fd967de - the module app.routes.portal_appointment_actions
# and the routes /portal/appointments/{id}/confirm|cancel do not exist there
# (import/collection error / 404). The extraction-equivalence bites
# (shared projection owner, shared lifecycle owner) intentionally PIN existing
# ownership; the shared PUBLIC projection owner build_portal_appointment_view
# also does not exist on the parent, so those bite too.
#
# Tokens are minted locally with PyJWT (HS256 test secret) - no network, no
# real Supabase project, no provider is ever contacted. No notification is
# sent on any path (booking_service sends none; P5-A adds none).
#
# Run (owner-local, PowerShell 5.1):
#   $env:ALLOW_DESTRUCTIVE_CALENDAR_TESTS="yes"
#   $env:TEST_DATABASE_URL="postgresql://postgres:...@127.0.0.1:55437/mia_p3b2_test"
#   python -m pytest calendar_tests\test_portal_appointment_actions.py -v

import os
import sys
import time
import uuid
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from calendar_tests.conftest import requires_db  # noqa: E402  (env bootstrap)

# app.config needs DATABASE_URL at import; the Group A tests never connect.
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
NOT_FOUND_DETAIL = "Appointment not found."
UNEXPECTED_DETAIL = "Unable to update appointment."

# The COMPLETE approved action-response field set (leak-prevention pin) - the
# SAME 13 fields the read GET returns. Drift here means the contract diverged.
# SLICE 4B1 - DELIBERATE pin amendment (same mechanism, same rationale as
# APPROVED_APPOINTMENT_FIELDS in test_portal_appointments.py): internal_note
# joins the approved portal action surface.
APPROVED_ACTION_FIELDS = {
    "appointment_id", "patient_name", "patient_phone", "patient_email",
    "new_or_returning", "reason", "urgency", "start_datetime",
    "end_datetime", "status", "confirmed_at", "source",
    "notification_outcome", "internal_note",
}
# Markers that must NEVER appear in an action response.
FORBIDDEN_BODY_MARKERS = [
    "client_id", "slot_id", "conversation_id", "notify_error",
    "office_sms_sent", "office_email_sent", "patient_sms_sent",
    "api_key", "client_key", "settings",
    "notification_email", "notification_phone", "created_at", "updated_at",
]


def _token(sub, *, secret=TEST_SECRET, aud=AUDIENCE, exp_delta=300,
           email="office@example.test"):
    """Mint a Supabase-shaped access token (test_portal_appointments.py pattern)."""
    claims = {
        "sub": str(sub),
        "aud": aud,
        "exp": int(time.time()) + exp_delta,
        "email": email,
        "role": "authenticated",
        "iss": TEST_ISSUER,
    }
    return pyjwt.encode(claims, secret, algorithm="HS256")


# ===========================================================================
# GROUP A - route wiring / mapping / fail-closed (NO database)
# ===========================================================================

from app.services.booking_service import BookingResult  # noqa: E402
from app.routes import portal as portal_routes  # noqa: E402
from app.routes import portal_appointments as portal_appt_routes  # noqa: E402
from app.routes import portal_appointment_actions as action_routes  # noqa: E402
from app.services import booking_service  # noqa: E402


class _FakeClient:
    def __init__(self):
        self.id = uuid.uuid4()


class _FakeIdentity:
    def __init__(self):
        self.client = _FakeClient()


def _fake_appt(status="confirmed", *, confirmed_at=None):
    """A minimal object exposing exactly the attributes the shared projection
    owner reads. No ORM, no database - Group A tests the ROUTE, not the model."""
    class _A:
        pass
    a = _A()
    a.id = uuid.uuid4()
    a.patient_name = "Kevin Alvarado"
    a.patient_phone = "516-555-1234"
    a.patient_email = None
    a.new_or_returning = "new"
    a.reason = "cleaning"
    a.urgency = "routine"
    a.start_datetime = datetime(2026, 7, 16, 14, 0, tzinfo=UTC)
    a.end_datetime = datetime(2026, 7, 16, 14, 45, tzinfo=UTC)
    a.status = status
    a.confirmed_at = confirmed_at
    a.source = "mia_widget"
    a.office_sms_sent = False
    a.office_email_sent = False
    a.notify_error = None
    a.internal_note = None   # 4B1: the projection now reads this column
    return a


@pytest.fixture()
def action_app(monkeypatch):
    """The REAL portal + portal_appointments + action routers in one app.
    get_db is overridden with a harmless dummy (Group A never uses the session
    because booking_service is monkeypatched); the portal identity is
    overridden per test to bypass the real auth ONLY where the test is about
    mapping, not about auth. The portal_auth secret is configured so a
    malformed token is rejected at signature decode (401) without a DB."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.services import portal_auth
    monkeypatch.setenv(portal_auth.ENV_JWT_SECRET, TEST_SECRET)
    monkeypatch.delenv(portal_auth.ENV_JWKS_URL, raising=False)
    monkeypatch.setenv(portal_auth.ENV_ISSUER, TEST_ISSUER)
    portal_auth._jwks_clients.clear()

    app = FastAPI()
    app.include_router(portal_routes.router)
    app.include_router(portal_appt_routes.router)
    app.include_router(action_routes.router)
    app.dependency_overrides[portal_routes.get_db] = lambda: iter([object()])
    with TestClient(app) as client:
        yield app, client


def _bypass_identity(app):
    app.dependency_overrides[portal_routes.require_portal_identity] = \
        lambda: _FakeIdentity()


def _post(client, path, token=None):
    headers = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return client.post(path, headers=headers)


# --- routes exist + require authentication -------------------------------

def test_action_routes_are_registered(action_app):
    app, _ = action_app
    paths = {(tuple(sorted(r.methods)), r.path)
             for r in app.routes if getattr(r, "methods", None)}
    assert (("POST",), "/portal/appointments/{appointment_id}/confirm") in paths
    assert (("POST",), "/portal/appointments/{appointment_id}/cancel") in paths


def test_confirm_missing_token_is_401(action_app):
    _, client = action_app
    res = _post(client, f"/portal/appointments/{uuid.uuid4()}/confirm")
    assert res.status_code == 401
    assert res.json()["detail"] == INVALID_DETAIL


def test_cancel_missing_token_is_401(action_app):
    _, client = action_app
    res = _post(client, f"/portal/appointments/{uuid.uuid4()}/cancel")
    assert res.status_code == 401
    assert res.json()["detail"] == INVALID_DETAIL


def test_confirm_malformed_token_is_401(action_app):
    _, client = action_app
    res = _post(client, f"/portal/appointments/{uuid.uuid4()}/confirm", "not-a-jwt")
    assert res.status_code == 401


# --- confirm mapping ------------------------------------------------------

@pytest.mark.parametrize("reason,confirmed", [
    ("ok", "2026-07-16T14:01:00Z"),
    ("already_confirmed", "2026-07-16T13:00:00Z"),
])
def test_confirm_success_is_200_exact_view(action_app, monkeypatch, reason, confirmed):
    app, client = action_app
    _bypass_identity(app)
    appt = _fake_appt(status="confirmed",
                      confirmed_at=datetime(2026, 7, 16, 14, 1, tzinfo=UTC))
    monkeypatch.setattr(booking_service, "confirm_appointment",
                        lambda db, cid, aid, *, now_utc: BookingResult(
                            True, reason, appointment=appt))
    res = _post(client, f"/portal/appointments/{uuid.uuid4()}/confirm", "tok")
    assert res.status_code == 200
    body = res.json()
    assert set(body.keys()) == APPROVED_ACTION_FIELDS
    assert body["status"] == "confirmed"
    raw = res.text
    for marker in FORBIDDEN_BODY_MARKERS:
        assert marker not in raw, f"forbidden marker leaked: {marker}"


def test_confirm_missing_is_404_opaque(action_app, monkeypatch):
    app, client = action_app
    _bypass_identity(app)
    monkeypatch.setattr(booking_service, "confirm_appointment",
                        lambda db, cid, aid, *, now_utc: BookingResult(
                            False, "appointment_missing"))
    res = _post(client, f"/portal/appointments/{uuid.uuid4()}/confirm", "tok")
    assert res.status_code == 404
    assert res.json()["detail"] == NOT_FOUND_DETAIL


def test_confirm_not_confirmable_is_409_with_status(action_app, monkeypatch):
    app, client = action_app
    _bypass_identity(app)
    monkeypatch.setattr(booking_service, "confirm_appointment",
                        lambda db, cid, aid, *, now_utc: BookingResult(
                            False, "not_confirmable", detail="cancelled"))
    res = _post(client, f"/portal/appointments/{uuid.uuid4()}/confirm", "tok")
    assert res.status_code == 409
    assert res.json()["detail"] == "Appointment is cancelled and cannot be confirmed."


# --- cancel mapping -------------------------------------------------------

def test_cancel_success_is_200_exact_view(action_app, monkeypatch):
    app, client = action_app
    _bypass_identity(app)
    appt = _fake_appt(status="cancelled")
    monkeypatch.setattr(booking_service, "cancel_appointment",
                        lambda db, cid, aid: BookingResult(True, "ok", appointment=appt))
    res = _post(client, f"/portal/appointments/{uuid.uuid4()}/cancel", "tok")
    assert res.status_code == 200
    body = res.json()
    assert set(body.keys()) == APPROVED_ACTION_FIELDS
    assert body["status"] == "cancelled"
    for marker in FORBIDDEN_BODY_MARKERS:
        assert marker not in res.text


def test_cancel_slot_missing_is_404_opaque(action_app, monkeypatch):
    app, client = action_app
    _bypass_identity(app)
    monkeypatch.setattr(booking_service, "cancel_appointment",
                        lambda db, cid, aid: BookingResult(False, "slot_missing"))
    res = _post(client, f"/portal/appointments/{uuid.uuid4()}/cancel", "tok")
    assert res.status_code == 404
    assert res.json()["detail"] == NOT_FOUND_DETAIL


def test_cancel_already_cancelled_is_409_frozen(action_app, monkeypatch):
    app, client = action_app
    _bypass_identity(app)
    appt = _fake_appt(status="cancelled")
    monkeypatch.setattr(booking_service, "cancel_appointment",
                        lambda db, cid, aid: BookingResult(
                            False, "already_cancelled", appointment=appt))
    res = _post(client, f"/portal/appointments/{uuid.uuid4()}/cancel", "tok")
    assert res.status_code == 409
    assert res.json()["detail"] == "Appointment is already cancelled."


def test_cancel_not_cancellable_is_409_with_status(action_app, monkeypatch):
    app, client = action_app
    _bypass_identity(app)
    monkeypatch.setattr(booking_service, "cancel_appointment",
                        lambda db, cid, aid: BookingResult(
                            False, "not_cancellable", detail="completed"))
    res = _post(client, f"/portal/appointments/{uuid.uuid4()}/cancel", "tok")
    assert res.status_code == 409
    assert res.json()["detail"] == "Appointment is completed and cannot be cancelled."


# --- G1: fail closed on an unexpected reason (never a silent 200) ----------

@pytest.mark.parametrize("success,reason", [
    (True, "weird_unexpected_success"),    # success, unknown reason
    (False, "totally_unexpected_failure"), # failure, unknown reason
    (True, "already_cancelled"),           # a real reason, but NOT a confirm success
])
def test_confirm_unexpected_reason_fails_closed_500(action_app, monkeypatch, success, reason):
    app, client = action_app
    _bypass_identity(app)
    # give an odd success an appointment so ONLY the reason is unexpected (G1)
    result = BookingResult(success, reason,
                           appointment=(_fake_appt() if success else None))
    monkeypatch.setattr(booking_service, "confirm_appointment",
                        lambda db, cid, aid, *, now_utc: result)
    res = _post(client, f"/portal/appointments/{uuid.uuid4()}/confirm", "tok")
    assert res.status_code == 500
    assert res.json()["detail"] == UNEXPECTED_DETAIL
    # G1: the raw unexpected reason must never reach the client.
    assert result.reason not in res.text


@pytest.mark.parametrize("success,reason", [
    (True, "already_confirmed"),   # a real reason, but NOT a cancel success
    (True, "weird"),
    (False, "surprise"),
])
def test_cancel_unexpected_reason_fails_closed_500(action_app, monkeypatch, success, reason):
    app, client = action_app
    _bypass_identity(app)
    result = BookingResult(success, reason,
                           appointment=(_fake_appt() if success else None))
    monkeypatch.setattr(booking_service, "cancel_appointment",
                        lambda db, cid, aid: result)
    res = _post(client, f"/portal/appointments/{uuid.uuid4()}/cancel", "tok")
    assert res.status_code == 500
    assert res.json()["detail"] == UNEXPECTED_DETAIL
    assert result.reason not in res.text


# --- G2: a success MUST carry an appointment (never a malformed body) ------

def test_confirm_success_without_appointment_fails_closed_500(action_app, monkeypatch):
    app, client = action_app
    _bypass_identity(app)
    monkeypatch.setattr(booking_service, "confirm_appointment",
                        lambda db, cid, aid, *, now_utc: BookingResult(
                            True, "ok", appointment=None))
    res = _post(client, f"/portal/appointments/{uuid.uuid4()}/confirm", "tok")
    assert res.status_code == 500
    assert res.json()["detail"] == UNEXPECTED_DETAIL


def test_cancel_success_without_appointment_fails_closed_500(action_app, monkeypatch):
    app, client = action_app
    _bypass_identity(app)
    monkeypatch.setattr(booking_service, "cancel_appointment",
                        lambda db, cid, aid: BookingResult(True, "ok", appointment=None))
    res = _post(client, f"/portal/appointments/{uuid.uuid4()}/cancel", "tok")
    assert res.status_code == 500
    assert res.json()["detail"] == UNEXPECTED_DETAIL


# --- extraction-equivalence: one projection owner, one lifecycle owner -----

def test_action_and_read_share_one_projection_owner():
    """Rule 3 + C2: the action router builds its response through the EXACT
    SAME public projection owner the read GET uses - not a copy. (Also a bite:
    build_portal_appointment_view does not exist on the parent.)"""
    assert action_routes.build_portal_appointment_view is \
        portal_appt_routes.build_portal_appointment_view


def test_portal_and_admin_share_one_lifecycle_owner():
    """Rule 2/3: the portal action router delegates to the SAME
    booking_service module the internal admin Calendar route uses - one
    lifecycle owner, no second appointment state machine."""
    from app.routes import calendar as admin_calendar
    assert action_routes.booking_service is booking_service
    assert admin_calendar.booking_service is booking_service


def test_confirm_injects_utc_now(action_app, monkeypatch):
    """The confirm route injects an aware UTC now (the admin-route pattern),
    so the lifecycle owner never has to read the clock itself."""
    captured = {}
    def spy(db, cid, aid, *, now_utc):
        captured["now"] = now_utc
        return BookingResult(True, "ok", appointment=_fake_appt())
    app, client = action_app
    _bypass_identity(app)
    monkeypatch.setattr(booking_service, "confirm_appointment", spy)
    _post(client, f"/portal/appointments/{uuid.uuid4()}/confirm", "tok")
    assert captured["now"].tzinfo is not None
    assert captured["now"].utcoffset() == timedelta(0)


# ===========================================================================
# GROUP B - real lifecycle over Postgres (requires_db, owner-local PG17)
# ===========================================================================

@pytest.fixture(scope="module")
def office_users_table(engine):
    """Run the REAL migration 007 (sole creation authority for office_users)."""
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
    row = OfficeUser(auth_user_id=uuid.uuid4(), client_id=client.id, active=active)
    db.add(row)
    db.commit()
    return row


def _seed_appointment(db, client, *, status="pending", slot_status="booked",
                      start_utc=None, end_utc=None):
    """Seed one slot + appointment directly so the state under test is exact.
    Returns (appointment, slot)."""
    from app.calendar_models import AppointmentSlot, Appointment
    if start_utc is None:
        start_utc = datetime(2026, 7, 16, 14, 0, tzinfo=UTC)
    if end_utc is None:
        end_utc = start_utc + timedelta(minutes=45)
    slot = AppointmentSlot(
        client_id=client.id, start_datetime=start_utc, end_datetime=end_utc,
        status=slot_status,
    )
    db.add(slot)
    db.flush()
    appointment = Appointment(
        client_id=client.id, slot_id=slot.id, conversation_id=None,
        patient_name="Kevin Alvarado", patient_phone="516-555-1234",
        patient_email=None, new_or_returning="new", reason="cleaning",
        urgency="routine", start_datetime=start_utc, end_datetime=end_utc,
        status=status, source="mia_widget",
        office_sms_sent=False, office_email_sent=False, notify_error=None,
    )
    db.add(appointment)
    db.commit()
    return appointment, slot


@pytest.fixture()
def portal_http(db, office_users_table, monkeypatch):
    """Real app: P2 identity + Portal Appointments (read) + Portal Appointment
    Actions routers, driven over HTTP with the REAL portal_auth running. One
    get_db override covers all three (they import the SAME callable)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.services import portal_auth
    monkeypatch.setenv(portal_auth.ENV_JWT_SECRET, TEST_SECRET)
    monkeypatch.delenv(portal_auth.ENV_JWKS_URL, raising=False)
    monkeypatch.setenv(portal_auth.ENV_ISSUER, TEST_ISSUER)
    portal_auth._jwks_clients.clear()
    app = FastAPI()
    app.include_router(portal_routes.router)
    app.include_router(portal_appt_routes.router)
    app.include_router(action_routes.router)
    app.dependency_overrides[portal_routes.get_db] = lambda: db
    with TestClient(app) as client:
        yield client


def _http_post(portal_http, path, token=None):
    headers = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return portal_http.post(path, headers=headers)


@requires_db
def test_db_confirm_pending_becomes_confirmed(portal_http, db, client_row):
    appt, _ = _seed_appointment(db, client_row, status="pending")
    user = _bind_office_user(db, client_row)
    res = _http_post(portal_http, f"/portal/appointments/{appt.id}/confirm",
                     _token(user.auth_user_id))
    assert res.status_code == 200
    body = res.json()
    assert set(body.keys()) == APPROVED_ACTION_FIELDS
    assert body["status"] == "confirmed"
    assert body["confirmed_at"] is not None
    db.expire_all()
    from app.calendar_models import Appointment
    refreshed = db.get(Appointment, appt.id)
    assert refreshed.status == "confirmed"


@requires_db
def test_db_confirm_is_idempotent_200(portal_http, db, client_row):
    appt, _ = _seed_appointment(db, client_row, status="pending")
    user = _bind_office_user(db, client_row)
    tok = _token(user.auth_user_id)
    first = _http_post(portal_http, f"/portal/appointments/{appt.id}/confirm", tok)
    second = _http_post(portal_http, f"/portal/appointments/{appt.id}/confirm", tok)
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["confirmed_at"] == second.json()["confirmed_at"]


@requires_db
@pytest.mark.parametrize("status", ["cancelled", "completed", "no_show"])
def test_db_confirm_invalid_transition_is_409(portal_http, db, client_row, status):
    appt, _ = _seed_appointment(db, client_row, status=status)
    user = _bind_office_user(db, client_row)
    res = _http_post(portal_http, f"/portal/appointments/{appt.id}/confirm",
                     _token(user.auth_user_id))
    assert res.status_code == 409


@requires_db
def test_db_cancel_frees_booked_slot(portal_http, db, client_row):
    appt, slot = _seed_appointment(db, client_row, status="pending",
                                   slot_status="booked")
    user = _bind_office_user(db, client_row)
    res = _http_post(portal_http, f"/portal/appointments/{appt.id}/cancel",
                     _token(user.auth_user_id))
    assert res.status_code == 200
    assert res.json()["status"] == "cancelled"
    db.expire_all()
    from app.calendar_models import AppointmentSlot
    refreshed = db.get(AppointmentSlot, slot.id)
    assert refreshed.status == "available"
    assert refreshed.held_until is None


@requires_db
def test_db_cancel_drifted_slot_is_pinned_untouched(portal_http, db, client_row):
    """C7: a linked slot that has DRIFTED (not booked) is left EXACTLY as-is;
    the appointment still cancels. No repair/coercion in P5-A."""
    appt, slot = _seed_appointment(db, client_row, status="pending",
                                   slot_status="blocked")
    user = _bind_office_user(db, client_row)
    res = _http_post(portal_http, f"/portal/appointments/{appt.id}/cancel",
                     _token(user.auth_user_id))
    assert res.status_code == 200
    assert res.json()["status"] == "cancelled"
    db.expire_all()
    from app.calendar_models import AppointmentSlot
    refreshed = db.get(AppointmentSlot, slot.id)
    assert refreshed.status == "blocked", "drifted slot must be left untouched"


@requires_db
def test_db_cancel_already_cancelled_is_409(portal_http, db, client_row):
    appt, _ = _seed_appointment(db, client_row, status="cancelled")
    user = _bind_office_user(db, client_row)
    res = _http_post(portal_http, f"/portal/appointments/{appt.id}/cancel",
                     _token(user.auth_user_id))
    assert res.status_code == 409
    assert res.json()["detail"] == "Appointment is already cancelled."


@requires_db
@pytest.mark.parametrize("status", ["completed", "no_show"])
def test_db_cancel_terminal_is_409(portal_http, db, client_row, status):
    appt, _ = _seed_appointment(db, client_row, status=status)
    user = _bind_office_user(db, client_row)
    res = _http_post(portal_http, f"/portal/appointments/{appt.id}/cancel",
                     _token(user.auth_user_id))
    assert res.status_code == 409


@requires_db
def test_db_released_slot_reappears_in_availability(portal_http, db, client_row):
    """F3: the EXACT slot linked to the cancelled appointment is bookable
    again immediately - Mia's availability reads fresh (no cache). The slot
    is placed at NY-local noon a few days out so it is unambiguously inside
    the booking horizon and its local-day window."""
    from app.services import availability_service
    from app.services.calendar_settings_service import load_calendar_settings
    ny_target = (datetime.now(ZoneInfo(NY)) + timedelta(days=3)).date()
    start = datetime(ny_target.year, ny_target.month, ny_target.day, 12, 0,
                     tzinfo=ZoneInfo(NY)).astimezone(UTC)
    appt, slot = _seed_appointment(db, client_row, status="pending",
                                   slot_status="booked",
                                   start_utc=start,
                                   end_utc=start + timedelta(minutes=45))
    released_slot_id = slot.id
    released_start = slot.start_datetime
    user = _bind_office_user(db, client_row)
    res = _http_post(portal_http, f"/portal/appointments/{appt.id}/cancel",
                     _token(user.auth_user_id))
    assert res.status_code == 200
    db.expire_all()
    settings = load_calendar_settings(client_row)
    slots = availability_service.get_available_slots(
        db, client_row.id, settings, ny_target, "any",
        datetime.now(UTC), None)
    # Prefer proving the EXACT released slot id reappears; fall back to the
    # exact normalized start instant only if id is not exposed (it is).
    ids = {getattr(s, "id", None) for s in slots}
    if any(i is not None for i in ids):
        assert released_slot_id in ids, (
            "the exact released slot must reappear in availability")
    else:
        from app.services.calendar_settings_service import ensure_utc
        starts = {ensure_utc(s.start_datetime) for s in slots}
        assert ensure_utc(released_start) in starts, (
            "the exact released slot start must reappear in availability")


@requires_db
def test_db_tenant_isolation_confirm(portal_http, db, client_row, office_b):
    """Office A cannot confirm Office B's appointment; it is 404-opaque and
    B's row is unchanged."""
    appt_b, _ = _seed_appointment(db, office_b, status="pending")
    user_a = _bind_office_user(db, client_row)
    res = _http_post(portal_http, f"/portal/appointments/{appt_b.id}/confirm",
                     _token(user_a.auth_user_id))
    assert res.status_code == 404
    assert res.json()["detail"] == NOT_FOUND_DETAIL
    db.expire_all()
    from app.calendar_models import Appointment
    assert db.get(Appointment, appt_b.id).status == "pending"


@requires_db
def test_db_tenant_isolation_cancel(portal_http, db, client_row, office_b):
    appt_b, _ = _seed_appointment(db, office_b, status="pending")
    user_a = _bind_office_user(db, client_row)
    res = _http_post(portal_http, f"/portal/appointments/{appt_b.id}/cancel",
                     _token(user_a.auth_user_id))
    assert res.status_code == 404
    db.expire_all()
    from app.calendar_models import Appointment
    assert db.get(Appointment, appt_b.id).status == "pending"


@requires_db
def test_db_unknown_and_foreign_are_indistinguishable_404(portal_http, db,
                                                          client_row, office_b):
    user_a = _bind_office_user(db, client_row)
    appt_b, _ = _seed_appointment(db, office_b, status="pending")
    unknown = _http_post(portal_http, f"/portal/appointments/{uuid.uuid4()}/confirm",
                         _token(user_a.auth_user_id))
    foreign = _http_post(portal_http, f"/portal/appointments/{appt_b.id}/confirm",
                         _token(user_a.auth_user_id))
    assert unknown.status_code == foreign.status_code == 404
    assert unknown.json() == foreign.json()


@requires_db
def test_db_confirm_vs_cancel_concurrency_is_serialized(
        office_users_table, db, client_row, monkeypatch):
    """C1 (F5): Confirm and Cancel racing on one pending appointment resolve
    to EXACTLY one of the two valid serialized outcomes, and the final state
    is cancelled in BOTH. Serialization is the appointment-row FOR UPDATE
    alone (no CAS/advisory lock).

    Each worker creates and OWNS its own SQLAlchemy Session INSIDE its own
    thread, wired through a ZERO-ARGUMENT generator dependency override
    (never `lambda s=session: s`, which FastAPI would treat as a request
    parameter and Pydantic would try to deepcopy - the v1.0.1 harness bug).
    The shared `db` fixture session is used ONLY for setup and is NEVER used
    inside a worker. The two worker Session identities are proven distinct,
    worker exceptions are captured explicitly (so a dead thread reports the
    real error, not an opaque KeyError), and the final state is read from a
    THIRD independent session. Every session is closed in finally."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.database import SessionLocal
    from app.services import portal_auth

    # Real auth for the worker apps (mirrors the portal_http fixture).
    monkeypatch.setenv(portal_auth.ENV_JWT_SECRET, TEST_SECRET)
    monkeypatch.delenv(portal_auth.ENV_JWKS_URL, raising=False)
    monkeypatch.setenv(portal_auth.ENV_ISSUER, TEST_ISSUER)
    portal_auth._jwks_clients.clear()

    # Seed via the shared session, then COMMIT so the independent worker
    # sessions can see the row (the shared session is used ONLY for setup).
    appt, _ = _seed_appointment(db, client_row, status="pending")
    user = _bind_office_user(db, client_row)
    tok = _token(user.auth_user_id)
    appointment_id = str(appt.id)   # captured BEFORE any worker starts

    results = {}
    session_ids = {}
    worker_errors = {}
    barrier = threading.Barrier(2)

    def do(action):
        # Each worker owns its OWN session, created INSIDE this thread.
        session = SessionLocal()
        try:
            app = FastAPI()
            app.include_router(portal_routes.router)
            app.include_router(action_routes.router)

            def override_get_db():
                # Zero-argument generator: FastAPI yields THIS worker's
                # session and never treats it as a request parameter.
                yield session

            app.dependency_overrides[portal_routes.get_db] = override_get_db

            with TestClient(app) as client:
                session_ids[action] = id(session)   # identity before the race
                barrier.wait()                      # both workers mutate together
                response = client.post(
                    f"/portal/appointments/{appointment_id}/{action}",
                    headers={"Authorization": f"Bearer {tok}"},
                )
                results[action] = response.status_code
        except BaseException as exc:   # a barrier deadlock or any worker crash
            worker_errors[action] = exc
            try:
                barrier.abort()        # never let the peer block forever
            except BaseException:
                pass
        finally:
            session.close()

    t1 = threading.Thread(target=do, args=("confirm",))
    t2 = threading.Thread(target=do, args=("cancel",))
    t1.start(); t2.start()
    t1.join(); t2.join()

    # A dead worker must surface the REAL exception, not an opaque KeyError.
    assert not worker_errors, f"worker(s) failed: {worker_errors!r}"
    assert "confirm" in results and "cancel" in results, (
        f"both workers must complete; got results={results!r} "
        f"errors={worker_errors!r}")
    # Prove the two workers used DISTINCT Session objects.
    assert session_ids.get("confirm") is not None
    assert session_ids.get("cancel") is not None
    assert session_ids["confirm"] != session_ids["cancel"], (
        "each worker must own an independent SQLAlchemy Session")

    pair = (results["confirm"], results["cancel"])
    assert pair in {(409, 200), (200, 200)}, f"unexpected serialized pair {pair}"

    # Final state from a THIRD independent authoritative session.
    check = SessionLocal()
    try:
        from app.calendar_models import Appointment
        import uuid as _uuid
        assert check.get(Appointment, _uuid.UUID(appointment_id)).status == "cancelled"
    finally:
        check.close()
