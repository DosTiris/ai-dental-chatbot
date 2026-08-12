# calendar_tests/test_slot_management_owner.py
#
# P4-A (Portal Slot Schedule Controls v1, contract v1.2 SS4 / SS8.7-8.8):
# the shared slot-management owner (slot_management_service) plus the
# BEHAVIOR-PRESERVATION pins for the admin block extraction (Correction B).
#
# GROUPS:
#   * Owner tests (requires_db): block_slot / unblock_slot outcome matrix -
#     including the Rule-12 preservation pins that a blocked slot may be
#     idempotently re-blocked and a cancelled slot may be blocked (the frozen
#     admin behavior, deliberately NOT "improved"), and that unblock is
#     strictly blocked -> available with every other state refused
#     mutation-free.
#   * Admin route pins (requires_db, HTTP): POST /admin/calendar/slots/{id}/
#     block behaves byte-equivalently after the delegation - same 404
#     wording, same 409 wording, block clears holds, tenant isolation.
#
# BITE PROOF: the unblock owner tests FAIL against untouched fd257005
# (app.services.slot_management_service does not exist there -> ImportError
# at collection). The admin pins PASS against fd257005 BY DESIGN - they are
# the behavior-preservation regression net, frozen-parent semantics first.
#
# Run (owner-local, PowerShell 5.1):
#   $env:ALLOW_DESTRUCTIVE_CALENDAR_TESTS="yes"
#   $env:TEST_DATABASE_URL="postgresql://postgres:test@localhost:5433/mia_calendar_test"
#   python -m pytest calendar_tests\test_slot_management_owner.py -v

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from calendar_tests.conftest import requires_db  # noqa: E402

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://placeholder:placeholder@localhost:1/never_connected_placeholder",
)

UTC = timezone.utc


def _make_slot(db, client, *, status="available", start=None, end=None,
               held_until=None, held_by=None):
    """Seed one slot row with exact status/hold state for the case under
    test (never through the publish pipeline - the state is the input)."""
    from app.calendar_models import AppointmentSlot

    start = start or datetime(2026, 8, 21, 13, 0, tzinfo=UTC)
    end = end or (start + timedelta(hours=1))
    slot = AppointmentSlot(
        client_id=client.id, start_datetime=start, end_datetime=end,
        status=status, held_until=held_until,
        held_by_conversation_id=held_by,
    )
    db.add(slot)
    db.commit()
    return slot


# ---------------------------------------------------------------------------
# Shared-owner tests: block_slot
# ---------------------------------------------------------------------------

@requires_db
def test_block_available_slot_ok(db, client_row):
    from app.services import slot_management_service as sms
    slot = _make_slot(db, client_row, status="available")
    result = sms.block_slot(db, client_row.id, slot.id)
    assert result.ok and result.reason == sms.REASON_OK
    db.refresh(slot)
    assert slot.status == "blocked"
    assert slot.held_until is None and slot.held_by_conversation_id is None


@requires_db
def test_block_held_slot_clears_hold(db, client_row):
    """The ONE shared rule: blocking clears the hold fields (the frozen
    admin behavior, now owned by apply_block)."""
    from app.services import slot_management_service as sms
    holder = uuid.uuid4()
    slot = _make_slot(
        db, client_row, status="held",
        held_until=datetime.now(UTC) + timedelta(minutes=5), held_by=holder)
    result = sms.block_slot(db, client_row.id, slot.id)
    assert result.ok
    db.refresh(slot)
    assert slot.status == "blocked"
    assert slot.held_until is None and slot.held_by_conversation_id is None


@requires_db
def test_block_booked_slot_refused_mutation_free(db, client_row):
    from app.services import slot_management_service as sms
    slot = _make_slot(db, client_row, status="booked")
    result = sms.block_slot(db, client_row.id, slot.id)
    assert not result.ok and result.reason == sms.REASON_SLOT_BOOKED
    db.refresh(slot)
    assert slot.status == "booked"


