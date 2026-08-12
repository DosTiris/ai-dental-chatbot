# calendar_tests/test_portal_lead_writes.py
#
# P3-B2: the portal office workflow WRITE paths (status + note) - the
# first customer-facing portal mutations.
#
# Proven at the REAL HTTP layer (real portal routers, real P2 JWT
# authentication - only the session dependency is overridden, the
# test_portal_leads.py pattern):
#   - unauthenticated and cross-tenant writes fail closed; foreign and
#     nonexistent leads share the identical tenant-opaque 404; a smuggled
#     client_id (body AND query) changes nothing;
#   - the closed status vocabulary (contacted/booked/closed/null) is
#     enforced; "new" is refused - portal clearing is null;
#   - note rules: trimmed 1..2000 accepted, whitespace-only refused, >2000
#     refused, null clears;
#   - every accepted mutation (set, same-value save, clear) advances the
#     SERVER-generated concurrency token; lead_status never changes;
#     status writes never touch note fields and vice versa;
#   - optimistic concurrency is REAL compare-and-set: a stale token gets
#     409 and changes nothing, and a two-session interleave cannot revert
#     newer persisted state (the CAS bite: a blind or echo-timestamp
#     implementation fails these);
#   - the workflow response carries exactly the approved slice;
#   - v1.0.1 (audit item 5): under a CONTROLLED clock - frozen, and moved
#     BACKWARD behind the persisted token - every accepted mutation still
#     produces a STRICTLY newer token (different-value save, same-value
#     save, and clear-to-NULL). A plain-clock token implementation fails
#     both controlled-clock tests.

import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from calendar_tests.conftest import requires_db  # noqa: E402

import jwt as pyjwt  # noqa: E402

pytestmark = requires_db

TEST_SECRET = "portal-test-secret-0123456789abcdef0123456789"
TEST_ISSUER = "https://p2-test-project.supabase.co/auth/v1"
AUDIENCE = "authenticated"

STATUS_PATH = "/portal/leads/{lead_id}/status"
NOTE_PATH = "/portal/leads/{lead_id}/note"
WORKFLOW_KEYS = {"lead_id", "office_status", "office_status_updated_at",
                 "office_note", "office_note_updated_at"}


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
    )
    db.add(client)
    db.commit()
    return client


@pytest.fixture(scope="module")
def office_users_table(engine):
    """Run the REAL migration 007 (sole creation authority for office_users)
    up before this module and down after it (test_portal_leads.py pattern)."""
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
    """Real app with BOTH portal routers over HTTP; real JWT auth; only the
    session dependency overridden (test_portal_leads.py pattern)."""
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


def _make_lead(db, client, **overrides):
    from app.models import Conversation

    values = dict(
        id=uuid.uuid4(),
        client_id=client.id,
        lead_name="Kevin Alvarado",
        lead_phone="516-555-1234",
        lead_reason="cleaning/checkup",
        is_lead=True,
        lead_status="completed",
        last_lead_at=datetime.now(timezone.utc),
    )
    values.update(overrides)
    conversation = Conversation(**values)
    db.add(conversation)
    db.commit()
    return conversation


def _put(portal_http, path_template, lead_id, body, token):
    headers = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return portal_http.put(
        path_template.format(lead_id=lead_id), json=body, headers=headers)


def _status_body(status, expected):
    return {"office_status": status,
            "expected_office_status_updated_at": expected}


def _note_body(note, expected):
    return {"office_note": note,
            "expected_office_note_updated_at": expected}


def _iso(value):
    """Normalize a datetime to the wire notation pydantic v2 uses (Z suffix)
    so equality against response tokens compares instants, not notations."""
    return value.isoformat().replace("+00:00", "Z")


def _reload(db, conversation):
    db.expire_all()
    from app.models import Conversation

    return db.query(Conversation).filter(
        Conversation.id == conversation.id).one()


# ---------------------------------------------------------------------------
# Authentication / tenancy
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path_template,body", [
    (STATUS_PATH, {"office_status": "contacted",
                   "expected_office_status_updated_at": None}),
    (NOTE_PATH, {"office_note": "call back",
                 "expected_office_note_updated_at": None}),
])
def test_unauthenticated_writes_fail_closed(
        portal_http, db, client_row, path_template, body):
    """No token and a garbage token both 401, and nothing is written."""
    lead = _make_lead(db, client_row)

    no_token = _put(portal_http, path_template, lead.id, body, None)
    bad_token = _put(portal_http, path_template, lead.id, body, "not-a-jwt")
    assert no_token.status_code == 401
    assert bad_token.status_code == 401

    row = _reload(db, lead)
    assert row.office_status is None and row.office_note is None
    assert row.office_status_updated_at is None
    assert row.office_note_updated_at is None


