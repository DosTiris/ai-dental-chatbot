# calendar_tests/test_notification_idempotency.py
#
# PATCH 9A (Senior Audit Recommended #1): the notification-attempt ledger
# and database-enforced duplicate suppression. This file is the single
# owner of the 9A regression matrix (Rule 6): the mandatory entry session
# contract, the immutable snapshot, the transaction-free provider boundary,
# the atomic ON CONFLICT claim, the sending/sent/unknown state machine, the
# atomic outcome CAS + projection, monotonic flags, runtime legacy
# suppression (approved Option B), malformed-notify_error preservation, and
# final reconciliation.
#
# TEST-SIDE FAKES ONLY (Patch 2D convention): _send_sms/_send_email are
# replaced with recording fakes BELOW the orchestration, so the real claim /
# boundary / outcome code executes but no Twilio/Resend provider can run.
#
# TEST-SIDE LEDGER SEEDING: a few tests insert notification_attempts rows
# directly through the ORM to represent committed pre-existing states
# (another worker's in-flight claim, a crash-orphaned 'sending', a prior
# terminal outcome). That is test fixture data — the same technique the
# migration tests use with raw SQL — and does not create a second
# application write pathway (Rule 15: app code goes through the repository).
#
# POSTGRESQL-ONLY like the rest of the calendar suite (conftest safeguards):
# the claim statement and the FOR UPDATE NOWAIT probes are PostgreSQL
# semantics — exactly what production runs.

import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text as sa_text
from sqlalchemy.exc import IntegrityError, OperationalError

from calendar_tests.conftest import requires_db

UTC = timezone.utc
pytestmark = requires_db

OFFICE_PHONE = "516-555-0100"
OFFICE_EMAIL = "frontdesk@testdental.example"
PATIENT_PHONE = "516-555-1234"


def _now():
    return datetime.now(UTC)


def _settings(client):
    from app.services.calendar_settings_service import load_calendar_settings
    return load_calendar_settings(client)


def _set_office_contacts(db, client, phone, email):
    """Explicit per-test contact configuration (Rule 4 — the conftest client
    starts with both unset)."""
    client.notification_phone = phone
    client.notification_email = email
    db.add(client)
    db.commit()


def _make_appointment(db, client, status=None):
    """One committed appointment on a fresh staff-published slot
    (conversation_id=None: staff-style row, exempt from the Patch 1
    per-conversation index — this file tests notifications, not booking)."""
    from app.calendar_models import AppointmentStatus
    from app.repositories.appointment_repository import (
        create_appointment_from_slot,
        create_slot,
    )
    start = _now() + timedelta(hours=48)
    slot = create_slot(db, client.id, start, start + timedelta(minutes=45))
    db.commit()
    appointment = create_appointment_from_slot(
        db, slot=slot, conversation_id=None,
        status=status or AppointmentStatus.PENDING,
        patient_name="Kevin Alvarado", patient_phone=PATIENT_PHONE,
        patient_email=None, new_or_returning="new",
        reason="cleaning/checkup", urgency="routine",
    )
    db.commit()
    return appointment


def _recording_sms(monkeypatch, fail=False):
    from app.services import notification_service
    sent = []

    def fake_send_sms(to_phone, body):
        sent.append((to_phone, body))
        if fail:
            raise RuntimeError("sms provider down (test fake)")

    monkeypatch.setattr(notification_service, "_send_sms", fake_send_sms)
    return sent


def _recording_email(monkeypatch, fail=False):
    from app.services import notification_service
    sent = []

    def fake_send_email(to_email, subject, email_html):
        sent.append((to_email, subject))
        if fail:
            raise RuntimeError("email provider down (test fake)")

    monkeypatch.setattr(notification_service, "_send_email", fake_send_email)
    return sent


def _send(db, client, appointment):
    """Invoke the service the way the production caller does: settings are
    evaluated FIRST (which may lazily refresh expired ORM attributes and
    autobegin a read-only transaction), then the TEST-OWNED read work is
    ended, so the service is entered — like production — with a clean,
    transaction-free session (the strict approved entry contract)."""
    from app.services import notification_service
    settings = _settings(client)
    db.rollback()          # End test-owned read work before the service call.
    return notification_service.send_booking_notifications(
        db, client, appointment, settings
    )


def _attempt_rows(db, appointment_id):
    """{channel: NotificationAttempt} read directly (test-side inspection)."""
    from app.calendar_models import NotificationAttempt
    rows = (db.query(NotificationAttempt)
            .filter(NotificationAttempt.appointment_id == appointment_id)
            .all())
    return {row.channel: row for row in rows}


def _seed_attempt(db, appointment, channel, status):
    """Directly commit one pre-existing ledger row (test fixture data)."""
    from app.calendar_models import (NotificationAttempt,
                                     NotificationAttemptStatus)
    row = NotificationAttempt(
        appointment_id=appointment.id, channel=channel, status=status,
        resolved_at=(_now()
                     if status in NotificationAttemptStatus.TERMINAL
                     else None),
    )
    db.add(row)
    db.commit()
    return row.id


def _foreign_client(db):
    """A second, unrelated dental office (tenant-isolation scenarios)."""
    from app.models import Client
    other = Client(
        id=uuid.uuid4(), practice_name="Other Office",
        api_key=f"key-{uuid.uuid4()}", active=True,
        settings={"timezone": "America/New_York"},
    )
    db.add(other)
    db.commit()
    return other


# ===========================================================================
# A. MANDATORY ENTRY SESSION CONTRACT (strict — correction pass 1)
#
# These tests call the service DIRECTLY with pre-computed settings, so each
# one fully controls the exact session state at the moment of invocation.
# ===========================================================================

def test_entry_pending_orm_state_abstains_without_rollback(
        db, client_row, monkeypatch, capsys):
    """Pending identity-map state (dirty) at entry: nothing is sent, no
    claim row is created, the caller's pending work is PRESERVED (no
    rollback), and one controlled entry event carries only the fixed name
    and the appointment UUID."""
    from app.services import notification_service
    sms = _recording_sms(monkeypatch)
    email = _recording_email(monkeypatch)
    _set_office_contacts(db, client_row, OFFICE_PHONE, OFFICE_EMAIL)
    appointment = _make_appointment(db, client_row)
    settings = _settings(client_row)
    db.rollback()

    client_row.practice_name = "Renamed Mid-Flight Dental"   # dirty, uncommitted
    assert db.dirty

    outcome = notification_service.send_booking_notifications(
        db, client_row, appointment, settings)

    assert sms == [] and email == []
    assert (outcome.office_sms_sent, outcome.office_email_sent,
            outcome.errors) == (False, False, [])
    assert db.dirty                                # caller state preserved
    out = capsys.readouterr().out
    assert "event=entry_contract_violation" in out
    assert str(appointment.id) in out
    db.commit()                                    # caller's work still commits
    db.refresh(client_row)
    assert client_row.practice_name == "Renamed Mid-Flight Dental"
    assert _attempt_rows(db, appointment.id) == {}


def test_entry_active_write_transaction_abstains_without_rollback(
        db, client_row, monkeypatch, capsys):
    """Raw session.execute DML never appears in new/dirty/deleted — exactly
    why the active-transaction check is mandatory. A caller-owned open
    transaction abstains: no send, no claim, no rollback of the caller's
    uncommitted write."""
    from app.services import notification_service
    sms = _recording_sms(monkeypatch)
    _recording_email(monkeypatch)
    _set_office_contacts(db, client_row, OFFICE_PHONE, OFFICE_EMAIL)
    appointment = _make_appointment(db, client_row)
    settings = _settings(client_row)
    db.rollback()

    db.execute(sa_text("UPDATE clients SET practice_name = :n WHERE id = :i"),
               {"n": "Raw DML Dental", "i": str(client_row.id)})
    assert not db.dirty and db.in_transaction()    # invisible to identity map

    outcome = notification_service.send_booking_notifications(
        db, client_row, appointment, settings)

    assert sms == []
    assert outcome.office_sms_sent is False and outcome.errors == []
    assert db.in_transaction()                     # caller transaction intact
    assert "event=entry_contract_violation" in capsys.readouterr().out
    db.commit()                                    # caller work survives
    db.expire_all()
    assert client_row.practice_name == "Raw DML Dental"
    assert _attempt_rows(db, appointment.id) == {}


