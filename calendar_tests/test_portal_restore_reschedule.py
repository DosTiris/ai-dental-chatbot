# calendar_tests/test_portal_restore_reschedule.py
#
# PHASE 3A Slice 4C - Cancelled Appointment Recovery + Rescheduling: proves
# the transport, fail-closed, and lifecycle contract of the TWO new routes
# appended to app/routes/portal_appointment_actions.py
#
#     POST /portal/appointments/{appointment_id}/restore
#     POST /portal/appointments/{appointment_id}/reschedule       {slot_id}
#     POST /portal/appointments/{appointment_id}/restore-to-slot  {slot_id}
#
# v1.0.1 MODE PIN (audit correction F1): "Change time" (reschedule,
# ACTIVE-ONLY) and "Choose another time" (restore-to-slot, CANCELLED-ONLY)
# are DIFFERENT server-owned commands. The legal starting status is
# enforced under the appointment row lock; a stale command whose row
# changed mode concurrently is REFUSED, never reinterpreted - a stale
# Change-time must never resurrect a cancellation, and a stale recovery
# must never become an ordinary move. Both routes delegate into ONE
# shared engine (_move_appointment_to_slot) so locking, slot judgement,
# unique-index arbitration, and transaction rules exist exactly once.
#
# and of the TWO new lifecycle-owner functions appended to
# app/services/booking_service.py (restore_appointment /
# reschedule_appointment). The frozen pre-4C bytes of both files are
# untouched; the pre-existing suites pin that separately.
#
# TWO GROUPS (the P5-A split, same rationale):
#
#   GROUP A - route wiring / mapping / fail-closed (NO database). The REAL
#     router runs over HTTP with the frozen booking_service monkeypatched,
#     so these bites prove ONLY the routes' own responsibilities: both
#     routes exist and require auth; the STRICT one-field reschedule body
#     rejects every smuggled server-owned key with 422 BEFORE any service
#     call; every BookingResult.reason maps to the exact status + wording
#     (including the staff-booking sentences imported VERBATIM, never
#     retyped); the guardrails fail closed (G1/G2); and a 200 body is
#     EXACTLY the approved field set with none of the forbidden markers.
#
#   GROUP B - real lifecycle over Postgres (requires_db, owner-local PG17).
#     The REAL service runs against real rows: restore success + field
#     preservation + the confirmed_at non-write invariant; every original-
#     slot refusal (booked / actively held / blocked / vanished / started)
#     with proof nothing mutated; expired-hold lazy reclamation (D4);
#     conversation_conflict through a REAL seeded Conversation row;
#     reschedule of an active appointment (old slot freed, target booked,
#     identity fields preserved); the cancelled->confirmed atomic combined
#     move (old slot untouched - cancellation already freed or reused it);
#     same_slot; the drifted-old-slot pin (C7); status gates; tenant
#     isolation with unknown/foreign 404 opacity; and the two serialized
#     concurrency races (restore-vs-restore on one appointment,
#     reschedule-vs-reschedule onto one target slot) using the P5-A
#     threaded pattern: each worker owns its own SessionLocal() created
#     INSIDE its thread behind a zero-argument generator override, and the
#     final state is read from an independent third session.
#
# BITE PROOF: every bite FAILS against the pre-4C parent - the routes,
# service functions, and wording constants do not exist there (404 /
# AttributeError / import error).
#
# No notification is sent on any path (booking_service sends none; Slice 4C
# adds none) - pinned explicitly in Group B.
#
# Run (owner-local, PowerShell 5.1):
#   $env:ALLOW_DESTRUCTIVE_CALENDAR_TESTS="yes"
#   $env:TEST_DATABASE_URL="postgresql://postgres:...@127.0.0.1:5433/mia_phase3a_test"
#   python -m pytest calendar_tests\test_portal_restore_reschedule.py -v

import os
import sys
import time
import uuid
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

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

# The COMPLETE approved action-response field set - the SAME pin the P5-A /
# 4B1 suites carry. Slice 4C adds NO field to the portal surface.
APPROVED_ACTION_FIELDS = {
    "appointment_id", "patient_name", "patient_phone", "patient_email",
    "new_or_returning", "reason", "urgency", "start_datetime",
    "end_datetime", "status", "confirmed_at", "source",
    "notification_outcome", "internal_note",
}
FORBIDDEN_BODY_MARKERS = [
    "client_id", "slot_id", "conversation_id", "notify_error",
    "office_sms_sent", "office_email_sent", "patient_sms_sent",
    "api_key", "client_key", "settings",
    "notification_email", "notification_phone", "created_at", "updated_at",
]

# Server-owned keys the STRICT reschedule body must reject with 422 before
# any code of ours runs (extra="forbid" at the Pydantic boundary). This is
# the F1 lesson generalized: the browser owns ONE value here - the chosen
# real slot id - and nothing else.
FORBIDDEN_RESCHEDULE_KEYS = [
    "urgency", "client_id", "status", "source", "start_datetime",
    "end_datetime", "provider_id", "service_id", "patient_name",
    "patient_phone", "confirmed_at", "conversation_id", "internal_note",
]


def _token(sub, *, secret=TEST_SECRET, aud=AUDIENCE, exp_delta=300,
           email="office@example.test"):
    """Mint a Supabase-shaped access token (the P2 suite pattern)."""
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
from app.routes import portal_staff_booking as staff_booking_routes  # noqa: E402
from app.services import booking_service  # noqa: E402


class _FakeClient:
    def __init__(self):
        self.id = uuid.uuid4()


class _FakeIdentity:
    def __init__(self):
        self.client = _FakeClient()


def _fake_appt(status="confirmed", *, confirmed_at=None):
    """Minimal object exposing exactly what the shared projection owner
    reads (the P5-A helper, unchanged)."""
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
    a.internal_note = None
    return a


@pytest.fixture()
def action_app(monkeypatch):
    """The REAL portal + read + action routers in one app (the P5-A fixture
    verbatim: dummy get_db, real portal_auth secret so a malformed token is
    rejected at signature decode without a DB)."""
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


def _post(client, path, token=None, json_body=None):
    headers = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if json_body is None:
        return client.post(path, headers=headers)
    return client.post(path, headers=headers, json=json_body)


def _slot_body(slot_id=None):
    return {"slot_id": str(slot_id or uuid.uuid4())}


# --- routes exist + require authentication -------------------------------

def test_4c_routes_are_registered(action_app):
    app, _ = action_app
    paths = {(tuple(sorted(r.methods)), r.path)
             for r in app.routes if getattr(r, "methods", None)}
    assert (("POST",), "/portal/appointments/{appointment_id}/restore") in paths
    assert (("POST",), "/portal/appointments/{appointment_id}/reschedule") in paths
    assert (("POST",),
            "/portal/appointments/{appointment_id}/restore-to-slot") in paths


def test_restore_missing_token_is_401(action_app):
    _, client = action_app
    res = _post(client, f"/portal/appointments/{uuid.uuid4()}/restore")
    assert res.status_code == 401
    assert res.json()["detail"] == INVALID_DETAIL


def test_reschedule_missing_token_is_401(action_app):
    _, client = action_app
    res = _post(client, f"/portal/appointments/{uuid.uuid4()}/reschedule",
                json_body=_slot_body())
    assert res.status_code == 401
    assert res.json()["detail"] == INVALID_DETAIL


def test_restore_to_slot_missing_token_is_401(action_app):
    _, client = action_app
    res = _post(client,
                f"/portal/appointments/{uuid.uuid4()}/restore-to-slot",
                json_body=_slot_body())
    assert res.status_code == 401
    assert res.json()["detail"] == INVALID_DETAIL


def test_restore_malformed_token_is_401(action_app):
    _, client = action_app
    res = _post(client, f"/portal/appointments/{uuid.uuid4()}/restore",
                "not-a-jwt")
    assert res.status_code == 401


# --- the STRICT reschedule body (F1 generalized) ---------------------------

@pytest.mark.parametrize("forbidden_key", FORBIDDEN_RESCHEDULE_KEYS)
def test_reschedule_rejects_every_smuggled_server_owned_key_422(
        action_app, monkeypatch, forbidden_key):
    """extra='forbid' refuses each smuggled key with 422 BEFORE the
    lifecycle owner is ever called - proven by a tripwire monkeypatch."""
    app, client = action_app
    _bypass_identity(app)
    def _explode(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("service must not be reached on a 422 body")
    monkeypatch.setattr(booking_service, "reschedule_appointment", _explode)
    body = _slot_body()
    body[forbidden_key] = "smuggled"
    res = _post(client, f"/portal/appointments/{uuid.uuid4()}/reschedule",
                "tok", json_body=body)
    assert res.status_code == 422


def test_reschedule_missing_slot_id_is_422(action_app, monkeypatch):
    app, client = action_app
    _bypass_identity(app)
    monkeypatch.setattr(
        booking_service, "reschedule_appointment",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("unreached")))
    res = _post(client, f"/portal/appointments/{uuid.uuid4()}/reschedule",
                "tok", json_body={})
    assert res.status_code == 422