def test_own_office_can_mutate_and_foreign_cannot(
        portal_http, db, client_row, second_client,
        office_user_a, office_user_b):
    """Office A mutates its own lead; Office B gets the tenant-opaque 404
    against that same lead and nothing changes."""
    lead = _make_lead(db, client_row)

    own = _put(portal_http, STATUS_PATH, lead.id,
               _status_body("contacted", None),
               _token(office_user_a.auth_user_id))
    assert own.status_code == 200
    assert own.json()["office_status"] == "contacted"

    foreign = _put(portal_http, STATUS_PATH, lead.id,
                   _status_body("booked", None),
                   _token(office_user_b.auth_user_id))
    nonexistent = _put(portal_http, STATUS_PATH, uuid.uuid4(),
                       _status_body("booked", None),
                       _token(office_user_b.auth_user_id))
    assert foreign.status_code == 404
    assert nonexistent.status_code == 404
    # Tenant opacity: foreign and nonexistent are INDISTINGUISHABLE.
    assert foreign.json() == nonexistent.json()

    row = _reload(db, lead)
    assert row.office_status == "contacted"          # B changed nothing


def test_smuggled_client_id_cannot_alter_tenancy(
        portal_http, db, client_row, second_client,
        office_user_a, office_user_b):
    """A client_id in the body AND the query string is ignored: tenancy
    comes exclusively from the verified identity."""
    lead_b = _make_lead(db, second_client)

    body = _status_body("booked", None)
    body["client_id"] = str(second_client.id)
    response = portal_http.put(
        STATUS_PATH.format(lead_id=lead_b.id)
        + f"?client_id={second_client.id}",
        json=body,
        headers={"Authorization":
                 f"Bearer {_token(office_user_a.auth_user_id)}"},
    )
    assert response.status_code == 404               # still Office A's view

    row = _reload(db, lead_b)
    assert row.office_status is None                 # untouched


# ---------------------------------------------------------------------------
# Status writer
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("word", ["contacted", "booked", "closed"])
def test_each_portal_status_value_accepted(
        portal_http, db, client_row, office_user_a, word):
    lead = _make_lead(db, client_row)
    response = _put(portal_http, STATUS_PATH, lead.id,
                    _status_body(word, None),
                    _token(office_user_a.auth_user_id))
    assert response.status_code == 200
    payload = response.json()
    assert set(payload.keys()) == WORKFLOW_KEYS
    assert payload["office_status"] == word
    assert payload["office_status_updated_at"] is not None

    row = _reload(db, lead)
    assert row.office_status == word
    assert row.lead_status == "completed"            # system value untouched


@pytest.mark.parametrize("bad", ["new", "NEW", "followup", "", "Contacted"])
def test_arbitrary_or_legacy_status_rejected(
        portal_http, db, client_row, office_user_a, bad):
    """The closed vocabulary refuses everything else - INCLUDING the legacy
    reset word "new": portal clearing is null. Nothing is written."""
    lead = _make_lead(db, client_row)
    response = _put(portal_http, STATUS_PATH, lead.id,
                    _status_body(bad, None),
                    _token(office_user_a.auth_user_id))
    assert response.status_code == 400

    row = _reload(db, lead)
    assert row.office_status is None
    assert row.office_status_updated_at is None


def test_status_clear_same_value_and_token_advance(
        portal_http, db, client_row, office_user_a):
    """Set -> same-value save -> clear: every accepted mutation advances the
    server token; clear keeps a non-NULL advancing token; lead_status and
    the note fields never move."""
    note_token = datetime(2026, 3, 3, tzinfo=timezone.utc)
    lead = _make_lead(db, client_row, office_note="existing note",
                      office_note_updated_at=note_token)
    auth = _token(office_user_a.auth_user_id)

    first = _put(portal_http, STATUS_PATH, lead.id,
                 _status_body("contacted", None), auth)
    assert first.status_code == 200
    token_1 = first.json()["office_status_updated_at"]

    same = _put(portal_http, STATUS_PATH, lead.id,
                _status_body("contacted", token_1), auth)
    assert same.status_code == 200
    token_2 = same.json()["office_status_updated_at"]
    assert token_2 > token_1                         # ISO strings order

    clear = _put(portal_http, STATUS_PATH, lead.id,
                 _status_body(None, token_2), auth)
    assert clear.status_code == 200
    cleared = clear.json()
    assert cleared["office_status"] is None
    token_3 = cleared["office_status_updated_at"]
    assert token_3 is not None and token_3 > token_2

    row = _reload(db, lead)
    assert row.office_status is None
    assert row.office_status_updated_at is not None
    assert row.lead_status == "completed"
    assert row.office_note == "existing note"        # note pair untouched
    assert row.office_note_updated_at == note_token


