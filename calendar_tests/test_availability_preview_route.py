# calendar_tests/test_availability_preview_route.py
#
# Prototype B B2 — commit candidate 2: authenticated read-only
# availability-preview route + the approved minimal B1 request-contract
# expansion (service_key optional; None = generic preview).
#
# Proves, at the REAL HTTP layer wherever transport behavior is the claim:
#   - the approved schema expansion: omitted / explicit-None service_key is
#     accepted, blank stays rejected, nonblank behavior is unchanged;
#   - every credential failure keeps the existing identical 401;
#   - tenant mismatch returns the existing 404 — including with MALFORMED
#     dates (the raw-string ordering proof) — indistinguishable from a
#     nonexistent client, with the preview owner provably never invoked;
#   - the locked date/range/selected-day 422s surface after the gates;
#   - service-key handling: missing key = generic mode with None passed to
#     B1 and the mapping owner NOT consulted; generic mode includes
#     service-reserved slots (no hidden placeholder filter); a valid
#     tenant-enabled master key translates through the mapping owner;
#     whitespace is accepted; blank/unknown/case-mismatched/admin_other/
#     tenant-disabled/direct-policy input all return the single locked 422
#     detail; no "appointment"+" request" fallback exists in the route;
#   - successful previews are STRICTLY READ-ONLY: exactly one
#     appointment_slots range SELECT, no write owner invoked, and every
#     relevant row (clients, calendar_admin_credentials, conversations,
#     messages, appointment_slots, appointments, notification_attempts)
#     is byte-unchanged — including a REAL expired-held row that is
#     reported eligible while its stored fields stay untouched.
#
# FIXTURES: shared db / client_row / conversation_row / engine from
# conftest.py UNCHANGED; http / office fixtures local to this file
# (test_admin_auth.py pattern).
#
# Run: ALLOW_DESTRUCTIVE_CALENDAR_TESTS=yes TEST_DATABASE_URL=... \
#      pytest calendar_tests/test_availability_preview_route.py -v
# The pure schema-contract tests at the top run without a database.

import sys
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pydantic
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from calendar_tests.conftest import requires_db  # noqa: E402

from app.schemas import AvailabilityPreviewRequest  # noqa: E402

UTC = ZoneInfo("UTC")
OFFICE_TZ = ZoneInfo("America/New_York")

PREVIEW_PATH = "/admin/calendar/availability-preview"

# The exact locked response contracts under test.
INVALID_DETAIL = "Invalid admin key."
NOT_FOUND_DETAIL = "Client not found."
SERVICE_KEY_DETAIL = "service_key is not available for preview"
LOCKED_DAY_STATES = {"past", "open", "full", "unavailable"}

# conftest.py sets ADMIN_API_KEY=test-admin-key for app.config; the global
# key must keep receiving 401 on the new route too.
GLOBAL_ADMIN_KEY = "test-admin-key"


def _now():
    return datetime.now(UTC)


def _today_local():
    return _now().astimezone(OFFICE_TZ).date()


def _day(days_ahead):
    return (_today_local() + timedelta(days=days_ahead)).isoformat()


# ===========================================================================
# 1. APPROVED B1 REQUEST-CONTRACT EXPANSION (pure — no database)
# ===========================================================================

def test_request_accepts_omitted_service_key():
    request = AvailabilityPreviewRequest(
        start_day=date(2026, 8, 1), end_day=date(2026, 8, 7),
    )
    assert request.service_key is None


def test_request_accepts_explicit_none_service_key():
    request = AvailabilityPreviewRequest(
        start_day=date(2026, 8, 1), end_day=date(2026, 8, 7),
        service_key=None,
    )
    assert request.service_key is None


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_request_still_rejects_blank_service_key(blank):
    with pytest.raises(pydantic.ValidationError):
        AvailabilityPreviewRequest(
            start_day=date(2026, 8, 1), end_day=date(2026, 8, 7),
            service_key=blank,
        )


def test_request_nonblank_service_key_behavior_unchanged():
    request = AvailabilityPreviewRequest(
        start_day=date(2026, 8, 1), end_day=date(2026, 8, 7),
        service_key="cleaning/checkup",
    )
    assert request.service_key == "cleaning/checkup"