@pytest.mark.parametrize("forbidden_key", FORBIDDEN_RESCHEDULE_KEYS)
def test_restore_to_slot_rejects_every_smuggled_server_owned_key_422(
        action_app, monkeypatch, forbidden_key):
    """The SAME strict one-field model guards the cancelled-only command:
    every smuggled server-owned key is 422 BEFORE the lifecycle owner is
    ever called (tripwire monkeypatch)."""
    app, client = action_app
    _bypass_identity(app)
    def _explode(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("service must not be reached on a 422 body")
    monkeypatch.setattr(booking_service, "restore_appointment_to_slot",
                        _explode)
    body = _slot_body()
    body[forbidden_key] = "smuggled"
    res = _post(client,
                f"/portal/appointments/{uuid.uuid4()}/restore-to-slot",
                "tok", json_body=body)
    assert res.status_code == 422


def test_restore_to_slot_missing_slot_id_is_422(action_app, monkeypatch):
    app, client = action_app
    _bypass_identity(app)
    monkeypatch.setattr(
        booking_service, "restore_appointment_to_slot",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("unreached")))
    res = _post(client,
                f"/portal/appointments/{uuid.uuid4()}/restore-to-slot",
                "tok", json_body={})
    assert res.status_code == 422


def test_reschedule_non_uuid_slot_id_is_422(action_app, monkeypatch):
    app, client = action_app
    _bypass_identity(app)
    monkeypatch.setattr(
        booking_service, "reschedule_appointment",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("unreached")))
    res = _post(client, f"/portal/appointments/{uuid.uuid4()}/reschedule",
                "tok", json_body={"slot_id": "next tuesday at noon"})
    assert res.status_code == 422, (
        "a typed datetime phrase can never be a slot id - the strict UUID "
        "type refuses it before any code of ours runs")


# --- restore mapping ------------------------------------------------------

def test_restore_success_is_200_exact_view(action_app, monkeypatch):
    app, client = action_app
    _bypass_identity(app)
    appt = _fake_appt(status="confirmed")
    monkeypatch.setattr(booking_service, "restore_appointment",
                        lambda db, cid, aid, *, now_utc: BookingResult(
                            True, "ok", appointment=appt))
    res = _post(client, f"/portal/appointments/{uuid.uuid4()}/restore", "tok")
    assert res.status_code == 200
    body = res.json()
    assert set(body.keys()) == APPROVED_ACTION_FIELDS
    assert body["status"] == "confirmed"
    for marker in FORBIDDEN_BODY_MARKERS:
        assert marker not in res.text, f"forbidden marker leaked: {marker}"


def test_restore_missing_is_404_opaque(action_app, monkeypatch):
    app, client = action_app
    _bypass_identity(app)
    monkeypatch.setattr(booking_service, "restore_appointment",
                        lambda db, cid, aid, *, now_utc: BookingResult(
                            False, "appointment_missing"))
    res = _post(client, f"/portal/appointments/{uuid.uuid4()}/restore", "tok")
    assert res.status_code == 404
    assert res.json()["detail"] == NOT_FOUND_DETAIL


def test_restore_not_restorable_is_409_sanitized_status(action_app, monkeypatch):
    app, client = action_app
    _bypass_identity(app)
    monkeypatch.setattr(booking_service, "restore_appointment",
                        lambda db, cid, aid, *, now_utc: BookingResult(
                            False, "not_restorable", detail="confirmed"))
    res = _post(client, f"/portal/appointments/{uuid.uuid4()}/restore", "tok")
    assert res.status_code == 409
    assert res.json()["detail"] == \
        "Appointment is confirmed and cannot be restored."


@pytest.mark.parametrize("reason", [
    "slot_missing", "slot_taken", "slot_blocked", "slot_started",
])
def test_restore_slot_refusals_collapse_to_one_409_sentence(
        action_app, monkeypatch, reason):
    """Every original-slot refusal is the SAME honest sentence: the office
    outcome is identical (choose another time), and the wording never leaks
    WHY the slot is unavailable (a held slot's holder is nobody's business)."""
    app, client = action_app
    _bypass_identity(app)
    monkeypatch.setattr(booking_service, "restore_appointment",
                        lambda db, cid, aid, *, now_utc: BookingResult(
                            False, reason))
    res = _post(client, f"/portal/appointments/{uuid.uuid4()}/restore", "tok")
    assert res.status_code == 409
    assert res.json()["detail"] == action_routes.RESTORE_SLOT_UNAVAILABLE_DETAIL
    assert res.json()["detail"] == "Original time is no longer available."


def test_restore_conversation_conflict_is_409(action_app, monkeypatch):
    app, client = action_app
    _bypass_identity(app)
    monkeypatch.setattr(booking_service, "restore_appointment",
                        lambda db, cid, aid, *, now_utc: BookingResult(
                            False, "conversation_conflict"))
    res = _post(client, f"/portal/appointments/{uuid.uuid4()}/restore", "tok")
    assert res.status_code == 409
    assert res.json()["detail"] == \
        "The patient's chat conversation already has an active appointment."


# --- reschedule mapping ---------------------------------------------------

def test_reschedule_success_is_200_exact_view(action_app, monkeypatch):
    app, client = action_app
    _bypass_identity(app)
    appt = _fake_appt(status="confirmed")
    captured = {}
    def spy(db, cid, aid, slot_id, *, now_utc):
        captured["slot_id"] = slot_id
        captured["now"] = now_utc
        return BookingResult(True, "ok", appointment=appt)
    monkeypatch.setattr(booking_service, "reschedule_appointment", spy)
    target = uuid.uuid4()
    res = _post(client, f"/portal/appointments/{uuid.uuid4()}/reschedule",
                "tok", json_body=_slot_body(target))
    assert res.status_code == 200
    body = res.json()
    assert set(body.keys()) == APPROVED_ACTION_FIELDS
    for marker in FORBIDDEN_BODY_MARKERS:
        assert marker not in res.text, f"forbidden marker leaked: {marker}"
    assert captured["slot_id"] == target, (
        "the chosen real slot id reaches the lifecycle owner verbatim")
    assert captured["now"].tzinfo is not None
    assert captured["now"].utcoffset() == timedelta(0)


def test_reschedule_appointment_missing_is_404_opaque(action_app, monkeypatch):
    app, client = action_app
    _bypass_identity(app)
    monkeypatch.setattr(booking_service, "reschedule_appointment",
                        lambda db, cid, aid, sid, *, now_utc: BookingResult(
                            False, "appointment_missing"))
    res = _post(client, f"/portal/appointments/{uuid.uuid4()}/reschedule",
                "tok", json_body=_slot_body())
    assert res.status_code == 404
    assert res.json()["detail"] == NOT_FOUND_DETAIL


def test_reschedule_slot_missing_is_404_staff_booking_wording(action_app,
                                                              monkeypatch):
    app, client = action_app
    _bypass_identity(app)
    monkeypatch.setattr(booking_service, "reschedule_appointment",
                        lambda db, cid, aid, sid, *, now_utc: BookingResult(
                            False, "slot_missing"))
    res = _post(client, f"/portal/appointments/{uuid.uuid4()}/reschedule",
                "tok", json_body=_slot_body())
    assert res.status_code == 404
    assert res.json()["detail"] == "Slot not found."


@pytest.mark.parametrize("reason,detail", [
    ("slot_taken", "Slot is no longer available."),
    ("slot_blocked", "Slot is blocked and cannot be booked."),
    ("slot_started", "Slot has already started and cannot be booked."),
])
def test_reschedule_target_refusals_use_staff_booking_wording(
        action_app, monkeypatch, reason, detail):
    app, client = action_app
    _bypass_identity(app)
    monkeypatch.setattr(booking_service, "reschedule_appointment",
                        lambda db, cid, aid, sid, *, now_utc: BookingResult(
                            False, reason))
    res = _post(client, f"/portal/appointments/{uuid.uuid4()}/reschedule",
                "tok", json_body=_slot_body())
    assert res.status_code == 409
    assert res.json()["detail"] == detail


def test_reschedule_not_reschedulable_is_409_sanitized_status(action_app,
                                                              monkeypatch):
    app, client = action_app
    _bypass_identity(app)
    monkeypatch.setattr(booking_service, "reschedule_appointment",
                        lambda db, cid, aid, sid, *, now_utc: BookingResult(
                            False, "not_reschedulable", detail="completed"))
    res = _post(client, f"/portal/appointments/{uuid.uuid4()}/reschedule",
                "tok", json_body=_slot_body())
    assert res.status_code == 409
    assert res.json()["detail"] == \
        "Appointment is completed and cannot be rescheduled."


def test_reschedule_same_slot_is_409(action_app, monkeypatch):
    app, client = action_app
    _bypass_identity(app)
    monkeypatch.setattr(booking_service, "reschedule_appointment",
                        lambda db, cid, aid, sid, *, now_utc: BookingResult(
                            False, "same_slot"))
    res = _post(client, f"/portal/appointments/{uuid.uuid4()}/reschedule",
                "tok", json_body=_slot_body())
    assert res.status_code == 409
    assert res.json()["detail"] == "Appointment already has this time."


