# calendar_tests/test_portal_staff_booking_route.py
#
# Route-focused regression tests for PHASE 3A SLICE 2: the authenticated
# office-portal staff-booking endpoint
#   POST /portal/schedule/slots/{slot_id}/book
# (app/routes/portal_staff_booking.py).
#
# These tests prove the ROUTE's responsibilities only - transport wiring,
# tenant binding, strict-body rejection, the injected booking instant, the
# BookingResult -> HTTP mapping, the fail-closed success gate, the shared
# leak-safe response projection, and silence on every notification channel.
# The booking rule itself (lock, refusals, unique-index race arbiter) is the
# frozen Slice 1 owner, already proven by calendar_tests/test_booking_db.py;
# nothing there is replaced or weakened here.
#
# Pattern: route functions are invoked DIRECTLY with the session and the
# authenticated identity supplied (the established Patch 5 convention used
# by the admin-route tests in test_booking_db.py). The authentication owner
# itself is exercised separately through require_portal_identity, and the
# endpoint's declaration of that dependency is proven by inspecting the
# registered route - so the wiring, not just the function body, is pinned.
#
# v1.0.1 (audit correction F2) adds a REAL-HTTP section at the end of this
# file: the actual app.main application is exercised with FastAPI's
# TestClient (the tests/portal/test_portal_config_endpoint.py convention),
# with authentication satisfied by overriding the ONE frozen
# require_portal_identity callable - never a second authentication owner -
# and the 401 case running with NO override, through the real owner.
#
# Requires a throwaway Postgres via TEST_DATABASE_URL - see conftest.py.

import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from calendar_tests.conftest import make_conversation, requires_db

UTC = ZoneInfo("UTC")
pytestmark = requires_db

ROUTE_PATH = "/portal/schedule/slots/{slot_id}/book"


def _now():
    return datetime.now(UTC)


def _settings(client):
    from app.services.calendar_settings_service import load_calendar_settings
    return load_calendar_settings(client)


def _make_slot(db, client, hours_from_now=48.0):
    """A published AVAILABLE slot relative to the real clock (the route
    injects real now_utc, so route tests anchor to the real clock too)."""
    from app.repositories.appointment_repository import create_slot
    start = _now() + timedelta(hours=hours_from_now)
    slot = create_slot(db, client.id, start, start + timedelta(minutes=45))
    db.commit()
    return slot


def _identity(client):
    """A verified-shape PortalIdentity bound to the given office. The
    OfficeUser instance is deliberately NEVER persisted: office_users lives
    on PortalBase (migration 007 is its sole creation authority) and the
    route consumes only identity.client - exactly what
    portal_auth.resolve_office_identity would hand it."""
    from app.portal_models import OfficeUser, OfficeUserRole
    from app.services.portal_auth import PortalIdentity
    office_user = OfficeUser(
        id=uuid.uuid4(),
        auth_user_id=uuid.uuid4(),
        client_id=client.id,
        role=OfficeUserRole.OFFICE_ADMIN,
        active=True,
    )
    return PortalIdentity(client=client, office_user=office_user,
                          email="staff@test.example")


def _request_body(**overrides):
    from app.routes.portal_staff_booking import StaffBookingRequest
    fields = dict(patient_name="Kevin Alvarado",
                  patient_phone="516-555-1234")
    fields.update(overrides)
    return StaffBookingRequest(**fields)


def _book(db, client, slot_id, **overrides):
    """Invoke the route function directly (the established pattern)."""
    from app.routes.portal_staff_booking import portal_staff_book_slot
    return portal_staff_book_slot(
        slot_id=slot_id, body=_request_body(**overrides),
        identity=_identity(client), db=db,
    )


def _trap_notification_channels(monkeypatch):
    """Staff booking must never message ANYONE (Slice 1 owner decision D7):
    trap both provider send functions so any invocation is counted AND
    fails loudly."""
    from app.services import notification_service
    calls = {"sms": 0, "email": 0}

    def sms_trap(*args, **kwargs):
        calls["sms"] += 1
        raise AssertionError("staff booking route invoked _send_sms")

    def email_trap(*args, **kwargs):
        calls["email"] += 1
        raise AssertionError("staff booking route invoked _send_email")

    monkeypatch.setattr(notification_service, "_send_sms", sms_trap)
    monkeypatch.setattr(notification_service, "_send_email", email_trap)
    return calls