def test_request_range_rules_unchanged_in_both_modes():
    # The date rules stayed untouched by the expansion: a reversed range is
    # rejected identically with and without a service key.
    with pytest.raises(pydantic.ValidationError):
        AvailabilityPreviewRequest(
            start_day=date(2026, 8, 5), end_day=date(2026, 8, 4),
        )
    with pytest.raises(pydantic.ValidationError):
        AvailabilityPreviewRequest(
            start_day=date(2026, 8, 5), end_day=date(2026, 8, 4),
            service_key="cleaning/checkup",
        )


# ===========================================================================
# Local fixtures and helpers (HTTP layer)
# ===========================================================================

def _provision(db, client, label="pytest preview tool"):
    """Insert ONE credential the approved way: only the hash is persisted.
    Returns (raw_key, credential_row); the raw key exists only in memory
    and is never placed directly inside assert expressions."""
    from app.calendar_models import CalendarAdminCredential
    from app.services.calendar_admin_auth import generate_calendar_admin_key

    raw_key, key_hash = generate_calendar_admin_key()
    credential = CalendarAdminCredential(
        id=uuid.uuid4(), client_id=client.id, key_hash=key_hash, label=label
    )
    db.add(credential)
    db.commit()
    return raw_key, credential


def _make_office(db, *, practice_name="Preview Dental", settings=None,
                 active=True):
    """A dedicated office row for tests that need their own settings."""
    from app.models import Client

    client = Client(
        id=uuid.uuid4(),
        practice_name=practice_name,
        api_key=f"key-{uuid.uuid4()}",
        active=active,
        settings=settings,
    )
    db.add(client)
    db.commit()
    return client


@pytest.fixture()
def office_b(db):
    """A SECOND office. Deliberately WITHOUT calendar settings: no mismatch
    path under test may ever load it, so its settings must never matter."""
    return _make_office(db, practice_name="Other Dental", settings=None)


