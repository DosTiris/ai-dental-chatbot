# calendar_tests/test_admin_auth.py
#
# PATCH 5 (Senior Audit Critical #2): per-office Calendar admin credentials.
#
# Proves, at the REAL HTTP layer wherever transport behavior is the claim:
#   - every credential failure (missing/empty/malformed/unknown/revoked/
#     inactive client) returns the identical 401 — and a MISSING header is
#     401, never 422;
#   - the old global ADMIN_API_KEY receives 401 on every Calendar route;
#   - a tenant mismatch returns the identical 404 on every Calendar route,
#     is indistinguishable from a nonexistent client id, never queries the
#     foreign tenant, and — for the three write actions — provably mutates
#     NOTHING and invokes NO write owner and NO notification channel;
#   - authentication database failures fail CLOSED: the original error
#     propagates as a server failure (never converted to 401, never a
#     global-key fallback), the session is rolled back, and no route
#     business operation executes;
#   - key generation produces the approved shape and persists nothing;
#     rotation works (two active keys; revoking one leaves the other alive).
#
# FIXTURES: local to this file per the approved plan. The shared db /
# client_row / engine fixtures from conftest.py are used UNCHANGED.
#
# HTTP TESTS: fastapi.testclient.TestClient (requires the test-only httpx
# dependency — python -m pip install pytest sqlalchemy psycopg2-binary httpx).
# Every route-parametrized 401/404 request is OTHERWISE VALID (real ids,
# valid query parameters, valid body) so the asserted status can only come
# from authorization, never from an unrelated 422 (approved condition 3).
#
# SECRET HANDLING (approved condition 6): raw credentials are generated in
# memory per test and are never printed and never placed directly inside
# assert expressions — pytest's assertion introspection would otherwise echo
# them into failure output. Shape checks are computed into booleans first.

import sys
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from calendar_tests.conftest import requires_db  # noqa: E402

pytestmark = requires_db

UTC = ZoneInfo("UTC")

# The exact response contracts under test (byte-identical to pre-Patch-5).
INVALID_DETAIL = "Invalid admin key."
NOT_FOUND_DETAIL = "Client not found."

# conftest.py sets ADMIN_API_KEY=test-admin-key for app.config. Patch 5's
# whole point: this value must be REJECTED by every Calendar route.
GLOBAL_ADMIN_KEY = "test-admin-key"

# All six Calendar-admin routes, for the route-parametrized proofs.
ROUTE_CASES = [
    "create_slots",
    "list_slots",
    "block_slot",
    "list_appointments",
    "confirm_appointment",
    "cancel_appointment",
]

# The three WRITE actions for the mutation-free cross-tenant regression
# (approved final condition 1).
WRITE_CASES = ["block_slot", "cancel_appointment", "confirm_appointment"]


def _now():
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Local fixtures and helpers
# ---------------------------------------------------------------------------

def _provision(db, client, label="pytest tool"):
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


@pytest.fixture()
def office_b(db):
    """A SECOND office. Deliberately WITHOUT calendar settings: no mismatch
    path under test may ever load it, so its settings must never matter."""
    from app.models import Client

    client = Client(
        id=uuid.uuid4(),
        practice_name="Other Dental",
        api_key=f"key-{uuid.uuid4()}",
        active=True,
    )
    db.add(client)
    db.commit()
    return client