def _slot_appointments(db, slot_id):
    """All appointment rows for one slot, read through a THIRD independent
    session so only COMMITTED state is judged (the established pattern)."""
    from app.database import SessionLocal
    from app.calendar_models import Appointment
    verify = SessionLocal()
    try:
        return (
            verify.query(Appointment)
            .filter(Appointment.slot_id == slot_id)
            .all()
        )
    finally:
        verify.close()


def _assert_untouched_available_slot(db, slot):
    """Shared mutation-free proof for every refusal against an AVAILABLE
    slot: still available, hold columns clear, zero appointment rows."""
    from app.calendar_models import SlotStatus
    db.rollback()          # discard this session's view; re-read committed
    db.refresh(slot)
    assert slot.status == SlotStatus.AVAILABLE
    assert slot.held_until is None
    assert slot.held_by_conversation_id is None
    assert _slot_appointments(db, slot.id) == []


# ---------------------------------------------------------------------------
# Wiring and authentication
# ---------------------------------------------------------------------------

def test_route_is_registered_with_portal_identity_dependency():
    """The endpoint must exist at EXACTLY the contract path/method and must
    DECLARE the frozen P2 dependencies (require_portal_identity + get_db) -
    the same callables, not copies - so authentication cannot be bypassed by
    construction and test dependency-overrides keep covering this router."""
    from app.routes import portal_staff_booking
    from app.routes.portal import get_db, require_portal_identity

    matches = [r for r in portal_staff_booking.router.routes
               if getattr(r, "path", None) == ROUTE_PATH]
    assert len(matches) == 1, f"expected exactly one route at {ROUTE_PATH}"
    route = matches[0]
    assert route.methods == {"POST"}

    dependency_calls = {d.call for d in route.dependant.dependencies}
    assert require_portal_identity in dependency_calls
    assert get_db in dependency_calls


def test_unauthenticated_request_rejected(db):
    """The declared dependency itself (the P2 owner through the P2 transport
    wiring) rejects a missing and a non-Bearer Authorization header with the
    single indistinguishable 401 - proven BEFORE any server JWT
    configuration is consulted, so this holds in every environment."""
    from fastapi import HTTPException
    from app.routes.portal import require_portal_identity
    from app.services.portal_auth import INVALID_PORTAL_CREDENTIALS_DETAIL

    with pytest.raises(HTTPException) as missing:
        require_portal_identity(authorization=None, db=db)
    assert missing.value.status_code == 401
    assert missing.value.detail == INVALID_PORTAL_CREDENTIALS_DETAIL

    with pytest.raises(HTTPException) as malformed:
        require_portal_identity(authorization="Basic not-a-bearer", db=db)
    assert malformed.value.status_code == 401
    assert malformed.value.detail == INVALID_PORTAL_CREDENTIALS_DETAIL


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------