def test_entry_row_lock_transaction_abstains(db, client_row, monkeypatch):
    """A lock-holding caller transaction abstains identically: nothing sent,
    no rows, the caller's lock and transaction untouched."""
    from app.services import notification_service
    sms = _recording_sms(monkeypatch)
    _recording_email(monkeypatch)
    _set_office_contacts(db, client_row, OFFICE_PHONE, OFFICE_EMAIL)
    appointment = _make_appointment(db, client_row)
    settings = _settings(client_row)
    db.rollback()

    db.execute(sa_text("SELECT id FROM appointments WHERE id = :i FOR UPDATE"),
               {"i": str(appointment.id)})
    outcome = notification_service.send_booking_notifications(
        db, client_row, appointment, settings)

    assert sms == [] and outcome.errors == []
    assert db.in_transaction()                     # lock/transaction intact
    db.rollback()
    assert _attempt_rows(db, appointment.id) == {}


def test_entry_readonly_transaction_abstains_strictly(
        db, client_row, monkeypatch, capsys):
    """STRICT contract (correction pass 1): even a transaction opened purely
    by reads (lazy refresh of an expired ORM attribute) causes abstention —
    the service never queries PostgreSQL to classify the transaction and
    never rolls it back. Nothing is sent, no row exists, the caller's open
    transaction remains open, one controlled entry event is logged."""
    from app.services import notification_service
    sms = _recording_sms(monkeypatch)
    email = _recording_email(monkeypatch)
    _set_office_contacts(db, client_row, OFFICE_PHONE, OFFICE_EMAIL)
    appointment = _make_appointment(db, client_row)
    settings = _settings(client_row)
    db.rollback()

    db.expire_all()
    _ = client_row.practice_name                   # read-only autobegin
    assert db.in_transaction()

    outcome = notification_service.send_booking_notifications(
        db, client_row, appointment, settings)

    assert sms == [] and email == []
    assert (outcome.office_sms_sent, outcome.office_email_sent,
            outcome.errors) == (False, False, [])
    assert db.in_transaction()                     # NOT rolled back
    out = capsys.readouterr().out
    assert "event=entry_contract_violation" in out
    assert str(appointment.id) in out
    db.rollback()
    assert _attempt_rows(db, appointment.id) == {}


def test_production_finalize_path_reaches_service_with_clean_session(
        db, client_row, conversation_row, monkeypatch):
    """THE production caller contract, proven on the production preceding
    operation: immediately after finalize_booking's commit — the exact
    point where booking_conversation invokes the service — the session is
    clean and transaction-free, the strict entry contract passes, both
    channels execute, and the session returns clean and transaction-free."""
    from app.services import notification_service
    from app.services.appointment_hold_service import place_hold
    from app.services.booking_service import finalize_booking
    from app.repositories.appointment_repository import create_slot
    sms = _recording_sms(monkeypatch)
    email = _recording_email(monkeypatch)
    _set_office_contacts(db, client_row, OFFICE_PHONE, OFFICE_EMAIL)
    settings = _settings(client_row)
    start = _now() + timedelta(hours=48)
    slot = create_slot(db, client_row.id, start, start + timedelta(minutes=45))
    db.commit()

    place_hold(db, client_row.id, slot.id, conversation_row.id,
               settings=settings, time_preference="any",
               service_key=None, now_utc=_now())
    result = finalize_booking(
        db, client_row.id, slot.id, conversation_row.id,
        settings=settings, now_utc=_now(),
        time_preference="any", service_key=None,
        patient_name="Kevin Alvarado", patient_phone=PATIENT_PHONE,
        patient_email=None, new_or_returning="new",
        reason="cleaning/checkup", urgency="routine",
    )
    assert result.success

    # The documented contract, at the exact production invocation point:
    assert not (db.new or db.dirty or db.deleted or db.in_transaction())

    outcome = notification_service.send_booking_notifications(
        db, client_row, result.appointment, settings)

    assert [d for d, _ in sms] == [OFFICE_PHONE]
    assert [d for d, _ in email] == [OFFICE_EMAIL]
    assert outcome.office_sms_sent and outcome.office_email_sent
    assert not (db.new or db.dirty or db.deleted or db.in_transaction())


# ===========================================================================
# B. SNAPSHOT AND THE TRANSACTION-FREE PROVIDER BOUNDARY
# ===========================================================================

def test_snapshot_built_before_first_claim(db, client_row, monkeypatch):
    """The immutable snapshot is constructed BEFORE any claim statement."""
    from app.services import notification_service
    from app.repositories import notification_attempt_repository as repo
    _recording_sms(monkeypatch)
    _recording_email(monkeypatch)
    _set_office_contacts(db, client_row, OFFICE_PHONE, OFFICE_EMAIL)
    appointment = _make_appointment(db, client_row)

    order = []
    real_snapshot = notification_service.build_notification_snapshot
    real_claim = repo.claim_channel_attempt

    def spy_snapshot(*a, **k):
        order.append("snapshot")
        return real_snapshot(*a, **k)

    def spy_claim(*a, **k):
        order.append("claim")
        return real_claim(*a, **k)

    monkeypatch.setattr(notification_service, "build_notification_snapshot",
                        spy_snapshot)
    monkeypatch.setattr(notification_service.notification_attempt_repository,
                        "claim_channel_attempt", spy_claim)
    _send(db, client_row, appointment)
    assert order[0] == "snapshot" and order.count("snapshot") == 1
    assert order[1:] == ["claim", "claim"]


def test_no_sql_no_transaction_during_provider_execution(
        db, client_row, monkeypatch, engine):
    """During each provider stub: zero SQL statements are emitted on the
    engine, the session holds NO transaction, and only immutable snapshot
    scalars were handed over (no ORM object can lazily re-query)."""
    from sqlalchemy import event
    from app.services import notification_service
    _set_office_contacts(db, client_row, OFFICE_PHONE, OFFICE_EMAIL)
    appointment = _make_appointment(db, client_row)

    in_provider = {"flag": False}
    sql_in_provider = []
    tx_states = []

    def listener(conn, cursor, statement, parameters, context, executemany):
        if in_provider["flag"]:
            sql_in_provider.append(statement)

    def fake_sms(to_phone, body):
        in_provider["flag"] = True
        tx_states.append(db.in_transaction())
        assert isinstance(to_phone, str) and isinstance(body, str)
        in_provider["flag"] = False

    def fake_email(to_email, subject, email_html):
        in_provider["flag"] = True
        tx_states.append(db.in_transaction())
        assert isinstance(email_html, str) and "<pre" in email_html
        in_provider["flag"] = False

    event.listen(engine, "before_cursor_execute", listener)
    try:
        monkeypatch.setattr(notification_service, "_send_sms", fake_sms)
        monkeypatch.setattr(notification_service, "_send_email", fake_email)
        _send(db, client_row, appointment)
    finally:
        event.remove(engine, "before_cursor_execute", listener)

    assert sql_in_provider == []
    assert tx_states == [False, False]