@pytest.fixture()
def http(db):
    """A real FastAPI app containing the calendar router, driven over HTTP.
    Only get_db is overridden (to the shared test session); authorization
    runs FOR REAL on every request."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.routes import calendar as calendar_routes

    app = FastAPI()
    app.include_router(calendar_routes.router)
    app.dependency_overrides[calendar_routes.get_db] = lambda: db
    with TestClient(app) as client:
        yield client


@pytest.fixture()
def office_key(db, client_row):
    raw_key, _credential = _provision(db, client_row)
    return raw_key


@pytest.fixture()
def preview_spy(monkeypatch):
    """Wrap the route-bound preview builder: captures the VALIDATED request
    handed to B1, then delegates to the real owner — so every spy-using
    test still exercises the genuine B1 pathway end to end."""
    from app.routes import calendar as calendar_routes
    from app.services.availability_preview_service import (
        build_availability_preview as real_build,
    )

    captured = {}

    def spy(db_arg, client_arg, request_arg, now_arg):
        captured["service_key"] = request_arg.service_key
        captured["request"] = request_arg
        return real_build(db_arg, client_arg, request_arg, now_arg)

    monkeypatch.setattr(calendar_routes, "build_availability_preview", spy)
    return captured


def _preview(http, key, client_id, start, end, selected=None,
             service_key=None):
    params = {"client_id": str(client_id), "start_day": start,
              "end_day": end}
    if selected is not None:
        params["selected_day"] = selected
    if service_key is not None:
        params["service_key"] = service_key
    headers = {} if key is None else {"X-Admin-Key": key}
    return http.get(PREVIEW_PATH, params=params, headers=headers)


def _publish_slot(db, client, *, days_ahead=3, hour=10, service_key=None):
    """One AVAILABLE published slot at an exact office-local wall time.
    Returns (slot, local_day)."""
    from app.repositories.appointment_repository import create_slot

    local_day = _today_local() + timedelta(days=days_ahead)
    start_utc = datetime(
        local_day.year, local_day.month, local_day.day, hour, 0,
        tzinfo=OFFICE_TZ,
    ).astimezone(UTC)
    slot = create_slot(
        db, client.id, start_utc, start_utc + timedelta(minutes=45),
        service_key=service_key,
    )
    db.commit()
    return slot, local_day


# ===========================================================================
# 2. AUTHENTICATION — the existing identical 401 on the new route
# ===========================================================================

@requires_db
def test_missing_header_is_401(http, client_row):
    response = _preview(http, None, client_row.id, _day(1), _day(7))
    assert response.status_code == 401
    assert response.json()["detail"] == INVALID_DETAIL


@requires_db
@pytest.mark.parametrize("bad_key", ["", "   ", "not-a-key", "mia_cal_short"])
def test_blank_and_malformed_keys_are_401(http, client_row, bad_key):
    response = _preview(http, bad_key, client_row.id, _day(1), _day(7))
    assert response.status_code == 401
    assert response.json()["detail"] == INVALID_DETAIL


@requires_db
def test_unknown_wellformed_key_is_401(http, client_row):
    from app.services.calendar_admin_auth import generate_calendar_admin_key

    unknown_raw, _unused_hash = generate_calendar_admin_key()  # never stored
    response = _preview(http, unknown_raw, client_row.id, _day(1), _day(7))
    assert response.status_code == 401
    assert response.json()["detail"] == INVALID_DETAIL


@requires_db
def test_revoked_key_is_401(http, db, client_row):
    raw_key, credential = _provision(db, client_row, label="to-revoke")
    credential.active = False
    credential.revoked_at = _now()
    db.commit()
    response = _preview(http, raw_key, client_row.id, _day(1), _day(7))
    assert response.status_code == 401
    assert response.json()["detail"] == INVALID_DETAIL


@requires_db
def test_inactive_client_key_is_401(http, db):
    inactive = _make_office(db, practice_name="Closed Dental", active=False)
    raw_key, _credential = _provision(db, inactive)
    response = _preview(http, raw_key, inactive.id, _day(1), _day(7))
    assert response.status_code == 401
    assert response.json()["detail"] == INVALID_DETAIL


@requires_db
def test_global_admin_key_is_401(http, client_row):
    response = _preview(http, GLOBAL_ADMIN_KEY, client_row.id,
                        _day(1), _day(7))
    assert response.status_code == 401
    assert response.json()["detail"] == INVALID_DETAIL


@requires_db
def test_correct_office_credential_succeeds(http, office_key, client_row):
    response = _preview(http, office_key, client_row.id, _day(1), _day(7))
    assert response.status_code == 200
    payload = response.json()
    assert payload["client_id"] == str(client_row.id)
    assert {d["state"] for d in payload["days"]} <= LOCKED_DAY_STATES


# ===========================================================================
# 3. TENANT ISOLATION — existing 404, gates BEFORE parameter semantics
# ===========================================================================

@requires_db
def test_authenticated_foreign_client_id_returns_404(
    http, office_key, office_b,
):
    response = _preview(http, office_key, office_b.id, _day(1), _day(7))
    assert response.status_code == 404
    assert response.json()["detail"] == NOT_FOUND_DETAIL


@requires_db
def test_mismatch_is_indistinguishable_from_nonexistent_client(
    http, office_key, office_b,
):
    foreign = _preview(http, office_key, office_b.id, _day(1), _day(7))
    nonexistent = _preview(http, office_key, uuid.uuid4(), _day(1), _day(7))
    assert foreign.status_code == nonexistent.status_code == 404
    assert foreign.json() == nonexistent.json()


@requires_db
def test_foreign_client_id_with_malformed_dates_returns_404(
    http, office_key, office_b,
):
    # THE raw-string ordering proof: date semantics must never be revealed
    # to a mismatched caller, so mismatch + malformed dates is 404, not 422.
    response = _preview(http, office_key, office_b.id,
                        "2026-13-40", "not-a-date")
    assert response.status_code == 404
    assert response.json()["detail"] == NOT_FOUND_DETAIL


@requires_db
def test_foreign_tenant_never_reaches_the_preview_owner(
    http, office_key, office_b, monkeypatch,
):
    # If the tenant gate ever ran late, the trap would convert the request
    # into a 500 — the 404 below proves the preview owner was never invoked
    # and the caller-supplied foreign id was never used as a tenant.
    from app.routes import calendar as calendar_routes

    def trap(*_args, **_kwargs):
        raise AssertionError("foreign tenant reached the preview owner")

    monkeypatch.setattr(calendar_routes, "build_availability_preview", trap)
    response = _preview(http, office_key, office_b.id, _day(1), _day(7))
    assert response.status_code == 404
    assert response.json()["detail"] == NOT_FOUND_DETAIL


# ===========================================================================
# 4. DATE / RANGE / SELECTED-DAY RULES (after the gates -> 422)
# ===========================================================================

@requires_db
def test_valid_seven_day_request(http, office_key, client_row):
    response = _preview(http, office_key, client_row.id, _day(1), _day(7))
    assert response.status_code == 200
    payload = response.json()
    assert len(payload["days"]) == 7
    assert {d["state"] for d in payload["days"]} <= LOCKED_DAY_STATES
    assert payload["range_start"] == _day(1)
    assert payload["range_end"] == _day(7)


@requires_db
def test_valid_thirty_one_day_request(http, office_key, client_row):
    response = _preview(http, office_key, client_row.id, _day(0), _day(30))
    assert response.status_code == 200
    assert len(response.json()["days"]) == 31


@requires_db
def test_range_over_thirty_one_days_is_422(http, office_key, client_row):
    response = _preview(http, office_key, client_row.id, _day(0), _day(31))
    assert response.status_code == 422


@requires_db
def test_reversed_range_is_422(http, office_key, client_row):
    response = _preview(http, office_key, client_row.id, _day(5), _day(4))
    assert response.status_code == 422


@requires_db
@pytest.mark.parametrize(
    "bad_day", ["2026-13-40", "not-a-date", "2026-02-30"],
)
def test_malformed_local_date_is_422(http, office_key, client_row, bad_day):
    response = _preview(http, office_key, client_row.id, bad_day, _day(7))
    assert response.status_code == 422


@requires_db
@pytest.mark.parametrize("selected_offset", [8, 0])
def test_selected_day_outside_range_is_422(
    http, office_key, client_row, selected_offset,
):
    response = _preview(http, office_key, client_row.id, _day(1), _day(7),
                        selected=_day(selected_offset))
    assert response.status_code == 422


# ===========================================================================
# 5. SERVICE-KEY HANDLING
# ===========================================================================

@requires_db
def test_missing_service_key_is_generic_and_passes_none_to_b1(
    http, db, office_key, client_row, preview_spy, monkeypatch,
):
    # Locked generic contract: no service_key query parameter -> the B1
    # request carries None AND the mapping owner is never consulted.
    from app.routes import calendar as calendar_routes

    def mapping_trap(*_args, **_kwargs):
        raise AssertionError("mapping owner consulted in generic mode")

    monkeypatch.setattr(
        calendar_routes, "calendar_policy_value_for_master_service",
        mapping_trap,
    )
    response = _preview(http, office_key, client_row.id, _day(1), _day(7))
    assert response.status_code == 200
    assert preview_spy["service_key"] is None


@requires_db
def test_generic_mode_includes_service_reserved_slots(
    http, db, office_key, client_row,
):
    # Kevin-approved proof 7: generic mode is a REAL None filter, not a
    # placeholder value — otherwise the service-reserved slots below would
    # be silently filtered out by the service-mismatch policy rule.
    generic_slot, local_day = _publish_slot(db, client_row, hour=10)
    _publish_slot(db, client_row, hour=14, service_key="cleaning/checkup")
    _publish_slot(db, client_row, hour=15, service_key="orthodontics")

    response = _preview(
        http, office_key, client_row.id,
        local_day.isoformat(), (local_day + timedelta(days=1)).isoformat(),
        selected=local_day.isoformat(),
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["selected_day"] == local_day.isoformat()
    assert [s["local_start_time"] for s in payload["slots"]] == [
        "10:00 AM", "2:00 PM", "3:00 PM",
    ]


@requires_db
def test_valid_enabled_master_key_maps_and_filters(
    http, db, office_key, client_row, preview_spy,
):
    # "cleaning_checkup" is enabled by default for client_row; it must
    # translate to the existing policy value "cleaning/checkup" and the
    # policy owner must then admit the generic + matching slots while
    # rejecting the differently-reserved one.
    _generic, local_day = _publish_slot(db, client_row, hour=10)
    _publish_slot(db, client_row, hour=14, service_key="cleaning/checkup")
    _publish_slot(db, client_row, hour=15, service_key="orthodontics")

    response = _preview(
        http, office_key, client_row.id,
        local_day.isoformat(), (local_day + timedelta(days=1)).isoformat(),
        selected=local_day.isoformat(), service_key="cleaning_checkup",
    )
    assert response.status_code == 200
    assert preview_spy["service_key"] == "cleaning/checkup"
    assert [s["local_start_time"] for s in response.json()["slots"]] == [
        "10:00 AM", "2:00 PM",
    ]


@requires_db
def test_surrounding_whitespace_is_accepted(
    http, office_key, client_row, preview_spy,
):
    response = _preview(http, office_key, client_row.id, _day(1), _day(7),
                        service_key="  cleaning_checkup  ")
    assert response.status_code == 200
    assert preview_spy["service_key"] == "cleaning/checkup"


@requires_db
@pytest.mark.parametrize(
    "rejected_key",
    [
        "",                    # blank (supplied, never downgraded to generic)
        "   ",                 # whitespace-only
        "Cleaning_Checkup",    # case mismatch (matching is case-sensitive)
        "totally_unknown",     # unknown key
        "insurance_question",  # admin_other: enabled by default, unmapped
        "cleaning/checkup",    # direct internal policy value — not accepted
    ],
)
def test_rejected_service_keys_return_the_single_locked_422(
    http, office_key, client_row, rejected_key,
):
    response = _preview(http, office_key, client_row.id, _day(1), _day(7),
                        service_key=rejected_key)
    assert response.status_code == 422
    assert response.json()["detail"] == SERVICE_KEY_DETAIL


@requires_db
def test_mapped_but_tenant_disabled_key_is_422(http, db):
    braces_office = _make_office(db, practice_name="Braces Only Dental",
                                 settings={
                                     "timezone": "America/New_York",
                                     "enabled_services": ["braces"],
                                 })
    raw_key, _credential = _provision(db, braces_office)
    response = _preview(http, raw_key, braces_office.id, _day(1), _day(7),
                        service_key="cleaning_checkup")
    assert response.status_code == 422
    assert response.json()["detail"] == SERVICE_KEY_DETAIL


@requires_db
def test_no_appointment_request_fallback_exists(http, office_key, client_row):
    # Static proof: the route module never carries the chat fallback string
    # (checked against the REAL on-disk source)...
    from app.routes import calendar as calendar_module

    source = Path(calendar_module.__file__).read_text(encoding="utf-8")
    assert ("appointment" + " request") not in source
    # ...and behavioral proof: an unknown key is REJECTED, never silently
    # bucketed into a generic preview.
    response = _preview(http, office_key, client_row.id, _day(1), _day(7),
                        service_key="totally_unknown")
    assert response.status_code == 422


# ===========================================================================
# 6. STRICTLY READ-ONLY BEHAVIOR
# ===========================================================================

def _row_snapshot(db, spec):
    """Refresh every row from the DATABASE and capture the fields a
    read-only endpoint must leave byte-untouched."""
    db.expire_all()
    snapshot = []
    for row, fields in spec:
        db.refresh(row)
        snapshot.append(tuple(getattr(row, field) for field in fields))
    return snapshot


@requires_db
def test_successful_preview_changes_no_rows(
    http, db, client_row, conversation_row, monkeypatch,
):
    from app.calendar_models import (
        Appointment, NotificationAttempt, SlotStatus,
    )
    from app.models import Message
    from app.repositories import appointment_repository
    from app.services import booking_service, notification_service

    raw_key, credential = _provision(db, client_row)

    # --- fixture rows across every relevant table (setup writes are the
    # test's own; the PREVIEW below must add none) ---
    message = Message(conversation_id=conversation_row.id, role="user",
                      content="hi")
    db.add(message)

    open_slot, local_day = _publish_slot(db, client_row, hour=10)
    reserved_slot, _ = _publish_slot(db, client_row, hour=14,
                                     service_key="cleaning/checkup")
    booked_slot, _ = _publish_slot(db, client_row, days_ahead=4, hour=10)
    booked_slot.status = SlotStatus.BOOKED
    appointment = Appointment(
        client_id=client_row.id,
        slot_id=booked_slot.id,
        conversation_id=conversation_row.id,
        patient_name="Kevin Alvarado",
        patient_phone="516-555-1234",
        start_datetime=booked_slot.start_datetime,
        end_datetime=booked_slot.end_datetime,
    )
    db.add(appointment)
    db.flush()
    attempt = NotificationAttempt(
        appointment_id=appointment.id, channel="office_sms",
        status="sent", resolved_at=_now(),
    )
    db.add(attempt)
    db.commit()

    spec = [
        (client_row, ("practice_name", "api_key", "active", "settings")),
        (credential, ("client_id", "key_hash", "label", "active",
                      "revoked_at")),
        (conversation_row, ("booking_state", "lead_name", "lead_phone",
                            "lead_reason", "is_lead", "final_closed")),
        (message, ("conversation_id", "role", "content", "created_at")),
        (open_slot, ("status", "held_until", "held_by_conversation_id",
                     "start_datetime", "end_datetime", "service_key",
                     "provider_name", "created_at")),
        (reserved_slot, ("status", "held_until", "held_by_conversation_id",
                         "start_datetime", "end_datetime", "service_key",
                         "provider_name", "created_at")),
        (booked_slot, ("status", "held_until", "held_by_conversation_id",
                       "start_datetime", "end_datetime", "service_key",
                       "provider_name", "created_at")),
        (appointment, ("status", "confirmed_at", "start_datetime",
                       "end_datetime", "notify_error", "office_sms_sent",
                       "office_email_sent", "patient_sms_sent",
                       "created_at", "updated_at")),
        (attempt, ("appointment_id", "channel", "status", "created_at",
                   "resolved_at")),
    ]
    before = _row_snapshot(db, spec)

    # --- arm the traps AFTER setup: exactly one slots range SELECT, and no
    # write/hold/notification owner may run during the preview ---
    real_list = appointment_repository.list_slots_between
    range_selects = []

    def counting_list(*args, **kwargs):
        range_selects.append(args)
        return real_list(*args, **kwargs)

    monkeypatch.setattr(appointment_repository, "list_slots_between",
                        counting_list)

    def trap(name):
        def _trap(*_args, **_kwargs):
            raise AssertionError(f"read-only preview invoked {name}")
        return _trap

    monkeypatch.setattr(booking_service, "finalize_booking",
                        trap("booking_service.finalize_booking"))
    monkeypatch.setattr(booking_service, "confirm_appointment",
                        trap("booking_service.confirm_appointment"))
    monkeypatch.setattr(booking_service, "cancel_appointment",
                        trap("booking_service.cancel_appointment"))
    monkeypatch.setattr(notification_service, "send_booking_notifications",
                        trap("notification_service.send_booking_notifications"))

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.routes import calendar as calendar_routes

    app = FastAPI()
    app.include_router(calendar_routes.router)
    app.dependency_overrides[calendar_routes.get_db] = lambda: db
    with TestClient(app) as http_client:
        response = http_client.get(PREVIEW_PATH, params={
            "client_id": str(client_row.id),
            "start_day": local_day.isoformat(),
            "end_day": (local_day + timedelta(days=2)).isoformat(),
            "selected_day": local_day.isoformat(),
        }, headers={"X-Admin-Key": raw_key})

    assert response.status_code == 200
    assert [s["local_start_time"] for s in response.json()["slots"]] == [
        "10:00 AM", "2:00 PM",
    ]
    # Exactly ONE appointment_slots range SELECT (brief: query boundary).
    assert len(range_selects) == 1
    # No pending session mutation of any kind after the request.
    assert not db.new
    assert not db.deleted
    assert not db.dirty
    # Every relevant row byte-unchanged in the DATABASE.
    after = _row_snapshot(db, spec)
    assert after == before


@requires_db
def test_expired_held_row_is_eligible_and_left_unchanged(
    http, db, client_row, conversation_row, office_key,
):
    from app.calendar_models import SlotStatus
    from app.services.calendar_settings_service import ensure_utc

    slot, local_day = _publish_slot(db, client_row, hour=11)
    expired_at = _now() - timedelta(minutes=10)
    slot.status = SlotStatus.HELD
    slot.held_until = expired_at
    slot.held_by_conversation_id = conversation_row.id
    db.commit()

    response = _preview(
        http, office_key, client_row.id,
        local_day.isoformat(), (local_day + timedelta(days=1)).isoformat(),
        selected=local_day.isoformat(),
    )
    assert response.status_code == 200
    payload = response.json()
    day_states = {d["local_date"]: d["state"] for d in payload["days"]}
    # Lazy-reclaim INTERPRETATION: the expired hold reads as bookable...
    assert day_states[local_day.isoformat()] == "open"
    assert [s["local_start_time"] for s in payload["slots"]] == ["11:00 AM"]

    # ...while the STORED row remains held and byte-unchanged (read-only).
    db.expire_all()
    db.refresh(slot)
    assert slot.status == SlotStatus.HELD
    assert ensure_utc(slot.held_until) == expired_at
    assert slot.held_by_conversation_id == conversation_row.id


@requires_db
def test_booking_disabled_tenant_still_gets_informational_preview(http, db):
    paused = _make_office(db, practice_name="Paused Dental", settings={
        "timezone": "America/New_York",
        "calendar": {"booking_enabled": False},
    })
    raw_key, _credential = _provision(db, paused)
    response = _preview(http, raw_key, paused.id, _day(1), _day(7))
    assert response.status_code == 200
    payload = response.json()
    assert payload["booking_enabled"] is False
    assert len(payload["days"]) == 7
    # Informational only: the tenant setting itself is never altered.
    db.expire_all()
    db.refresh(paused)
    assert paused.settings["calendar"]["booking_enabled"] is False
