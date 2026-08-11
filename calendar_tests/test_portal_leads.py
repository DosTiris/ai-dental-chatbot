# calendar_tests/test_portal_leads.py
#
# P3-B1 (Office Portal read-only Dashboard + Leads): proves the security
# and behavior contract against the REAL portal_leads router + service.
#
# GROUPS:
#   * Pure tests (no database): filter validation closed vocabularies and
#     LIKE wildcard escaping.
#   * HTTP tests (requires_db, house harness): every endpoint fails closed
#     unauthenticated; an office reads ONLY its own leads even with
#     overlapping-looking data; a stray client_id cannot override the
#     token-bound tenant; a foreign lead id is indistinguishable from a
#     nonexistent one AND from a non-lead conversation; search and filters
#     cannot escape tenant scope; search wildcards are literal; pagination
#     is bounded and stable; the responses contain EXACTLY the approved
#     fields; dashboard counts are tenant-specific; and the P2 /portal/me
#     surface still behaves.
#
# FIXTURES: local to this file, mirroring calendar_tests/test_portal_auth.py
# (the shared db / client_row / engine fixtures from conftest.py are used
# UNCHANGED; the office_users_table fixture runs the REAL migration 007).
#
# Tokens are minted locally with PyJWT (HS256 test secret) - no network,
# no real Supabase project, no provider is ever contacted.
#
# Run (owner-local, PowerShell 5.1):
#   $env:ALLOW_DESTRUCTIVE_CALENDAR_TESTS="yes"
#   $env:TEST_DATABASE_URL="postgresql://postgres:test@localhost:5433/mia_calendar_test"
#   python -m pytest calendar_tests\test_portal_leads.py -v

import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from calendar_tests.conftest import requires_db  # noqa: E402  (env bootstrap)

# app.config needs DATABASE_URL at import; the pure tests never connect, so
# an unreachable placeholder keeps them runnable anywhere (the
# test_portal_auth.py pattern). setdefault never overrides the real test
# database when TEST_DATABASE_URL is set (conftest already exported it).
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://placeholder:placeholder@localhost:1/never_connected_placeholder",
)

import jwt as pyjwt  # noqa: E402

TEST_SECRET = "portal-test-secret-0123456789abcdef0123456789"
AUDIENCE = "authenticated"
TEST_ISSUER = "https://p2-test-project.supabase.co/auth/v1"

UTC = timezone.utc

# The exact response contracts under test.
INVALID_DETAIL = "Invalid portal credentials."
NOT_FOUND_DETAIL = "Lead not found."

# The COMPLETE approved portal lead field sets (leak prevention pins).
APPROVED_SUMMARY_FIELDS = {
    "lead_id", "lead_name", "lead_phone", "lead_email", "lead_reason",
    "lead_status", "lead_patient_type", "lead_time_window",
    "lead_is_emergency", "lead_is_priority", "lead_is_outside_hours",
    "lead_outside_hours_note", "lead_email_opt_out",
    "last_lead_at", "created_at",
}
APPROVED_DETAIL_FIELDS = APPROVED_SUMMARY_FIELDS | {
    "messages", "messages_total", "messages_truncated"}
APPROVED_MESSAGE_FIELDS = {"role", "content", "created_at"}
APPROVED_DASHBOARD_FIELDS = {
    "practice_name", "total_conversations", "total_leads", "urgent_leads",
    "leads_last_7_days", "recent_leads",
}
# Markers that must NEVER appear anywhere in a portal data response body.
FORBIDDEN_BODY_MARKERS = [
    "client_id", "api_key", "client_key", "visitor_id", "settings",
    "notification_email", "notification_phone", "source_text",
    "booking_state", "key_hash", "auth_user_id",
]


def _token(sub, *, secret=TEST_SECRET, aud=AUDIENCE, exp_delta=300,
           email="office@example.test"):
    """Mint a Supabase-shaped access token (test_portal_auth.py pattern)."""
    claims = {
        "sub": str(sub),
        "aud": aud,
        "exp": int(time.time()) + exp_delta,
        "email": email,
        "role": "authenticated",
        "iss": TEST_ISSUER,
    }
    return pyjwt.encode(claims, secret, algorithm="HS256")