@pytest.fixture()
def http(db):
    """A real FastAPI app containing the calendar router, driven over HTTP.
    Only get_db is overridden (to the shared test session); the Patch 5
    authorization dependency runs FOR REAL on every request."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.routes import calendar as calendar_routes

    app = FastAPI()
    app.include_router(calendar_routes.router)
    app.dependency_overrides[calendar_routes.get_db] = lambda: db
    with TestClient(app) as client:
        yield client


def _publish_slot(db, client, start_utc=None):
    """One AVAILABLE published slot (repository path, like _slot_row in
    test_booking_db.py)."""
    from app.repositories.appointment_repository import create_slot

    if start_utc is None:
        start_utc = (_now() + timedelta(days=2)).replace(
            minute=0, second=0, microsecond=0
        )
    slot = create_slot(db, client.id, start_utc, start_utc + timedelta(minutes=45))
    db.commit()
    return slot


def _pending_appointment(db, client):
    """One PENDING appointment on a BOOKED slot (staff-style row with
    conversation_id=None — the established exemption from the
    per-conversation unique index, as in the DST route test).
    Returns (appointment, booked_slot)."""
    from app.calendar_models import AppointmentStatus, SlotStatus
    from app.repositories.appointment_repository import create_appointment_from_slot

    slot = _publish_slot(db, client)
    appointment = create_appointment_from_slot(
        db, slot=slot, conversation_id=None,
        status=AppointmentStatus.PENDING,
        patient_name="Route Test Patient",
        patient_phone=f"516-555-{uuid.uuid4().hex[:4]}",
        patient_email=None, new_or_returning=None,
        reason="cleaning/checkup", urgency="routine",
    )
    slot.status = SlotStatus.BOOKED
    db.commit()
    return appointment, slot


def _request_for(route_case, client_id, slot_id, appointment_id):
    """An OTHERWISE-VALID request for each route (approved condition 3):
    real ids, valid query parameters, valid aware-datetime body."""
    if route_case == "create_slots":
        return ("post", "/admin/calendar/slots", {
            "json": {
                "client_id": str(client_id),
                "slots": [{
                    "start_datetime": "2026-07-16T13:30:00-04:00",
                    "end_datetime": "2026-07-16T14:15:00-04:00",
                }],
            }
        })
    if route_case == "list_slots":
        return ("get", "/admin/calendar/slots", {
            "params": {"client_id": str(client_id), "day": "2026-07-16"}
        })
    if route_case == "block_slot":
        return ("post", f"/admin/calendar/slots/{slot_id}/block", {
            "params": {"client_id": str(client_id)}
        })
    if route_case == "list_appointments":
        return ("get", "/admin/calendar/appointments", {
            "params": {"client_id": str(client_id),
                       "start_day": "2026-07-01", "end_day": "2026-07-31"}
        })
    if route_case == "confirm_appointment":
        return ("post",
                f"/admin/calendar/appointments/{appointment_id}/confirm",
                {"params": {"client_id": str(client_id)}})
    assert route_case == "cancel_appointment"
    return ("post",
            f"/admin/calendar/appointments/{appointment_id}/cancel",
            {"params": {"client_id": str(client_id)}})


def _send(http, route_case, headers, client_id, slot_id, appointment_id):
    method, url, kwargs = _request_for(route_case, client_id, slot_id,
                                       appointment_id)
    return getattr(http, method)(url, headers=headers, **kwargs)


def _trap_notification_channels(monkeypatch):
    """Both provider send functions trapped (the established test_booking_db
    pattern): any invocation counts AND fails."""
    from app.services import notification_service

    calls = {"sms": 0, "email": 0}

    def sms_trap(*args, **kwargs):
        calls["sms"] += 1
        raise AssertionError("cross-tenant path invoked _send_sms")

    def email_trap(*args, **kwargs):
        calls["email"] += 1
        raise AssertionError("cross-tenant path invoked _send_email")

    monkeypatch.setattr(notification_service, "_send_sms", sms_trap)
    monkeypatch.setattr(notification_service, "_send_email", email_trap)
    return calls


def _trap_write_owners(monkeypatch):
    """Every service/repository write owner the three admin write routes
    delegate to, trapped so a foreign-tenant execution counts AND fails
    (approved condition 1: no write function for the foreign tenant runs)."""
    from app.repositories import appointment_repository
    from app.services import booking_service

    calls = {"confirm_appointment": 0, "cancel_appointment": 0,
             "get_slot_for_update": 0}

    def trap(name):
        def _trap(*args, **kwargs):
            calls[name] += 1
            raise AssertionError(f"cross-tenant path invoked {name}")
        return _trap

    monkeypatch.setattr(booking_service, "confirm_appointment",
                        trap("confirm_appointment"))
    monkeypatch.setattr(booking_service, "cancel_appointment",
                        trap("cancel_appointment"))
    monkeypatch.setattr(appointment_repository, "get_slot_for_update",
                        trap("get_slot_for_update"))
    return calls


def _foreign_state_snapshot(db, appointment, booked_slot, open_slot):
    """Every Office B field a rejected cross-tenant write must leave
    byte-untouched (approved condition 1)."""
    db.refresh(appointment)
    db.refresh(booked_slot)
    db.refresh(open_slot)
    return (
        appointment.status,
        appointment.confirmed_at,
        appointment.office_sms_sent,
        appointment.office_email_sent,
        appointment.patient_sms_sent,
        appointment.notify_error,
        booked_slot.status,
        booked_slot.held_until,
        open_slot.status,
        open_slot.held_until,
    )


# ---------------------------------------------------------------------------
# Key-generation helper (4 tests)
# ---------------------------------------------------------------------------

def test_generated_key_prefix_and_token_shape():
    """The raw key is mia_cal_ + exactly 43 base64url characters, and matches
    the single owner's own validation pattern."""
    from app.services import calendar_admin_auth as auth

    raw_key, _key_hash = auth.generate_calendar_admin_key()
    has_prefix = raw_key.startswith(auth.RAW_KEY_PREFIX)
    token_length_ok = (
        len(raw_key) == len(auth.RAW_KEY_PREFIX) + auth.RAW_KEY_TOKEN_LENGTH
    )
    matches_own_pattern = auth.RAW_KEY_PATTERN.fullmatch(raw_key) is not None
    assert has_prefix
    assert token_length_ok
    assert matches_own_pattern