def test_stale_status_token_conflicts_and_changes_nothing(
        portal_http, db, client_row, office_user_a):
    lead = _make_lead(db, client_row)
    auth = _token(office_user_a.auth_user_id)

    first = _put(portal_http, STATUS_PATH, lead.id,
                 _status_body("contacted", None), auth)
    fresh_token = first.json()["office_status_updated_at"]

    stale = _put(portal_http, STATUS_PATH, lead.id,
                 _status_body("closed", None), auth)     # None is now stale
    assert stale.status_code == 409

    row = _reload(db, lead)
    assert row.office_status == "contacted"
    assert _iso(row.office_status_updated_at) == fresh_token


# ---------------------------------------------------------------------------
# Note writer
# ---------------------------------------------------------------------------

def test_valid_note_saves_trimmed(portal_http, db, client_row, office_user_a):
    lead = _make_lead(db, client_row)
    response = _put(portal_http, NOTE_PATH, lead.id,
                    _note_body("  Called patient, left voicemail.  ", None),
                    _token(office_user_a.auth_user_id))
    assert response.status_code == 200
    payload = response.json()
    assert payload["office_note"] == "Called patient, left voicemail."
    assert payload["office_note_updated_at"] is not None

    row = _reload(db, lead)
    assert row.office_note == "Called patient, left voicemail."
    assert row.lead_status == "completed"


def test_note_boundary_2000_accepted_2001_rejected(
        portal_http, db, client_row, office_user_a):
    lead = _make_lead(db, client_row)
    auth = _token(office_user_a.auth_user_id)

    ok = _put(portal_http, NOTE_PATH, lead.id,
              _note_body("x" * 2000, None), auth)
    assert ok.status_code == 200
    token_1 = ok.json()["office_note_updated_at"]

    too_long = _put(portal_http, NOTE_PATH, lead.id,
                    _note_body("x" * 2001, token_1), auth)
    assert too_long.status_code == 400

    row = _reload(db, lead)
    assert row.office_note == "x" * 2000             # refused write changed nothing
    assert _iso(row.office_note_updated_at) == token_1


@pytest.mark.parametrize("blank", [" ", "   ", "\t", "\n \t"])
def test_whitespace_only_note_rejected(
        portal_http, db, client_row, office_user_a, blank):
    lead = _make_lead(db, client_row)
    response = _put(portal_http, NOTE_PATH, lead.id,
                    _note_body(blank, None),
                    _token(office_user_a.auth_user_id))
    assert response.status_code == 400

    row = _reload(db, lead)
    assert row.office_note is None
    assert row.office_note_updated_at is None


def test_note_clear_same_value_and_token_advance(
        portal_http, db, client_row, office_user_a):
    """Save -> same-value save -> null clear: token advances each time;
    status fields never move."""
    status_token = datetime(2026, 4, 4, tzinfo=timezone.utc)
    lead = _make_lead(db, client_row, office_status="booked",
                      office_status_updated_at=status_token)
    auth = _token(office_user_a.auth_user_id)

    first = _put(portal_http, NOTE_PATH, lead.id,
                 _note_body("call back after 3pm", None), auth)
    assert first.status_code == 200
    token_1 = first.json()["office_note_updated_at"]

    same = _put(portal_http, NOTE_PATH, lead.id,
                _note_body("call back after 3pm", token_1), auth)
    assert same.status_code == 200
    token_2 = same.json()["office_note_updated_at"]
    assert token_2 > token_1

    clear = _put(portal_http, NOTE_PATH, lead.id, _note_body(None, token_2),
                 auth)
    assert clear.status_code == 200
    cleared = clear.json()
    assert cleared["office_note"] is None
    assert cleared["office_note_updated_at"] > token_2

    row = _reload(db, lead)
    assert row.office_note is None
    assert row.office_note_updated_at is not None
    assert row.office_status == "booked"             # status pair untouched
    assert row.office_status_updated_at == status_token


def test_stale_note_token_conflicts_and_changes_nothing(
        portal_http, db, client_row, office_user_a):
    lead = _make_lead(db, client_row)
    auth = _token(office_user_a.auth_user_id)

    first = _put(portal_http, NOTE_PATH, lead.id,
                 _note_body("version one", None), auth)
    fresh_token = first.json()["office_note_updated_at"]

    stale = _put(portal_http, NOTE_PATH, lead.id,
                 _note_body("version two", None), auth)
    assert stale.status_code == 409

    row = _reload(db, lead)
    assert row.office_note == "version one"
    assert _iso(row.office_note_updated_at) == fresh_token