@requires_db
@pytest.mark.parametrize("status", ["blocked", "cancelled"])
def test_block_preserves_frozen_admin_semantics(db, client_row, status):
    """Rule-12 preservation pin: the frozen admin rule blocks ANY non-booked
    status - re-blocking a blocked slot and blocking a cancelled slot both
    succeed exactly as on the frozen parent. Deliberately NOT 'improved'."""
    from app.services import slot_management_service as sms
    slot = _make_slot(db, client_row, status=status)
    result = sms.block_slot(db, client_row.id, slot.id)
    assert result.ok and result.reason == sms.REASON_OK
    db.refresh(slot)
    assert slot.status == "blocked"


@requires_db
def test_block_unknown_and_foreign_indistinguishable(db, client_row):
    from app.models import Client
    from app.services import slot_management_service as sms
    other = Client(id=uuid.uuid4(), practice_name="Other Dental",
                   api_key=f"key-{uuid.uuid4()}", active=True, settings={})
    db.add(other)
    db.commit()
    foreign = _make_slot(db, other, status="available")
    missing = sms.block_slot(db, client_row.id, uuid.uuid4())
    cross = sms.block_slot(db, client_row.id, foreign.id)
    assert missing.reason == cross.reason == sms.REASON_SLOT_MISSING
    db.refresh(foreign)
    assert foreign.status == "available"  # B's row untouched (Rule 15)


# ---------------------------------------------------------------------------
# Shared-owner tests: unblock_slot (NEW P4-A rule - bites vs fd257005)
# ---------------------------------------------------------------------------

@requires_db
def test_unblock_blocked_slot_ok(db, client_row):
    from app.services import slot_management_service as sms
    slot = _make_slot(db, client_row, status="blocked")
    result = sms.unblock_slot(db, client_row.id, slot.id)
    assert result.ok and result.reason == sms.REASON_OK
    db.refresh(slot)
    assert slot.status == "available"
    assert slot.held_until is None and slot.held_by_conversation_id is None


@requires_db
@pytest.mark.parametrize("status,held_until_offset", [
    ("available", None),
    ("held", +5),      # actively held
    ("held", -5),      # expired hold - STILL refused: only blocked unblocks
    ("booked", None),
    ("cancelled", None),
])
def test_unblock_refuses_every_non_blocked_state(db, client_row, status,
                                                 held_until_offset):
    """blocked -> available ONLY (contract SS4). Every other state is
    rejected with the closed status word and left byte-untouched - no
    coercion, no hold clearing, no resurrection of cancelled."""
    from app.services import slot_management_service as sms
    held_until = None
    held_by = None
    if held_until_offset is not None:
        held_until = datetime.now(UTC) + timedelta(minutes=held_until_offset)
        held_by = uuid.uuid4()
    slot = _make_slot(db, client_row, status=status,
                      held_until=held_until, held_by=held_by)
    result = sms.unblock_slot(db, client_row.id, slot.id)
    assert not result.ok and result.reason == sms.REASON_SLOT_NOT_BLOCKED
    assert result.detail == status  # closed vocabulary word only
    db.refresh(slot)
    assert slot.status == status
    assert slot.held_until == held_until
    assert slot.held_by_conversation_id == held_by


@requires_db
def test_unblock_unknown_and_foreign_indistinguishable(db, client_row):
    from app.models import Client
    from app.services import slot_management_service as sms
    other = Client(id=uuid.uuid4(), practice_name="Other Dental",
                   api_key=f"key-{uuid.uuid4()}", active=True, settings={})
    db.add(other)
    db.commit()
    foreign = _make_slot(db, other, status="blocked")
    missing = sms.unblock_slot(db, client_row.id, uuid.uuid4())
    cross = sms.unblock_slot(db, client_row.id, foreign.id)
    assert missing.reason == cross.reason == sms.REASON_SLOT_MISSING
    db.refresh(foreign)
    assert foreign.status == "blocked"


# ---------------------------------------------------------------------------
# Admin route pins (Correction B): byte-equivalent behavior after delegation.
# These pins encode the FROZEN-PARENT semantics; they must pass before AND
# after the extraction.
# ---------------------------------------------------------------------------