def test_generated_keys_are_unique():
    """Independently generated credentials differ — raw AND hash."""
    from app.services.calendar_admin_auth import generate_calendar_admin_key

    raw_1, hash_1 = generate_calendar_admin_key()
    raw_2, hash_2 = generate_calendar_admin_key()
    raw_differs = raw_1 != raw_2
    hash_differs = hash_1 != hash_2
    assert raw_differs
    assert hash_differs


def test_hash_is_sha256_lowercase_hex_of_raw():
    """The stored form is the SHA-256 hex digest of the raw key: 64 lowercase
    hex characters (the migration CHECK's exact shape), and
    hash_calendar_admin_key reproduces the generator's own value."""
    import hashlib
    import re as _re
    from app.services import calendar_admin_auth as auth

    raw_key, key_hash = auth.generate_calendar_admin_key()
    matches_reference = (
        key_hash == hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
    )
    stored_shape_ok = _re.fullmatch(r"[0-9a-f]{64}", key_hash) is not None
    rehash_matches = auth.hash_calendar_admin_key(raw_key) == key_hash
    assert matches_reference
    assert stored_shape_ok
    assert rehash_matches


def test_generation_persists_nothing(db):
    """The generator is PURE: it takes no session (cannot persist even by
    mistake) and generating a key writes no credential row."""
    import inspect
    from app.calendar_models import CalendarAdminCredential
    from app.services import calendar_admin_auth as auth

    rows_before = db.query(CalendarAdminCredential).count()
    raw_key, key_hash = auth.generate_calendar_admin_key()
    rows_after = db.query(CalendarAdminCredential).count()
    assert rows_after == rows_before
    signature = inspect.signature(auth.generate_calendar_admin_key)
    takes_no_parameters = len(signature.parameters) == 0
    assert takes_no_parameters
    stored_form_is_not_raw = key_hash != raw_key
    assert stored_form_is_not_raw


