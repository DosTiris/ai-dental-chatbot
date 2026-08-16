# calendar_tests/test_appointment_internal_note.py
#
# PHASE 3A Slice 4B1 - APPOINTMENT INTERNAL NOTES: behavioral proof of the
# storage/API contract against real PostgreSQL.
#
# WHAT IS PROVEN HERE
#   1. Normalization has ONE owner and one behavior (blank -> NULL, outer
#      trim only, inner newlines preserved, 2000 hard limit, no silent
#      truncation).
#   2. Staff booking with a note is ATOMIC: the appointment and its note are
#      one transaction; an over-limit note creates NOTHING; booking without
#      a note behaves exactly as before; the strict request model still
#      rejects every server-owned key; no notification is ever sent.
#   3. The edit endpoint replaces/clears under the tenant lock, changes
#      NOTHING else on the row, uses the existing not-found convention for
#      cross-tenant and nonexistent ids alike, works for any source and any
#      status (including cancelled), and sends no notification.
#   4. The portal projection carries the note; the OFFICE-ONLY boundary
#      holds: notification builders and snapshots never contain it, and a
#      repository-wide containment audit pins which modules may name it.
#
# Conventions reused verbatim from calendar_tests/test_portal_staff_booking_
# route.py: direct route-function invocation, PortalIdentity construction,
# notification channel traps, independent-session verification of committed
# state, and the TestClient-over-app.main group with the ONE
# require_portal_identity override.

import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from calendar_tests.conftest import requires_db  # noqa: E402

pytestmark = requires_db

UTC = timezone.utc

# The exact approved portal appointment field set AFTER the deliberate 4B1
# amendment (kept in lockstep with test_portal_appointments.py).
APPROVED_FIELDS = {
    "appointment_id", "patient_name", "patient_phone", "patient_email",
    "new_or_returning", "reason", "urgency", "start_datetime",
    "end_datetime", "status", "confirmed_at", "source",
    "notification_outcome", "internal_note",
}

# A marker that could never occur by accident: if it shows up in any
# patient-facing or notification output, the office-only boundary is broken.
NOTE_MARKER = "OFFICE-ONLY-NOTE-7f3a1c"


# ---------------------------------------------------------------------------
# Shared helpers (the staff-booking route test conventions)
# ---------------------------------------------------------------------------

def _now():
    return datetime.now(UTC)


def _make_slot(db, client, hours_from_now=48.0):
    from app.repositories.appointment_repository import create_slot
    start = _now() + timedelta(hours=hours_from_now)
    slot = create_slot(db, client.id, start, start + timedelta(minutes=45))
    db.commit()
    return slot


def _identity(client):
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


def _book(db, client, slot_id, **overrides):
    from app.routes.portal_staff_booking import (
        StaffBookingRequest, portal_staff_book_slot,
    )
    fields = dict(patient_name="Kevin Alvarado",
                  patient_phone="516-555-1234")
    fields.update(overrides)
    return portal_staff_book_slot(
        slot_id=slot_id, body=StaffBookingRequest(**fields),
        identity=_identity(client), db=db,
    )


def _put_note(db, client, appointment_id, **body_fields):
    """Invoke the note route function directly (the established pattern).
    body_fields absent = the strict model's default (null = clear)."""
    from app.routes.portal_appointment_actions import (
        InternalNoteUpdateRequest, portal_set_internal_note,
    )
    return portal_set_internal_note(
        appointment_id=appointment_id,
        payload=InternalNoteUpdateRequest(**body_fields),
        identity=_identity(client), db=db,
    )


def _committed_appointment(appointment_id):
    """The committed row through an INDEPENDENT session (the Option 2
    pattern): only durable state is ever judged."""
    from app.calendar_models import Appointment
    from app.database import SessionLocal
    verify = SessionLocal()
    try:
        return (
            verify.query(Appointment)
            .filter(Appointment.id == appointment_id)
            .first()
        )
    finally:
        verify.close()