def test_same_tenant_booking_succeeds_with_one_committed_appointment(
        db, client_row, monkeypatch):
    """The authenticated office books its own AVAILABLE future slot: 200
    view through the SHARED projection, every server-owned value fixed by
    the frozen owner, EXACTLY ONE committed appointment (independent-session
    read), the slot committed BOOKED with hold columns clear, and total
    silence on both notification channels."""
    from app.calendar_models import AppointmentStatus, SlotStatus
    from app.routes.portal_appointments import PortalAppointmentView
    from app.services.booking_service import STAFF_BOOKING_SOURCE
    from app.services.calendar_settings_service import ensure_utc

    calls = _trap_notification_channels(monkeypatch)
    slot = _make_slot(db, client_row)
    slot_start = ensure_utc(slot.start_datetime)
    slot_end = ensure_utc(slot.end_datetime)

    view = _book(db, client_row, slot.id,
                 patient_email="  ",             # blank -> normalized NULL
                 new_or_returning="new",
                 reason="implant consultation")

    # The response is the ONE shared leak-safe projection.
    assert isinstance(view, PortalAppointmentView)
    assert view.status == AppointmentStatus.CONFIRMED
    assert view.source == STAFF_BOOKING_SOURCE
    assert view.confirmed_at is None              # D5: never staff-confirmed
    assert view.patient_name == "Kevin Alvarado"
    assert view.patient_phone == "516-555-1234"
    assert view.patient_email is None
    assert view.new_or_returning == "new"
    assert view.reason == "implant consultation"
    assert view.urgency == "routine"              # server-owned (F1)
    # Times come from the LOCKED authoritative row, nothing else.
    assert view.start_datetime == slot_start
    assert view.end_datetime == slot_end
    assert view.notification_outcome == "pending"

    # EXACTLY ONE committed appointment, judged from an independent session.
    rows = _slot_appointments(db, slot.id)
    assert len(rows) == 1
    stored = rows[0]
    assert stored.id == view.appointment_id
    assert stored.client_id == client_row.id
    assert stored.conversation_id is None         # server-owned NULL
    assert stored.status == AppointmentStatus.CONFIRMED
    assert stored.source == STAFF_BOOKING_SOURCE
    assert ensure_utc(stored.start_datetime) == slot_start
    assert ensure_utc(stored.end_datetime) == slot_end

    # The slot is committed BOOKED with hold bookkeeping clear.
    db.rollback()
    db.refresh(slot)
    assert slot.status == SlotStatus.BOOKED
    assert slot.held_until is None
    assert slot.held_by_conversation_id is None

    assert calls == {"sms": 0, "email": 0}


def test_urgency_is_server_owned(db, client_row):
    """v1.0.1 audit correction F1: the request model declares NO urgency
    field, so a supplied one is an undeclared key (rejected by the strict
    model - also proven across the real HTTP boundary below), and every
    stored staff booking carries the route's server-owned "routine"."""
    import pydantic
    from app.routes.portal_staff_booking import StaffBookingRequest

    with pytest.raises(pydantic.ValidationError):
        StaffBookingRequest(patient_name="Kevin Alvarado",
                            patient_phone="516-555-1234",
                            urgency="priority")

    slot = _make_slot(db, client_row, hours_from_now=49.0)
    view = _book(db, client_row, slot.id, patient_phone="516-555-2001")
    assert view.urgency == "routine"
    rows = _slot_appointments(db, slot.id)
    assert len(rows) == 1 and rows[0].urgency == "routine"


# ---------------------------------------------------------------------------
# Not-found and tenant isolation
# ---------------------------------------------------------------------------

def test_nonexistent_slot_not_found(db, client_row):
    """An unknown slot id is a 404 with EXACTLY the shared portal wording."""
    from fastapi import HTTPException
    from app.routes.portal_staff_booking import SLOT_NOT_FOUND_DETAIL

    with pytest.raises(HTTPException) as excinfo:
        _book(db, client_row, uuid.uuid4())
    assert excinfo.value.status_code == 404
    assert excinfo.value.detail == SLOT_NOT_FOUND_DETAIL


def test_cross_tenant_slot_indistinguishable_and_untouched(db, client_row):
    """Office B, authenticated AS ITSELF, probes office A's real slot id:
    the refusal is byte-identical to the unknown-id 404 (no ownership leak),
    and office A's slot provably gains no appointment and no mutation."""
    from fastapi import HTTPException
    from app.models import Client

    slot_a = _make_slot(db, client_row)

    office_b = Client(id=uuid.uuid4(), practice_name="Other Dental",
                      api_key=f"key-{uuid.uuid4()}", active=True)
    db.add(office_b)
    db.commit()

    with pytest.raises(HTTPException) as foreign:
        _book(db, office_b, slot_a.id)
    with pytest.raises(HTTPException) as unknown:
        _book(db, office_b, uuid.uuid4())

    assert foreign.value.status_code == 404
    assert unknown.value.status_code == 404
    assert foreign.value.detail == unknown.value.detail  # indistinguishable

    _assert_untouched_available_slot(db, slot_a)


# ---------------------------------------------------------------------------
# Slot-state refusals
# ---------------------------------------------------------------------------

