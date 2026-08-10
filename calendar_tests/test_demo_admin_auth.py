# calendar_tests/test_demo_admin_auth.py
#
# SEC-1 (P1B finding F-P1B-1): the /admin/demo-requests* management endpoints
# in app/routes/demo.py must require the global operator ADMIN_API_KEY --
# the SAME canonical require_admin dependency app/routes/admin.py uses --
# while the public POST /demo-request intake stays public and unchanged.
#
# TWO GROUPS:
#   * No-database tests: run anywhere (no TEST_DATABASE_URL needed). They
#     prove the auth gate rejects missing/blank/wrong keys with 401 BEFORE
#     the handler can touch SessionLocal at all, that only a valid key
#     reaches the database boundary, that the public route carries no admin
#     dependency, and that demo.py reuses admin.py's require_admin (single
#     owner, Rule 3) rather than a second implementation.
#   * Database tests (requires_db, house harness rules in conftest.py):
#     prove pre-patch functional semantics are unchanged under a valid key,
#     that rejected writes mutate nothing, and that the public intake still
#     inserts and notifies without any admin key (email senders replaced by
#     recorders -- no real provider is ever called; house rule).
#
# Run (owner-local, PowerShell 5.1):
#   $env:ALLOW_DESTRUCTIVE_CALENDAR_TESTS="yes"
#   $env:TEST_DATABASE_URL="postgresql://postgres:test@localhost:5433/mia_calendar_test"
#   python -m pytest calendar_tests\test_demo_admin_auth.py -v

import os
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from calendar_tests.conftest import requires_db  # noqa: E402  (env bootstrap)

# app.config refuses to import without DATABASE_URL. conftest sets it only
# when TEST_DATABASE_URL is present; the no-database tests below never open a
# connection (SessionLocal is either bombed or unreachable behind a 401), so
# an unreachable placeholder keeps them runnable anywhere. setdefault never
# overrides the real test database when TEST_DATABASE_URL is set.
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://placeholder:placeholder@localhost:1/never_connected_placeholder",
)

WRONG_KEY = "definitely-not-the-admin-key"

# (method, path-template, json-body) for the three protected management routes.
ADMIN_ROUTE_CASES = [
    ("GET", "/admin/demo-requests", None),
    ("POST", "/admin/demo-requests/{rid}/status", {"status": "contacted"}),
    ("POST", "/admin/demo-requests/{rid}/notes", {"notes": "sec1 note"}),
]


def _valid_admin_key() -> str:
    """The effective global operator key (conftest defaults it to a test
    value before any app import). Never printed, never asserted on."""
    from app.config import ADMIN_API_KEY

    if not ADMIN_API_KEY:
        pytest.skip("ADMIN_API_KEY resolved empty; conftest normally sets a test value")
    return ADMIN_API_KEY


def _request(client, method, path_template, body, headers):
    path = path_template.format(rid=str(uuid.uuid4()))
    if method == "GET":
        return client.get(path, headers=headers)
    return client.post(path, json=body, headers=headers)


# ---------------------------------------------------------------------------
# No-database fixture: real demo router, SessionLocal and both email senders
# replaced by recording bombs -- any call is BOTH recorded and fatal.
# ---------------------------------------------------------------------------