def _slot_appointments(db, slot_id):
    from app.calendar_models import Appointment
    from app.database import SessionLocal
    verify = SessionLocal()
    try:
        return (
            verify.query(Appointment)
            .filter(Appointment.slot_id == slot_id)
            .all()
        )
    finally:
        verify.close()


def _trap_notification_channels(monkeypatch):
    from app.services import notification_service
    calls = {"sms": 0, "email": 0}

    def sms_trap(*args, **kwargs):
        calls["sms"] += 1
        raise AssertionError("internal-note work invoked _send_sms")

    def email_trap(*args, **kwargs):
        calls["email"] += 1
        raise AssertionError("internal-note work invoked _send_email")

    monkeypatch.setattr(notification_service, "_send_sms", sms_trap)
    monkeypatch.setattr(notification_service, "_send_email", email_trap)
    return calls


def _second_client(db):
    """A second, unrelated office (tenant B) for cross-tenant proofs."""
    from app.models import Client
    other = Client(
        id=uuid.uuid4(),
        practice_name="Other Dental",
        api_key=f"key-{uuid.uuid4()}",
        active=True,
        settings={"timezone": "America/New_York",
                  "calendar": {"booking_enabled": True}},
        notification_email=None,
        notification_phone=None,
    )
    db.add(other)
    db.commit()
    return other


def _booked_appointment(db, client, note=None):
    """One committed staff-booked appointment (optionally with a note),
    returning the committed row from an independent session."""
    from fastapi import HTTPException  # noqa: F401  (documented import site)
    overrides = {} if note is None else {"internal_note": note}
    slot = _make_slot(db, client)
    view = _book(db, client, slot.id, **overrides)
    return _committed_appointment(view.appointment_id), slot


# ---------------------------------------------------------------------------
# 1. Normalization: the single owner's exact behavior (no DB needed)
# ---------------------------------------------------------------------------

def test_normalize_none_empty_and_whitespace_are_null():
    from app.services.appointment_note_service import normalize_internal_note
    assert normalize_internal_note(None) is None
    assert normalize_internal_note("") is None
    assert normalize_internal_note("   \t \r\n ") is None


def test_normalize_trims_outer_and_preserves_inner_newlines():
    from app.services.appointment_note_service import normalize_internal_note
    assert normalize_internal_note("  line one\nline two  ") == (
        "line one\nline two")


def test_normalize_2000_ok_2001_refused_never_truncated():
    from app.services.appointment_note_service import (
        INVALID_INTERNAL_NOTE_DETAIL, normalize_internal_note,
    )
    assert normalize_internal_note("x" * 2000) == "x" * 2000
    # Padding trims down to exactly the limit: allowed.
    assert normalize_internal_note("  " + "y" * 2000 + "  ") == "y" * 2000
    with pytest.raises(ValueError) as excinfo:
        normalize_internal_note("z" * 2001)
    assert str(excinfo.value) == INVALID_INTERNAL_NOTE_DETAIL


# ---------------------------------------------------------------------------
# 2. Staff booking: atomic booking-time note
# ---------------------------------------------------------------------------

def test_booking_without_note_behaves_exactly_as_before(db, client_row,
                                                        monkeypatch):
    calls = _trap_notification_channels(monkeypatch)
    slot = _make_slot(db, client_row)
    view = _book(db, client_row, slot.id)          # no internal_note field
    row = _committed_appointment(view.appointment_id)
    assert row.internal_note is None
    assert view.internal_note is None
    assert row.source == "portal_staff" and row.status == "confirmed"
    assert row.confirmed_at is None
    assert calls == {"sms": 0, "email": 0}