def test_reschedule_conversation_conflict_is_409(action_app, monkeypatch):
    app, client = action_app
    _bypass_identity(app)
    monkeypatch.setattr(booking_service, "reschedule_appointment",
                        lambda db, cid, aid, sid, *, now_utc: BookingResult(
                            False, "conversation_conflict"))
    res = _post(client, f"/portal/appointments/{uuid.uuid4()}/reschedule",
                "tok", json_body=_slot_body())
    assert res.status_code == 409
    assert res.json()["detail"] == \
        "The patient's chat conversation already has an active appointment."


def test_reschedule_cancelled_row_is_the_stale_refusal_sentence(action_app,
                                                                monkeypatch):
    """v1.0.1 F1: the ACTIVE-ONLY command finding a CANCELLED row refuses
    with the existing not_reschedulable mapping - the stale Change-time is
    never converted into a recovery."""
    app, client = action_app
    _bypass_identity(app)
    monkeypatch.setattr(booking_service, "reschedule_appointment",
                        lambda db, cid, aid, sid, *, now_utc: BookingResult(
                            False, "not_reschedulable", detail="cancelled"))
    res = _post(client, f"/portal/appointments/{uuid.uuid4()}/reschedule",
                "tok", json_body=_slot_body())
    assert res.status_code == 409
    assert res.json()["detail"] == \
        "Appointment is cancelled and cannot be rescheduled."


# --- restore-to-slot mapping (the CANCELLED-ONLY command) ------------------

def test_restore_to_slot_success_is_200_exact_view(action_app, monkeypatch):
    app, client = action_app
    _bypass_identity(app)
    appt = _fake_appt(status="confirmed")
    captured = {}
    def spy(db, cid, aid, slot_id, *, now_utc):
        captured["slot_id"] = slot_id
        captured["now"] = now_utc
        return BookingResult(True, "ok", appointment=appt)
    monkeypatch.setattr(booking_service, "restore_appointment_to_slot", spy)
    target = uuid.uuid4()
    res = _post(client,
                f"/portal/appointments/{uuid.uuid4()}/restore-to-slot",
                "tok", json_body=_slot_body(target))
    assert res.status_code == 200
    body = res.json()
    assert set(body.keys()) == APPROVED_ACTION_FIELDS
    assert body["status"] == "confirmed"
    for marker in FORBIDDEN_BODY_MARKERS:
        assert marker not in res.text, f"forbidden marker leaked: {marker}"
    assert captured["slot_id"] == target
    assert captured["now"].tzinfo is not None
    assert captured["now"].utcoffset() == timedelta(0)


def test_restore_to_slot_appointment_missing_is_404_opaque(action_app,
                                                           monkeypatch):
    app, client = action_app
    _bypass_identity(app)
    monkeypatch.setattr(booking_service, "restore_appointment_to_slot",
                        lambda db, cid, aid, sid, *, now_utc: BookingResult(
                            False, "appointment_missing"))
    res = _post(client,
                f"/portal/appointments/{uuid.uuid4()}/restore-to-slot",
                "tok", json_body=_slot_body())
    assert res.status_code == 404
    assert res.json()["detail"] == NOT_FOUND_DETAIL


@pytest.mark.parametrize("detail", ["confirmed", "pending", "completed"])
def test_restore_to_slot_not_cancelled_is_409_sanitized_status(
        action_app, monkeypatch, detail):
    """v1.0.1 F1: the CANCELLED-ONLY command finding any other status under
    the lock refuses with the restore wording - the stale recovery is never
    converted into an ordinary move."""
    app, client = action_app
    _bypass_identity(app)
    monkeypatch.setattr(booking_service, "restore_appointment_to_slot",
                        lambda db, cid, aid, sid, *, now_utc: BookingResult(
                            False, "not_restorable", detail=detail))
    res = _post(client,
                f"/portal/appointments/{uuid.uuid4()}/restore-to-slot",
                "tok", json_body=_slot_body())
    assert res.status_code == 409
    assert res.json()["detail"] == \
        f"Appointment is {detail} and cannot be restored."


def test_restore_to_slot_conversation_conflict_is_409(action_app, monkeypatch):
    app, client = action_app
    _bypass_identity(app)
    monkeypatch.setattr(booking_service, "restore_appointment_to_slot",
                        lambda db, cid, aid, sid, *, now_utc: BookingResult(
                            False, "conversation_conflict"))
    res = _post(client,
                f"/portal/appointments/{uuid.uuid4()}/restore-to-slot",
                "tok", json_body=_slot_body())
    assert res.status_code == 409
    assert res.json()["detail"] == \
        "The patient's chat conversation already has an active appointment."


def test_restore_to_slot_slot_missing_is_404_staff_booking_wording(
        action_app, monkeypatch):
    app, client = action_app
    _bypass_identity(app)
    monkeypatch.setattr(booking_service, "restore_appointment_to_slot",
                        lambda db, cid, aid, sid, *, now_utc: BookingResult(
                            False, "slot_missing"))
    res = _post(client,
                f"/portal/appointments/{uuid.uuid4()}/restore-to-slot",
                "tok", json_body=_slot_body())
    assert res.status_code == 404
    assert res.json()["detail"] == "Slot not found."


@pytest.mark.parametrize("reason,detail", [
    ("slot_taken", "Slot is no longer available."),
    ("slot_blocked", "Slot is blocked and cannot be booked."),
    ("slot_started", "Slot has already started and cannot be booked."),
])
def test_restore_to_slot_target_refusals_use_staff_booking_wording(
        action_app, monkeypatch, reason, detail):
    app, client = action_app
    _bypass_identity(app)
    monkeypatch.setattr(booking_service, "restore_appointment_to_slot",
                        lambda db, cid, aid, sid, *, now_utc: BookingResult(
                            False, reason))
    res = _post(client,
                f"/portal/appointments/{uuid.uuid4()}/restore-to-slot",
                "tok", json_body=_slot_body())
    assert res.status_code == 409
    assert res.json()["detail"] == detail


# --- G1/G2: fail closed ----------------------------------------------------

@pytest.mark.parametrize("success,reason", [
    (True, "weird_unexpected_success"),
    (False, "totally_unexpected_failure"),
    (True, "already_confirmed"),     # a real word, but not a restore success
])
def test_restore_unexpected_reason_fails_closed_500(action_app, monkeypatch,
                                                    success, reason):
    app, client = action_app
    _bypass_identity(app)
    result = BookingResult(success, reason,
                           appointment=(_fake_appt() if success else None))
    monkeypatch.setattr(booking_service, "restore_appointment",
                        lambda db, cid, aid, *, now_utc: result)
    res = _post(client, f"/portal/appointments/{uuid.uuid4()}/restore", "tok")
    assert res.status_code == 500
    assert res.json()["detail"] == UNEXPECTED_DETAIL
    assert result.reason not in res.text


@pytest.mark.parametrize("success,reason", [
    (True, "weird"),
    (False, "surprise"),
])
def test_reschedule_unexpected_reason_fails_closed_500(action_app, monkeypatch,
                                                       success, reason):
    app, client = action_app
    _bypass_identity(app)
    result = BookingResult(success, reason,
                           appointment=(_fake_appt() if success else None))
    monkeypatch.setattr(booking_service, "reschedule_appointment",
                        lambda db, cid, aid, sid, *, now_utc: result)
    res = _post(client, f"/portal/appointments/{uuid.uuid4()}/reschedule",
                "tok", json_body=_slot_body())
    assert res.status_code == 500
    assert res.json()["detail"] == UNEXPECTED_DETAIL
    assert result.reason not in res.text


def test_restore_success_without_appointment_fails_closed_500(action_app,
                                                              monkeypatch):
    app, client = action_app
    _bypass_identity(app)
    monkeypatch.setattr(booking_service, "restore_appointment",
                        lambda db, cid, aid, *, now_utc: BookingResult(
                            True, "ok", appointment=None))
    res = _post(client, f"/portal/appointments/{uuid.uuid4()}/restore", "tok")
    assert res.status_code == 500
    assert res.json()["detail"] == UNEXPECTED_DETAIL


def test_reschedule_success_without_appointment_fails_closed_500(action_app,
                                                                 monkeypatch):
    app, client = action_app
    _bypass_identity(app)
    monkeypatch.setattr(booking_service, "reschedule_appointment",
                        lambda db, cid, aid, sid, *, now_utc: BookingResult(
                            True, "ok", appointment=None))
    res = _post(client, f"/portal/appointments/{uuid.uuid4()}/reschedule",
                "tok", json_body=_slot_body())
    assert res.status_code == 500
    assert res.json()["detail"] == UNEXPECTED_DETAIL