# ---------------------------------------------------------------------------
# Pure tests - no database, no HTTP.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_status", ["hot", "deleted", "NEW LEAD", "n3w"])
def test_status_filter_closed_vocabulary_rejects_unknown(bad_status):
    from fastapi import HTTPException
    from app.services import portal_leads_service as svc

    with pytest.raises(HTTPException) as excinfo:
        svc.validate_list_filters(bad_status, None, None, 25, 0)
    assert excinfo.value.status_code == 400
    assert "status must be one of" in excinfo.value.detail


@pytest.mark.parametrize("good_status", [
    "new", "contacted", "booked", "completed", "closed",
    " New ", "COMPLETED",   # normalization: trimmed and lowercased
])
def test_status_filter_accepts_and_normalizes_known_values(good_status):
    from app.services import portal_leads_service as svc

    status, _, _, _, _ = svc.validate_list_filters(good_status, None, None, 25, 0)
    assert status == good_status.strip().lower()


@pytest.mark.parametrize("q,days,limit,offset,fragment", [
    ("x" * 101, None, 25, 0, "q must be at most"),
    (None, 0, 25, 0, "days must be between"),
    (None, 366, 25, 0, "days must be between"),
    (None, None, 0, 0, "limit must be between"),
    (None, None, 101, 0, "limit must be between"),
    (None, None, 25, -1, "offset must be >="),
])
def test_filter_bounds_are_explicit_400s(q, days, limit, offset, fragment):
    from fastapi import HTTPException
    from app.services import portal_leads_service as svc

    with pytest.raises(HTTPException) as excinfo:
        svc.validate_list_filters(None, q, days, limit, offset)
    assert excinfo.value.status_code == 400
    assert fragment in excinfo.value.detail


def test_like_wildcards_are_escaped_to_literals():
    from app.services import portal_leads_service as svc

    assert svc.escape_like_pattern("100%") == "100\\%"
    assert svc.escape_like_pattern("a_b") == "a\\_b"
    assert svc.escape_like_pattern("c\\d") == "c\\\\d"


# ---------------------------------------------------------------------------
# HTTP fixtures (house harness, mirroring test_portal_auth.py).
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def office_users_table(engine):
    """Run the REAL migration 007 (sole creation authority for office_users,
    F-P2-3) up before this module and down after it."""
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
    """A SECOND office whose leads must never appear for Office A."""
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


def _bind_office_user(db, client):
    """One active Supabase-user -> office binding (migration 007 row)."""
    from app.portal_models import OfficeUser

    row = OfficeUser(auth_user_id=uuid.uuid4(), client_id=client.id)
    db.add(row)
    db.commit()
    return row