def test_booking_with_note_is_one_atomic_transaction(db, client_row,
                                                     monkeypatch):
    calls = _trap_notification_channels(monkeypatch)
    slot = _make_slot(db, client_row)
    view = _book(db, client_row, slot.id,
                 internal_note="  Prefers morning calls.\nGate code 4411  ")
    row = _committed_appointment(view.appointment_id)
    # One committed row carrying the NORMALIZED note - created together.
    assert row.internal_note == "Prefers morning calls.\nGate code 4411"
    assert view.internal_note == "Prefers morning calls.\nGate code 4411"
    assert row.client_id == client_row.id
    rows = _slot_appointments(db, slot.id)
    assert len(rows) == 1 and rows[0].id == row.id
    # The note changed no booking semantics.
    assert (row.status, row.source, row.urgency, row.confirmed_at) == (
        "confirmed", "portal_staff", "routine", None)
    assert calls == {"sms": 0, "email": 0}
    assert (row.office_sms_sent, row.office_email_sent,
            row.patient_sms_sent, row.notify_error) == (
        False, False, False, None)


def test_blank_booking_time_note_becomes_null(db, client_row):
    slot = _make_slot(db, client_row)
    view = _book(db, client_row, slot.id, internal_note="   \n  ")
    assert _committed_appointment(view.appointment_id).internal_note is None


def test_over_limit_booking_note_is_422_and_creates_nothing(db, client_row):
    from fastapi import HTTPException
    from app.calendar_models import SlotStatus
    from app.services.appointment_note_service import (
        INVALID_INTERNAL_NOTE_DETAIL,
    )
    slot = _make_slot(db, client_row)
    with pytest.raises(HTTPException) as excinfo:
        _book(db, client_row, slot.id, internal_note="x" * 2001)
    assert excinfo.value.status_code == 422
    assert excinfo.value.detail == INVALID_INTERNAL_NOTE_DETAIL
    # Atomicity: NOTHING persisted - no appointment, slot untouched.
    db.rollback()
    db.refresh(slot)
    assert slot.status == SlotStatus.AVAILABLE
    assert _slot_appointments(db, slot.id) == []


@pytest.mark.parametrize("field,value", [
    ("client_id", str(uuid.uuid4())),
    ("urgency", "urgent"),
    ("status", "confirmed"),
    ("source", "mia_widget"),
    ("provider_name", "Dr. X"),
    ("service_key", "implant"),
    ("start_datetime", "2026-09-01T14:00:00Z"),
    ("conversation_id", str(uuid.uuid4())),
    ("confirmed_at", "2026-09-01T14:00:00Z"),
])
def test_booking_request_still_rejects_server_owned_keys(field, value):
    """The strict model with the new optional note still rejects EVERY
    server-owned key (the v1.0.1 F1 guarantee, re-proven post-4B1)."""
    import pydantic
    from app.routes.portal_staff_booking import StaffBookingRequest
    with pytest.raises(pydantic.ValidationError):
        StaffBookingRequest(patient_name="K", patient_phone="5",
                            internal_note="fine", **{field: value})


# ---------------------------------------------------------------------------
# 3. The edit/clear endpoint
# ---------------------------------------------------------------------------

def test_edit_replaces_note_and_returns_the_portal_view(db, client_row,
                                                        monkeypatch):
    calls = _trap_notification_channels(monkeypatch)
    row, _ = _booked_appointment(db, client_row)
    view = _put_note(db, client_row, row.id,
                     internal_note="  Ask about x-rays\nfrom 2024  ")
    assert view.internal_note == "Ask about x-rays\nfrom 2024"
    assert _committed_appointment(row.id).internal_note == (
        "Ask about x-rays\nfrom 2024")
    assert calls == {"sms": 0, "email": 0}


def test_edit_overwrites_an_existing_note(db, client_row):
    row, _ = _booked_appointment(db, client_row, note="first note")
    _put_note(db, client_row, row.id, internal_note="second note")
    assert _committed_appointment(row.id).internal_note == "second note"


def test_explicit_null_and_whitespace_clear(db, client_row):
    """v1.0.1 F1: clearing is EXPLICIT - a null value or a blank string.
    (An absent field refuses; see the dedicated F1 tests below.)"""
    for clearing_body in ({"internal_note": None},
                          {"internal_note": "   \n "}):
        row, _ = _booked_appointment(db, client_row, note="to be cleared")
        assert _committed_appointment(row.id).internal_note == "to be cleared"
        _put_note(db, client_row, row.id, **clearing_body)
        assert _committed_appointment(row.id).internal_note is None, (
            clearing_body)


