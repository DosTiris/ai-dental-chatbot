# calendar_tests/test_admin_lead_status_compat.py
#
# P3-B2-S2 (legacy admin status compatibility): POST /admin/leads/status must
# write operator workflow into the office-owned office_status column
# (migration 008) and must NEVER touch Mia's system-owned lead_status.
#
# Proven at the REAL HTTP layer (fastapi.testclient.TestClient against
# app.main, the same standard as test_admin_auth.py):
#   - the wire contract is unchanged: same route, same body field name
#     (lead_status), same accepted vocabulary {new, contacted, booked,
#     closed}, same 400/404 details, same global X-Admin-Key auth;
#   - "contacted"/"booked"/"closed" store exactly that office_status;
#   - legacy "new" clears office_status to NULL and never writes "new";
#   - office_status_updated_at is server-generated and ADVANCES on every
#     successful mutation, including clear-to-NULL and same-value re-set,
#     and is never reset to NULL (the migration-008 one-directional
#     contract);
#   - THE OLD CORRUPTION CANNOT RECUR: with lead_status seeded "completed",
#     an operator "contacted" leaves lead_status "completed" byte-for-byte
#     while office_status becomes "contacted";
#   - office_note / office_note_updated_at and every other conversation
#     field are untouched by operator status mutations;
#   - the response keeps the prior keys (ok, conversation_id, lead_status,
#     unchanged) plus additive office_status, and "unchanged" is now
#     computed truthfully (the pre-S2 code hardcoded True; the sole caller
#     static/admin/dashboard.html reads only res.ok / error detail, so the
#     correction is caller-safe by evidence);
#   - GET /admin/leads carries the additive office_status field so the
#     dashboard can re-render operator state after a reload.
#
# FIXTURES: the shared engine/db/client_row fixtures from conftest.py are
# used UNCHANGED. conftest sets ADMIN_API_KEY=test-admin-key for app.config.

import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from calendar_tests.conftest import requires_db  # noqa: E402

pytestmark = requires_db

STATUS_PATH = "/admin/leads/status"
GLOBAL_ADMIN_KEY = "test-admin-key"   # pinned by conftest for app.config

# The exact response key set (prior keys + the additive office_status).
RESPONSE_KEYS = {"ok", "conversation_id", "lead_status", "office_status",
                 "unchanged"}


@pytest.fixture()
def http_client():
    """Real HTTP layer against the real application (house standard)."""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        yield client


def _headers():
    return {"x-admin-key": GLOBAL_ADMIN_KEY}


def _make_lead(db, client_row, *, lead_status="new", office_status=None,
               office_status_updated_at=None, office_note=None,
               office_note_updated_at=None, last_lead_at=None):
    """One lead row seeded DIRECTLY (below the endpoint under test) so each
    test controls the exact starting state, including pre-S2 legacy shapes."""
    from app.models import Conversation

    conversation = Conversation(
        id=uuid.uuid4(),
        client_id=client_row.id,
        lead_name="Kevin Alvarado",
        lead_phone="516-555-1234",
        lead_reason="cleaning/checkup",
        is_lead=True,
        last_lead_at=last_lead_at,
        lead_status=lead_status,
        office_status=office_status,
        office_status_updated_at=office_status_updated_at,
        office_note=office_note,
        office_note_updated_at=office_note_updated_at,
    )
    db.add(conversation)
    db.commit()
    return conversation


def _post_status(http_client, conversation, word):
    return http_client.post(
        STATUS_PATH,
        headers=_headers(),
        json={"conversation_id": str(conversation.id), "lead_status": word},
    )


def _reload(db, conversation):
    """Read the row back through a fresh SELECT (never trust the identity
    map to reflect what the endpoint's own session committed)."""
    db.expire_all()
    from app.models import Conversation

    return db.query(Conversation).filter(
        Conversation.id == conversation.id).one()


@pytest.mark.parametrize("word", ["contacted", "booked", "closed"])
def test_manual_word_writes_office_status_not_lead_status(
        http_client, db, client_row, word):
    """Each manual word stores exactly that office_status, stamps a server
    token, and leaves the seeded system lead_status untouched."""
    conversation = _make_lead(db, client_row, lead_status="new")
    before = datetime.now(timezone.utc)

    response = _post_status(http_client, conversation, word)
    assert response.status_code == 200
    payload = response.json()
    assert set(payload.keys()) == RESPONSE_KEYS
    assert payload["ok"] is True
    assert payload["conversation_id"] == str(conversation.id)
    assert payload["office_status"] == word
    assert payload["lead_status"] == "new"          # system value, untouched
    assert payload["unchanged"] is False            # NULL -> word is a change

    row = _reload(db, conversation)
    assert row.office_status == word
    assert row.lead_status == "new"
    assert row.office_status_updated_at is not None
    assert row.office_status_updated_at >= before   # server-side stamp