def test_no_locks_held_during_provider_execution(db, client_row, monkeypatch):
    """During the provider stub a SECOND session can immediately lock both
    the appointment row and the claim row (FOR UPDATE NOWAIT succeeds), so
    a slow provider can never stall staff operations or other workers."""
    from app.database import SessionLocal
    from app.services import notification_service
    _set_office_contacts(db, client_row, OFFICE_PHONE, None)
    appointment = _make_appointment(db, client_row)
    probe_ok = {}

    def fake_sms(to_phone, body):
        other = SessionLocal()
        try:
            other.execute(sa_text(
                "SELECT id FROM appointments WHERE id = :i FOR UPDATE NOWAIT"
            ), {"i": str(appointment.id)})
            other.execute(sa_text(
                "SELECT id FROM notification_attempts"
                " WHERE appointment_id = :i FOR UPDATE NOWAIT"
            ), {"i": str(appointment.id)})
            probe_ok["value"] = True
        finally:
            other.rollback()
            other.close()

    monkeypatch.setattr(notification_service, "_send_sms", fake_sms)
    _recording_email(monkeypatch)
    _send(db, client_row, appointment)
    assert probe_ok.get("value") is True


def test_boundary_violation_no_provider_call_claim_stays_sending(
        db, client_row, monkeypatch, capsys):
    """An unexpected open transaction at the provider boundary (induced by a
    commit wrapper that immediately re-opens one): the provider is NOT
    called, the remaining channel is NOT claimed, the claim stays honestly
    'sending' with resolved_at NULL, one controlled boundary event is
    logged, and the final reconciliation still runs."""
    from app.calendar_models import NotificationChannel
    sms = _recording_sms(monkeypatch)
    email = _recording_email(monkeypatch)
    _set_office_contacts(db, client_row, OFFICE_PHONE, OFFICE_EMAIL)
    appointment = _make_appointment(db, client_row)
    db.rollback()

    real_commit = db.commit

    def commit_then_reopen():
        real_commit()
        db.execute(sa_text("SELECT 1"))            # re-opens a transaction

    monkeypatch.setattr(db, "commit", commit_then_reopen)
    outcome = _send(db, client_row, appointment)
    monkeypatch.setattr(db, "commit", real_commit)
    db.rollback()

    assert sms == [] and email == []               # no provider execution
    rows = _attempt_rows(db, appointment.id)
    assert set(rows) == {NotificationChannel.OFFICE_SMS}   # email never claimed
    assert rows[NotificationChannel.OFFICE_SMS].status == "sending"
    assert rows[NotificationChannel.OFFICE_SMS].resolved_at is None
    out = capsys.readouterr().out
    assert "event=transaction_boundary_violation" in out
    assert "channel=office_sms" in out and str(appointment.id) in out
    assert outcome.office_sms_sent is False and outcome.errors == []
    db.refresh(appointment)                        # reconciliation committed:
    assert appointment.notify_error is None        # sending -> no error entry


# ===========================================================================
# C. THE ATOMIC CLAIM
# ===========================================================================

def test_claim_race_exactly_one_winner(db, client_row):
    """Two sessions in two threads race the claim for the SAME channel: the
    unique index arbitrates atomically — exactly one CLAIMED, the other
    EXISTING, exactly one row, no IntegrityError anywhere."""
    from app.database import SessionLocal
    from app.calendar_models import NotificationChannel
    from app.repositories import notification_attempt_repository as repo
    from app.services.notification_service import _CLAIM_ALLOWED_STORED_ERRORS
    appointment = _make_appointment(db, client_row)
    client_id, appointment_id = client_row.id, appointment.id
    results = {}

    def attempt(name):
        session = SessionLocal()
        try:
            results[name] = repo.claim_channel_attempt(
                session, client_id, appointment_id,
                NotificationChannel.OFFICE_SMS,
                _CLAIM_ALLOWED_STORED_ERRORS[NotificationChannel.OFFICE_SMS],
            )
            session.commit()
        finally:
            session.close()

    threads = [threading.Thread(target=attempt, args=(n,)) for n in "ab"]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    dispositions = sorted(r.disposition for r in results.values())
    assert dispositions == ["claimed", "existing"]
    assert len(_attempt_rows(db, appointment_id)) == 1


def test_losing_claim_leaves_transaction_usable(db, client_row):
    """ON CONFLICT DO NOTHING (never a raised 23505): after a losing claim
    the SAME transaction keeps working — further statements and the commit
    succeed. An ORM INSERT + IntegrityError would have aborted it."""
    from app.calendar_models import NotificationChannel
    from app.repositories import notification_attempt_repository as repo
    from app.services.notification_service import _CLAIM_ALLOWED_STORED_ERRORS
    appointment = _make_appointment(db, client_row)
    allowed = _CLAIM_ALLOWED_STORED_ERRORS[NotificationChannel.OFFICE_SMS]

    first = repo.claim_channel_attempt(
        db, client_row.id, appointment.id,
        NotificationChannel.OFFICE_SMS, allowed)
    second = repo.claim_channel_attempt(          # same open transaction
        db, client_row.id, appointment.id,
        NotificationChannel.OFFICE_SMS, allowed)

    assert first.disposition == "claimed"
    assert second.disposition == "existing"
    assert second.attempt_id == first.attempt_id
    assert second.existing_status == "sending"
    assert db.execute(sa_text("SELECT 1")).scalar() == 1   # still usable
    db.commit()                                            # commit succeeds
    assert len(_attempt_rows(db, appointment.id)) == 1


def test_claim_blocked_by_true_sent_flag(db, client_row):
    """The claim's INSERT..SELECT enforces the monotonic flag atomically: a
    true sent flag (legacy or post-9A) admits no new claim and no row."""
    from app.calendar_models import NotificationChannel
    from app.repositories import notification_attempt_repository as repo
    from app.services.notification_service import _CLAIM_ALLOWED_STORED_ERRORS
    appointment = _make_appointment(db, client_row)
    appointment.office_sms_sent = True
    db.add(appointment)
    db.commit()

    result = repo.claim_channel_attempt(
        db, client_row.id, appointment.id, NotificationChannel.OFFICE_SMS,
        _CLAIM_ALLOWED_STORED_ERRORS[NotificationChannel.OFFICE_SMS])
    db.commit()
    assert result.disposition == "not_claimable"
    assert _attempt_rows(db, appointment.id) == {}


def test_claim_tenant_isolated(db, client_row):
    """A foreign tenant's ids claim nothing: derived tenancy is enforced
    INSIDE the claim statement (Rule 15)."""
    from app.calendar_models import NotificationChannel
    from app.repositories import notification_attempt_repository as repo
    from app.services.notification_service import _CLAIM_ALLOWED_STORED_ERRORS
    appointment = _make_appointment(db, client_row)
    intruder = _foreign_client(db)

    result = repo.claim_channel_attempt(
        db, intruder.id, appointment.id, NotificationChannel.OFFICE_SMS,
        _CLAIM_ALLOWED_STORED_ERRORS[NotificationChannel.OFFICE_SMS])
    db.commit()
    assert result.disposition == "not_claimable"
    assert _attempt_rows(db, appointment.id) == {}


# ===========================================================================
# D. ATOMIC OUTCOME CAS + PROJECTION
# ===========================================================================

def test_success_records_sent_with_projection(db, client_row, monkeypatch):
    """Happy path, both channels: two 'sent' ledger rows with resolved_at,
    both flags true, notify_error None, backward-compatible outcome."""
    sms = _recording_sms(monkeypatch)
    email = _recording_email(monkeypatch)
    _set_office_contacts(db, client_row, OFFICE_PHONE, OFFICE_EMAIL)
    appointment = _make_appointment(db, client_row)

    outcome = _send(db, client_row, appointment)

    assert [d for d, _ in sms] == [OFFICE_PHONE]
    assert [d for d, _ in email] == [OFFICE_EMAIL]
    assert (outcome.office_sms_sent, outcome.office_email_sent,
            outcome.patient_sms_sent, outcome.errors) == (True, True, False, [])
    rows = _attempt_rows(db, appointment.id)
    assert {c: r.status for c, r in rows.items()} == {
        "office_sms": "sent", "office_email": "sent"}
    assert all(r.resolved_at is not None for r in rows.values())
    db.refresh(appointment)
    assert appointment.office_sms_sent and appointment.office_email_sent
    assert appointment.notify_error is None
    assert appointment.patient_sms_sent is False