def test_over_limit_edit_is_422_and_mutates_nothing(db, client_row):
    from fastapi import HTTPException
    from app.services.appointment_note_service import (
        INVALID_INTERNAL_NOTE_DETAIL,
    )
    row, _ = _booked_appointment(db, client_row, note="kept intact")
    with pytest.raises(HTTPException) as excinfo:
        _put_note(db, client_row, row.id, internal_note="x" * 2001)
    assert excinfo.value.status_code == 422
    assert excinfo.value.detail == INVALID_INTERNAL_NOTE_DETAIL
    assert _committed_appointment(row.id).internal_note == "kept intact"


def test_edit_request_model_is_strict(db, client_row):
    import pydantic
    from app.routes.portal_appointment_actions import (
        InternalNoteUpdateRequest,
    )
    for smuggled in ({"client_id": str(uuid.uuid4())},
                     {"status": "cancelled"},
                     {"source": "mia_widget"},
                     {"slot_id": str(uuid.uuid4())}):
        with pytest.raises(pydantic.ValidationError):
            InternalNoteUpdateRequest(internal_note="ok", **smuggled)


def test_cross_tenant_edit_is_indistinguishable_and_harmless(db, client_row):
    from fastapi import HTTPException
    other = _second_client(db)
    row, _ = _booked_appointment(db, other, note="tenant B's private note")
    # Tenant A's identity attacks tenant B's appointment id.
    with pytest.raises(HTTPException) as excinfo:
        _put_note(db, client_row, row.id, internal_note="stolen")
    assert excinfo.value.status_code == 404
    assert excinfo.value.detail == "Appointment not found."
    assert _committed_appointment(row.id).internal_note == (
        "tenant B's private note")


def test_nonexistent_appointment_uses_the_same_not_found(db, client_row):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as excinfo:
        _put_note(db, client_row, uuid.uuid4(), internal_note="ghost")
    assert excinfo.value.status_code == 404
    assert excinfo.value.detail == "Appointment not found."


def test_edit_changes_nothing_but_the_note(db, client_row):
    from app.calendar_models import SlotStatus
    row, slot = _booked_appointment(db, client_row)
    before = (row.status, row.slot_id, row.source, row.confirmed_at,
              row.start_datetime, row.end_datetime, row.urgency, row.reason,
              row.patient_name, row.patient_phone, row.office_sms_sent,
              row.office_email_sent, row.patient_sms_sent, row.notify_error)
    _put_note(db, client_row, row.id, internal_note="only this changes")
    after_row = _committed_appointment(row.id)
    after = (after_row.status, after_row.slot_id, after_row.source,
             after_row.confirmed_at, after_row.start_datetime,
             after_row.end_datetime, after_row.urgency, after_row.reason,
             after_row.patient_name, after_row.patient_phone,
             after_row.office_sms_sent, after_row.office_email_sent,
             after_row.patient_sms_sent, after_row.notify_error)
    assert after == before
    db.rollback()
    db.refresh(slot)
    assert slot.status == SlotStatus.BOOKED   # inventory untouched


def test_mia_created_appointment_can_carry_an_office_note(db, client_row):
    """The note is an OFFICE capability, not a portal_staff-only one."""
    from app.calendar_models import AppointmentStatus
    from app.repositories.appointment_repository import (
        create_appointment_from_slot, get_slot_for_update,
    )
    slot = _make_slot(db, client_row)
    locked = get_slot_for_update(db, client_row.id, slot.id)
    appointment = create_appointment_from_slot(
        db, slot=locked, conversation_id=None,
        patient_name="Widget Patient", patient_phone="516-555-0000",
        patient_email=None, new_or_returning=None, reason="cleaning",
        urgency="routine", status=AppointmentStatus.CONFIRMED,
        source="mia_widget",
    )
    db.commit()
    view = _put_note(db, client_row, appointment.id,
                     internal_note="insurance card on file")
    assert view.source == "mia_widget"
    assert _committed_appointment(appointment.id).internal_note == (
        "insurance card on file")