def test_already_booked_slot_refused(db, client_row):
    """A second staff booking of the SAME slot is a 409 with the collapsed
    slot_taken wording, and exactly the first appointment survives."""
    from fastapi import HTTPException
    from app.routes.portal_staff_booking import SLOT_TAKEN_DETAIL

    slot = _make_slot(db, client_row)
    first = _book(db, client_row, slot.id)

    with pytest.raises(HTTPException) as excinfo:
        _book(db, client_row, slot.id, patient_phone="516-555-8888")
    assert excinfo.value.status_code == 409
    assert excinfo.value.detail == SLOT_TAKEN_DETAIL

    rows = _slot_appointments(db, slot.id)
    assert len(rows) == 1 and rows[0].id == first.appointment_id


def test_actively_held_chatbot_slot_refused(db, client_row, conversation_row):
    """A slot under an ACTIVE chatbot hold is refused with the SAME 409
    wording as a booked slot (never distinguishable, never stolen), and the
    patient's hold survives byte-for-byte."""
    from fastapi import HTTPException
    from app.calendar_models import SlotStatus
    from app.routes.portal_staff_booking import SLOT_TAKEN_DETAIL
    from app.services.appointment_hold_service import place_hold
    from app.services.calendar_settings_service import ensure_utc

    slot = _make_slot(db, client_row)
    held = place_hold(db, client_row.id, slot.id, conversation_row.id,
                      settings=_settings(client_row), time_preference="any",
                      service_key=None, now_utc=_now())
    assert held.success
    db.refresh(slot)
    held_until_before = ensure_utc(slot.held_until)

    with pytest.raises(HTTPException) as excinfo:
        _book(db, client_row, slot.id)
    assert excinfo.value.status_code == 409
    assert excinfo.value.detail == SLOT_TAKEN_DETAIL

    db.rollback()
    db.refresh(slot)
    assert slot.status == SlotStatus.HELD
    assert slot.held_by_conversation_id == conversation_row.id
    assert ensure_utc(slot.held_until) == held_until_before
    assert _slot_appointments(db, slot.id) == []


def test_expired_hold_is_lazily_reclaimed_by_staff_booking(
        db, client_row, conversation_row):
    """An EXPIRED chatbot hold does not block the route: with the route's
    REAL injected now_utc past held_until, the booking succeeds and the
    stale hold bookkeeping is cleared - the one lazy-reclaim rule, unchanged
    (this also pins that the route injects a genuine current instant)."""
    from app.calendar_models import SlotStatus
    from app.services.appointment_hold_service import place_hold

    slot = _make_slot(db, client_row)
    assert place_hold(db, client_row.id, slot.id, conversation_row.id,
                      settings=_settings(client_row), time_preference="any",
                      service_key=None, now_utc=_now()).success
    slot.held_until = _now() - timedelta(minutes=1)   # force expiry
    db.commit()

    view = _book(db, client_row, slot.id)

    db.rollback()
    db.refresh(slot)
    assert slot.status == SlotStatus.BOOKED
    assert slot.held_until is None
    assert slot.held_by_conversation_id is None
    rows = _slot_appointments(db, slot.id)
    assert len(rows) == 1 and rows[0].id == view.appointment_id


def test_blocked_slot_refused(db, client_row):
    """A staff-blocked slot is a 409 with the blocked wording, mutation-free."""
    from fastapi import HTTPException
    from app.calendar_models import SlotStatus
    from app.routes.portal_staff_booking import SLOT_BLOCKED_DETAIL

    slot = _make_slot(db, client_row)
    slot.status = SlotStatus.BLOCKED
    db.commit()

    with pytest.raises(HTTPException) as excinfo:
        _book(db, client_row, slot.id)
    assert excinfo.value.status_code == 409
    assert excinfo.value.detail == SLOT_BLOCKED_DETAIL

    db.rollback()
    db.refresh(slot)
    assert slot.status == SlotStatus.BLOCKED
    assert _slot_appointments(db, slot.id) == []


def test_already_started_slot_refused(db, client_row):
    """A slot whose start time is in the past (relative to the route's REAL
    injected now) is a 409 with the started wording, mutation-free."""
    from fastapi import HTTPException
    from app.routes.portal_staff_booking import SLOT_STARTED_DETAIL

    slot = _make_slot(db, client_row, hours_from_now=-1.0)

    with pytest.raises(HTTPException) as excinfo:
        _book(db, client_row, slot.id)
    assert excinfo.value.status_code == 409
    assert excinfo.value.detail == SLOT_STARTED_DETAIL

    _assert_untouched_available_slot(db, slot)