@pytest.mark.parametrize("failing_channel", ["office_sms", "office_email"])
def test_provider_exception_records_unknown(db, client_row, monkeypatch,
                                            failing_channel):
    """A caught provider exception becomes the honest UNKNOWN state (never a
    claim of definite non-delivery), projected as the fixed send_failed
    vocabulary entry; the other channel is fully independent."""
    sms_fails = failing_channel == "office_sms"
    _recording_sms(monkeypatch, fail=sms_fails)
    _recording_email(monkeypatch, fail=not sms_fails)
    _set_office_contacts(db, client_row, OFFICE_PHONE, OFFICE_EMAIL)
    appointment = _make_appointment(db, client_row)

    outcome = _send(db, client_row, appointment)

    rows = _attempt_rows(db, appointment.id)
    other = ("office_email" if sms_fails else "office_sms")
    assert rows[failing_channel].status == "unknown"
    assert rows[failing_channel].resolved_at is not None
    assert rows[other].status == "sent"
    db.refresh(appointment)
    assert appointment.notify_error == f"{failing_channel}: send_failed"
    assert outcome.errors == [f"{failing_channel}: send_failed"]


def test_outcome_commit_failure_rolls_back_ledger_and_projection(
        db, client_row, monkeypatch, capsys):
    """The outcome CAS and the projection share ONE commit: when it fails,
    BOTH roll back — the attempt stays 'sending', no false sent flag can
    exist, per-channel independence is preserved (email still completes),
    and one controlled outcome_record_failed event is logged."""
    _recording_sms(monkeypatch)
    _recording_email(monkeypatch)
    _set_office_contacts(db, client_row, OFFICE_PHONE, OFFICE_EMAIL)
    appointment = _make_appointment(db, client_row)
    db.rollback()

    real_commit = db.commit
    calls = {"n": 0}

    def failing_second_commit():
        calls["n"] += 1
        if calls["n"] == 2:                        # the SMS outcome commit
            raise RuntimeError("commit failed (test fake)")
        real_commit()

    monkeypatch.setattr(db, "commit", failing_second_commit)
    outcome = _send(db, client_row, appointment)
    monkeypatch.setattr(db, "commit", real_commit)
    db.rollback()

    rows = _attempt_rows(db, appointment.id)
    assert rows["office_sms"].status == "sending"          # rolled back
    assert rows["office_sms"].resolved_at is None
    assert rows["office_email"].status == "sent"           # independent
    db.refresh(appointment)
    assert appointment.office_sms_sent is False            # no false flag
    assert appointment.office_email_sent is True
    assert appointment.notify_error is None                # sending: no entry
    assert outcome.office_sms_sent is False
    assert outcome.office_email_sent is True
    assert "event=outcome_record_failed" in capsys.readouterr().out


def test_terminal_attempt_and_resolved_at_immutable(db, client_row,
                                                    monkeypatch):
    """A stale writer can alter NOTHING terminal: sent -> unknown,
    unknown -> sent, and terminal-with-new-resolved_at are all
    unrepresentable through the status-guarded CAS (rowcount 0,
    byte-identical row)."""
    from app.repositories import notification_attempt_repository as repo
    _recording_sms(monkeypatch)
    _recording_email(monkeypatch)
    _set_office_contacts(db, client_row, OFFICE_PHONE, OFFICE_EMAIL)
    appointment = _make_appointment(db, client_row)
    _send(db, client_row, appointment)
    rows = _attempt_rows(db, appointment.id)
    sent_id = rows["office_sms"].id
    original_resolved = rows["office_sms"].resolved_at

    assert repo.cas_attempt_to_terminal(
        db, client_row.id, sent_id, "unknown", _now()) == 0
    assert repo.cas_attempt_to_terminal(
        db, client_row.id, sent_id, "sent", _now()) == 0    # resolved_at rewrite
    db.commit()
    db.expire_all()
    row = _attempt_rows(db, appointment.id)["office_sms"]
    assert row.status == "sent"
    assert row.resolved_at == original_resolved
    with pytest.raises(ValueError):                         # loud, Rule 4
        repo.cas_attempt_to_terminal(db, client_row.id, sent_id,
                                     "sending", _now())


def test_foreign_tenant_cas_and_reads_mutation_free(db, client_row,
                                                    monkeypatch):
    """Foreign-tenant CAS: rowcount 0, nothing changes; foreign-tenant
    ledger reads return nothing; disambiguation reads are tenant-scoped."""
    from app.repositories import notification_attempt_repository as repo
    _recording_sms(monkeypatch, fail=True)
    _recording_email(monkeypatch)
    _set_office_contacts(db, client_row, OFFICE_PHONE, None)
    appointment = _make_appointment(db, client_row)
    _send(db, client_row, appointment)
    attempt_id = _attempt_rows(db, appointment.id)["office_sms"].id
    intruder = _foreign_client(db)

    assert repo.cas_attempt_to_terminal(
        db, intruder.id, attempt_id, "sent", _now()) == 0
    db.commit()
    assert repo.get_attempts_by_appointment(
        db, intruder.id, appointment.id) == {}
    assert repo.get_attempt_for_tenant(db, intruder.id, attempt_id) is None
    assert repo.get_attempt_for_tenant(
        db, client_row.id, attempt_id) is not None
    db.expire_all()
    assert _attempt_rows(db, appointment.id)["office_sms"].status == "unknown"


def test_lock_order_appointment_before_attempts(db, client_row, monkeypatch):
    """The approved lock order: while the outcome transaction reads the
    ledger rows, the appointment row is ALREADY locked (a second session's
    FOR UPDATE NOWAIT on it fails) — appointment first, attempts second,
    everywhere."""
    from app.database import SessionLocal
    from app.services import notification_service
    repo = notification_service.notification_attempt_repository
    _recording_sms(monkeypatch)
    _set_office_contacts(db, client_row, OFFICE_PHONE, None)
    appointment = _make_appointment(db, client_row)

    real_reader = repo.get_attempts_by_appointment
    observed = {}

    def spy_reader(session, client_id, appointment_id):
        other = SessionLocal()
        try:
            other.execute(sa_text(
                "SELECT id FROM appointments WHERE id = :i FOR UPDATE NOWAIT"
            ), {"i": str(appointment_id)})
            observed["appointment_lockable"] = True     # would be a violation
        except OperationalError:
            observed["appointment_lockable"] = False    # correctly locked
        finally:
            other.rollback()
            other.close()
        return real_reader(session, client_id, appointment_id)

    monkeypatch.setattr(repo, "get_attempts_by_appointment", spy_reader)
    _send(db, client_row, appointment)
    assert observed["appointment_lockable"] is False


# ===========================================================================
# E. MONOTONIC PROJECTION
# ===========================================================================

@pytest.mark.parametrize("attempt_status", ["sending", "unknown"])
def test_true_flag_with_nonsent_attempt_stays_true(db, client_row,
                                                   monkeypatch, capsys,
                                                   attempt_status):
    """final_sent = flag OR sent: a true flag beside a sending/unknown
    attempt stays TRUE, carries NO error entry, suppresses the provider,
    and emits one controlled inconsistency event (fixed name + channel +
    UUID only)."""
    sms = _recording_sms(monkeypatch)
    _recording_email(monkeypatch)
    _set_office_contacts(db, client_row, OFFICE_PHONE, None)
    appointment = _make_appointment(db, client_row)
    _seed_attempt(db, appointment, "office_sms", attempt_status)
    appointment.office_sms_sent = True
    db.add(appointment)
    db.commit()

    outcome = _send(db, client_row, appointment)

    assert sms == []                                # provider suppressed
    db.refresh(appointment)
    assert appointment.office_sms_sent is True      # never downgraded
    assert appointment.notify_error is None or (
        "office_sms" not in appointment.notify_error)
    assert outcome.office_sms_sent is True
    out = capsys.readouterr().out
    assert "event=projection_inconsistency" in out
    assert "channel=office_sms" in out and str(appointment.id) in out
    for forbidden in ("Kevin", PATIENT_PHONE, "provider down"):
        assert forbidden not in out