@pytest.fixture()
def portal_http(db, office_users_table, monkeypatch):
    """Real app containing the P2 portal router AND the P3-B1 portal_leads
    router, driven over HTTP. Only the session dependency is overridden
    (portal_leads imports the SAME get_db callable, so one override covers
    both routers); BOTH authorization owners run for real."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.routes import portal as portal_routes
    from app.routes import portal_leads as portal_leads_routes
    from app.services import portal_auth

    monkeypatch.setenv(portal_auth.ENV_JWT_SECRET, TEST_SECRET)
    monkeypatch.delenv(portal_auth.ENV_JWKS_URL, raising=False)
    monkeypatch.setenv(portal_auth.ENV_ISSUER, TEST_ISSUER)
    portal_auth._jwks_clients.clear()

    app = FastAPI()
    app.include_router(portal_routes.router)
    app.include_router(portal_leads_routes.router)
    app.dependency_overrides[portal_routes.get_db] = lambda: db
    with TestClient(app) as client:
        yield client


def _get(portal_http, path, token=None):
    headers = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return portal_http.get(path, headers=headers)


def _make_lead(db, client, *, name, phone="516-555-0000", email=None,
               reason="cleaning", status="new", is_lead=True,
               emergency=False, priority=False, outside=False,
               new_patient=None, time_window=None, email_opt_out=False,
               outside_note=None, last_lead_at=None):
    """Seed one conversation row shaped like the real lead writer's output.
    last_lead_at is set EXPLICITLY per row (never left to now()-ties) so
    ordering assertions are deterministic."""
    from app.models import Conversation

    conversation = Conversation(
        id=uuid.uuid4(),
        client_id=client.id,
        is_lead=is_lead,
        lead_name=name,
        lead_phone=phone,
        lead_email=email,
        lead_reason=reason,
        lead_status=status,
        lead_is_emergency=emergency,
        lead_is_priority=priority,
        lead_is_outside_hours=outside,
        lead_is_new_patient=new_patient,
        lead_time_window=time_window,
        lead_email_opt_out=email_opt_out,
        lead_outside_hours_note=outside_note,
        last_lead_at=last_lead_at,
    )
    db.add(conversation)
    db.commit()
    return conversation


def _add_message(db, conversation, role, content, created_at):
    """One transcript line with an EXPLICIT created_at (distinct values per
    conversation, because tie order is broken by random UUIDs and is not
    assertable)."""
    from app.models import Message

    message = Message(
        id=uuid.uuid4(),
        conversation_id=conversation.id,
        role=role,
        content=content,
        created_at=created_at,
    )
    db.add(message)
    db.commit()
    return message


def _minutes_ago(minutes):
    return datetime.now(UTC) - timedelta(minutes=minutes)


def _days_ago(days):
    return datetime.now(UTC) - timedelta(days=days)


# ---------------------------------------------------------------------------
# Fail-closed authentication on every new endpoint.
# ---------------------------------------------------------------------------

@requires_db
@pytest.mark.parametrize("path", [
    "/portal/dashboard",
    "/portal/leads",
    f"/portal/leads/{uuid.uuid4()}",
])
def test_unauthenticated_access_fails_closed(db, portal_http, client_row, path):
    """No header and a garbage token both return the single indistinguishable
    portal 401 on every P3-B1 endpoint."""
    _bind_office_user(db, client_row)
    for headers in ({}, {"Authorization": "Bearer not.a.jwt"}):
        response = portal_http.get(path, headers=headers)
        assert response.status_code == 401
        assert response.json()["detail"] == INVALID_DETAIL


# ---------------------------------------------------------------------------
# Tenant isolation.
# ---------------------------------------------------------------------------

@requires_db
def test_office_receives_only_its_own_leads(db, portal_http, client_row, office_b):
    """Two offices with overlapping-looking data (same names, same phones):
    each token sees exactly and only its own rows."""
    user_a = _bind_office_user(db, client_row)
    user_b = _bind_office_user(db, office_b)
    a1 = _make_lead(db, client_row, name="Jordan Rivera",
                    phone="516-555-0100", last_lead_at=_minutes_ago(10))
    a2 = _make_lead(db, client_row, name="Sam Patel",
                    phone="516-555-0200", last_lead_at=_minutes_ago(20))
    b1 = _make_lead(db, office_b, name="Jordan Rivera",
                    phone="516-555-0100", last_lead_at=_minutes_ago(5))

    body_a = _get(portal_http, "/portal/leads",
                  _token(user_a.auth_user_id)).json()
    ids_a = {lead["lead_id"] for lead in body_a["leads"]}
    assert ids_a == {str(a1.id), str(a2.id)}
    assert body_a["total"] == 2

    body_b = _get(portal_http, "/portal/leads",
                  _token(user_b.auth_user_id)).json()
    ids_b = {lead["lead_id"] for lead in body_b["leads"]}
    assert ids_b == {str(b1.id)}
    assert body_b["total"] == 1


@requires_db
def test_stray_client_id_cannot_override_tenant_binding(db, portal_http,
                                                        client_row, office_b):
    """A ?client_id= pointing at the OTHER office changes nothing anywhere:
    the endpoints declare no tenant parameter, so none can be honored."""
    user_a = _bind_office_user(db, client_row)
    mine = _make_lead(db, client_row, name="Own Lead",
                      last_lead_at=_minutes_ago(1))
    _make_lead(db, office_b, name="Foreign Lead", last_lead_at=_minutes_ago(1))
    token = _token(user_a.auth_user_id)
    stray = f"?client_id={office_b.id}"

    listed = _get(portal_http, f"/portal/leads{stray}", token).json()
    assert {lead["lead_id"] for lead in listed["leads"]} == {str(mine.id)}

    dashboard = _get(portal_http, f"/portal/dashboard{stray}", token).json()
    assert dashboard["practice_name"] == client_row.practice_name
    assert dashboard["total_leads"] == 1


@requires_db
def test_foreign_missing_and_non_lead_detail_are_indistinguishable(
        db, portal_http, client_row, office_b):
    """Office B's lead id, a random id, and Office A's own NON-lead
    conversation id all return byte-identical 404 bodies (Rule 15)."""
    user_a = _bind_office_user(db, client_row)
    foreign = _make_lead(db, office_b, name="Foreign Lead",
                         last_lead_at=_minutes_ago(1))
    non_lead = _make_lead(db, client_row, name=None, is_lead=False,
                          phone=None)
    token = _token(user_a.auth_user_id)

    responses = [
        _get(portal_http, f"/portal/leads/{foreign.id}", token),
        _get(portal_http, f"/portal/leads/{uuid.uuid4()}", token),
        _get(portal_http, f"/portal/leads/{non_lead.id}", token),
    ]
    for response in responses:
        assert response.status_code == 404
    bodies = {response.text for response in responses}
    assert len(bodies) == 1
    assert responses[0].json()["detail"] == NOT_FOUND_DETAIL


@requires_db
def test_search_cannot_escape_tenant_scope(db, portal_http, client_row,
                                           office_b):
    """A search term matching ONLY the other office's lead returns an empty
    tenant-scoped result, never the foreign row."""
    user_a = _bind_office_user(db, client_row)
    _make_lead(db, client_row, name="Alpha Own", last_lead_at=_minutes_ago(1))
    _make_lead(db, office_b, name="Zebra Foreign",
               last_lead_at=_minutes_ago(1))

    body = _get(portal_http, "/portal/leads?q=Zebra",
                _token(user_a.auth_user_id)).json()
    assert body["total"] == 0
    assert body["leads"] == []


# ---------------------------------------------------------------------------
# Search, filters, ordering, pagination.
# ---------------------------------------------------------------------------

@requires_db
def test_search_wildcards_match_literally(db, portal_http, client_row):
    """q="%" matches only a lead whose data CONTAINS a percent sign - it is
    never treated as match-everything."""
    user = _bind_office_user(db, client_row)
    percent = _make_lead(db, client_row, name="100% Smile Co",
                         last_lead_at=_minutes_ago(1))
    _make_lead(db, client_row, name="Plain Name", last_lead_at=_minutes_ago(2))

    body = _get(portal_http, "/portal/leads?q=%25",
                _token(user.auth_user_id)).json()
    assert {lead["lead_id"] for lead in body["leads"]} == {str(percent.id)}


@requires_db
def test_search_matches_name_phone_and_email_case_insensitively(
        db, portal_http, client_row):
    user = _bind_office_user(db, client_row)
    by_name = _make_lead(db, client_row, name="Jordan Rivera",
                         last_lead_at=_minutes_ago(1))
    by_phone = _make_lead(db, client_row, name="Someone Else",
                          phone="917-555-7788", last_lead_at=_minutes_ago(2))
    by_email = _make_lead(db, client_row, name="Third Person",
                          email="smile@example.test",
                          last_lead_at=_minutes_ago(3))
    token = _token(user.auth_user_id)

    assert {l["lead_id"] for l in
            _get(portal_http, "/portal/leads?q=jordan", token)
            .json()["leads"]} == {str(by_name.id)}
    assert {l["lead_id"] for l in
            _get(portal_http, "/portal/leads?q=7788", token)
            .json()["leads"]} == {str(by_phone.id)}
    assert {l["lead_id"] for l in
            _get(portal_http, "/portal/leads?q=SMILE%40example", token)
            .json()["leads"]} == {str(by_email.id)}


@requires_db
def test_status_filter_and_unknown_status_400_over_http(db, portal_http,
                                                        client_row):
    user = _bind_office_user(db, client_row)
    _make_lead(db, client_row, name="Fresh", status="new",
               last_lead_at=_minutes_ago(1))
    done = _make_lead(db, client_row, name="Done", status="completed",
                      last_lead_at=_minutes_ago(2))
    token = _token(user.auth_user_id)

    body = _get(portal_http, "/portal/leads?status=completed", token).json()
    assert {lead["lead_id"] for lead in body["leads"]} == {str(done.id)}

    bad = _get(portal_http, "/portal/leads?status=hot", token)
    assert bad.status_code == 400
    assert "status must be one of" in bad.json()["detail"]


@requires_db
def test_days_window_filters_on_last_lead_at(db, portal_http, client_row):
    """A 40-day-old lead is outside days=30 but inside days=365; a lead with
    NULL last_lead_at can never satisfy a window but appears without one."""
    user = _bind_office_user(db, client_row)
    recent = _make_lead(db, client_row, name="Recent",
                        last_lead_at=_days_ago(1))
    old = _make_lead(db, client_row, name="Old", last_lead_at=_days_ago(40))
    undated = _make_lead(db, client_row, name="Undated", last_lead_at=None)
    token = _token(user.auth_user_id)

    within_30 = _get(portal_http, "/portal/leads?days=30", token).json()
    assert {l["lead_id"] for l in within_30["leads"]} == {str(recent.id)}

    within_365 = _get(portal_http, "/portal/leads?days=365", token).json()
    assert {l["lead_id"] for l in within_365["leads"]} == {
        str(recent.id), str(old.id)}

    unwindowed = _get(portal_http, "/portal/leads", token).json()
    assert {l["lead_id"] for l in unwindowed["leads"]} == {
        str(recent.id), str(old.id), str(undated.id)}


@requires_db
def test_ordering_urgency_flags_then_recency(db, portal_http, client_row):
    """Emergency outranks priority outranks after-hours outranks plain
    recency - the operator-admin ordering, now tenant-scoped."""
    user = _bind_office_user(db, client_row)
    plain_new = _make_lead(db, client_row, name="Plain Newest",
                           last_lead_at=_minutes_ago(1))
    plain_old = _make_lead(db, client_row, name="Plain Oldest",
                           last_lead_at=_minutes_ago(60))
    outside = _make_lead(db, client_row, name="After Hours", outside=True,
                         last_lead_at=_minutes_ago(50))
    priority = _make_lead(db, client_row, name="Priority", priority=True,
                          last_lead_at=_minutes_ago(40))
    emergency = _make_lead(db, client_row, name="Emergency", emergency=True,
                           last_lead_at=_minutes_ago(30))

    body = _get(portal_http, "/portal/leads",
                _token(user.auth_user_id)).json()
    ordered_ids = [lead["lead_id"] for lead in body["leads"]]
    assert ordered_ids == [str(emergency.id), str(priority.id),
                           str(outside.id), str(plain_new.id),
                           str(plain_old.id)]


@requires_db
def test_pagination_slices_are_disjoint_and_total_is_stable(db, portal_http,
                                                            client_row):
    user = _bind_office_user(db, client_row)
    seeded = {
        str(_make_lead(db, client_row, name=f"Lead {index}",
                       last_lead_at=_minutes_ago(index + 1)).id)
        for index in range(5)
    }
    token = _token(user.auth_user_id)

    collected = []
    for offset in (0, 2, 4):
        page = _get(portal_http,
                    f"/portal/leads?limit=2&offset={offset}", token).json()
        assert page["total"] == 5
        assert page["limit"] == 2
        assert page["offset"] == offset
        collected.extend(lead["lead_id"] for lead in page["leads"])
    assert len(collected) == 5              # no duplicates across pages
    assert set(collected) == seeded         # no dropped rows

    too_big = _get(portal_http, "/portal/leads?limit=101", token)
    assert too_big.status_code == 400
    negative = _get(portal_http, "/portal/leads?offset=-1", token)
    assert negative.status_code == 400


# ---------------------------------------------------------------------------
# Leak prevention: exact approved field sets, nothing more.
# ---------------------------------------------------------------------------

@requires_db
def test_list_items_expose_exactly_the_approved_fields(db, portal_http,
                                                       client_row):
    user = _bind_office_user(db, client_row)
    _make_lead(db, client_row, name="Pin Me", new_patient=True,
               time_window="Tue morning", email="pin@example.test",
               last_lead_at=_minutes_ago(1))

    body = _get(portal_http, "/portal/leads",
                _token(user.auth_user_id)).json()
    assert set(body.keys()) == {"total", "limit", "offset", "leads"}
    assert set(body["leads"][0].keys()) == APPROVED_SUMMARY_FIELDS
    assert body["leads"][0]["lead_patient_type"] == "new"


@requires_db
def test_detail_exposes_exactly_the_approved_fields_and_no_markers(
        db, portal_http, client_row):
    """The detail body carries the approved fields plus the transcript, and
    the serialized body never contains any forbidden marker (credential,
    tenant identifier, evidence column, booking state...)."""
    user = _bind_office_user(db, client_row)
    lead = _make_lead(db, client_row, name="Detail Lead",
                      new_patient=False, outside=True,
                      outside_note="Called at 9pm",
                      last_lead_at=_minutes_ago(1))
    _add_message(db, lead, "user", "My tooth hurts", _minutes_ago(9))
    _add_message(db, lead, "assistant", "I can help with that",
                 _minutes_ago(8))

    response = _get(portal_http, f"/portal/leads/{lead.id}",
                    _token(user.auth_user_id))
    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == APPROVED_DETAIL_FIELDS
    assert body["lead_patient_type"] == "returning"
    for message in body["messages"]:
        assert set(message.keys()) == APPROVED_MESSAGE_FIELDS
    lowered = response.text.lower()
    for marker in FORBIDDEN_BODY_MARKERS:
        assert marker not in lowered, f"forbidden marker {marker!r} leaked"


@requires_db
def test_detail_transcript_is_own_tenant_only_and_oldest_first(
        db, portal_http, client_row, office_b):
    """The transcript contains exactly the lead's own messages in
    created_at order; identical text seeded under the OTHER office's lead
    never bleeds in."""
    user_a = _bind_office_user(db, client_row)
    mine = _make_lead(db, client_row, name="Mine",
                      last_lead_at=_minutes_ago(1))
    theirs = _make_lead(db, office_b, name="Theirs",
                        last_lead_at=_minutes_ago(1))
    _add_message(db, mine, "user", "hello", _minutes_ago(30))
    _add_message(db, mine, "assistant", "hi there", _minutes_ago(29))
    _add_message(db, mine, "user", "I need a cleaning", _minutes_ago(28))
    _add_message(db, theirs, "user", "hello", _minutes_ago(30))

    body = _get(portal_http, f"/portal/leads/{mine.id}",
                _token(user_a.auth_user_id)).json()
    assert [m["content"] for m in body["messages"]] == [
        "hello", "hi there", "I need a cleaning"]
    assert [m["role"] for m in body["messages"]] == [
        "user", "assistant", "user"]
    assert body["messages_total"] == 3
    assert body["messages_truncated"] is False


# ---------------------------------------------------------------------------
# Dashboard.
# ---------------------------------------------------------------------------

@requires_db
def test_dashboard_counts_and_recent_leads_are_tenant_specific(
        db, portal_http, client_row, office_b):
    """Office A's dashboard reflects ONLY Office A: three conversations,
    two leads, one flagged priority, one active in the last 7 days - while
    Office B holds more rows of everything."""
    user_a = _bind_office_user(db, client_row)
    fresh = _make_lead(db, client_row, name="Fresh Lead", priority=True,
                       last_lead_at=_days_ago(1))
    _make_lead(db, client_row, name="Old Completed", status="completed",
               last_lead_at=_days_ago(30))
    _make_lead(db, client_row, name=None, is_lead=False, phone=None)
    for index in range(4):
        _make_lead(db, office_b, name=f"B Lead {index}", emergency=True,
                   last_lead_at=_days_ago(1))

    body = _get(portal_http, "/portal/dashboard",
                _token(user_a.auth_user_id)).json()
    assert set(body.keys()) == APPROVED_DASHBOARD_FIELDS
    assert body["practice_name"] == client_row.practice_name
    assert body["total_conversations"] == 3
    assert body["total_leads"] == 2
    assert body["urgent_leads"] == 1
    assert body["leads_last_7_days"] == 1
    recent_ids = [lead["lead_id"] for lead in body["recent_leads"]]
    assert str(fresh.id) in recent_ids
    assert len(recent_ids) == 2
    for lead in body["recent_leads"]:
        assert set(lead.keys()) == APPROVED_SUMMARY_FIELDS


@requires_db
def test_dashboard_recent_strip_is_capped(db, portal_http, client_row):
    from app.services import portal_leads_service as svc

    user = _bind_office_user(db, client_row)
    for index in range(svc.DASHBOARD_RECENT_LEADS + 3):
        _make_lead(db, client_row, name=f"Lead {index}",
                   last_lead_at=_minutes_ago(index + 1))

    body = _get(portal_http, "/portal/dashboard",
                _token(user.auth_user_id)).json()
    assert len(body["recent_leads"]) == svc.DASHBOARD_RECENT_LEADS
    assert body["total_leads"] == svc.DASHBOARD_RECENT_LEADS + 3


# ---------------------------------------------------------------------------
# Audit corrections (v1.0.1): A1 status semantics + A2 bounded transcript.
# ---------------------------------------------------------------------------

@requires_db
def test_completed_intake_is_not_represented_as_staff_handling(
        db, portal_http, client_row):
    """A1 bite: a lead whose intake Mia just completed must surface with
    the RAW system status only - no dashboard metric may translate the
    system-written lifecycle into a staff-handling claim. The old
    'new_leads' metric (which read status 'new' as 'untouched by staff')
    must be gone entirely."""
    user = _bind_office_user(db, client_row)
    done = _make_lead(db, client_row, name="Captured Request",
                      status="completed", last_lead_at=_minutes_ago(1))
    token = _token(user.auth_user_id)

    dashboard = _get(portal_http, "/portal/dashboard", token).json()
    assert "new_leads" not in dashboard          # the misleading metric is gone
    assert dashboard["urgent_leads"] == 0        # completed carries no urgency
    assert dashboard["total_leads"] == 1

    detail = _get(portal_http, f"/portal/leads/{done.id}", token).json()
    assert detail["lead_status"] == "completed"  # raw system value, unrelabeled


@requires_db
def test_urgent_leads_counts_emergency_or_priority_flags_only(
        db, portal_http, client_row):
    """A1: urgent_leads is a pure urgency-FLAG count - emergency OR
    priority, counted once per lead - and lead_status plays no part."""
    user = _bind_office_user(db, client_row)
    _make_lead(db, client_row, name="E", emergency=True,
               last_lead_at=_minutes_ago(1))
    _make_lead(db, client_row, name="P", priority=True,
               last_lead_at=_minutes_ago(2))
    _make_lead(db, client_row, name="EP", emergency=True, priority=True,
               last_lead_at=_minutes_ago(3))
    _make_lead(db, client_row, name="Plain new", status="new",
               last_lead_at=_minutes_ago(4))
    _make_lead(db, client_row, name="Plain completed", status="completed",
               last_lead_at=_minutes_ago(5))

    body = _get(portal_http, "/portal/dashboard",
                _token(user.auth_user_id)).json()
    assert body["urgent_leads"] == 3
    assert body["total_leads"] == 5


@requires_db
def test_recent_leads_strip_is_recency_ordered(db, portal_http, client_row):
    """A1 bite: the 'Recent leads' strip is ordered by recency ALONE -
    an old emergency row must not displace or outrank a newer plain lead.
    (The main Leads list keeps urgency-first ordering; proven separately.)"""
    user = _bind_office_user(db, client_row)
    old_emergency = _make_lead(db, client_row, name="Old Emergency",
                               emergency=True, last_lead_at=_days_ago(10))
    newer_plain = _make_lead(db, client_row, name="Newer Plain",
                             last_lead_at=_minutes_ago(1))

    body = _get(portal_http, "/portal/dashboard",
                _token(user.auth_user_id)).json()
    assert [lead["lead_id"] for lead in body["recent_leads"]] == [
        str(newer_plain.id), str(old_emergency.id)]


@requires_db
def test_transcript_is_bounded_and_truncation_is_explicit(
        db, portal_http, client_row, monkeypatch):
    """A2 bite: the transcript can never be unbounded. With the named
    limit lowered to 5, a 7-message conversation returns exactly the FIRST
    five chronologically, states the true total, and flags the cut - the
    office is never silently shown a partial transcript."""
    from app.services import portal_leads_service as svc

    monkeypatch.setattr(svc, "TRANSCRIPT_MESSAGE_LIMIT", 5)
    user = _bind_office_user(db, client_row)
    lead = _make_lead(db, client_row, name="Long Chat",
                      last_lead_at=_minutes_ago(1))
    for index in range(7):
        _add_message(db, lead, "user", f"message {index}",
                     _minutes_ago(60 - index))

    body = _get(portal_http, f"/portal/leads/{lead.id}",
                _token(user.auth_user_id)).json()
    assert len(body["messages"]) == 5
    assert [m["content"] for m in body["messages"]] == [
        f"message {index}" for index in range(5)]   # oldest first, first five
    assert body["messages_total"] == 7
    assert body["messages_truncated"] is True


@requires_db
def test_transcript_bound_does_not_weaken_tenant_isolation(
        db, portal_http, client_row, office_b, monkeypatch):
    """A2: with the bound active, a foreign lead id still returns the one
    indistinguishable 404 - the truncation pathway adds no new oracle."""
    from app.services import portal_leads_service as svc

    monkeypatch.setattr(svc, "TRANSCRIPT_MESSAGE_LIMIT", 5)
    user_a = _bind_office_user(db, client_row)
    foreign = _make_lead(db, office_b, name="Foreign Long Chat",
                         last_lead_at=_minutes_ago(1))
    for index in range(7):
        _add_message(db, foreign, "user", f"message {index}",
                     _minutes_ago(60 - index))

    response = _get(portal_http, f"/portal/leads/{foreign.id}",
                    _token(user_a.auth_user_id))
    assert response.status_code == 404
    assert response.json()["detail"] == NOT_FOUND_DETAIL


# ---------------------------------------------------------------------------
# P2 surface unchanged.
# ---------------------------------------------------------------------------

@requires_db
def test_portal_me_still_bootstraps_alongside_the_new_router(db, portal_http,
                                                             client_row):
    """The P2 identity endpoint keeps its exact contract with the P3-B1
    router mounted beside it."""
    user = _bind_office_user(db, client_row)
    response = _get(portal_http, "/portal/me", _token(user.auth_user_id))
    assert response.status_code == 200
    assert set(response.json().keys()) == {
        "client_id", "practice_name", "role", "email"}