# ---------------------------------------------------------------------------
# Patient-data validation (single owner: the shared INSERT primitive)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_fields", [
    {"patient_name": "   "},
    {"patient_phone": "   "},
], ids=["blank_name", "blank_phone"])
def test_invalid_patient_data_refused(db, client_row, monkeypatch, bad_fields):
    """Blank name/phone reaches the SINGLE validation owner and comes back
    as 422 with the route's one wording; nothing is inserted, the slot stays
    available, and nobody is messaged."""
    from fastapi import HTTPException
    from app.routes.portal_staff_booking import INVALID_PATIENT_DATA_DETAIL

    calls = _trap_notification_channels(monkeypatch)
    slot = _make_slot(db, client_row)

    with pytest.raises(HTTPException) as excinfo:
        _book(db, client_row, slot.id, **bad_fields)
    assert excinfo.value.status_code == 422
    assert excinfo.value.detail == INVALID_PATIENT_DATA_DETAIL

    _assert_untouched_available_slot(db, slot)
    assert calls == {"sms": 0, "email": 0}


# ---------------------------------------------------------------------------
# Strict transport: no client-supplied authority
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("field", [
    "client_id",
    "status",
    "source",
    "provider_id",
    "provider_name",
    "service_id",
    "service_key",
    "start_datetime",
    "end_datetime",
    "conversation_id",
    "confirmed_at",
    "urgency",
])
def test_request_model_rejects_injected_authority_field(field):
    """The strict request model (extra='forbid', the SS5-B convention)
    rejects EVERY undeclared authority field at the transport layer - which
    FastAPI surfaces as 422 - so a smuggled tenant/status/source/provider/
    service/datetime/urgency key can never be silently ignored."""
    import pydantic
    from app.routes.portal_staff_booking import StaffBookingRequest

    fields = dict(patient_name="Kevin Alvarado",
                  patient_phone="516-555-1234")
    fields[field] = "smuggled-value"
    with pytest.raises(pydantic.ValidationError):
        StaffBookingRequest(**fields)


# ---------------------------------------------------------------------------
# Fail-closed success gate (guardrails G1/G2)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fake_result_kwargs", [
    dict(success=False, reason="hold_lost"),        # foreign refusal reason
    dict(success=True, reason="already_confirmed"),  # foreign success reason
    dict(success=True, reason="ok", appointment=None),  # success w/o row
], ids=["unknown_refusal_reason", "unknown_success_reason",
        "success_without_appointment"])
def test_unexpected_booking_result_fails_closed(db, client_row, monkeypatch,
                                                fake_result_kwargs):
    """Any BookingResult outside the enumerated contract - a reason this
    route does not know, or an impossible success without an appointment -
    is a 500 with the ONE generic wording; the raw reason never reaches the
    client (guardrails G1/G2, Rule 4/16)."""
    from fastapi import HTTPException
    from app.services import booking_service
    from app.routes.portal_staff_booking import UNEXPECTED_RESULT_DETAIL

    fake = booking_service.BookingResult(**fake_result_kwargs)

    def fake_finalize(*args, **kwargs):
        return fake

    monkeypatch.setattr(booking_service, "finalize_staff_booking",
                        fake_finalize)

    with pytest.raises(HTTPException) as excinfo:
        _book(db, client_row, uuid.uuid4())
    assert excinfo.value.status_code == 500
    assert excinfo.value.detail == UNEXPECTED_RESULT_DETAIL
    # The foreign reason word is never echoed. (Guarded: for the
    # success-without-appointment case the reason is the legitimate
    # "ok", whose letters coincidentally occur inside "book" - the
    # exact-detail assertion above already pins that body.)
    if fake_result_kwargs["reason"] != "ok":
        assert fake.reason not in excinfo.value.detail