def test_true_flag_with_recipient_removed_no_skip_entry(db, client_row,
                                                        monkeypatch):
    """A channel that already succeeded keeps its truth even after the
    office removes the recipient: flag stays true, NO skipped entry appears
    (approved: final_sent true -> no entry of any kind)."""
    _recording_sms(monkeypatch)
    _recording_email(monkeypatch)
    _set_office_contacts(db, client_row, OFFICE_PHONE, OFFICE_EMAIL)
    appointment = _make_appointment(db, client_row)
    _send(db, client_row, appointment)

    _set_office_contacts(db, client_row, None, OFFICE_EMAIL)   # phone removed
    outcome = _send(db, client_row, appointment)

    db.refresh(appointment)
    assert appointment.office_sms_sent is True
    assert appointment.notify_error is None
    assert outcome.office_sms_sent is True and outcome.errors == []


def test_projection_recompute_is_idempotent(db, client_row, monkeypatch):
    """Reconciliation may only repeat the deterministic computation: a
    second invocation over settled state changes no flag, no entry, no
    ledger row."""
    _recording_sms(monkeypatch, fail=True)
    _recording_email(monkeypatch)
    _set_office_contacts(db, client_row, OFFICE_PHONE, OFFICE_EMAIL)
    appointment = _make_appointment(db, client_row)
    _send(db, client_row, appointment)
    db.refresh(appointment)
    before = (appointment.office_sms_sent, appointment.office_email_sent,
              appointment.notify_error)
    rows_before = {c: (r.status, r.resolved_at)
                   for c, r in _attempt_rows(db, appointment.id).items()}

    _send(db, client_row, appointment)              # zero-outcome invocation

    db.expire_all()
    assert (appointment.office_sms_sent, appointment.office_email_sent,
            appointment.notify_error) == before
    assert {c: (r.status, r.resolved_at)
            for c, r in _attempt_rows(db, appointment.id).items()} == rows_before


def test_zero_claim_invocation_still_reconciles(db, client_row, monkeypatch):
    """Final reconciliation runs on EVERY zero-outcome invocation — proven
    by planting a stale (approved-vocabulary) notify_error that only the
    reconciliation recompute would clear."""
    from app.services.notification_service import (OFFICE_EMAIL_SKIPPED,
                                                   OFFICE_SMS_SKIPPED)
    sms = _recording_sms(monkeypatch)
    email = _recording_email(monkeypatch)
    _set_office_contacts(db, client_row, OFFICE_PHONE, OFFICE_EMAIL)
    appointment = _make_appointment(db, client_row)
    appointment.office_sms_sent = True              # legacy: both already sent
    appointment.office_email_sent = True
    appointment.notify_error = f"{OFFICE_SMS_SKIPPED}; {OFFICE_EMAIL_SKIPPED}"
    db.add(appointment)
    db.commit()

    outcome = _send(db, client_row, appointment)

    assert sms == [] and email == []                # zero claims, zero sends
    assert _attempt_rows(db, appointment.id) == {}
    db.refresh(appointment)
    assert appointment.notify_error is None         # reconciliation ran
    assert outcome.office_sms_sent and outcome.office_email_sent
    assert outcome.errors == []


# ===========================================================================
# F. RUNTIME LEGACY SUPPRESSION (approved Option B)
# ===========================================================================

@pytest.mark.parametrize("channel,flag_attr", [
    ("office_sms", "office_sms_sent"),
    ("office_email", "office_email_sent"),
])
def test_legacy_sent_flag_suppresses_and_remains_true(db, client_row,
                                                      monkeypatch, channel,
                                                      flag_attr):
    """A pre-006 appointment whose channel already succeeded: no claim row,
    no provider execution, the flag remains true, no error entry — and the
    OTHER channel proceeds normally."""
    sms = _recording_sms(monkeypatch)
    email = _recording_email(monkeypatch)
    _set_office_contacts(db, client_row, OFFICE_PHONE, OFFICE_EMAIL)
    appointment = _make_appointment(db, client_row)
    setattr(appointment, flag_attr, True)
    db.add(appointment)
    db.commit()

    outcome = _send(db, client_row, appointment)

    executed = {("office_sms" if sms else None),
                ("office_email" if email else None)} - {None}
    assert channel not in executed                 # legacy channel suppressed
    assert len(executed) == 1                      # other channel executed
    rows = _attempt_rows(db, appointment.id)
    assert channel not in rows and len(rows) == 1
    db.refresh(appointment)
    assert getattr(appointment, flag_attr) is True
    assert appointment.notify_error is None
    assert outcome.office_sms_sent and outcome.office_email_sent


@pytest.mark.parametrize("channel", ["office_sms", "office_email"])
def test_legacy_send_failed_suppresses_and_is_preserved(db, client_row,
                                                        monkeypatch, channel):
    """A pre-006 legacy send_failed (an attempt whose true outcome is
    unknowable): the channel is NOT re-claimed or re-sent in 9A, and the
    fixed entry is preserved in the recomposed value in exact SMS-first
    order."""
    sms = _recording_sms(monkeypatch)
    email = _recording_email(monkeypatch)
    _set_office_contacts(db, client_row, OFFICE_PHONE, OFFICE_EMAIL)
    appointment = _make_appointment(db, client_row)
    appointment.notify_error = f"{channel}: send_failed"
    db.add(appointment)
    db.commit()

    outcome = _send(db, client_row, appointment)

    executed = [c for c, lst in
                (("office_sms", sms), ("office_email", email)) if lst]
    assert executed == [c for c in ("office_sms", "office_email")
                        if c != channel]
    rows = _attempt_rows(db, appointment.id)
    assert channel not in rows
    db.refresh(appointment)
    assert appointment.notify_error == f"{channel}: send_failed"
    assert outcome.errors == [f"{channel}: send_failed"]


def test_legacy_skipped_permits_claim_after_configuration(db, client_row,
                                                          monkeypatch):
    """A legacy skipped entry is NOT send-protective: once the office
    configures the recipient, the channel is claimable exactly once and the
    stale skipped entry is recomposed away."""
    from app.services.notification_service import OFFICE_SMS_SKIPPED
    sms = _recording_sms(monkeypatch)
    _recording_email(monkeypatch)
    _set_office_contacts(db, client_row, OFFICE_PHONE, OFFICE_EMAIL)
    appointment = _make_appointment(db, client_row)
    appointment.notify_error = OFFICE_SMS_SKIPPED        # legacy, pre-006
    db.add(appointment)
    db.commit()

    outcome = _send(db, client_row, appointment)

    assert [d for d, _ in sms] == [OFFICE_PHONE]          # claimed + sent once
    db.refresh(appointment)
    assert appointment.office_sms_sent is True
    assert appointment.notify_error is None
    assert outcome.errors == []


def test_missing_recipients_no_rows_and_fixed_skip_entries(db, client_row,
                                                           monkeypatch):
    """Both recipients missing: zero attempt rows, zero provider calls, and
    the reconciliation projects both fixed skipped entries in exact
    SMS-first order (a zero-outcome invocation end to end)."""
    from app.services.notification_service import (OFFICE_EMAIL_SKIPPED,
                                                   OFFICE_SMS_SKIPPED)
    sms = _recording_sms(monkeypatch)
    email = _recording_email(monkeypatch)
    _set_office_contacts(db, client_row, None, None)
    appointment = _make_appointment(db, client_row)

    outcome = _send(db, client_row, appointment)

    assert sms == [] and email == []
    assert _attempt_rows(db, appointment.id) == {}
    db.refresh(appointment)
    assert appointment.notify_error == (
        f"{OFFICE_SMS_SKIPPED}; {OFFICE_EMAIL_SKIPPED}")
    assert outcome.errors == [OFFICE_SMS_SKIPPED, OFFICE_EMAIL_SKIPPED]
    assert outcome.office_sms_sent is False
    assert outcome.office_email_sent is False