def test_cancelled_appointment_retains_and_updates_its_note(db, client_row):
    from app.services import booking_service
    row, _ = _booked_appointment(db, client_row, note="pre-cancel note")
    cancel = booking_service.cancel_appointment(db, client_row.id, row.id)
    assert cancel.success
    after_cancel = _committed_appointment(row.id)
    assert after_cancel.status == "cancelled"
    assert after_cancel.internal_note == "pre-cancel note"   # retained
    _put_note(db, client_row, row.id, internal_note="post-cancel note")
    assert _committed_appointment(row.id).internal_note == "post-cancel note"


# ---------------------------------------------------------------------------
# 4. Full-wire HTTP (TestClient over app.main, the ONE auth owner)
# ---------------------------------------------------------------------------

@contextmanager
def _authenticated_app(client_row):
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


def test_http_note_edit_requires_authentication(db, client_row):
    from app.main import app
    from app.routes.portal import require_portal_identity
    assert require_portal_identity not in app.dependency_overrides
    row, _ = _booked_appointment(db, client_row, note="private")
    response = _http(app).put(
        f"/portal/appointments/{row.id}/internal-note",
        json={"internal_note": "attacker"})
    assert response.status_code == 401
    assert _committed_appointment(row.id).internal_note == "private"


def test_http_note_edit_round_trips_with_the_exact_approved_body(
        db, client_row):
    row, _ = _booked_appointment(db, client_row)
    with _authenticated_app(client_row) as app:
        response = _http(app).put(
            f"/portal/appointments/{row.id}/internal-note",
            json={"internal_note": "  wire note  "})
    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == APPROVED_FIELDS
    assert body["internal_note"] == "wire note"
    assert _committed_appointment(row.id).internal_note == "wire note"


def test_http_booking_with_note_round_trips(db, client_row):
    slot = _make_slot(db, client_row)
    with _authenticated_app(client_row) as app:
        response = _http(app).post(
            f"/portal/schedule/slots/{slot.id}/book",
            json={"patient_name": "Kevin Alvarado",
                  "patient_phone": "516-555-1234",
                  "internal_note": "booked over the phone"})
    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == APPROVED_FIELDS
    assert body["internal_note"] == "booked over the phone"


def test_http_portal_read_returns_the_note(db, client_row):
    row, _ = _booked_appointment(db, client_row, note="visible to office")
    with _authenticated_app(client_row) as app:
        response = _http(app).get("/portal/appointments")
    assert response.status_code == 200
    members = response.json()["appointments"]
    match = [m for m in members if m["appointment_id"] == str(row.id)]
    assert len(match) == 1
    assert match[0]["internal_note"] == "visible to office"


# ---------------------------------------------------------------------------
# 5. THE OFFICE-ONLY LEAK BOUNDARY
# ---------------------------------------------------------------------------

def test_notification_builders_never_contain_the_note(db, client_row):
    """Every notification builder rendered against an appointment carrying
    the marker note must be marker-free - office SMS, office email, the
    future-only patient SMS, and the snapshot the send path consumes."""
    from app.services import notification_service
    from app.services.calendar_settings_service import load_calendar_settings
    row, _ = _booked_appointment(db, client_row, note=NOTE_MARKER)
    settings = load_calendar_settings(client_row)

    office_sms = notification_service.build_office_sms(
        row, "Test Dental", settings)
    office_email = notification_service.build_office_email_body(
        row, "Test Dental", settings)
    patient_sms = notification_service.build_patient_sms(
        row, "Test Dental", settings)
    snapshot = notification_service.build_notification_snapshot(
        client_row, row, settings)

    for rendered in (office_sms, office_email, patient_sms):
        assert NOTE_MARKER not in rendered
    assert NOTE_MARKER not in repr(snapshot)