@pytest.mark.parametrize("success,reason", [
    (True, "weird"),
    (False, "surprise"),
])
def test_restore_to_slot_unexpected_reason_fails_closed_500(
        action_app, monkeypatch, success, reason):
    app, client = action_app
    _bypass_identity(app)
    result = BookingResult(success, reason,
                           appointment=(_fake_appt() if success else None))
    monkeypatch.setattr(booking_service, "restore_appointment_to_slot",
                        lambda db, cid, aid, sid, *, now_utc: result)
    res = _post(client,
                f"/portal/appointments/{uuid.uuid4()}/restore-to-slot",
                "tok", json_body=_slot_body())
    assert res.status_code == 500
    assert res.json()["detail"] == UNEXPECTED_DETAIL
    assert result.reason not in res.text


def test_restore_to_slot_success_without_appointment_fails_closed_500(
        action_app, monkeypatch):
    app, client = action_app
    _bypass_identity(app)
    monkeypatch.setattr(booking_service, "restore_appointment_to_slot",
                        lambda db, cid, aid, sid, *, now_utc: BookingResult(
                            True, "ok", appointment=None))
    res = _post(client,
                f"/portal/appointments/{uuid.uuid4()}/restore-to-slot",
                "tok", json_body=_slot_body())
    assert res.status_code == 500
    assert res.json()["detail"] == UNEXPECTED_DETAIL


# --- single-ownership pins -------------------------------------------------

def test_wording_constants_are_imported_not_retyped():
    """Rule 3: the reschedule target refusals reuse the staff-booking
    sentence OBJECTS, so the two surfaces can never drift apart silently."""
    assert action_routes.SLOT_NOT_FOUND_DETAIL is \
        staff_booking_routes.SLOT_NOT_FOUND_DETAIL
    assert action_routes.SLOT_TAKEN_DETAIL is \
        staff_booking_routes.SLOT_TAKEN_DETAIL
    assert action_routes.SLOT_BLOCKED_DETAIL is \
        staff_booking_routes.SLOT_BLOCKED_DETAIL
    assert action_routes.SLOT_STARTED_DETAIL is \
        staff_booking_routes.SLOT_STARTED_DETAIL


def test_one_lifecycle_owner_and_one_projection_owner():
    assert action_routes.booking_service is booking_service
    assert action_routes.build_portal_appointment_view is \
        portal_appt_routes.build_portal_appointment_view


def test_both_move_commands_share_one_engine():
    """v1.0.1 F1 + Rule 3: the two MODE-PINNED public commands are thin
    wrappers over the SAME private engine - locking, slot judgement,
    unique-index arbitration, and transaction rules exist exactly once.
    Proven structurally: both public functions' code delegates to
    _move_appointment_to_slot and contains no locking of its own."""
    import inspect
    engine = booking_service._move_appointment_to_slot
    assert callable(engine)
    for fn in (booking_service.reschedule_appointment,
               booking_service.restore_appointment_to_slot):
        source = inspect.getsource(fn)
        assert "_move_appointment_to_slot(" in source, (
            f"{fn.__name__} must delegate into the one shared engine")
        assert "get_appointment_for_update" not in source, (
            f"{fn.__name__} must not duplicate locking outside the engine")
    assert "cancelled_mode=False" in inspect.getsource(
        booking_service.reschedule_appointment)
    assert "cancelled_mode=True" in inspect.getsource(
        booking_service.restore_appointment_to_slot)


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


def _future_utc(days=3, hour=16):
    """An aware-UTC instant safely in the FUTURE (restore/reschedule refuse
    slots whose start has passed, so the happy paths must live ahead of
    now - the seeded July 2026 default of the P5-A helper is already in the
    past for this suite's purposes)."""
    base = datetime.now(UTC) + timedelta(days=days)
    return base.replace(hour=hour, minute=0, second=0, microsecond=0)


def _seed_slot(db, client, *, status="available", start_utc=None,
               minutes=45, held_until=None, held_by_conversation_id=None):
    from app.calendar_models import AppointmentSlot
    if start_utc is None:
        start_utc = _future_utc()
    slot = AppointmentSlot(
        client_id=client.id, start_datetime=start_utc,
        end_datetime=start_utc + timedelta(minutes=minutes), status=status,
    )
    if held_until is not None:
        slot.held_until = held_until
    if held_by_conversation_id is not None:
        slot.held_by_conversation_id = held_by_conversation_id
    db.add(slot)
    db.commit()
    return slot


def _seed_appointment(db, client, *, status="pending", slot_status="booked",
                      start_utc=None, minutes=45, conversation_id=None,
                      confirmed_at=None, internal_note=None):
    """Seed one slot + appointment directly so the state under test is exact
    (the P5-A helper extended with the Slice 4C knobs). Returns (appt, slot)."""
    from app.calendar_models import AppointmentSlot, Appointment
    if start_utc is None:
        start_utc = _future_utc()
    end_utc = start_utc + timedelta(minutes=minutes)
    slot = AppointmentSlot(
        client_id=client.id, start_datetime=start_utc, end_datetime=end_utc,
        status=slot_status,
    )
    db.add(slot)
    db.flush()
    appointment = Appointment(
        client_id=client.id, slot_id=slot.id, conversation_id=conversation_id,
        patient_name="Kevin Alvarado", patient_phone="516-555-1234",
        patient_email=None, new_or_returning="new", reason="cleaning",
        urgency="routine", start_datetime=start_utc, end_datetime=end_utc,
        status=status, source="mia_widget", confirmed_at=confirmed_at,
        office_sms_sent=False, office_email_sent=False, notify_error=None,
        internal_note=internal_note,
    )
    db.add(appointment)
    db.commit()
    return appointment, slot


def _seed_conversation(db, client):
    from app.models import Conversation
    conversation = Conversation(client_id=client.id, visitor_id="v-4c")
    db.add(conversation)
    db.commit()
    return conversation


@pytest.fixture()
def portal_http(db, office_users_table, monkeypatch):
    """Real app over HTTP with the REAL portal_auth running (P5-A fixture)."""
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


def _http_post(portal_http, path, token=None, json_body=None):
    headers = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if json_body is None:
        return portal_http.post(path, headers=headers)
    return portal_http.post(path, headers=headers, json=json_body)


def _notification_flags(db, appointment_id):
    from app.calendar_models import Appointment
    row = db.get(Appointment, appointment_id)
    return (row.office_sms_sent, row.office_email_sent, row.notify_error)


# --- restore: success + preservation ---------------------------------------

@requires_db
def test_db_restore_cancelled_becomes_confirmed_on_original_slot(
        portal_http, db, client_row):
    appt, slot = _seed_appointment(db, client_row, status="cancelled",
                                   slot_status="available",
                                   internal_note="call back Tuesday")
    user = _bind_office_user(db, client_row)
    res = _http_post(portal_http, f"/portal/appointments/{appt.id}/restore",
                     _token(user.auth_user_id))
    assert res.status_code == 200
    body = res.json()
    assert set(body.keys()) == APPROVED_ACTION_FIELDS
    assert body["status"] == "confirmed"
    db.expire_all()
    from app.calendar_models import Appointment, AppointmentSlot
    refreshed = db.get(Appointment, appt.id)
    assert refreshed.status == "confirmed"
    assert refreshed.slot_id == slot.id, "the ORIGINAL slot, never another"
    fresh_slot = db.get(AppointmentSlot, slot.id)
    assert fresh_slot.status == "booked", "the original slot is booked again"
    assert fresh_slot.held_until is None
    # Identity + office data preserved byte-for-byte.
    assert refreshed.patient_name == "Kevin Alvarado"
    assert refreshed.patient_phone == "516-555-1234"
    assert refreshed.source == "mia_widget"
    assert refreshed.urgency == "routine"
    assert refreshed.internal_note == "call back Tuesday"
    # No notification of any kind was sent or recorded (D7).
    assert _notification_flags(db, appt.id) == (False, False, None)


@requires_db
def test_db_restore_never_writes_confirmed_at(portal_http, db, client_row):
    """D5 invariant: confirm_appointment remains the ONLY writer of
    confirmed_at. A restore preserves whatever the row carried - here NULL -
    even though the appointment ends CONFIRMED."""
    appt, _ = _seed_appointment(db, client_row, status="cancelled",
                                slot_status="available", confirmed_at=None)
    user = _bind_office_user(db, client_row)
    res = _http_post(portal_http, f"/portal/appointments/{appt.id}/restore",
                     _token(user.auth_user_id))
    assert res.status_code == 200
    assert res.json()["confirmed_at"] is None
    db.expire_all()
    from app.calendar_models import Appointment
    assert db.get(Appointment, appt.id).confirmed_at is None


@requires_db
def test_db_restore_preserves_prior_confirmed_at(portal_http, db, client_row):
    stamp = datetime(2026, 7, 1, 15, 0, tzinfo=UTC)
    appt, _ = _seed_appointment(db, client_row, status="cancelled",
                                slot_status="available", confirmed_at=stamp)
    user = _bind_office_user(db, client_row)
    res = _http_post(portal_http, f"/portal/appointments/{appt.id}/restore",
                     _token(user.auth_user_id))
    assert res.status_code == 200
    db.expire_all()
    from app.calendar_models import Appointment
    assert db.get(Appointment, appt.id).confirmed_at == stamp