# ===========================================================================
# G. MALFORMED LEGACY notify_error
# ===========================================================================

MALFORMED = "TwilioRestException: Unable to reach +15165551234 (Kevin's cell)"


def test_malformed_blocks_claims_and_stays_byte_identical(db, client_row,
                                                          monkeypatch):
    """A stored value outside the approved grammar blocks BOTH no-row
    channels atomically inside the claim SQL: zero rows, zero provider
    calls, flags unchanged, and the stored text remains byte-identical
    (never rewritten by reconciliation)."""
    sms = _recording_sms(monkeypatch)
    email = _recording_email(monkeypatch)
    _set_office_contacts(db, client_row, OFFICE_PHONE, OFFICE_EMAIL)
    appointment = _make_appointment(db, client_row)
    appointment.notify_error = MALFORMED
    db.add(appointment)
    db.commit()

    _send(db, client_row, appointment)

    assert sms == [] and email == []
    assert _attempt_rows(db, appointment.id) == {}
    db.refresh(appointment)
    assert appointment.notify_error == MALFORMED           # byte-identical
    assert appointment.office_sms_sent is False
    assert appointment.office_email_sent is False


def test_malformed_absent_from_outcome_and_logs(db, client_row, monkeypatch,
                                                capsys):
    """The malformed text never enters NotificationOutcome or any log:
    errors is exactly [], and the one controlled legacy-error-withheld
    event carries only the fixed name and the appointment UUID. The
    AppointmentView contract keeps returning the fixed withheld marker."""
    from app.services.notification_service import (NOTIFY_ERROR_WITHHELD,
                                                   sanitize_stored_notify_error)
    _recording_sms(monkeypatch)
    _recording_email(monkeypatch)
    _set_office_contacts(db, client_row, OFFICE_PHONE, OFFICE_EMAIL)
    appointment = _make_appointment(db, client_row)
    appointment.notify_error = MALFORMED
    db.add(appointment)
    db.commit()

    outcome = _send(db, client_row, appointment)

    assert outcome.errors == []
    out = capsys.readouterr().out
    assert "event=legacy_error_withheld" in out
    assert str(appointment.id) in out
    for fragment in ("TwilioRestException", "Kevin", "+15165551234"):
        assert fragment not in out
    db.refresh(appointment)
    assert sanitize_stored_notify_error(
        appointment.notify_error) == NOTIFY_ERROR_WITHHELD


def test_malformed_with_sent_attempt_raises_flag_monotonically(
        db, client_row, monkeypatch):
    """Flags-only mode: beside a malformed stored value, a SENT attempt may
    still raise its flag to true (monotonic), the no-row channel stays
    suppressed, and the stored text is untouched."""
    sms = _recording_sms(monkeypatch)
    email = _recording_email(monkeypatch)
    _set_office_contacts(db, client_row, OFFICE_PHONE, OFFICE_EMAIL)
    appointment = _make_appointment(db, client_row)
    _seed_attempt(db, appointment, "office_sms", "sent")
    appointment.notify_error = MALFORMED
    db.add(appointment)
    db.commit()

    outcome = _send(db, client_row, appointment)

    assert sms == [] and email == []                # both suppressed
    db.refresh(appointment)
    assert appointment.office_sms_sent is True      # raised monotonically
    assert appointment.office_email_sent is False
    assert appointment.notify_error == MALFORMED    # byte-identical
    assert outcome.office_sms_sent is True and outcome.errors == []


def test_malformed_with_sending_and_unknown_attempts_suppresses(
        db, client_row, monkeypatch):
    """Beside a malformed stored value, sending and unknown attempts
    suppress normally, flags stay honestly false, the update is flags-only,
    and the stored text is untouched."""
    sms = _recording_sms(monkeypatch)
    email = _recording_email(monkeypatch)
    _set_office_contacts(db, client_row, OFFICE_PHONE, OFFICE_EMAIL)
    appointment = _make_appointment(db, client_row)
    _seed_attempt(db, appointment, "office_sms", "sending")
    _seed_attempt(db, appointment, "office_email", "unknown")
    appointment.notify_error = MALFORMED
    db.add(appointment)
    db.commit()

    outcome = _send(db, client_row, appointment)

    assert sms == [] and email == []
    db.refresh(appointment)
    assert appointment.office_sms_sent is False
    assert appointment.office_email_sent is False
    assert appointment.notify_error == MALFORMED
    assert outcome.errors == []


# ===========================================================================
# H. DUPLICATE SUPPRESSION, CONCURRENCY, INTEGRATION SEAMS
# ===========================================================================

def test_repeated_invocation_single_execution_per_channel(db, client_row,
                                                          monkeypatch):
    """THE core 9A guarantee at the service seam: a second invocation for
    the same appointment executes ZERO providers and honestly reports the
    reconciled prior success."""
    sms = _recording_sms(monkeypatch)
    email = _recording_email(monkeypatch)
    _set_office_contacts(db, client_row, OFFICE_PHONE, OFFICE_EMAIL)
    appointment = _make_appointment(db, client_row)

    first = _send(db, client_row, appointment)
    second = _send(db, client_row, appointment)

    assert len(sms) == 1 and len(email) == 1        # exactly one each, total
    assert first.office_sms_sent and first.office_email_sent
    assert second.office_sms_sent and second.office_email_sent
    assert second.errors == []
    assert len(_attempt_rows(db, appointment.id)) == 2


def test_repeated_invocation_after_unknown_does_not_retry(db, client_row,
                                                          monkeypatch):
    """9A has NO retry: after an unknown outcome, a later invocation
    executes nothing for that channel (re-sending might double-notify — the
    provider may have accepted the first call) and preserves the honest
    projection."""
    sms = _recording_sms(monkeypatch, fail=True)
    _recording_email(monkeypatch)
    _set_office_contacts(db, client_row, OFFICE_PHONE, OFFICE_EMAIL)
    appointment = _make_appointment(db, client_row)

    _send(db, client_row, appointment)
    second = _send(db, client_row, appointment)

    assert len(sms) == 1                            # no second execution
    rows = _attempt_rows(db, appointment.id)
    assert rows["office_sms"].status == "unknown"
    db.refresh(appointment)
    assert appointment.notify_error == "office_sms: send_failed"
    assert second.errors == ["office_sms: send_failed"]
    assert second.office_email_sent is True


def test_in_flight_sending_suppresses_with_controlled_log(db, client_row,
                                                          monkeypatch,
                                                          capsys):
    """An existing 'sending' row (another worker mid-flight, or a
    crash-orphaned claim): suppressed with one controlled in-flight event;
    the other channel proceeds; no error entry appears for the honest
    unresolved state."""
    sms = _recording_sms(monkeypatch)
    email = _recording_email(monkeypatch)
    _set_office_contacts(db, client_row, OFFICE_PHONE, OFFICE_EMAIL)
    appointment = _make_appointment(db, client_row)
    _seed_attempt(db, appointment, "office_sms", "sending")

    outcome = _send(db, client_row, appointment)

    assert sms == []
    assert [d for d, _ in email] == [OFFICE_EMAIL]
    out = capsys.readouterr().out
    assert "event=in_flight_suppressed" in out
    assert "channel=office_sms" in out and str(appointment.id) in out
    db.refresh(appointment)
    assert appointment.office_sms_sent is False
    assert appointment.notify_error is None
    assert outcome.errors == []