@pytest.fixture()
def admin_http(db, monkeypatch):
    """The real calendar admin router over HTTP with the real per-office
    credential owner running (the frozen test_admin_auth harness shape)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.routes import calendar as calendar_routes

    app = FastAPI()
    app.include_router(calendar_routes.router)
    app.dependency_overrides[calendar_routes.get_db] = lambda: db
    with TestClient(app) as http:
        yield http


def _issue_admin_key(db, client):
    """Provision one per-office Calendar admin credential the approved way
    (the frozen test_admin_auth._provision pattern): only the hash is
    persisted; the raw key exists only in memory."""
    from app.calendar_models import CalendarAdminCredential
    from app.services.calendar_admin_auth import generate_calendar_admin_key

    raw_key, key_hash = generate_calendar_admin_key()
    db.add(CalendarAdminCredential(
        id=uuid.uuid4(), client_id=client.id, key_hash=key_hash,
        label="p4a owner pins"))
    db.commit()
    return raw_key


@requires_db
def test_admin_block_pins_404_409_and_hold_clearing(admin_http, db, client_row):
    key = _issue_admin_key(db, client_row)
    headers = {"X-Admin-Key": key}

    # Unknown slot: the exact frozen 404 wording.
    r = admin_http.post(
        f"/admin/calendar/slots/{uuid.uuid4()}/block",
        params={"client_id": str(client_row.id)}, headers=headers)
    assert r.status_code == 404
    assert r.json()["detail"] == "Slot not found."

    # Booked slot: the exact frozen 409 wording, mutation-free.
    booked = _make_slot(db, client_row, status="booked")
    r = admin_http.post(
        f"/admin/calendar/slots/{booked.id}/block",
        params={"client_id": str(client_row.id)}, headers=headers)
    assert r.status_code == 409
    assert r.json()["detail"] == (
        "Slot has a booked appointment. Cancel the appointment first.")
    db.refresh(booked)
    assert booked.status == "booked"

    # Held slot: blocked with the hold cleared; frozen SlotView shape.
    held = _make_slot(db, client_row, status="held",
                      held_until=datetime.now(UTC) + timedelta(minutes=5),
                      held_by=uuid.uuid4())
    r = admin_http.post(
        f"/admin/calendar/slots/{held.id}/block",
        params={"client_id": str(client_row.id)}, headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "blocked"
    assert set(body.keys()) == {"id", "start_datetime", "end_datetime",
                                "status", "provider_name", "service_key"}
    db.refresh(held)
    assert held.held_until is None and held.held_by_conversation_id is None


@requires_db
@pytest.mark.parametrize("status", ["blocked", "cancelled"])
def test_admin_block_preserved_edge_semantics(admin_http, db, client_row,
                                              status):
    """Frozen-parent pin: re-blocking a blocked slot and blocking a
    cancelled slot both return 200 blocked (Rule 12: not 'improved')."""
    key = _issue_admin_key(db, client_row)
    slot = _make_slot(db, client_row, status=status)
    r = admin_http.post(
        f"/admin/calendar/slots/{slot.id}/block",
        params={"client_id": str(client_row.id)},
        headers={"X-Admin-Key": key})
    assert r.status_code == 200
    assert r.json()["status"] == "blocked"


@requires_db
def test_admin_block_tenant_mismatch_is_the_frozen_404(admin_http, db,
                                                       client_row):
    """Patch-5 pin: a mismatched client_id is 404 'Client not found.' BEFORE
    any slot semantics - indistinguishable from a nonexistent client."""
    key = _issue_admin_key(db, client_row)
    slot = _make_slot(db, client_row, status="available")
    r = admin_http.post(
        f"/admin/calendar/slots/{slot.id}/block",
        params={"client_id": str(uuid.uuid4())},
        headers={"X-Admin-Key": key})
    assert r.status_code == 404
    assert r.json()["detail"] == "Client not found."
    db.refresh(slot)
    assert slot.status == "available"