@requires_db
def test_db_restore_recopies_datetimes_from_slot(portal_http, db, client_row):
    """The appointment's start/end are justified COPIES of its slot; restore
    re-copies them under the lock so a drifted copy can never survive."""
    appt, slot = _seed_appointment(db, client_row, status="cancelled",
                                   slot_status="available")
    # Simulate drift: the appointment's copies were mangled while cancelled.
    from app.calendar_models import Appointment
    row = db.get(Appointment, appt.id)
    row.start_datetime = row.start_datetime - timedelta(hours=2)
    row.end_datetime = row.end_datetime - timedelta(hours=2)
    db.commit()
    user = _bind_office_user(db, client_row)
    res = _http_post(portal_http, f"/portal/appointments/{appt.id}/restore",
                     _token(user.auth_user_id))
    assert res.status_code == 200
    db.expire_all()
    from app.services.calendar_settings_service import ensure_utc
    refreshed = db.get(Appointment, appt.id)
    assert ensure_utc(refreshed.start_datetime) == ensure_utc(slot.start_datetime)
    assert ensure_utc(refreshed.end_datetime) == ensure_utc(slot.end_datetime)


# --- restore: refusals with nothing mutated --------------------------------

@requires_db
@pytest.mark.parametrize("status", ["pending", "confirmed", "completed", "no_show"])
def test_db_restore_non_cancelled_is_409_untouched(portal_http, db, client_row,
                                                   status):
    appt, slot = _seed_appointment(db, client_row, status=status,
                                   slot_status="booked")
    user = _bind_office_user(db, client_row)
    res = _http_post(portal_http, f"/portal/appointments/{appt.id}/restore",
                     _token(user.auth_user_id))
    assert res.status_code == 409
    assert res.json()["detail"] == \
        f"Appointment is {status} and cannot be restored."
    db.expire_all()
    from app.calendar_models import Appointment, AppointmentSlot
    assert db.get(Appointment, appt.id).status == status
    assert db.get(AppointmentSlot, slot.id).status == "booked"


@requires_db
def test_db_restore_refuses_when_original_slot_rebooked(portal_http, db,
                                                        client_row):
    """The exact production risk: Maria cancelled, ANOTHER patient booked the
    freed time, and the office presses Restore. Refused under the lock; the
    other patient's appointment and the slot are untouched."""
    appt, slot = _seed_appointment(db, client_row, status="cancelled",
                                   slot_status="booked")
    from app.calendar_models import Appointment
    other = Appointment(
        client_id=client_row.id, slot_id=slot.id, conversation_id=None,
        patient_name="Second Patient", patient_phone="516-555-9999",
        patient_email=None, new_or_returning="new", reason="checkup",
        urgency="routine", start_datetime=slot.start_datetime,
        end_datetime=slot.end_datetime, status="confirmed", source="portal_staff",
        office_sms_sent=False, office_email_sent=False, notify_error=None,
    )
    db.add(other)
    db.commit()
    user = _bind_office_user(db, client_row)
    res = _http_post(portal_http, f"/portal/appointments/{appt.id}/restore",
                     _token(user.auth_user_id))
    assert res.status_code == 409
    assert res.json()["detail"] == "Original time is no longer available."
    db.expire_all()
    assert db.get(Appointment, appt.id).status == "cancelled"
    assert db.get(Appointment, other.id).status == "confirmed"


@requires_db
def test_db_restore_refuses_blocked_original_slot(portal_http, db, client_row):
    appt, slot = _seed_appointment(db, client_row, status="cancelled",
                                   slot_status="blocked")
    user = _bind_office_user(db, client_row)
    res = _http_post(portal_http, f"/portal/appointments/{appt.id}/restore",
                     _token(user.auth_user_id))
    assert res.status_code == 409
    assert res.json()["detail"] == "Original time is no longer available."
    db.expire_all()
    from app.calendar_models import AppointmentSlot
    assert db.get(AppointmentSlot, slot.id).status == "blocked", (
        "a staff block is never overridden by a restore")


@requires_db
def test_db_restore_refuses_actively_held_original_slot(portal_http, db,
                                                        client_row):
    """A LIVE chatbot hold on the freed slot wins: the patient in the widget
    keeps their claim (D4), and the restore refuses without touching it."""
    conversation = _seed_conversation(db, client_row)
    appt, slot = _seed_appointment(db, client_row, status="cancelled",
                                   slot_status="available")
    from app.calendar_models import AppointmentSlot
    fresh = db.get(AppointmentSlot, slot.id)
    fresh.status = "held"
    fresh.held_until = datetime.now(UTC) + timedelta(minutes=5)
    fresh.held_by_conversation_id = conversation.id
    db.commit()
    user = _bind_office_user(db, client_row)
    res = _http_post(portal_http, f"/portal/appointments/{appt.id}/restore",
                     _token(user.auth_user_id))
    assert res.status_code == 409
    db.expire_all()
    held = db.get(AppointmentSlot, slot.id)
    assert held.status == "held" and held.held_by_conversation_id == conversation.id


@requires_db
def test_db_restore_reclaims_expired_hold(portal_http, db, client_row):
    """D4: an EXPIRED hold is dead inventory; restore lazily reclaims it
    exactly as finalize_staff_booking does, and the hold bookkeeping is
    cleared on the booked slot."""
    conversation = _seed_conversation(db, client_row)
    appt, slot = _seed_appointment(db, client_row, status="cancelled",
                                   slot_status="available")
    from app.calendar_models import AppointmentSlot
    fresh = db.get(AppointmentSlot, slot.id)
    fresh.status = "held"
    fresh.held_until = datetime.now(UTC) - timedelta(minutes=1)
    fresh.held_by_conversation_id = conversation.id
    db.commit()
    user = _bind_office_user(db, client_row)
    res = _http_post(portal_http, f"/portal/appointments/{appt.id}/restore",
                     _token(user.auth_user_id))
    assert res.status_code == 200
    db.expire_all()
    reclaimed = db.get(AppointmentSlot, slot.id)
    assert reclaimed.status == "booked"
    assert reclaimed.held_until is None
    assert reclaimed.held_by_conversation_id is None


@requires_db
def test_db_restore_refuses_started_slot(portal_http, db, client_row):
    appt, _ = _seed_appointment(db, client_row, status="cancelled",
                                slot_status="available",
                                start_utc=datetime.now(UTC) - timedelta(hours=2))
    user = _bind_office_user(db, client_row)
    res = _http_post(portal_http, f"/portal/appointments/{appt.id}/restore",
                     _token(user.auth_user_id))
    assert res.status_code == 409
    assert res.json()["detail"] == "Original time is no longer available."


@requires_db
def test_db_restore_conversation_conflict(portal_http, db, client_row):
    """The cancelled appointment came from a chat conversation that has since
    produced ANOTHER active appointment: restoring would give one
    conversation two active appointments, so it refuses."""
    conversation = _seed_conversation(db, client_row)
    cancelled, _ = _seed_appointment(db, client_row, status="cancelled",
                                     slot_status="available",
                                     conversation_id=conversation.id)
    _seed_appointment(db, client_row, status="confirmed",
                      slot_status="booked",
                      start_utc=_future_utc(days=4),
                      conversation_id=conversation.id)
    user = _bind_office_user(db, client_row)
    res = _http_post(portal_http,
                     f"/portal/appointments/{cancelled.id}/restore",
                     _token(user.auth_user_id))
    assert res.status_code == 409
    assert res.json()["detail"] == \
        "The patient's chat conversation already has an active appointment."
    db.expire_all()
    from app.calendar_models import Appointment
    assert db.get(Appointment, cancelled.id).status == "cancelled"


# --- reschedule: active appointment ----------------------------------------

@requires_db
def test_db_reschedule_active_moves_and_frees_old_slot(portal_http, db,
                                                       client_row):
    appt, old_slot = _seed_appointment(db, client_row, status="confirmed",
                                       slot_status="booked",
                                       confirmed_at=datetime(2026, 7, 1, 15, 0,
                                                             tzinfo=UTC),
                                       internal_note="prefers mornings")
    target = _seed_slot(db, client_row, start_utc=_future_utc(days=5, hour=14))
    user = _bind_office_user(db, client_row)
    res = _http_post(portal_http,
                     f"/portal/appointments/{appt.id}/reschedule",
                     _token(user.auth_user_id),
                     json_body={"slot_id": str(target.id)})
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "confirmed", "an active move preserves status"
    db.expire_all()
    from app.calendar_models import Appointment, AppointmentSlot
    from app.services.calendar_settings_service import ensure_utc
    refreshed = db.get(Appointment, appt.id)
    assert refreshed.slot_id == target.id
    assert ensure_utc(refreshed.start_datetime) == ensure_utc(target.start_datetime)
    assert ensure_utc(refreshed.end_datetime) == ensure_utc(target.end_datetime)
    assert db.get(AppointmentSlot, target.id).status == "booked"
    assert db.get(AppointmentSlot, old_slot.id).status == "available", (
        "the OLD booked slot is freed - the cancel path's release rule")
    # Identity, status history, and office data preserved.
    assert refreshed.confirmed_at == datetime(2026, 7, 1, 15, 0, tzinfo=UTC)
    assert refreshed.internal_note == "prefers mornings"
    assert refreshed.source == "mia_widget"
    assert _notification_flags(db, appt.id) == (False, False, None)


