# calendar_tests/test_portal_notification_settings.py
#
# P6-A: the Office Portal NOTIFICATION DESTINATION management paths (GET read
# + PUT full replacement) - proven at the REAL HTTP layer (real portal
# routers, real P2 JWT authentication; only the session dependency is
# overridden, the test_portal_lead_writes.py pattern). The role-guard proofs
# additionally override the identity dependency to inject a non-admin role,
# the only way to exercise the guard's negative branch while office_admin is
# the sole role portal_auth accepts.
#
# Proven here:
#   - GET returns EXACTLY the three approved keys and no secret/tenant field
#     (leak-prevention bite); the legacy both-NULL row reads cleanly;
#   - unauthenticated GET/PUT fail closed with no write;
#   - office_admin is required on BOTH endpoints (403 for any other role);
#   - valid email-only, phone-only, and both-set saves persist to the
#     existing first-class columns and advance the server token;
#   - the empty-destination invariant: a result that would clear BOTH is 422
#     with NO write (row + token byte-unchanged) - the portal-write rule, not
#     a schema CHECK;
#   - permissive validation (D7): a dashed number and a +E.164 number both
#     save; malformed email/phone and over-length values are 422 with no
#     write;
#   - optimistic concurrency is REAL compare-and-set: a stale token is 409 and
#     changes nothing, a fresh retry then succeeds, and the response token is
#     SERVER-minted (never the echoed request value);
#   - the closed body vocabulary: an extra field (e.g. a smuggled client_id)
#     is 422 with no write (extra="forbid"); a query parameter cannot change
#     the tenant;
#   - tenant isolation: each office's write touches only its own row;
#   - under a controlled clock frozen/rewound behind the persisted token,
#     every accepted write still mints a STRICTLY newer token;
#   - notification regression: after a save the two existing send owners'
#     recipient columns reflect the new destinations (send BEHAVIOR unchanged;
#     only WHERE the destinations are stored changed).

import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from calendar_tests.conftest import requires_db  # noqa: E402

import jwt as pyjwt  # noqa: E402

pytestmark = requires_db

TEST_SECRET = "portal-test-secret-0123456789abcdef0123456789"
TEST_ISSUER = "https://p6-test-project.supabase.co/auth/v1"
AUDIENCE = "authenticated"

SETTINGS_PATH = "/portal/notification-settings"
VIEW_KEYS = {"notification_email", "notification_phone",
             "notification_settings_updated_at"}


def _token(sub, *, secret=TEST_SECRET, aud=AUDIENCE, exp_delta=300,
           email="office@example.test"):
    """Mint a Supabase-shaped access token (test_portal_auth.py pattern)."""
    import time
    claims = {
        "sub": str(sub),
        "aud": aud,
        "exp": int(time.time()) + exp_delta,
        "email": email,
        "role": "authenticated",
        "iss": TEST_ISSUER,
    }
    return pyjwt.encode(claims, secret, algorithm="HS256")


@pytest.fixture()
def second_client(db):
    """Office B: the foreign tenant every isolation proof mutates against."""
    from app.models import Client

    client = Client(
        id=uuid.uuid4(),
        practice_name="Other Dental",
        api_key=f"key-{uuid.uuid4()}",
        active=True,
        settings={},
        notification_email="b-office@example.com",
        notification_phone="+15550002222",
    )
    db.add(client)
    db.commit()
    return client


@pytest.fixture(scope="module")
def office_users_table(engine):
    """Run the REAL migration 007 (sole creation authority for office_users)
    up before this module and down after it (test_portal_lead_writes.py
    pattern)."""
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


@pytest.fixture()
def office_user_a(db, client_row, office_users_table):
    from app.portal_models import OfficeUser

    row = OfficeUser(auth_user_id=uuid.uuid4(), client_id=client_row.id)
    db.add(row)
    db.commit()
    return row


@pytest.fixture()
def office_user_b(db, second_client, office_users_table):
    from app.portal_models import OfficeUser

    row = OfficeUser(auth_user_id=uuid.uuid4(), client_id=second_client.id)
    db.add(row)
    db.commit()
    return row