def test_legacy_new_clears_office_status_and_keeps_token(
        http_client, db, client_row):
    """The legacy reset word clears the value to NULL, never writes "new"
    into office_status, and the token ADVANCES rather than resetting."""
    seeded_token = datetime(2026, 1, 1, tzinfo=timezone.utc)
    conversation = _make_lead(db, client_row, lead_status="new",
                              office_status="contacted",
                              office_status_updated_at=seeded_token)

    response = _post_status(http_client, conversation, "new")
    assert response.status_code == 200
    payload = response.json()
    assert payload["office_status"] is None
    assert payload["lead_status"] == "new"
    assert payload["unchanged"] is False            # contacted -> NULL

    row = _reload(db, conversation)
    assert row.office_status is None
    assert row.lead_status == "new"                 # untouched system value
    assert row.office_status_updated_at is not None # cleared value keeps token
    assert row.office_status_updated_at > seeded_token


def test_every_mutation_advances_the_token(http_client, db, client_row):
    """Three successive mutations (set, change, same-value re-set) each
    advance the server token; the same-value re-set reports unchanged."""
    conversation = _make_lead(db, client_row)

    assert _post_status(http_client, conversation, "contacted").status_code == 200
    first = _reload(db, conversation).office_status_updated_at

    assert _post_status(http_client, conversation, "booked").status_code == 200
    second = _reload(db, conversation).office_status_updated_at
    assert second > first

    response = _post_status(http_client, conversation, "booked")
    assert response.status_code == 200
    assert response.json()["unchanged"] is True     # truthful, not hardcoded
    third = _reload(db, conversation).office_status_updated_at
    assert third > second                            # still advances


def test_old_corruption_cannot_recur(http_client, db, client_row):
    """THE S2 regression proof: an operator "contacted" on a COMPLETED lead
    leaves Mia's lead_status exactly "completed" while the office value is
    stored separately. Pre-S2 this exact call overwrote lead_status."""
    conversation = _make_lead(db, client_row, lead_status="completed")

    response = _post_status(http_client, conversation, "contacted")
    assert response.status_code == 200
    payload = response.json()
    assert payload["lead_status"] == "completed"
    assert payload["office_status"] == "contacted"

    row = _reload(db, conversation)
    assert row.lead_status == "completed"
    assert row.office_status == "contacted"


def test_unknown_word_is_refused_and_writes_nothing(
        http_client, db, client_row):
    """The closed vocabulary is enforced with the SAME 400 detail shape as
    before, and a refused request mutates neither value nor token."""
    conversation = _make_lead(db, client_row)

    response = _post_status(http_client, conversation, "followup")
    assert response.status_code == 400
    assert response.json()["detail"] == (
        "lead_status must be one of ['booked', 'closed', 'contacted', 'new']"
    )

    row = _reload(db, conversation)
    assert row.office_status is None
    assert row.office_status_updated_at is None
    assert row.lead_status == "new"


def test_nonexistent_lead_behavior_unchanged(http_client, db, client_row):
    """A well-formed but unknown conversation id keeps the exact prior 404."""
    response = http_client.post(
        STATUS_PATH,
        headers=_headers(),
        json={"conversation_id": str(uuid.uuid4()),
              "lead_status": "contacted"},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "Conversation not found"


def test_authentication_behavior_unchanged(http_client, db, client_row):
    """Missing and wrong X-Admin-Key both stay 401 Unauthorized, and an
    unauthenticated request writes nothing."""
    conversation = _make_lead(db, client_row)

    body = {"conversation_id": str(conversation.id),
            "lead_status": "contacted"}
    missing = http_client.post(STATUS_PATH, json=body)
    assert missing.status_code == 401
    wrong = http_client.post(
        STATUS_PATH, headers={"x-admin-key": "not-the-key"}, json=body)
    assert wrong.status_code == 401

    row = _reload(db, conversation)
    assert row.office_status is None
    assert row.office_status_updated_at is None


def test_note_fields_and_other_state_untouched(http_client, db, client_row):
    """An operator status mutation never touches the office note pair or
    the seeded intake fields."""
    note_token = datetime(2026, 2, 2, tzinfo=timezone.utc)
    conversation = _make_lead(db, client_row,
                              office_note="Called patient, left voicemail.",
                              office_note_updated_at=note_token)

    assert _post_status(http_client, conversation, "booked").status_code == 200

    row = _reload(db, conversation)
    assert row.office_note == "Called patient, left voicemail."
    assert row.office_note_updated_at == note_token
    assert row.lead_name == "Kevin Alvarado"
    assert row.lead_phone == "516-555-1234"
    assert bool(row.is_lead) is True


def test_admin_leads_list_carries_additive_office_status(
        http_client, db, client_row):
    """GET /admin/leads now exposes the additive office_status field (the
    dashboard re-render path), while lead_status stays the system value."""
    # last_lead_at seeded inside the list window: /admin/leads applies a
    # days filter (default 30) and a NULL last_lead_at never satisfies it.
    conversation = _make_lead(db, client_row, lead_status="completed",
                              office_status="booked",
                              office_status_updated_at=datetime.now(
                                  timezone.utc),
                              last_lead_at=datetime.now(timezone.utc))

    response = http_client.get(
        "/admin/leads",
        params={"client_key": client_row.api_key, "limit": 25, "offset": 0},
        headers=_headers(),
    )
    assert response.status_code == 200
    rows = [r for r in response.json()
            if r["conversation_id"] == str(conversation.id)]
    assert len(rows) == 1
    assert rows[0]["office_status"] == "booked"
    assert rows[0]["lead_status"] == "completed"