@requires_db
def test_db_reschedule_pending_stays_pending(portal_http, db, client_row):
    appt, _ = _seed_appointment(db, client_row, status="pending",
                                slot_status="booked")
    target = _seed_slot(db, client_row, start_utc=_future_utc(days=5, hour=15))
    user = _bind_office_user(db, client_row)
    res = _http_post(portal_http,
                     f"/portal/appointments/{appt.id}/reschedule",
                     _token(user.auth_user_id),
                     json_body={"slot_id": str(target.id)})
    assert res.status_code == 200
    assert res.json()["status"] == "pending", (
        "moving a pending appointment never fabricates a confirmation")
    assert res.json()["confirmed_at"] is None


@requires_db
def test_db_reschedule_drifted_old_slot_is_pinned_untouched(portal_http, db,
                                                            client_row):
    """C7: an old slot that has DRIFTED (not booked) is left EXACTLY as-is;
    the appointment still moves."""
    appt, old_slot = _seed_appointment(db, client_row, status="confirmed",
                                       slot_status="blocked")
    target = _seed_slot(db, client_row, start_utc=_future_utc(days=5, hour=16))
    user = _bind_office_user(db, client_row)
    res = _http_post(portal_http,
                     f"/portal/appointments/{appt.id}/reschedule",
                     _token(user.auth_user_id),
                     json_body={"slot_id": str(target.id)})
    assert res.status_code == 200
    db.expire_all()
    from app.calendar_models import AppointmentSlot
    assert db.get(AppointmentSlot, old_slot.id).status == "blocked", (
        "drifted old slot must be left untouched")


@requires_db
def test_db_reschedule_same_slot_is_409_untouched(portal_http, db, client_row):
    appt, slot = _seed_appointment(db, client_row, status="confirmed",
                                   slot_status="booked")
    user = _bind_office_user(db, client_row)
    res = _http_post(portal_http,
                     f"/portal/appointments/{appt.id}/reschedule",
                     _token(user.auth_user_id),
                     json_body={"slot_id": str(slot.id)})
    assert res.status_code == 409
    assert res.json()["detail"] == "Appointment already has this time."
    db.expire_all()
    from app.calendar_models import Appointment, AppointmentSlot
    assert db.get(Appointment, appt.id).status == "confirmed"
    assert db.get(AppointmentSlot, slot.id).status == "booked"


@requires_db
@pytest.mark.parametrize("target_status,detail", [
    ("booked", "Slot is no longer available."),
    ("blocked", "Slot is blocked and cannot be booked."),
    ("cancelled", "Slot is blocked and cannot be booked."),
])
def test_db_reschedule_unbookable_target_is_409_untouched(
        portal_http, db, client_row, target_status, detail):
    appt, old_slot = _seed_appointment(db, client_row, status="confirmed",
                                       slot_status="booked")
    target = _seed_slot(db, client_row, status=target_status,
                        start_utc=_future_utc(days=5, hour=17))
    user = _bind_office_user(db, client_row)
    res = _http_post(portal_http,
                     f"/portal/appointments/{appt.id}/reschedule",
                     _token(user.auth_user_id),
                     json_body={"slot_id": str(target.id)})
    assert res.status_code == 409
    assert res.json()["detail"] == detail
    db.expire_all()
    from app.calendar_models import Appointment, AppointmentSlot
    refreshed = db.get(Appointment, appt.id)
    assert refreshed.slot_id == old_slot.id, "nothing moved"
    assert db.get(AppointmentSlot, old_slot.id).status == "booked", (
        "a refused move NEVER frees the old slot")


@requires_db
def test_db_reschedule_started_target_is_409(portal_http, db, client_row):
    appt, _ = _seed_appointment(db, client_row, status="confirmed",
                                slot_status="booked")
    target = _seed_slot(db, client_row,
                        start_utc=datetime.now(UTC) - timedelta(hours=1))
    user = _bind_office_user(db, client_row)
    res = _http_post(portal_http,
                     f"/portal/appointments/{appt.id}/reschedule",
                     _token(user.auth_user_id),
                     json_body={"slot_id": str(target.id)})
    assert res.status_code == 409
    assert res.json()["detail"] == "Slot has already started and cannot be booked."


@requires_db
@pytest.mark.parametrize("status", ["completed", "no_show"])
def test_db_reschedule_terminal_is_409(portal_http, db, client_row, status):
    appt, _ = _seed_appointment(db, client_row, status=status,
                                slot_status="booked")
    target = _seed_slot(db, client_row, start_utc=_future_utc(days=5, hour=18))
    user = _bind_office_user(db, client_row)
    res = _http_post(portal_http,
                     f"/portal/appointments/{appt.id}/reschedule",
                     _token(user.auth_user_id),
                     json_body={"slot_id": str(target.id)})
    assert res.status_code == 409
    assert res.json()["detail"] == \
        f"Appointment is {status} and cannot be rescheduled."


# --- reschedule: the cancelled combined path -------------------------------

@requires_db
def test_db_restore_to_slot_restores_and_moves_atomically(
        portal_http, db, client_row):
    """Choose Another Time (v1.0.1: its OWN cancelled-only command): ONE
    request restores AND moves - the appointment ends CONFIRMED on the
    target slot; the OLD slot (already freed or reused at cancellation) is
    untouched."""
    appt, old_slot = _seed_appointment(db, client_row, status="cancelled",
                                       slot_status="available")
    target = _seed_slot(db, client_row, start_utc=_future_utc(days=6, hour=14))
    user = _bind_office_user(db, client_row)
    res = _http_post(portal_http,
                     f"/portal/appointments/{appt.id}/restore-to-slot",
                     _token(user.auth_user_id),
                     json_body={"slot_id": str(target.id)})
    assert res.status_code == 200
    assert res.json()["status"] == "confirmed"
    assert res.json()["confirmed_at"] is None, "confirmed_at is never written"
    db.expire_all()
    from app.calendar_models import Appointment, AppointmentSlot
    refreshed = db.get(Appointment, appt.id)
    assert refreshed.status == "confirmed"
    assert refreshed.slot_id == target.id
    assert db.get(AppointmentSlot, target.id).status == "booked"
    assert db.get(AppointmentSlot, old_slot.id).status == "available", (
        "the cancelled path never touches the old slot - cancellation "
        "already settled it")
    assert _notification_flags(db, appt.id) == (False, False, None)


@requires_db
def test_db_restore_to_slot_old_slot_rebooked_stays_untouched(
        portal_http, db, client_row):
    """The cancelled-only command with the old slot REUSED by another
    patient: the move succeeds onto the target and the other booking is
    untouched."""
    appt, old_slot = _seed_appointment(db, client_row, status="cancelled",
                                       slot_status="booked")
    from app.calendar_models import Appointment
    other = Appointment(
        client_id=client_row.id, slot_id=old_slot.id, conversation_id=None,
        patient_name="Second Patient", patient_phone="516-555-9999",
        patient_email=None, new_or_returning="new", reason="checkup",
        urgency="routine", start_datetime=old_slot.start_datetime,
        end_datetime=old_slot.end_datetime, status="confirmed",
        source="portal_staff",
        office_sms_sent=False, office_email_sent=False, notify_error=None,
    )
    db.add(other)
    db.commit()
    target = _seed_slot(db, client_row, start_utc=_future_utc(days=6, hour=15))
    user = _bind_office_user(db, client_row)
    res = _http_post(portal_http,
                     f"/portal/appointments/{appt.id}/restore-to-slot",
                     _token(user.auth_user_id),
                     json_body={"slot_id": str(target.id)})
    assert res.status_code == 200
    db.expire_all()
    from app.calendar_models import AppointmentSlot
    assert db.get(Appointment, other.id).status == "confirmed"
    assert db.get(AppointmentSlot, old_slot.id).status == "booked"


@requires_db
def test_db_restore_to_slot_conversation_conflict(portal_http, db,
                                                  client_row):
    conversation = _seed_conversation(db, client_row)
    cancelled, _ = _seed_appointment(db, client_row, status="cancelled",
                                     slot_status="available",
                                     conversation_id=conversation.id)
    _seed_appointment(db, client_row, status="pending", slot_status="booked",
                      start_utc=_future_utc(days=4, hour=15),
                      conversation_id=conversation.id)
    target = _seed_slot(db, client_row, start_utc=_future_utc(days=6, hour=16))
    user = _bind_office_user(db, client_row)
    res = _http_post(portal_http,
                     f"/portal/appointments/{cancelled.id}/restore-to-slot",
                     _token(user.auth_user_id),
                     json_body={"slot_id": str(target.id)})
    assert res.status_code == 409
    assert res.json()["detail"] == \
        "The patient's chat conversation already has an active appointment."
    db.expire_all()
    from app.calendar_models import Appointment, AppointmentSlot
    assert db.get(Appointment, cancelled.id).status == "cancelled"
    assert db.get(AppointmentSlot, target.id).status == "available", (
        "a refused combined move books nothing")