# ---------------------------------------------------------------------------
# HTTP: every credential failure is the identical 401 (6 parametrized cases)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "mode", ["missing", "empty", "malformed", "unknown", "revoked",
             "inactive_client"]
)
def test_http_credential_failure_401(http, db, client_row, mode):
    """Missing (explicitly NOT 422), empty, malformed, unknown, revoked, and
    inactive-client credentials all get status 401 with the EXACT detail
    'Invalid admin key.' — indistinguishable from one another. The request
    is otherwise fully valid."""
    from app.services.calendar_admin_auth import generate_calendar_admin_key

    target_client_id = client_row.id
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
        from app.models import Client
        dormant = Client(id=uuid.uuid4(), practice_name="Dormant Dental",
                         api_key=f"key-{uuid.uuid4()}", active=False)
        db.add(dormant)
        db.commit()
        raw_key, _credential = _provision(db, dormant)
        headers = {"X-Admin-Key": raw_key}
        target_client_id = dormant.id  # its OWN tenant: only the client's
        #                                inactive state can cause the 401

    response = http.get(
        "/admin/calendar/slots",
        params={"client_id": str(target_client_id), "day": "2026-07-16"},
        headers=headers,
    )
    assert response.status_code == 401
    assert response.status_code != 422       # esp. the missing-header case
    assert response.json()["detail"] == INVALID_DETAIL


# ---------------------------------------------------------------------------
# HTTP: the global key is dead on every Calendar route (6 parametrized cases)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("route_case", ROUTE_CASES)
def test_http_global_admin_key_401(http, db, client_row, route_case):
    """The configured global ADMIN_API_KEY receives 401 on EVERY Calendar
    admin route. Requests are otherwise valid (real slot/appointment ids for
    the office, valid params/body), so 401 can only mean authorization."""
    appointment, _booked_slot = _pending_appointment(db, client_row)
    open_slot = _publish_slot(db, client_row)

    response = _send(http, route_case, {"X-Admin-Key": GLOBAL_ADMIN_KEY},
                     client_row.id, open_slot.id, appointment.id)
    assert response.status_code == 401
    assert response.json()["detail"] == INVALID_DETAIL


# ---------------------------------------------------------------------------
# HTTP: tenant mismatch is the identical 404 on every route (6 cases)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("route_case", ROUTE_CASES)
def test_http_tenant_mismatch_404(http, db, client_row, office_b, route_case):
    """A VALID Office A credential presented with Office B's client_id (and
    Office B's real resource ids in the path) gets 404 'Client not found.'
    on every route — before anything of Office B's is revealed."""
    raw_key, _credential = _provision(db, client_row)
    appointment, _booked_slot = _pending_appointment(db, office_b)
    open_slot = _publish_slot(db, office_b)

    response = _send(http, route_case, {"X-Admin-Key": raw_key},
                     office_b.id, open_slot.id, appointment.id)
    assert response.status_code == 404
    assert response.json()["detail"] == NOT_FOUND_DETAIL


def test_http_own_tenant_success(http, db, client_row):
    """The happy path through the full HTTP stack: a provisioned per-office
    key with the matching client_id lists the office's own slots (200)."""
    raw_key, _credential = _provision(db, client_row)
    slot = _publish_slot(
        db, client_row, start_utc=datetime(2026, 7, 16, 14, 0, tzinfo=UTC)
    )
    response = http.get(
        "/admin/calendar/slots",
        params={"client_id": str(client_row.id), "day": "2026-07-16"},
        headers={"X-Admin-Key": raw_key},
    )
    assert response.status_code == 200
    listed_ids = {item["id"] for item in response.json()}
    assert str(slot.id) in listed_ids


# ---------------------------------------------------------------------------
# Tenant identity, isolation mechanics, rotation (4 tests)
# ---------------------------------------------------------------------------

def test_valid_key_returns_authenticated_tenant(db, client_row):
    """The authorization owner returns the OWNING Client, and what the table
    stores is the 64-hex digest — never the raw key."""
    from app.services.calendar_admin_auth import authenticate_calendar_admin

    raw_key, credential = _provision(db, client_row)
    authenticated = authenticate_calendar_admin(db, raw_key)
    assert authenticated.id == client_row.id
    stored_is_digest_not_raw = (
        credential.key_hash != raw_key and len(credential.key_hash) == 64
    )
    assert stored_is_digest_not_raw