@pytest.fixture()
def nodb_http(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.routes import demo as demo_module

    calls = []

    def _forbid(name):
        def _bomb(*args, **kwargs):
            calls.append(name)
            raise AssertionError(
                f"{name} must not run for this request (SEC-1 auth gate)"
            )
        return _bomb

    monkeypatch.setattr(demo_module, "SessionLocal", _forbid("SessionLocal"))
    monkeypatch.setattr(demo_module, "send_demo_request_email", _forbid("send_demo_request_email"))
    monkeypatch.setattr(demo_module, "send_demo_confirmation_email", _forbid("send_demo_confirmation_email"))

    app = FastAPI()
    app.include_router(demo_module.router)
    # raise_server_exceptions=False: a bomb that fires inside a handler must
    # surface as an ordinary 500 response, not crash the test client, so the
    # calls list stays assertable in both directions.
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, calls


@pytest.mark.parametrize("method,path_template,body", ADMIN_ROUTE_CASES)
def test_missing_key_is_401_and_never_touches_the_database(nodb_http, method, path_template, body):
    client, calls = nodb_http
    response = _request(client, method, path_template, body, headers=None)
    assert response.status_code == 401
    assert response.json()["detail"] == "Unauthorized"
    assert calls == []  # rejected BEFORE SessionLocal -- zero database contact


@pytest.mark.parametrize("method,path_template,body", ADMIN_ROUTE_CASES)
def test_wrong_key_is_401_and_never_touches_the_database(nodb_http, method, path_template, body):
    client, calls = nodb_http
    response = _request(client, method, path_template, body,
                        headers={"X-Admin-Key": WRONG_KEY})
    assert response.status_code == 401
    assert response.json()["detail"] == "Unauthorized"
    assert calls == []


@pytest.mark.parametrize("method,path_template,body", ADMIN_ROUTE_CASES)
def test_blank_key_is_401_and_never_touches_the_database(nodb_http, method, path_template, body):
    client, calls = nodb_http
    response = _request(client, method, path_template, body,
                        headers={"X-Admin-Key": "   "})
    assert response.status_code == 401
    assert calls == []


@pytest.mark.parametrize("method,path_template,body", ADMIN_ROUTE_CASES)
def test_valid_key_is_the_only_path_to_the_database_boundary(nodb_http, method, path_template, body):
    """With the REAL key the dependency passes and the handler reaches its
    first SessionLocal() call -- where the bomb records exactly one contact.
    Together with the three rejection tests this pins the gate as the single
    difference between never-touching and touching the database."""
    client, calls = nodb_http
    response = _request(client, method, path_template, body,
                        headers={"X-Admin-Key": _valid_admin_key()})
    assert response.status_code != 401
    assert calls == ["SessionLocal"]


def _flatten_dependency_calls(dependant):
    found = []
    for sub in dependant.dependencies:
        found.append(sub.call)
        found.extend(_flatten_dependency_calls(sub))
    return found


def test_public_route_has_no_admin_dependency_and_admin_routes_do():
    """Structural proof, independent of transport AND of FastAPI's app
    composition internals: assert directly on the APIRouter's own route
    declarations (dependants are populated at decoration time, so this holds
    on the pinned fastapi==0.128.3 and on newer versions alike)."""
    from app.routes import demo as demo_module
    from app.routes.admin import require_admin

    admin_paths = {
        "/admin/demo-requests",
        "/admin/demo-requests/{request_id}/status",
        "/admin/demo-requests/{request_id}/notes",
    }
    seen_admin_paths = set()
    seen_public = False
    for route in demo_module.router.routes:
        path = getattr(route, "path", "")
        dependant = getattr(route, "dependant", None)
        if dependant is None:
            continue
        deps = _flatten_dependency_calls(dependant)
        if path == "/demo-request":
            seen_public = True
            assert require_admin not in deps
        if path in admin_paths:
            assert require_admin in deps
            seen_admin_paths.add(path)
    assert seen_public
    assert seen_admin_paths == admin_paths


def test_require_admin_is_the_canonical_admin_helper():
    """Rule 3: demo.py reuses admin.py's dependency object itself -- there is
    no second global-admin authentication implementation."""
    from app.routes import admin as admin_module
    from app.routes import demo as demo_module

    assert demo_module.require_admin is admin_module.require_admin


# ---------------------------------------------------------------------------
# Database tests (house harness: throwaway local PostgreSQL only).
# demo_requests has no ORM model or migration in-repo (DDL lives Supabase-
# side), so the harness creates a structural stand-in table and drops it.
# ---------------------------------------------------------------------------

# Module scope: the DROP at teardown needs ACCESS EXCLUSIVE; running it after
# every test would deadlock against the function-scoped db session's still-
# open read transaction (fixtures finalize in reverse order). One create per
# module + per-test row cleanup in db_http keeps tests isolated without locks.
@pytest.fixture(scope="module")
def demo_requests_table(engine):
    with engine.begin() as conn:
        conn.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS demo_requests (
                id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                name        text,
                practice_name text,
                email       text,
                phone       text,
                website     text,
                interest    text,
                message     text,
                source      text,
                status      text,
                notes       text,
                created_at  timestamptz NOT NULL DEFAULT now()
            )
            """
        )
    yield
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS demo_requests")


@pytest.fixture()
def db_http(db, demo_requests_table, monkeypatch):
    """Real router over the real (throwaway) database; only the two email
    senders are replaced by recorders so no provider is ever called."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.routes import demo as demo_module
    from sqlalchemy import text as sql_text

    db.execute(sql_text("DELETE FROM demo_requests"))
    db.commit()

    email_calls = {"office": [], "confirmation": []}
    monkeypatch.setattr(
        demo_module, "send_demo_request_email",
        lambda payload: email_calls["office"].append(payload),
    )
    monkeypatch.setattr(
        demo_module, "send_demo_confirmation_email",
        lambda payload: email_calls["confirmation"].append(payload),
    )

    app = FastAPI()
    app.include_router(demo_module.router)
    with TestClient(app) as client:
        yield client, email_calls


def _insert_row(db, status="new", notes=None):
    from sqlalchemy import text as sql_text

    row = db.execute(
        sql_text(
            """
            INSERT INTO demo_requests
                (name, practice_name, email, phone, website, interest,
                 message, source, status, notes)
            VALUES
                ('Pat Tester', 'Test Dental', 'pat@example.test', '5165550100',
                 NULL, 'mia', NULL, 'dos_tiris_website', :status, :notes)
            RETURNING id
            """
        ),
        {"status": status, "notes": notes},
    ).first()
    db.commit()
    return str(row[0])


def _fetch_row(db, rid):
    from sqlalchemy import text as sql_text

    return db.execute(
        sql_text("SELECT status, notes FROM demo_requests WHERE id = :id"),
        {"id": rid},
    ).mappings().first()


@requires_db
def test_valid_key_lists_demo_requests(db, db_http):
    client, _ = db_http
    first = _insert_row(db)
    second = _insert_row(db, status="contacted", notes="left voicemail")

    response = client.get("/admin/demo-requests",
                          headers={"X-Admin-Key": _valid_admin_key()})
    assert response.status_code == 200
    items = response.json()
    by_id = {item["id"]: item for item in items}
    assert {first, second} <= set(by_id)
    # Pre-patch response shape preserved, including the notes_text alias.
    for item in (by_id[first], by_id[second]):
        for field in ("id", "name", "practice_name", "email", "phone",
                      "website", "interest", "message", "source", "status",
                      "notes", "notes_text", "created_at"):
            assert field in item
    assert by_id[second]["notes_text"] == "left voicemail"
    assert by_id[first]["notes_text"] == ""  # COALESCE(notes, '')


@requires_db
def test_status_semantics_unchanged_with_valid_key(db, db_http):
    client, _ = db_http
    rid = _insert_row(db)
    key = {"X-Admin-Key": _valid_admin_key()}

    ok = client.post(f"/admin/demo-requests/{rid}/status",
                     json={"status": "contacted"}, headers=key)
    assert ok.status_code == 200
    assert ok.json() == {"ok": True, "status": "contacted"}
    assert _fetch_row(db, rid)["status"] == "contacted"

    bad = client.post(f"/admin/demo-requests/{rid}/status",
                      json={"status": "bogus"}, headers=key)
    assert bad.status_code == 400
    assert bad.json()["detail"] == "Invalid status."
    assert _fetch_row(db, rid)["status"] == "contacted"  # unchanged

    # Pre-patch behavior pinned: an unknown (valid-uuid) id still returns
    # ok=true with zero rows updated -- SEC-1 changes authentication only.
    ghost = client.post(f"/admin/demo-requests/{uuid.uuid4()}/status",
                        json={"status": "closed"}, headers=key)
    assert ghost.status_code == 200
    assert ghost.json() == {"ok": True, "status": "closed"}
    assert _fetch_row(db, rid)["status"] == "contacted"


@requires_db
def test_notes_semantics_unchanged_with_valid_key(db, db_http):
    client, _ = db_http
    rid = _insert_row(db)
    key = {"X-Admin-Key": _valid_admin_key()}

    ok = client.post(f"/admin/demo-requests/{rid}/notes",
                     json={"notes": "call Tuesday"}, headers=key)
    assert ok.status_code == 200
    assert ok.json() == {"ok": True, "id": rid, "notes": "call Tuesday"}
    assert _fetch_row(db, rid)["notes"] == "call Tuesday"

    missing = client.post(f"/admin/demo-requests/{uuid.uuid4()}/notes",
                          json={"notes": "x"}, headers=key)
    assert missing.status_code == 404
    assert missing.json()["detail"] == "Demo request not found."


@requires_db
def test_rejected_writes_mutate_nothing(db, db_http):
    client, _ = db_http
    rid = _insert_row(db, status="new", notes=None)

    for headers in (None, {"X-Admin-Key": WRONG_KEY}):
        s = client.post(f"/admin/demo-requests/{rid}/status",
                        json={"status": "closed"}, headers=headers)
        n = client.post(f"/admin/demo-requests/{rid}/notes",
                        json={"notes": "tampered"}, headers=headers)
        assert s.status_code == 401
        assert n.status_code == 401

    row = _fetch_row(db, rid)
    assert row["status"] == "new"
    assert row["notes"] is None


@requires_db
def test_public_intake_remains_public_and_unchanged(db, db_http):
    client, email_calls = db_http
    from sqlalchemy import text as sql_text

    payload = {
        "name": "Sam Public",
        "practice_name": "Public Smiles",
        "email": "sam@example.test",
        "phone": "(516) 555-0199",   # punctuation: proves digit normalization
        "website": None,
        "interest": "mia",
        "message": None,
    }
    response = client.post("/demo-request", json=payload)  # NO admin key
    assert response.status_code == 200
    assert response.json()["ok"] is True

    row = db.execute(sql_text(
        "SELECT phone, source, status FROM demo_requests WHERE email = 'sam@example.test'"
    )).mappings().first()
    assert row is not None
    assert row["phone"] == "5165550199"
    assert row["source"] == "dos_tiris_website"
    assert row["status"] == "new"
    assert len(email_calls["office"]) == 1
    assert len(email_calls["confirmation"]) == 1

    # Invalid phone: pre-patch 400 wording and no side effects.
    bad = client.post("/demo-request", json=dict(payload, phone="123",
                                                 email="short@example.test"))
    assert bad.status_code == 400
    assert bad.json()["detail"] == "Please enter a valid 10-digit phone number."
    ghost = db.execute(sql_text(
        "SELECT 1 FROM demo_requests WHERE email = 'short@example.test'"
    )).first()
    assert ghost is None
    assert len(email_calls["office"]) == 1  # unchanged