# --- v1.0.1 mode pin (F1): stale commands refuse, never reinterpret --------

@requires_db
def test_db_stale_change_time_after_cancel_never_resurrects(
        portal_http, db, client_row):
    """AUDIT RACE 1, the named deterministic scenario: A saw CONFIRMED and
    clicked Change time; B's Cancel committed first. A's stale ACTIVE-ONLY
    command MUST refuse: final appointment = CANCELLED, the proposed
    target = AVAILABLE, the appointment never moves, nothing resurrects."""
    appt, old_slot = _seed_appointment(db, client_row, status="confirmed",
                                       slot_status="booked")
    target = _seed_slot(db, client_row, start_utc=_future_utc(days=4, hour=11))
    user = _bind_office_user(db, client_row)
    tok = _token(user.auth_user_id)
    # B's cancel commits first (through the same portal surface).
    cancelled = _http_post(portal_http,
                           f"/portal/appointments/{appt.id}/cancel", tok)
    assert cancelled.status_code == 200
    # A's stale Change time arrives afterwards.
    stale = _http_post(portal_http,
                       f"/portal/appointments/{appt.id}/reschedule", tok,
                       json_body={"slot_id": str(target.id)})
    assert stale.status_code == 409
    assert stale.json()["detail"] == \
        "Appointment is cancelled and cannot be rescheduled."
    db.expire_all()
    from app.calendar_models import Appointment, AppointmentSlot
    final = db.get(Appointment, appt.id)
    assert final.status == "cancelled", "no resurrection, ever"
    assert final.slot_id == old_slot.id, "and no move"
    assert db.get(AppointmentSlot, target.id).status == "available", (
        "the proposed target was never claimed")
    assert db.get(AppointmentSlot, old_slot.id).status == "available", (
        "the cancel's release stands")


@requires_db
def test_db_stale_choose_another_time_after_restore_never_double_moves(
        portal_http, db, client_row):
    """AUDIT RACE 2, the named deterministic scenario: A saw CANCELLED and
    clicked Choose another time; B's Restore Original Time committed first.
    A's stale CANCELLED-ONLY command MUST refuse: the appointment remains
    CONFIRMED on its ORIGINAL slot, the proposed target stays AVAILABLE,
    and no second move happens."""
    appt, original = _seed_appointment(db, client_row, status="cancelled",
                                       slot_status="available")
    target = _seed_slot(db, client_row, start_utc=_future_utc(days=4, hour=12))
    user = _bind_office_user(db, client_row)
    tok = _token(user.auth_user_id)
    restored = _http_post(portal_http,
                          f"/portal/appointments/{appt.id}/restore", tok)
    assert restored.status_code == 200
    stale = _http_post(portal_http,
                       f"/portal/appointments/{appt.id}/restore-to-slot",
                       tok, json_body={"slot_id": str(target.id)})
    assert stale.status_code == 409
    assert stale.json()["detail"] == \
        "Appointment is confirmed and cannot be restored."
    db.expire_all()
    from app.calendar_models import Appointment, AppointmentSlot
    final = db.get(Appointment, appt.id)
    assert final.status == "confirmed"
    assert final.slot_id == original.id, (
        "still on the ORIGINAL slot - no second move")
    assert db.get(AppointmentSlot, original.id).status == "booked"
    assert db.get(AppointmentSlot, target.id).status == "available", (
        "the proposed target was never claimed")


@requires_db
@pytest.mark.parametrize("status", ["pending", "confirmed", "completed",
                                    "no_show"])
def test_db_restore_to_slot_refuses_every_non_cancelled_status(
        portal_http, db, client_row, status):
    """The CANCELLED-ONLY command against every other seeded status: 409
    with the sanitized status word, nothing mutated, target untouched."""
    appt, old_slot = _seed_appointment(db, client_row, status=status,
                                       slot_status="booked")
    target = _seed_slot(db, client_row, start_utc=_future_utc(days=4, hour=13))
    user = _bind_office_user(db, client_row)
    res = _http_post(portal_http,
                     f"/portal/appointments/{appt.id}/restore-to-slot",
                     _token(user.auth_user_id),
                     json_body={"slot_id": str(target.id)})
    assert res.status_code == 409
    assert res.json()["detail"] == \
        f"Appointment is {status} and cannot be restored."
    db.expire_all()
    from app.calendar_models import Appointment, AppointmentSlot
    assert db.get(Appointment, appt.id).status == status
    assert db.get(Appointment, appt.id).slot_id == old_slot.id
    assert db.get(AppointmentSlot, target.id).status == "available"


@requires_db
def test_db_reschedule_refuses_cancelled_row_untouched(portal_http, db,
                                                       client_row):
    """The ACTIVE-ONLY command against a seeded CANCELLED row (no prior
    request needed): 409 stale-refusal sentence, nothing mutated."""
    appt, old_slot = _seed_appointment(db, client_row, status="cancelled",
                                       slot_status="available")
    target = _seed_slot(db, client_row, start_utc=_future_utc(days=4, hour=14))
    user = _bind_office_user(db, client_row)
    res = _http_post(portal_http,
                     f"/portal/appointments/{appt.id}/reschedule",
                     _token(user.auth_user_id),
                     json_body={"slot_id": str(target.id)})
    assert res.status_code == 409
    assert res.json()["detail"] == \
        "Appointment is cancelled and cannot be rescheduled."
    db.expire_all()
    from app.calendar_models import Appointment, AppointmentSlot
    assert db.get(Appointment, appt.id).status == "cancelled"
    assert db.get(AppointmentSlot, target.id).status == "available"


# --- tenant isolation -------------------------------------------------------

@requires_db
def test_db_tenant_isolation_restore(portal_http, db, client_row, office_b):
    appt_b, _ = _seed_appointment(db, office_b, status="cancelled",
                                  slot_status="available")
    user_a = _bind_office_user(db, client_row)
    res = _http_post(portal_http, f"/portal/appointments/{appt_b.id}/restore",
                     _token(user_a.auth_user_id))
    assert res.status_code == 404
    assert res.json()["detail"] == NOT_FOUND_DETAIL
    db.expire_all()
    from app.calendar_models import Appointment
    assert db.get(Appointment, appt_b.id).status == "cancelled"


@requires_db
def test_db_tenant_isolation_reschedule_foreign_target_slot(
        portal_http, db, client_row, office_b):
    """Office A moving ITS appointment onto OFFICE B's slot: the target is
    invisible (404, staff-booking wording) and neither tenant's rows move."""
    appt_a, old_slot = _seed_appointment(db, client_row, status="confirmed",
                                         slot_status="booked")
    slot_b = _seed_slot(db, office_b, start_utc=_future_utc(days=6, hour=17))
    user_a = _bind_office_user(db, client_row)
    res = _http_post(portal_http,
                     f"/portal/appointments/{appt_a.id}/reschedule",
                     _token(user_a.auth_user_id),
                     json_body={"slot_id": str(slot_b.id)})
    assert res.status_code == 404
    assert res.json()["detail"] == "Slot not found."
    db.expire_all()
    from app.calendar_models import Appointment, AppointmentSlot
    assert db.get(Appointment, appt_a.id).slot_id == old_slot.id
    assert db.get(AppointmentSlot, slot_b.id).status == "available"


@requires_db
def test_db_unknown_and_foreign_restore_are_indistinguishable_404(
        portal_http, db, client_row, office_b):
    user_a = _bind_office_user(db, client_row)
    appt_b, _ = _seed_appointment(db, office_b, status="cancelled",
                                  slot_status="available")
    unknown = _http_post(portal_http,
                         f"/portal/appointments/{uuid.uuid4()}/restore",
                         _token(user_a.auth_user_id))
    foreign = _http_post(portal_http,
                         f"/portal/appointments/{appt_b.id}/restore",
                         _token(user_a.auth_user_id))
    assert unknown.status_code == foreign.status_code == 404
    assert unknown.json() == foreign.json()


# --- concurrency (the P5-A threaded pattern) --------------------------------