# ---------------------------------------------------------------------------
# Real HTTP integration through the actual application (audit correction F2)
# ---------------------------------------------------------------------------
# These do NOT replace the direct-invocation tests above: they prove the
# SAME contract holds across the real FastAPI boundary - routing, dependency
# resolution, strict-body validation, and JSON serialization - using the
# existing portal TestClient convention
# (tests/portal/test_portal_config_endpoint.py: TestClient over app.main).
# Authentication is satisfied by overriding the ONE frozen
# require_portal_identity callable that every portal router imports - never
# a second authentication owner - and the 401 case runs with NO override,
# through the real owner end-to-end.

from contextlib import contextmanager


@contextmanager
def _authenticated_app(client_row):
    """The real application with require_portal_identity overridden to the
    given office - the standard FastAPI test seam over the ONE frozen auth
    owner (the SAME callable object, so the override covers every router
    that imported it). The override is ALWAYS removed, even on assertion
    failure, so no later test inherits an authenticated application."""
    from app.main import app
    from app.routes.portal import require_portal_identity
    app.dependency_overrides[require_portal_identity] = (
        lambda: _identity(client_row))
    try:
        yield app
    finally:
        app.dependency_overrides.pop(require_portal_identity, None)


def _http(app):
    from fastapi.testclient import TestClient
    return TestClient(app)


def _book_url(slot_id):
    return f"/portal/schedule/slots/{slot_id}/book"


def test_http_missing_authorization_rejected(db, client_row):
    """A real POST with NO Authorization header is a 401 from the ONE frozen
    auth owner, through actual FastAPI dependency resolution - proven with
    no override installed, and mutation-free."""
    from app.main import app
    from app.routes.portal import require_portal_identity
    from app.services.portal_auth import INVALID_PORTAL_CREDENTIALS_DETAIL

    assert require_portal_identity not in app.dependency_overrides
    slot = _make_slot(db, client_row)

    response = _http(app).post(_book_url(slot.id), json={
        "patient_name": "Kevin Alvarado",
        "patient_phone": "516-555-1234",
    })
    assert response.status_code == 401
    assert response.json()["detail"] == INVALID_PORTAL_CREDENTIALS_DETAIL
    _assert_untouched_available_slot(db, slot)


def test_http_authenticated_same_tenant_booking_succeeds(db, client_row):
    """A real authenticated POST books the office's own slot through the
    full application: 200, the shared leak-safe JSON shape with every
    server-owned value fixed, exactly one committed appointment, and the
    slot committed BOOKED."""
    from app.calendar_models import AppointmentStatus, SlotStatus
    from app.services.booking_service import STAFF_BOOKING_SOURCE

    slot = _make_slot(db, client_row)
    with _authenticated_app(client_row) as app:
        response = _http(app).post(_book_url(slot.id), json={
            "patient_name": "Kevin Alvarado",
            "patient_phone": "516-555-1234",
            "new_or_returning": "new",
        })
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == AppointmentStatus.CONFIRMED
    assert body["source"] == STAFF_BOOKING_SOURCE
    assert body["urgency"] == "routine"          # server-owned (F1)
    assert body["patient_name"] == "Kevin Alvarado"
    assert body["confirmed_at"] is None          # D5

    rows = _slot_appointments(db, slot.id)
    assert len(rows) == 1
    assert str(rows[0].id) == body["appointment_id"]
    assert rows[0].conversation_id is None
    db.rollback()
    db.refresh(slot)
    assert slot.status == SlotStatus.BOOKED


@pytest.mark.parametrize("field", ["client_id", "urgency"])
def test_http_undeclared_field_rejected(db, client_row, field):
    """A real POST carrying an undeclared authority field - including the
    now-undeclared urgency (F1) - is a 422 from the strict model through
    actual FastAPI body validation, and provably books nothing."""
    slot = _make_slot(db, client_row)
    payload = {"patient_name": "Kevin Alvarado",
               "patient_phone": "516-555-1234",
               field: "smuggled-value"}
    with _authenticated_app(client_row) as app:
        response = _http(app).post(_book_url(slot.id), json=payload)
    assert response.status_code == 422
    _assert_untouched_available_slot(db, slot)


def test_http_route_mounted_exactly_once():
    """The new POST path exists on the real application EXACTLY once."""
    from app.main import app
    matches = [r for r in app.routes
               if getattr(r, "path", None) == ROUTE_PATH
               and "POST" in getattr(r, "methods", set())]
    assert len(matches) == 1