def test_tenant_mismatch_rejected_before_foreign_lookup(
    engine, http, db, client_row, office_b
):
    """PROOF the foreign tenant is never queried: a cursor-level statement
    capture during the mismatch request shows Office B's UUID in NO executed
    statement or bound parameter set.

    Run-1 correction (test instrumentation only): _provision commits the
    shared session, which EXPIRES office_b; touching office_b.id while the
    listener is armed issues a fixture refresh SELECT carrying Office B's
    UUID, which the capture then mistakes for a foreign-tenant query by the
    route. So every ORM-backed value is resolved to plain UUID/str BEFORE
    the listener is installed, and office_b is never accessed while the
    capture is active."""
    from sqlalchemy import event as sqlalchemy_event

    raw_key, _credential = _provision(db, client_row)

    # Resolve all ORM-backed values before SQL capture begins.
    foreign_client_id = office_b.id
    foreign_id_text = str(foreign_client_id)

    captured = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        captured.append((str(statement), repr(parameters)))

    # Install listener only after the values above are plain UUID/string
    # values — office_b is NOT touched again until it is removed.
    sqlalchemy_event.listen(engine, "before_cursor_execute", capture)
    try:
        response = http.get(
            "/admin/calendar/slots",
            params={"client_id": foreign_id_text, "day": "2026-07-16"},
            headers={"X-Admin-Key": raw_key},
        )
    finally:
        sqlalchemy_event.remove(engine, "before_cursor_execute", capture)

    assert response.status_code == 404
    assert response.json()["detail"] == NOT_FOUND_DETAIL
    assert len(captured) > 0                       # the capture really ran
    auth_select_captured = any(
        "calendar_admin_credentials" in statement
        for statement, _parameters in captured
    )
    assert auth_select_captured                    # the auth SELECT is there
    foreign_id_queried = any(
        foreign_id_text in statement or foreign_id_text in parameters
        for statement, parameters in captured
    )
    assert not foreign_id_queried


def test_foreign_tenant_indistinguishable_from_nonexistent_uuid(
    http, db, client_row, office_b
):
    """A REAL other office and a random nonexistent UUID produce the exact
    same (status, detail) — no existence oracle."""
    raw_key, _credential = _provision(db, client_row)

    def probe(client_id):
        response = http.get(
            "/admin/calendar/slots",
            params={"client_id": str(client_id), "day": "2026-07-16"},
            headers={"X-Admin-Key": raw_key},
        )
        return response.status_code, response.json()["detail"]

    real_foreign = probe(office_b.id)
    nonexistent = probe(uuid.uuid4())
    assert real_foreign == (404, NOT_FOUND_DETAIL)
    assert nonexistent == real_foreign


def test_rotation_second_key_works_after_first_revoked(http, db, client_row):
    """The documented rotation path: two active credentials overlap and both
    authenticate; revoking the first kills ONLY the first."""
    raw_old, credential_old = _provision(db, client_row, label="old tool")
    raw_new, _credential_new = _provision(db, client_row, label="rotated tool")

    def status_for(raw_key):
        return http.get(
            "/admin/calendar/slots",
            params={"client_id": str(client_row.id), "day": "2026-07-16"},
            headers={"X-Admin-Key": raw_key},
        ).status_code

    assert status_for(raw_old) == 200      # overlap window: both alive
    assert status_for(raw_new) == 200

    credential_old.active = False
    credential_old.revoked_at = _now()
    db.commit()

    assert status_for(raw_old) == 401      # revocation effective immediately
    assert status_for(raw_new) == 200      # rotated key untouched