def _threaded_race(db, monkeypatch, paths, bodies, token):
    """Run two portal POSTs simultaneously, each worker owning its OWN
    SessionLocal() created INSIDE its thread behind a zero-argument generator
    override. Returns (results, worker_errors, session_ids)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.database import SessionLocal
    from app.services import portal_auth

    monkeypatch.setenv(portal_auth.ENV_JWT_SECRET, TEST_SECRET)
    monkeypatch.delenv(portal_auth.ENV_JWKS_URL, raising=False)
    monkeypatch.setenv(portal_auth.ENV_ISSUER, TEST_ISSUER)
    portal_auth._jwks_clients.clear()

    results = {}
    session_ids = {}
    worker_errors = {}
    barrier = threading.Barrier(2)

    def do(name):
        session = SessionLocal()
        try:
            app = FastAPI()
            app.include_router(portal_routes.router)
            app.include_router(action_routes.router)

            def override_get_db():
                yield session

            app.dependency_overrides[portal_routes.get_db] = override_get_db
            with TestClient(app) as client:
                session_ids[name] = id(session)
                barrier.wait()
                kwargs = {"headers": {"Authorization": f"Bearer {token}"}}
                if bodies[name] is not None:
                    kwargs["json"] = bodies[name]
                response = client.post(paths[name], **kwargs)
                results[name] = response.status_code
        except BaseException as exc:
            worker_errors[name] = exc
            try:
                barrier.abort()
            except BaseException:
                pass
        finally:
            session.close()

    threads = [threading.Thread(target=do, args=(name,)) for name in paths]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results, worker_errors, session_ids


@requires_db
def test_db_restore_vs_restore_concurrency_exactly_one_wins(
        office_users_table, db, client_row, monkeypatch):
    """Two receptionists press Restore together: the appointment row lock
    serializes them - the first flips cancelled->confirmed, the second finds
    a non-cancelled row and refuses 409. Exactly one winner, one final
    confirmed state, one booked slot."""
    appt, slot = _seed_appointment(db, client_row, status="cancelled",
                                   slot_status="available")
    user = _bind_office_user(db, client_row)
    tok = _token(user.auth_user_id)
    appointment_id = str(appt.id)

    results, worker_errors, session_ids = _threaded_race(
        db, monkeypatch,
        paths={"first": f"/portal/appointments/{appointment_id}/restore",
               "second": f"/portal/appointments/{appointment_id}/restore"},
        bodies={"first": None, "second": None}, token=tok)

    assert not worker_errors, f"worker(s) failed: {worker_errors!r}"
    assert set(results) == {"first", "second"}
    assert session_ids["first"] != session_ids["second"]
    assert sorted(results.values()) == [200, 409], (
        f"exactly one restore may win; got {results!r}")

    from app.database import SessionLocal
    check = SessionLocal()
    try:
        from app.calendar_models import Appointment, AppointmentSlot
        assert check.get(Appointment, appt.id).status == "confirmed"
        final_slot = check.get(AppointmentSlot, slot.id)
        assert final_slot.status == "booked"
    finally:
        check.close()


@requires_db
def test_db_two_reschedules_race_for_one_target_slot(
        office_users_table, db, client_row, monkeypatch):
    """Two DIFFERENT appointments race onto ONE open target slot: the slot
    lock + uq_active_appointment_per_slot guarantee exactly one occupies it.
    The loser's appointment is exactly where it was, still on its old slot."""
    appt_1, old_1 = _seed_appointment(db, client_row, status="confirmed",
                                      slot_status="booked",
                                      start_utc=_future_utc(days=3, hour=14))
    appt_2, old_2 = _seed_appointment(db, client_row, status="confirmed",
                                      slot_status="booked",
                                      start_utc=_future_utc(days=3, hour=15))
    target = _seed_slot(db, client_row, start_utc=_future_utc(days=3, hour=16))
    user = _bind_office_user(db, client_row)
    tok = _token(user.auth_user_id)
    body = {"slot_id": str(target.id)}

    results, worker_errors, session_ids = _threaded_race(
        db, monkeypatch,
        paths={"one": f"/portal/appointments/{appt_1.id}/reschedule",
               "two": f"/portal/appointments/{appt_2.id}/reschedule"},
        bodies={"one": dict(body), "two": dict(body)}, token=tok)

    assert not worker_errors, f"worker(s) failed: {worker_errors!r}"
    assert session_ids["one"] != session_ids["two"]
    assert sorted(results.values()) == [200, 409], (
        f"exactly one move may claim the slot; got {results!r}")

    from app.database import SessionLocal
    check = SessionLocal()
    try:
        from app.calendar_models import Appointment, AppointmentSlot
        assert check.get(AppointmentSlot, target.id).status == "booked"
        one = check.get(Appointment, appt_1.id)
        two = check.get(Appointment, appt_2.id)
        winners = [row for row in (one, two) if row.slot_id == target.id]
        assert len(winners) == 1, "exactly ONE appointment occupies the target"
        loser = two if winners[0] is one else one
        loser_old = old_1 if loser is one else old_2
        assert loser.slot_id == loser_old.id, (
            "the loser is exactly where it was - no partial move")
        assert check.get(AppointmentSlot, loser_old.id).status == "booked", (
            "and its old slot was never freed")
    finally:
        check.close()


@requires_db
def test_db_change_time_overlapping_cancel_race(
        office_users_table, db, client_row, monkeypatch):
    """AUDIT RACE 1, the threaded OVERLAP form: Change time and Cancel race
    for one CONFIRMED appointment's row lock. Whichever order the lock
    serializes, the invariants hold: Cancel always lands (a moved
    appointment is still cancellable), the appointment ends CANCELLED, and
    NO slot remains claimed by it - if Cancel won the lock first, the stale
    Change time refused 409 and the target was never touched; if Change
    time slipped in first, the subsequent Cancel released the target it had
    just claimed. Either way: no resurrection, no lingering booking."""
    appt, old_slot = _seed_appointment(db, client_row, status="confirmed",
                                       slot_status="booked",
                                       start_utc=_future_utc(days=3, hour=10))
    target = _seed_slot(db, client_row, start_utc=_future_utc(days=3, hour=11))
    user = _bind_office_user(db, client_row)
    tok = _token(user.auth_user_id)

    results, worker_errors, session_ids = _threaded_race(
        db, monkeypatch,
        paths={"change": f"/portal/appointments/{appt.id}/reschedule",
               "cancel": f"/portal/appointments/{appt.id}/cancel"},
        bodies={"change": {"slot_id": str(target.id)}, "cancel": None},
        token=tok)

    assert not worker_errors, f"worker(s) failed: {worker_errors!r}"
    assert session_ids["change"] != session_ids["cancel"]
    assert results["cancel"] == 200, "Cancel always lands"
    assert results["change"] in (200, 409), (
        f"the move either preceded the cancel or was refused; got "
        f"{results!r}")

    from app.database import SessionLocal
    check = SessionLocal()
    try:
        from app.calendar_models import Appointment, AppointmentSlot
        final = check.get(Appointment, appt.id)
        assert final.status == "cancelled", "the cancellation is FINAL"
        assert check.get(AppointmentSlot, target.id).status == "available", (
            "the target holds no lingering claim - no resurrection")
        assert check.get(AppointmentSlot, old_slot.id).status == "available", (
            "and the vacated original is released")
        if results["change"] == 409:
            # Cancel won the lock first: the stale command moved NOTHING.
            assert final.slot_id == old_slot.id, (
                "a refused stale Change time must not have moved the row")
    finally:
        check.close()


@requires_db
def test_db_choose_another_time_overlapping_restore_race(
        office_users_table, db, client_row, monkeypatch):
    """AUDIT RACE 2, the threaded OVERLAP form: Restore Original Time and
    Choose another time race for one CANCELLED appointment's row lock.
    Exactly ONE wins (the loser's stale command finds a CONFIRMED row and
    refuses 409 not_restorable); the appointment ends CONFIRMED on exactly
    ONE slot - the winner's - and the other candidate slot stays free. A
    stale recovery is never converted into a second move."""
    appt, original = _seed_appointment(db, client_row, status="cancelled",
                                       slot_status="available",
                                       start_utc=_future_utc(days=3, hour=12))
    target = _seed_slot(db, client_row, start_utc=_future_utc(days=3, hour=13))
    user = _bind_office_user(db, client_row)
    tok = _token(user.auth_user_id)

    results, worker_errors, session_ids = _threaded_race(
        db, monkeypatch,
        paths={"restore": f"/portal/appointments/{appt.id}/restore",
               "choose": f"/portal/appointments/{appt.id}/restore-to-slot"},
        bodies={"restore": None, "choose": {"slot_id": str(target.id)}},
        token=tok)

    assert not worker_errors, f"worker(s) failed: {worker_errors!r}"
    assert session_ids["restore"] != session_ids["choose"]
    assert sorted(results.values()) == [200, 409], (
        f"exactly one recovery may win; got {results!r}")

    from app.database import SessionLocal
    check = SessionLocal()
    try:
        from app.calendar_models import Appointment, AppointmentSlot
        final = check.get(Appointment, appt.id)
        assert final.status == "confirmed", "one recovery landed"
        original_row = check.get(AppointmentSlot, original.id)
        target_row = check.get(AppointmentSlot, target.id)
        if results["restore"] == 200:
            # The audit's named outcome: Restore won - the appointment is
            # CONFIRMED on its ORIGINAL slot and the proposed target stays
            # AVAILABLE; the stale Choose-another-time moved nothing.
            assert final.slot_id == original.id
            assert original_row.status == "booked"
            assert target_row.status == "available", "no second move"
        else:
            # Choose-another-time won the lock instead: confirmed on the
            # target; the stale Restore refused and the original stays free.
            assert final.slot_id == target.id
            assert target_row.status == "booked"
            assert original_row.status == "available"
        booked = [s for s in (original_row, target_row)
                  if s.status == "booked"]
        assert len(booked) == 1, "the appointment occupies EXACTLY one slot"
    finally:
        check.close()