def test_threaded_concurrent_invocations_one_execution_per_channel(
        db, client_row, monkeypatch):
    """Two complete service invocations race in two sessions/threads for the
    SAME appointment: across BOTH, each channel's provider executes at most
    once, and the final projection is uncorrupted (true/true, no error)."""
    from app.database import SessionLocal
    from app.models import Client
    from app.calendar_models import Appointment
    from app.services import notification_service
    from app.services.calendar_settings_service import load_calendar_settings
    _set_office_contacts(db, client_row, OFFICE_PHONE, OFFICE_EMAIL)
    appointment = _make_appointment(db, client_row)
    client_id, appointment_id = client_row.id, appointment.id
    db.rollback()

    sms_calls, email_calls = [], []
    monkeypatch.setattr(notification_service, "_send_sms",
                        lambda p, b: sms_calls.append(p))
    monkeypatch.setattr(notification_service, "_send_email",
                        lambda e, s, h: email_calls.append(e))

    def invoke():
        session = SessionLocal()
        try:
            local_client = session.get(Client, client_id)
            local_appointment = session.get(Appointment, appointment_id)
            settings = load_calendar_settings(local_client)
            session.rollback()     # End worker-owned read work (strict entry).
            notification_service.send_booking_notifications(
                session, local_client, local_appointment, settings)
        finally:
            session.rollback()
            session.close()

    threads = [threading.Thread(target=invoke) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(sms_calls) <= 1 and len(email_calls) <= 1
    rows = _attempt_rows(db, appointment_id)
    assert len(rows) == 2
    db.expire_all()
    fresh = db.get(Appointment, appointment_id)
    # Flags never corrupted: each true flag corresponds to a sent row.
    assert fresh.office_sms_sent == (rows["office_sms"].status == "sent")
    assert fresh.office_email_sent == (rows["office_email"].status == "sent")
    assert fresh.notify_error is None


def test_split_channel_workers_preserve_sms_first_error_order(db, client_row,
                                                              monkeypatch):
    """Split-channel outcomes landing in the opposite order (email's unknown
    first, SMS's unknown later) still compose the stored value in the fixed
    SMS-first grammar — the projection is recomputed, never appended."""
    from app.repositories import notification_attempt_repository as repo
    _recording_sms(monkeypatch)
    _recording_email(monkeypatch, fail=True)
    _set_office_contacts(db, client_row, OFFICE_PHONE, OFFICE_EMAIL)
    appointment = _make_appointment(db, client_row)
    # Worker A already claimed SMS and is mid-flight:
    sms_attempt_id = _seed_attempt(db, appointment, "office_sms", "sending")

    # Worker B (this invocation): SMS suppressed in-flight, email -> unknown.
    _send(db, client_row, appointment)
    db.refresh(appointment)
    assert appointment.notify_error == "office_email: send_failed"

    # Worker A now finishes with unknown; its outcome recomputes both.
    assert repo.cas_attempt_to_terminal(
        db, client_row.id, sms_attempt_id, "unknown", _now()) == 1
    db.commit()
    _send(db, client_row, appointment)              # zero-outcome reconcile

    db.refresh(appointment)
    assert appointment.notify_error == (
        "office_sms: send_failed; office_email: send_failed")
    assert appointment.office_sms_sent is False
    assert appointment.office_email_sent is False


def test_patient_channel_database_rejection(db, client_row):
    """No patient channel is representable: the channel CHECK rejects it at
    the database, mirroring migration 006 (Patch 2D policy, structural)."""
    from app.calendar_models import NotificationAttempt
    appointment = _make_appointment(db, client_row)
    db.add(NotificationAttempt(appointment_id=appointment.id,
                               channel="patient_sms", status="sending"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    assert _attempt_rows(db, appointment.id) == {}


def test_booking_survives_total_notification_failure(db, client_row,
                                                     monkeypatch):
    """Booking success is notification-independent: with BOTH providers
    down, the service returns normally (no exception), the appointment row
    is intact, and the honest unknown outcomes are recorded."""
    from app.calendar_models import Appointment, AppointmentStatus
    _recording_sms(monkeypatch, fail=True)
    _recording_email(monkeypatch, fail=True)
    _set_office_contacts(db, client_row, OFFICE_PHONE, OFFICE_EMAIL)
    appointment = _make_appointment(db, client_row)

    outcome = _send(db, client_row, appointment)    # must not raise

    assert outcome.errors == ["office_sms: send_failed",
                              "office_email: send_failed"]
    db.expire_all()
    fresh = db.get(Appointment, appointment.id)
    assert fresh is not None
    assert fresh.status == AppointmentStatus.PENDING
    rows = _attempt_rows(db, appointment.id)
    assert {r.status for r in rows.values()} == {"unknown"}


def test_channel_configured_later_claims_exactly_once(db, client_row,
                                                      monkeypatch):
    """Post-9A skipped channel, recipient configured later: the next
    invocation claims and sends that channel exactly once, while the
    already-sent channel stays suppressed and true."""
    from app.services.notification_service import OFFICE_EMAIL_SKIPPED
    sms = _recording_sms(monkeypatch)
    email = _recording_email(monkeypatch)
    _set_office_contacts(db, client_row, OFFICE_PHONE, None)
    appointment = _make_appointment(db, client_row)
    first = _send(db, client_row, appointment)
    assert first.errors == [OFFICE_EMAIL_SKIPPED]

    _set_office_contacts(db, client_row, OFFICE_PHONE, OFFICE_EMAIL)
    second = _send(db, client_row, appointment)

    assert len(sms) == 1                            # never re-sent
    assert [d for d, _ in email] == [OFFICE_EMAIL]  # sent exactly once, now
    assert second.office_sms_sent and second.office_email_sent
    assert second.errors == []
    db.refresh(appointment)
    assert appointment.notify_error is None

# ===========================================================================
# I. CLAIM-FAILURE ISOLATION (correction pass 1) + formatter regression
# ===========================================================================

def test_claim_repository_exception_isolated(db, client_row, monkeypatch,
                                             capsys):
    """A database failure inside the claim itself never propagates out and
    never sends: zero provider calls for that channel, no false ledger row
    or flag, one controlled claim_record_failed event (fixed name, channel,
    sanitized exception class, appointment UUID — never the raw text), the
    OTHER channel fully independent, the appointment untouched, and the
    function returns normally."""
    from app.calendar_models import AppointmentStatus
    from app.services import notification_service
    repo = notification_service.notification_attempt_repository
    sms = _recording_sms(monkeypatch)
    email = _recording_email(monkeypatch)
    _set_office_contacts(db, client_row, OFFICE_PHONE, OFFICE_EMAIL)
    appointment = _make_appointment(db, client_row)

    real_claim = repo.claim_channel_attempt

    def flaky_claim(session, client_id, appointment_id, channel, allowed):
        if channel == "office_sms":
            raise RuntimeError("db down (test fake)")
        return real_claim(session, client_id, appointment_id, channel,
                          allowed)

    monkeypatch.setattr(repo, "claim_channel_attempt", flaky_claim)
    outcome = _send(db, client_row, appointment)   # must not raise

    assert sms == []                               # never sent without a claim
    assert [d for d, _ in email] == [OFFICE_EMAIL]
    rows = _attempt_rows(db, appointment.id)
    assert set(rows) == {"office_email"}
    assert rows["office_email"].status == "sent"
    db.refresh(appointment)
    assert appointment.status == AppointmentStatus.PENDING   # booking intact
    assert appointment.office_sms_sent is False    # not falsely changed
    assert appointment.office_email_sent is True
    assert appointment.notify_error is None
    assert outcome.office_sms_sent is False
    assert outcome.office_email_sent is True
    assert outcome.errors == []
    out = capsys.readouterr().out
    assert "event=claim_record_failed" in out
    assert "channel=office_sms" in out
    assert "exc_class=RuntimeError" in out
    assert str(appointment.id) in out
    assert "db down" not in out                    # raw text withheld


def test_claim_commit_failure_isolated_and_nothing_falsely_recorded(
        db, client_row, monkeypatch, capsys):
    """A failure committing the claim rolls the claim back completely: zero
    provider calls, ZERO ledger rows for that channel (nothing half-exists),
    the other channel independent — and because nothing false was recorded,
    a later invocation claims and sends that channel exactly once."""
    from app.services import notification_service
    sms = _recording_sms(monkeypatch)
    email = _recording_email(monkeypatch)
    _set_office_contacts(db, client_row, OFFICE_PHONE, OFFICE_EMAIL)
    appointment = _make_appointment(db, client_row)
    db.rollback()

    real_commit = db.commit
    calls = {"n": 0}

    def failing_first_commit():
        calls["n"] += 1
        if calls["n"] == 1:                        # the SMS claim commit
            raise RuntimeError("commit failed (test fake)")
        real_commit()

    monkeypatch.setattr(db, "commit", failing_first_commit)
    first = notification_service.send_booking_notifications(
        db, client_row, appointment, _settings_precomputed(db, client_row))
    monkeypatch.setattr(db, "commit", real_commit)
    db.rollback()

    assert sms == []                               # no send without a claim
    assert [d for d, _ in email] == [OFFICE_EMAIL]
    rows = _attempt_rows(db, appointment.id)
    assert set(rows) == {"office_email"}           # SMS claim fully undone
    assert first.office_sms_sent is False and first.office_email_sent is True
    out = capsys.readouterr().out
    assert "event=claim_record_failed" in out and "channel=office_sms" in out
    assert "commit failed" not in out              # raw text withheld

    second = _send(db, client_row, appointment)    # nothing false recorded:
    assert [d for d, _ in sms] == [OFFICE_PHONE]   # claims + sends once now
    assert second.office_sms_sent and second.office_email_sent
    db.refresh(appointment)
    assert appointment.office_sms_sent is True
    assert appointment.notify_error is None


def _settings_precomputed(db, client):
    """Settings evaluated and the test-owned read work ended, so a direct
    service call afterwards meets the strict entry contract."""
    settings = _settings(client)
    db.rollback()
    return settings


def test_booking_survives_total_claim_persistence_failure(
        db, client_row, conversation_row, monkeypatch, capsys):
    """The preserved contract end to end on the production preceding
    operation: after finalize_booking commits, BOTH channels' claim
    persistence fails — the service still returns normally, the booked
    appointment is untouched, no flag or notify_error is falsely changed,
    no ledger row exists, and both failures are logged with controlled
    fields only."""
    from app.calendar_models import Appointment, AppointmentStatus
    from app.services import notification_service
    from app.services.appointment_hold_service import place_hold
    from app.services.booking_service import finalize_booking
    from app.repositories.appointment_repository import create_slot
    repo = notification_service.notification_attempt_repository
    sms = _recording_sms(monkeypatch)
    email = _recording_email(monkeypatch)
    _set_office_contacts(db, client_row, OFFICE_PHONE, OFFICE_EMAIL)
    settings = _settings(client_row)
    start = _now() + timedelta(hours=48)
    slot = create_slot(db, client_row.id, start, start + timedelta(minutes=45))
    db.commit()
    place_hold(db, client_row.id, slot.id, conversation_row.id,
               settings=settings, time_preference="any",
               service_key=None, now_utc=_now())
    result = finalize_booking(
        db, client_row.id, slot.id, conversation_row.id,
        settings=settings, now_utc=_now(),
        time_preference="any", service_key=None,
        patient_name="Kevin Alvarado", patient_phone=PATIENT_PHONE,
        patient_email=None, new_or_returning="new",
        reason="cleaning/checkup", urgency="routine",
    )
    assert result.success
    # Identity-map read ONLY (no SQL, no autobegin): touching the expired
    # result.appointment.id here would open a read transaction and the
    # strict entry contract would (correctly) abstain — this test proves
    # the exact production post-finalize invocation contract, so nothing
    # may open, then repair, a transaction before the service call.
    appointment_id = sa_inspect(result.appointment).identity[0]
    assert appointment_id is not None
    assert not db.in_transaction()

    def always_failing_claim(session, client_id, appointment_id_, channel,
                             allowed):
        raise RuntimeError("db down (test fake)")

    monkeypatch.setattr(repo, "claim_channel_attempt", always_failing_claim)
    outcome = notification_service.send_booking_notifications(
        db, client_row, result.appointment, settings)   # must not raise

    assert sms == [] and email == []
    assert _attempt_rows(db, appointment_id) == {}
    db.expire_all()
    fresh = db.get(Appointment, appointment_id)
    assert fresh is not None                        # the booking survives
    assert fresh.status == AppointmentStatus.PENDING
    assert fresh.office_sms_sent is False           # not falsely changed
    assert fresh.office_email_sent is False
    assert fresh.notify_error is None
    assert (outcome.office_sms_sent, outcome.office_email_sent,
            outcome.errors) == (False, False, [])
    out = capsys.readouterr().out
    assert out.count("event=claim_record_failed") == 2
    assert "channel=office_sms" in out and "channel=office_email" in out
    assert "db down" not in out


def test_format_local_exists_and_is_used_by_all_formatters():
    """Static AST regression (correction pass 1): _format_local exists at
    module scope and every message formatter calls it — the accidental
    orphaning of its body inside another function can never recur
    silently."""
    import ast
    import inspect
    from app.services import notification_service

    tree = ast.parse(inspect.getsource(notification_service))
    top_level = {node.name: node for node in tree.body
                 if isinstance(node, ast.FunctionDef)}
    assert "_format_local" in top_level

    def calls_format_local(function_name):
        return any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_format_local"
            for node in ast.walk(top_level[function_name])
        )

    for formatter in ("build_office_sms", "build_office_email_body",
                      "build_patient_sms"):
        assert formatter in top_level, formatter
        assert calls_format_local(formatter), formatter


def test_service_mismatched_client_and_appointment_suppresses_all_channels(
        db, client_row, monkeypatch, capsys):
    """v4 tenant-source security test: entering the SERVICE with Office B's
    client object but Office A's appointment must suppress everything. The
    snapshot carries the SUPPLIED client's id (never the appointment's own
    tenant id), so the atomic claim's tenant join inserts zero rows, no
    provider runs, no Office A patient data can reach Office B's
    recipients, reconciliation cannot lock or mutate the foreign
    appointment, and the service returns normally with an empty outcome."""
    from app.services import notification_service
    office_a = client_row
    sms = _recording_sms(monkeypatch)
    email = _recording_email(monkeypatch)
    _set_office_contacts(db, office_a, OFFICE_PHONE, OFFICE_EMAIL)
    appointment = _make_appointment(db, office_a)     # Office A's appointment

    office_b = _foreign_client(db)
    office_b.notification_phone = "516-555-0999"      # distinct recipients
    office_b.notification_email = "frontdesk@otheroffice.example"
    db.add(office_b)
    db.commit()

    db.refresh(appointment)
    before = (appointment.office_sms_sent, appointment.office_email_sent,
              appointment.patient_sms_sent, appointment.notify_error,
              appointment.status, appointment.patient_name,
              appointment.patient_phone, appointment.start_datetime)

    settings = _settings(office_b)
    # Direct snapshot assertion: client_id is the SUPPLIED client's id.
    snapshot = notification_service.build_notification_snapshot(
        office_b, appointment, settings)
    assert snapshot.client_id == office_b.id
    assert snapshot.appointment_id == appointment.id
    db.rollback()      # End test-owned snapshot/settings-read transaction.

    outcome = notification_service.send_booking_notifications(
        db, office_b, appointment, settings)          # must not raise

    assert sms == [] and email == []                  # zero provider calls
    assert _attempt_rows(db, appointment.id) == {}    # zero claim rows
    db.expire_all()
    after = (appointment.office_sms_sent, appointment.office_email_sent,
             appointment.patient_sms_sent, appointment.notify_error,
             appointment.status, appointment.patient_name,
             appointment.patient_phone, appointment.start_datetime)
    assert after == before                            # foreign row untouched
    assert appointment.patient_sms_sent is False
    assert (outcome.office_sms_sent, outcome.office_email_sent,
            outcome.patient_sms_sent, outcome.errors) == (
        False, False, False, [])                      # no raw detail
    out = capsys.readouterr().out
    for forbidden in ("Kevin", PATIENT_PHONE, "516-555-0999",
                      "otheroffice.example"):
        assert forbidden not in out