def test_office_only_containment_audit():
    """Repository-wide containment: 'internal_note' may be NAMED only by its
    approved owners. Notification, chat, conversation, public calendar,
    admin/export, and widget code must never reference it - so a future
    generic serialization or template edit cannot silently pull it in.
    (Behavioral leak coverage lives above; this audit pins the boundary.)"""
    root = Path(__file__).resolve().parents[1]
    approved = {
        root / "app" / "calendar_models.py",
        root / "app" / "services" / "appointment_note_service.py",
        root / "app" / "services" / "booking_service.py",
        root / "app" / "repositories" / "appointment_repository.py",
        root / "app" / "routes" / "portal_appointments.py",
        root / "app" / "routes" / "portal_staff_booking.py",
        root / "app" / "routes" / "portal_appointment_actions.py",
    }
    offenders = []
    for path in sorted((root / "app").rglob("*.py")):
        if path in approved:
            continue
        if "internal_note" in path.read_text(encoding="utf-8",
                                             errors="replace"):
            offenders.append(str(path.relative_to(root)))
    assert offenders == [], (
        "internal_note escaped its approved owners: " + ", ".join(offenders))
    # The boundary modules the contract names must be clean, explicitly.
    for must_be_clean in ("services/notification_service.py",
                          "routes/chat.py",
                          "services/booking_conversation.py",
                          "routes/calendar.py",
                          "routes/admin.py"):
        text = (root / "app" / Path(must_be_clean)).read_text(
            encoding="utf-8", errors="replace")
        assert "internal_note" not in text, must_be_clean


# ---------------------------------------------------------------------------
# v1.0.1 F1: an ACCIDENTALLY EMPTY PUT must never erase a stored note
# ---------------------------------------------------------------------------

def test_f1_empty_body_is_rejected_by_the_strict_model():
    """internal_note is REQUIRED-BUT-NULLABLE: {} fails validation at the
    model itself (the transport boundary), before any handler runs."""
    import pydantic
    from app.routes.portal_appointment_actions import (
        InternalNoteUpdateRequest,
    )
    with pytest.raises(pydantic.ValidationError):
        InternalNoteUpdateRequest()
    # The three legitimate shapes all construct.
    assert InternalNoteUpdateRequest(internal_note="text").internal_note == (
        "text")
    assert InternalNoteUpdateRequest(internal_note=None).internal_note is None
    assert InternalNoteUpdateRequest(
        internal_note="   ").internal_note == "   "


def test_f1_http_empty_put_is_422_and_the_note_survives(db, client_row):
    """Real wire: PUT {} is a 422 and the stored note remains value-identical
    - erasing office data must be a stated intent, never a side effect."""
    row, _ = _booked_appointment(db, client_row, note="must survive {}")
    with _authenticated_app(client_row) as app:
        response = _http(app).put(
            f"/portal/appointments/{row.id}/internal-note", json={})
    assert response.status_code == 422
    assert _committed_appointment(row.id).internal_note == "must survive {}"


def test_f1_http_extra_key_is_422_and_the_note_survives(db, client_row):
    row, _ = _booked_appointment(db, client_row, note="also survives")
    with _authenticated_app(client_row) as app:
        response = _http(app).put(
            f"/portal/appointments/{row.id}/internal-note",
            json={"internal_note": "new", "client_id": str(uuid.uuid4())})
    assert response.status_code == 422
    assert _committed_appointment(row.id).internal_note == "also survives"


def test_f1_http_explicit_null_still_clears(db, client_row):
    """The correction narrows ABSENT, not null: an explicit null remains the
    documented clear operation, end-to-end."""
    row, _ = _booked_appointment(db, client_row, note="clear me explicitly")
    with _authenticated_app(client_row) as app:
        response = _http(app).put(
            f"/portal/appointments/{row.id}/internal-note",
            json={"internal_note": None})
    assert response.status_code == 200
    assert response.json()["internal_note"] is None
    assert _committed_appointment(row.id).internal_note is None