# ---------------------------------------------------------------------------
# Cross-tenant writes are mutation-free (approved condition 1 — 3 cases)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("route_case", WRITE_CASES)
def test_cross_tenant_write_is_mutation_free(
    http, db, client_row, office_b, monkeypatch, route_case
):
    """Office A's credential attempting to block Office B's slot / cancel or
    confirm Office B's appointment: 404 'Client not found.', Office B's
    appointment and slot state byte-unchanged (status, confirmed_at,
    notification flags, notify_error, hold fields), zero SMS/email
    invocations, and zero executions of any service/repository write owner."""
    raw_key, _credential = _provision(db, client_row)

    # Build Office B's real resources FIRST (the builder legitimately uses
    # repository functions), snapshot, and only then arm the traps.
    appointment, booked_slot = _pending_appointment(db, office_b)
    open_slot = _publish_slot(db, office_b)
    state_before = _foreign_state_snapshot(db, appointment, booked_slot,
                                           open_slot)

    notification_calls = _trap_notification_channels(monkeypatch)
    write_owner_calls = _trap_write_owners(monkeypatch)

    response = _send(http, route_case, {"X-Admin-Key": raw_key},
                     office_b.id, open_slot.id, appointment.id)

    assert response.status_code == 404
    assert response.json()["detail"] == NOT_FOUND_DETAIL
    state_after = _foreign_state_snapshot(db, appointment, booked_slot,
                                          open_slot)
    assert state_after == state_before
    assert notification_calls == {"sms": 0, "email": 0}
    assert write_owner_calls == {"confirm_appointment": 0,
                                 "cancel_appointment": 0,
                                 "get_slot_for_update": 0}


# ---------------------------------------------------------------------------
# Authentication database failures fail closed (approved condition 2)
# ---------------------------------------------------------------------------

def test_auth_database_error_propagates_without_global_fallback(
    db, client_row, monkeypatch
):
    """A database exception during credential lookup: the session is rolled
    back; the ORIGINAL error stays visible as a server failure (never
    converted to 401, never authenticated, no global-key fallback — the
    authorization owner does not even import app.config); and no route
    business operation executes — proven BOTH by the slot's unchanged state
    AND by a trap on the block route's write owner
    (appointment_repository.get_slot_for_update) never being called."""
    import inspect
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.calendar_models import SlotStatus
    from app.repositories import appointment_repository
    from app.routes import calendar as calendar_routes
    from app.services import calendar_admin_auth as auth

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

    # (a) Direct proof: the ORIGINAL exception propagates unchanged (it is
    # NOT an HTTPException 401), after exactly one lookup attempt and
    # exactly one rollback.
    exploding = ExplodingSession(db)
    with pytest.raises(RuntimeError):
        auth.authenticate_calendar_admin(exploding, raw_key)
    assert exploding.query_attempts == 1
    assert exploding.rollback_calls == 1

    # (b) HTTP proof: the request surfaces as a SERVER failure (500), is not
    # authenticated, and the targeted business operation never ran — proven
    # two ways: the block route's write owner is trapped (any call counts
    # AND fails), and the office's own AVAILABLE slot is still AVAILABLE
    # after the block attempt.
    open_slot = _publish_slot(db, client_row)

    business_operation_calls = {"get_slot_for_update": 0}

    def slot_lock_trap(*args, **kwargs):
        business_operation_calls["get_slot_for_update"] += 1
        raise AssertionError(
            "route business operation ran despite auth database failure"
        )

    monkeypatch.setattr(appointment_repository, "get_slot_for_update",
                        slot_lock_trap)

    app = FastAPI()
    app.include_router(calendar_routes.router)
    exploding_for_http = ExplodingSession(db)
    app.dependency_overrides[calendar_routes.get_db] = (
        lambda: exploding_for_http
    )
    with TestClient(app, raise_server_exceptions=False) as http_client:
        response = http_client.post(
            f"/admin/calendar/slots/{open_slot.id}/block",
            params={"client_id": str(client_row.id)},
            headers={"X-Admin-Key": raw_key},
        )
    assert response.status_code == 500
    assert exploding_for_http.rollback_calls == 1
    assert business_operation_calls == {"get_slot_for_update": 0}
    db.refresh(open_slot)
    assert open_slot.status == SlotStatus.AVAILABLE

    # (c) No global-key fallback can exist: the authorization owner never
    # touches app.config (where ADMIN_API_KEY lives), and neither does the
    # Patch 5 route module.
    auth_source = inspect.getsource(auth)
    routes_source = inspect.getsource(calendar_routes)
    assert "app.config" not in auth_source
    assert "app.config" not in routes_source