# ---------------------------------------------------------------------------
# The CAS bite: two-session interleave cannot revert newer persisted state
# ---------------------------------------------------------------------------

def test_interleaved_stale_write_cannot_revert_newer_state(
        db, client_row, engine):
    """Session A loads the lead (token t0=None). Session B then sets the
    status (token t1). A's write with its remembered t0 MUST lose: the
    service returns 409 and B's value survives. A blind UPDATE or an
    echoed-client-timestamp implementation fails this test."""
    from fastapi import HTTPException
    from sqlalchemy.orm import sessionmaker
    from app.services import portal_leads_service as svc

    lead = _make_lead(db, client_row)
    token_a_saw = lead.office_status_updated_at      # None: never touched

    OtherSession = sessionmaker(bind=engine)
    session_b = OtherSession()
    try:
        from app.models import Client
        client_b_view = session_b.get(Client, client_row.id)
        svc.set_office_status(session_b, client_b_view, lead.id,
                              "booked", None)        # B wins first
    finally:
        session_b.close()

    with pytest.raises(HTTPException) as conflict:
        svc.set_office_status(db, client_row, lead.id,
                              "contacted", token_a_saw)
    assert conflict.value.status_code == 409

    row = _reload(db, lead)
    assert row.office_status == "booked"             # newer state survived


def test_workflow_fields_present_in_lead_detail(
        portal_http, db, client_row, office_user_a):
    """The extended read contract: detail carries the four office fields."""
    lead = _make_lead(db, client_row, office_status="closed",
                      office_status_updated_at=datetime(
                          2026, 5, 5, tzinfo=timezone.utc),
                      office_note="done",
                      office_note_updated_at=datetime(
                          2026, 5, 6, tzinfo=timezone.utc))
    response = portal_http.get(
        f"/portal/leads/{lead.id}",
        headers={"Authorization":
                 f"Bearer {_token(office_user_a.auth_user_id)}"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["office_status"] == "closed"
    assert payload["office_note"] == "done"
    assert payload["office_status_updated_at"].startswith("2026-05-05")
    assert payload["office_note_updated_at"].startswith("2026-05-06")


# ---------------------------------------------------------------------------
# v1.0.1: strict token advancement under a CONTROLLED clock (audit item 5)
# ---------------------------------------------------------------------------

def test_token_strictly_advances_under_frozen_clock(
        db, client_row, monkeypatch):
    """Pin the module's single clock source to ONE instant: set ->
    same-value save -> clear-to-NULL must each advance the persisted token
    anyway. An equal (non-advancing) token would let a browser holding the
    pre-mutation token silently pass a stale write."""
    from app.services import portal_leads_service as svc

    frozen = datetime(2026, 8, 12, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(svc, "_now_utc", lambda: frozen)

    lead = _make_lead(db, client_row)

    first = svc.set_office_status(db, client_row, lead.id, "contacted", None)
    token_1 = first.office_status_updated_at
    assert token_1 is not None

    same = svc.set_office_status(db, client_row, lead.id, "contacted",
                                 token_1)
    token_2 = same.office_status_updated_at
    assert token_2 > token_1              # frozen clock - still advanced

    cleared = svc.set_office_status(db, client_row, lead.id, None, token_2)
    token_3 = cleared.office_status_updated_at
    assert cleared.office_status is None
    assert token_3 is not None and token_3 > token_2

    row = _reload(db, lead)
    assert row.office_status is None
    assert row.office_status_updated_at == token_3


def test_token_strictly_advances_under_backward_clock(
        db, client_row, monkeypatch):
    """Move the clock BEHIND the persisted token (VM clock step / NTP
    correction): an accepted mutation must produce a token strictly newer
    than the persisted one - never the earlier wall-clock reading."""
    from app.services import portal_leads_service as svc

    persisted = datetime(2026, 8, 12, 12, 0, 0, tzinfo=timezone.utc)
    lead = _make_lead(db, client_row, office_note="existing",
                      office_note_updated_at=persisted)

    behind = persisted - timedelta(minutes=5)
    monkeypatch.setattr(svc, "_now_utc", lambda: behind)

    saved = svc.set_office_note(db, client_row, lead.id, "existing",
                                persisted)
    new_token = saved.office_note_updated_at
    assert new_token > persisted          # never the backward clock reading
    assert new_token > behind

    row = _reload(db, lead)
    assert row.office_note == "existing"
    assert row.office_note_updated_at == new_token