@pytest.fixture()
def portal_http(db, office_user_a, monkeypatch):
    """Real app with the portal auth router AND the P6-A settings router over
    HTTP; real JWT auth; only the session dependency overridden."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.routes import portal as portal_routes
    from app.routes import portal_notification_settings as settings_routes
    from app.services import portal_auth

    monkeypatch.setenv(portal_auth.ENV_JWT_SECRET, TEST_SECRET)
    monkeypatch.delenv(portal_auth.ENV_JWKS_URL, raising=False)
    monkeypatch.setenv(portal_auth.ENV_ISSUER, TEST_ISSUER)
    portal_auth._jwks_clients.clear()

    app = FastAPI()
    app.include_router(portal_routes.router)
    app.include_router(settings_routes.router)
    app.dependency_overrides[portal_routes.get_db] = lambda: db
    with TestClient(app) as client:
        yield client


def _get(portal_http, token):
    headers = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return portal_http.get(SETTINGS_PATH, headers=headers)


def _put(portal_http, body, token):
    headers = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return portal_http.put(SETTINGS_PATH, json=body, headers=headers)


def _body(email, phone, expected):
    return {"notification_email": email,
            "notification_phone": phone,
            "expected_notification_settings_updated_at": expected}


def _iso(value):
    """Normalize a datetime to the wire notation pydantic v2 uses (Z suffix)
    so equality against response tokens compares instants, not notations."""
    return value.isoformat().replace("+00:00", "Z")


def _parse_wire_instant(value):
    """Parse a server concurrency-token WIRE STRING back to a timezone-aware
    datetime for CHRONOLOGICAL comparison.

    Why this exists (v1.0.2 test-harness correction): the API serializes the
    token as an ISO-8601 string and OMITS the fractional-seconds field when the
    microsecond component is zero. So "2026-08-13T12:00:00.000001Z" (one
    microsecond LATER) sorts LEXICOGRAPHICALLY BEFORE "2026-08-13T12:00:00Z".
    A string ">" therefore does NOT test chronological ordering. Parsing both
    sides to aware datetimes compares INSTANTS, which is the invariant the
    controlled-clock test actually asserts.

    This parsing is test-only and does NOT weaken C4: the browser still treats
    the token as opaque and echoes the exact server string verbatim; this
    helper never reserializes a token before sending it back to the API.
    """
    from datetime import datetime as _dt

    return _dt.fromisoformat(value.replace("Z", "+00:00"))


def _reload(db, client):
    db.expire_all()
    from app.models import Client

    return db.query(Client).filter(Client.id == client.id).one()


# ---------------------------------------------------------------------------
# Read + leak prevention
# ---------------------------------------------------------------------------

def test_get_returns_exact_slice_and_no_secrets(
        portal_http, db, client_row, office_user_a):
    """GET returns EXACTLY the three approved keys - and the legacy both-NULL
    client (no destinations, no token) reads cleanly."""
    response = _get(portal_http, _token(office_user_a.auth_user_id))
    assert response.status_code == 200
    payload = response.json()
    assert set(payload.keys()) == VIEW_KEYS
    assert payload["notification_email"] is None
    assert payload["notification_phone"] is None
    assert payload["notification_settings_updated_at"] is None
    # No secret or tenant field may ride along.
    for forbidden in ("client_id", "id", "api_key", "practice_name",
                      "settings", "notify_error"):
        assert forbidden not in payload


def test_get_reflects_configured_destinations(
        portal_http, db, client_row, office_user_a):
    client_row.notification_email = "front-desk@example.com"
    client_row.notification_phone = "516-555-7777"
    db.commit()
    response = _get(portal_http, _token(office_user_a.auth_user_id))
    assert response.status_code == 200
    payload = response.json()
    assert payload["notification_email"] == "front-desk@example.com"
    assert payload["notification_phone"] == "516-555-7777"


# ---------------------------------------------------------------------------
# Authentication / authorization
# ---------------------------------------------------------------------------

def test_unauthenticated_fails_closed(portal_http, db, client_row):
    """No token and a garbage token both 401, and nothing is written."""
    before = _reload(db, client_row)
    email_before, phone_before = before.notification_email, before.notification_phone
    token_before = before.notification_settings_updated_at

    assert _get(portal_http, None).status_code == 401
    assert _get(portal_http, "not-a-jwt").status_code == 401
    assert _put(portal_http, _body("x@example.com", None, None),
                None).status_code == 401
    assert _put(portal_http, _body("x@example.com", None, None),
                "not-a-jwt").status_code == 401

    after = _reload(db, client_row)
    assert after.notification_email == email_before
    assert after.notification_phone == phone_before
    assert after.notification_settings_updated_at == token_before


def _inject_role(portal_http, client_row, role):
    """Override the identity dependency to inject a chosen role - the only way
    to reach the guard's non-admin branch while office_admin is the sole role
    portal_auth accepts."""
    from app.routes.portal import require_portal_identity
    from app.services.portal_auth import PortalIdentity
    from app.portal_models import OfficeUser

    fake_user = OfficeUser(auth_user_id=uuid.uuid4(),
                           client_id=client_row.id, role=role)
    identity = PortalIdentity(client=client_row, office_user=fake_user,
                              email=None)
    portal_http.app.dependency_overrides[require_portal_identity] = (
        lambda: identity)


def test_non_admin_role_is_forbidden_on_get_and_put(
        portal_http, db, client_row, office_user_a):
    """A non-admin role is 403 on BOTH endpoints and writes nothing."""
    _inject_role(portal_http, client_row, "viewer")
    before = _reload(db, client_row)

    get_response = _get(portal_http, _token(office_user_a.auth_user_id))
    put_response = _put(portal_http,
                        _body("admin-only@example.com", None, None),
                        _token(office_user_a.auth_user_id))
    assert get_response.status_code == 403
    assert put_response.status_code == 403

    after = _reload(db, client_row)
    assert after.notification_email == before.notification_email
    assert after.notification_settings_updated_at == \
        before.notification_settings_updated_at


# ---------------------------------------------------------------------------
# Valid writes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("email,phone", [
    ("front-desk@example.com", None),        # email only
    (None, "+15550001111"),                  # phone only (E.164)
    (None, "516-555-7777"),                  # phone only (dashed, D7)
    ("front-desk@example.com", "516-555-7777"),  # both
])
def test_valid_saves_persist_and_advance_token(
        portal_http, db, client_row, office_user_a, email, phone):
    response = _put(portal_http, _body(email, phone, None),
                    _token(office_user_a.auth_user_id))
    assert response.status_code == 200
    payload = response.json()
    assert set(payload.keys()) == VIEW_KEYS
    assert payload["notification_email"] == email
    assert payload["notification_phone"] == phone
    assert payload["notification_settings_updated_at"] is not None

    row = _reload(db, client_row)
    assert row.notification_email == email
    assert row.notification_phone == phone
    assert row.notification_settings_updated_at is not None
    # The response token is the persisted token (authoritative re-read).
    assert _iso(row.notification_settings_updated_at) == \
        payload["notification_settings_updated_at"]


def test_both_empty_is_422_with_no_write(
        portal_http, db, client_row, office_user_a):
    """Clearing BOTH destinations is refused (422) and nothing is written -
    the portal-write invariant, enforced without a schema CHECK."""
    before = _reload(db, client_row)
    response = _put(portal_http, _body(None, None, None),
                    _token(office_user_a.auth_user_id))
    assert response.status_code == 422
    after = _reload(db, client_row)
    assert after.notification_email == before.notification_email
    assert after.notification_phone == before.notification_phone
    assert after.notification_settings_updated_at == \
        before.notification_settings_updated_at


def test_blank_strings_count_as_empty_and_are_422(
        portal_http, db, client_row, office_user_a):
    """Whitespace-only values normalize to empty, so both-blank is the
    both-empty refusal (422), not a save of blank strings."""
    response = _put(portal_http, _body("   ", "  ", None),
                    _token(office_user_a.auth_user_id))
    assert response.status_code == 422
    row = _reload(db, client_row)
    assert row.notification_settings_updated_at is None


@pytest.mark.parametrize("email,phone", [
    ("no-at-sign", None),
    ("two@@ats.com", None),
    ("has space@example.com", None),
    ("@nolocal.com", None),
    ("nodomain@", None),
    ("x" * 250 + "@e.com", None),            # > 254
    (None, "phone!!"),                       # illegal char
    (None, "()+- ."),                        # no digit
    (None, "1" * 33),                        # > 32
])
def test_malformed_values_are_422_with_no_write(
        portal_http, db, client_row, office_user_a, email, phone):
    before = _reload(db, client_row)
    response = _put(portal_http, _body(email, phone, None),
                    _token(office_user_a.auth_user_id))
    assert response.status_code == 422
    after = _reload(db, client_row)
    assert after.notification_email == before.notification_email
    assert after.notification_phone == before.notification_phone
    assert after.notification_settings_updated_at == \
        before.notification_settings_updated_at


# ---------------------------------------------------------------------------
# Optimistic concurrency (compare-and-set)
# ---------------------------------------------------------------------------

def test_stale_token_is_409_and_changes_nothing_then_fresh_retry_succeeds(
        portal_http, db, client_row, office_user_a):
    token = _token(office_user_a.auth_user_id)
    # First save from the NULL baseline succeeds and mints a token.
    first = _put(portal_http, _body("a@example.com", None, None), token)
    assert first.status_code == 200
    current = first.json()["notification_settings_updated_at"]

    # A stale expected token (the original NULL) now conflicts.
    stale = _put(portal_http, _body("b@example.com", None, None), token)
    assert stale.status_code == 409
    row = _reload(db, client_row)
    assert row.notification_email == "a@example.com"     # unchanged
    assert _iso(row.notification_settings_updated_at) == current

    # Retrying with the CURRENT token succeeds.
    retry = _put(portal_http, _body("b@example.com", None, current), token)
    assert retry.status_code == 200
    assert retry.json()["notification_email"] == "b@example.com"
    assert retry.json()["notification_settings_updated_at"] != current


def test_response_token_is_server_minted_not_echoed(
        portal_http, db, client_row, office_user_a):
    """A blind/echo implementation would return the request's expected value;
    a real CAS returns a fresh server token distinct from it."""
    token = _token(office_user_a.auth_user_id)
    forged = _iso(datetime(2000, 1, 1, tzinfo=timezone.utc))
    # From the NULL baseline the CAS matches on NULL (expected=None), so the
    # write is accepted; the response token must NOT be the forged value and
    # must be a real recent instant.
    response = _put(portal_http, _body("a@example.com", None, None), token)
    assert response.status_code == 200
    minted = response.json()["notification_settings_updated_at"]
    assert minted != forged
    assert minted is not None


# ---------------------------------------------------------------------------
# Closed body vocabulary + tenant safety
# ---------------------------------------------------------------------------

def test_extra_body_field_is_422_with_no_write(
        portal_http, db, client_row, office_user_a):
    """extra="forbid": a smuggled client_id (or any unknown key) is a 422, not
    a silent tenant change or a partial write."""
    before = _reload(db, client_row)
    body = _body("a@example.com", None, None)
    body["client_id"] = str(uuid.uuid4())
    response = _put(portal_http, body, _token(office_user_a.auth_user_id))
    assert response.status_code == 422
    after = _reload(db, client_row)
    assert after.notification_email == before.notification_email
    assert after.notification_settings_updated_at == \
        before.notification_settings_updated_at


def test_query_parameter_cannot_change_tenant(
        portal_http, db, client_row, second_client, office_user_a):
    """A smuggled ?client_id query is ignored: the write lands on the token's
    own tenant, and Office B is untouched."""
    b_before = _reload(db, second_client)
    response = portal_http.put(
        SETTINGS_PATH + "?client_id=" + str(second_client.id),
        json=_body("a@example.com", None, None),
        headers={"Authorization": f"Bearer {_token(office_user_a.auth_user_id)}"})
    assert response.status_code == 200
    a_row = _reload(db, client_row)
    b_row = _reload(db, second_client)
    assert a_row.notification_email == "a@example.com"
    assert b_row.notification_email == b_before.notification_email
    assert b_row.notification_settings_updated_at == \
        b_before.notification_settings_updated_at


def test_each_office_writes_only_its_own_row(
        portal_http, db, client_row, second_client,
        office_user_a, office_user_b):
    """Office A's save changes A only; Office B's save changes B only."""
    a_response = _put(portal_http, _body("a@example.com", None, None),
                      _token(office_user_a.auth_user_id))
    b_response = _put(portal_http, _body("bb@example.com", None, None),
                      _token(office_user_b.auth_user_id))
    assert a_response.status_code == 200 and b_response.status_code == 200
    a_row = _reload(db, client_row)
    b_row = _reload(db, second_client)
    assert a_row.notification_email == "a@example.com"
    assert b_row.notification_email == "bb@example.com"


# ---------------------------------------------------------------------------
# Controlled-clock strict token advancement
# ---------------------------------------------------------------------------

def test_token_strictly_advances_under_a_frozen_backward_clock(
        portal_http, db, client_row, office_user_a, monkeypatch):
    """Under a clock frozen and then moved BEHIND the persisted token, every
    accepted write still mints a STRICTLY newer token (a plain-clock token
    implementation regresses here)."""
    from app.services import portal_notification_settings_service as svc

    frozen = datetime(2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(svc, "_now_utc", lambda: frozen)
    token = _token(office_user_a.auth_user_id)

    first = _put(portal_http, _body("a@example.com", None, None), token)
    assert first.status_code == 200
    t1 = first.json()["notification_settings_updated_at"]

    # Move the clock BACKWARD, then save the SAME value with the current token.
    monkeypatch.setattr(
        svc, "_now_utc",
        lambda: frozen - timedelta(minutes=5))
    second = _put(portal_http, _body("a@example.com", None, t1), token)
    assert second.status_code == 200
    t2 = second.json()["notification_settings_updated_at"]
    # CHRONOLOGICAL comparison (not lexical): the token is one microsecond
    # newer despite the backward clock. The wire strings t1/t2 are sent to the
    # API verbatim elsewhere; only this ASSERTION parses them to instants.
    assert _parse_wire_instant(t2) > _parse_wire_instant(t1)

    # And a clear-of-one-channel save (still one destination left) advances.
    monkeypatch.setattr(svc, "_now_utc", lambda: frozen - timedelta(minutes=5))
    third = _put(portal_http, _body("a@example.com", None, t2), token)
    assert third.status_code == 200
    t3 = third.json()["notification_settings_updated_at"]
    assert _parse_wire_instant(t3) > _parse_wire_instant(t2)


def test_wire_token_ordering_is_chronological_not_lexical():
    """Serialization-boundary bite (v1.0.2): a token one MICROSECOND later is
    serialized WITHOUT the fractional field when the microsecond component of
    the earlier token is zero, so it sorts LEXICOGRAPHICALLY EARLIER. This bite
    pins the trap directly - lexical string order is misleading; the invariant
    is chronological and must be checked on PARSED instants. No database is
    used here; it exercises only the wire/parse boundary the controlled-clock
    test depends on."""
    earlier = "2026-08-13T12:00:00Z"
    later = "2026-08-13T12:00:00.000001Z"     # exactly one microsecond later

    # The trap: raw STRING ordering is MISLEADING (proves why "t2 > t1" on
    # strings was the harness defect).
    assert later < earlier

    # The correct CHRONOLOGICAL ordering (what the token invariant means).
    assert _parse_wire_instant(later) > _parse_wire_instant(earlier)

    # And the parser round-trips the exact instants (aware, UTC).
    assert _parse_wire_instant(earlier) == datetime(
        2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc)
    assert _parse_wire_instant(later) == datetime(
        2026, 8, 13, 12, 0, 0, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Notification regression (destinations flow to the existing send owners)
# ---------------------------------------------------------------------------

def test_saved_destinations_are_the_columns_the_senders_read(
        portal_http, db, client_row, office_user_a):
    """After a save, the two existing send owners' recipient columns
    (Client.notification_email / notification_phone) hold the new
    destinations - proof the portal changed WHERE destinations are stored, not
    HOW notifications send."""
    response = _put(portal_http,
                    _body("alerts@example.com", "+15550009999", None),
                    _token(office_user_a.auth_user_id))
    assert response.status_code == 200
    row = _reload(db, client_row)
    assert row.notification_email == "alerts@example.com"
    assert row.notification_phone == "+15550009999"


# ---------------------------------------------------------------------------
# F3 - Fake-provider regression bites: the EXISTING send owners observe the
# portal-updated destinations and their channel eligibility follows them, with
# NO real Twilio/Resend call and NO change to send semantics. The owners are
# exercised unmodified; only their provider boundaries are monkeypatched
# (the test_notification_idempotency.py convention). This does NOT modify
# chat.py or notification_service.py.
# ---------------------------------------------------------------------------

from datetime import timedelta as _timedelta   # noqa: E402  (local to F3 block)


def _set_contacts(db, client, email, phone):
    """Configure the office destinations directly (conftest starts them
    unset). Mirrors the notification-suite _set_office_contacts helper."""
    client.notification_email = email
    client.notification_phone = phone
    db.add(client)
    db.commit()


def _recording_office_sms(monkeypatch, fail=False):
    """Replace the Twilio boundary in notification_service with a recorder so
    NO real SMS can be sent."""
    from app.services import notification_service
    sent = []

    def fake_send_sms(to_phone, body):
        sent.append((to_phone, body))
        if fail:
            raise RuntimeError("sms provider down (test fake)")

    monkeypatch.setattr(notification_service, "_send_sms", fake_send_sms)
    return sent


def _recording_office_email(monkeypatch, fail=False):
    """Replace the Resend boundary in notification_service with a recorder."""
    from app.services import notification_service
    sent = []

    def fake_send_email(to_email, subject, email_html):
        sent.append((to_email, subject))
        if fail:
            raise RuntimeError("email provider down (test fake)")

    monkeypatch.setattr(notification_service, "_send_email", fake_send_email)
    return sent


def _make_appointment(db, client):
    """One committed staff-style appointment (conversation_id=None), the
    test_notification_idempotency.py pattern."""
    from app.calendar_models import AppointmentStatus
    from app.repositories.appointment_repository import (
        create_appointment_from_slot, create_slot,
    )
    from datetime import datetime, timezone
    start = datetime.now(timezone.utc) + _timedelta(hours=48)
    slot = create_slot(db, client.id, start, start + _timedelta(minutes=45))
    db.commit()
    appointment = create_appointment_from_slot(
        db, slot=slot, conversation_id=None,
        status=AppointmentStatus.PENDING,
        patient_name="Kevin Alvarado", patient_phone="516-555-1234",
        patient_email=None, new_or_returning="new",
        reason="cleaning/checkup", urgency="routine",
    )
    db.commit()
    return appointment


def _send_booking(db, client, appointment):
    """Invoke send_booking_notifications the way the production caller does:
    compute settings first, end the test-owned read work (db.rollback), then
    enter the service with a clean, transaction-free session (its strict
    approved entry contract)."""
    from app.services import notification_service
    from app.services.calendar_settings_service import load_calendar_settings
    settings = load_calendar_settings(client)
    db.rollback()
    return notification_service.send_booking_notifications(
        db, client, appointment, settings)


def _make_completed_lead(db, client, **overrides):
    """A notification-ready lead conversation (name + phone)."""
    from app.models import Conversation
    values = dict(
        id=uuid.uuid4(), client_id=client.id,
        lead_name="Kevin Alvarado", lead_phone="516-555-1234",
        lead_reason="cleaning/checkup", is_lead=True,
    )
    values.update(overrides)
    conversation = Conversation(**values)
    db.add(conversation)
    db.commit()
    return conversation


def _recording_lead_boundaries(monkeypatch):
    """Replace the chat.py office-lead send boundaries with recorders so the
    real Twilio/Resend paths never run and chat.py is not modified."""
    from app.routes import chat
    email_sent = []
    sms_sent = []

    def fake_email(to_email, subject, body_text):
        email_sent.append((to_email, subject))

    def fake_sms(to_phone, body):
        sms_sent.append((to_phone, body))

    monkeypatch.setattr(chat, "send_office_lead_email", fake_email)
    monkeypatch.setattr(chat, "send_office_lead_sms", fake_sms)
    return email_sent, sms_sent


# --- Booking path -----------------------------------------------------------

def test_booking_path_observes_portal_updated_destinations(
        portal_http, db, client_row, office_user_a, monkeypatch):
    """End-to-end: a portal PUT sets BOTH destinations; the booking
    notification path then attempts BOTH channels to exactly those portal
    values, with no real provider call."""
    put = _put(portal_http,
               _body("front@portal.example", "+15551230000", None),
               _token(office_user_a.auth_user_id))
    assert put.status_code == 200

    sms = _recording_office_sms(monkeypatch)
    email = _recording_office_email(monkeypatch)
    client = _reload(db, client_row)
    appointment = _make_appointment(db, client)
    _send_booking(db, client, appointment)

    assert len(email) == 1 and email[0][0] == "front@portal.example"
    assert len(sms) == 1 and sms[0][0] == "+15551230000"


def test_booking_email_only_attempts_only_email(
        db, client_row, monkeypatch):
    """Email-only configuration: only the office email channel is eligible;
    the SMS provider boundary is never called."""
    _set_contacts(db, client_row, "only@portal.example", None)
    sms = _recording_office_sms(monkeypatch)
    email = _recording_office_email(monkeypatch)
    appointment = _make_appointment(db, client_row)
    _send_booking(db, client_row, appointment)
    assert len(email) == 1 and email[0][0] == "only@portal.example"
    assert sms == []


def test_booking_phone_only_attempts_only_sms(
        db, client_row, monkeypatch):
    """Phone-only configuration: only the office SMS channel is eligible; the
    email provider boundary is never called."""
    _set_contacts(db, client_row, None, "+15559990000")
    sms = _recording_office_sms(monkeypatch)
    email = _recording_office_email(monkeypatch)
    appointment = _make_appointment(db, client_row)
    _send_booking(db, client_row, appointment)
    assert len(sms) == 1 and sms[0][0] == "+15559990000"
    assert email == []


# --- Lead path --------------------------------------------------------------

def test_lead_path_observes_portal_updated_destinations(
        portal_http, db, client_row, office_user_a, monkeypatch):
    """End-to-end: a portal PUT sets BOTH destinations; the completed-lead
    notification path then sends to exactly those portal values on both
    channels, with no real provider call."""
    put = _put(portal_http,
               _body("lead@portal.example", "+15557770000", None),
               _token(office_user_a.auth_user_id))
    assert put.status_code == 200

    email_sent, sms_sent = _recording_lead_boundaries(monkeypatch)
    client = _reload(db, client_row)
    conversation = _make_completed_lead(db, client)

    from app.routes import chat
    result = chat.notify_office_of_completed_lead(db, client, conversation)
    email_ok, sms_ok, email_err, sms_err = result

    assert len(email_sent) == 1 and email_sent[0][0] == "lead@portal.example"
    assert len(sms_sent) == 1 and sms_sent[0][0] == "+15557770000"
    assert email_ok is True and sms_ok is True
    assert email_err is None and sms_err is None


def test_lead_email_only_attempts_only_email(
        db, client_row, monkeypatch):
    """Email-only lead configuration: only the email channel is attempted."""
    _set_contacts(db, client_row, "leadonly@portal.example", None)
    email_sent, sms_sent = _recording_lead_boundaries(monkeypatch)
    conversation = _make_completed_lead(db, client_row)
    from app.routes import chat
    chat.notify_office_of_completed_lead(db, client_row, conversation)
    assert len(email_sent) == 1 and email_sent[0][0] == "leadonly@portal.example"
    assert sms_sent == []


def test_lead_phone_only_attempts_only_sms(
        db, client_row, monkeypatch):
    """Phone-only lead configuration: only the SMS channel is attempted."""
    _set_contacts(db, client_row, None, "+15556660000")
    email_sent, sms_sent = _recording_lead_boundaries(monkeypatch)
    conversation = _make_completed_lead(db, client_row)
    from app.routes import chat
    chat.notify_office_of_completed_lead(db, client_row, conversation)
    assert len(sms_sent) == 1 and sms_sent[0][0] == "+15556660000"
    assert email_sent == []


def test_lead_emergency_channel_behavior_unchanged(
        db, client_row, monkeypatch):
    """An emergency lead with email-only destinations still routes to the
    email channel only - P6-A changes WHERE destinations point, not the
    emergency channel behavior. The emergency subject prefix still flows."""
    _set_contacts(db, client_row, "urgent@portal.example", None)
    email_sent, sms_sent = _recording_lead_boundaries(monkeypatch)
    conversation = _make_completed_lead(
        db, client_row, lead_is_emergency=True)
    from app.routes import chat
    chat.notify_office_of_completed_lead(db, client_row, conversation)
    assert len(email_sent) == 1 and email_sent[0][0] == "urgent@portal.example"
    assert sms_sent == []
    assert email_sent[0][1].startswith("EMERGENCY")   # prefix behavior intact
