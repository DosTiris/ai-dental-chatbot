# app/repositories/notification_attempt_repository.py
#
# OWNER OF: every read and write against notification_attempts (Patch 9A —
# Senior Audit Recommended #1). No other module may query this table
# (Rule 15).
#
# TENANT OWNERSHIP (approved derived-tenancy design): the table has NO
# client_id column. EVERY function here resolves ownership through the
# appointments table and filters by appointments.client_id — a foreign
# tenant's ids insert nothing, read nothing, and update nothing (Rule 15:
# no office may ever touch another office's data).
#
# TRANSACTION OWNERSHIP (repository convention): this layer does NOT
# commit. The calling service (notification_service) owns every
# begin/commit/rollback boundary, because the claim, the outcome
# compare-and-set, and the appointment projection are service-defined
# units of work.
#
# POSTGRESQL-ONLY ON PURPOSE: the atomic claim uses PostgreSQL's
# INSERT ... ON CONFLICT DO NOTHING ... RETURNING. The calendar test suite
# runs on PostgreSQL only (the conftest safeguards enforce that, the same
# ground the migration-005 regex CHECK already relies on) and production
# is PostgreSQL. SQLite is not supported here and no fallback is provided:
# a silent fallback would be a hidden second claim pathway (Rule 4).

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, FrozenSet, Optional

from sqlalchemy import literal, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.calendar_models import (
    Appointment,
    NotificationAttempt,
    NotificationAttemptStatus,
    NotificationChannel,
)


# ---------------------------------------------------------------------------
# Claim dispositions — the ONLY values ClaimResult.disposition can carry.
# ---------------------------------------------------------------------------
class ClaimDisposition:
    CLAIMED = "claimed"                # This caller inserted the row and owns
                                       # exactly one provider execution.
    EXISTING = "existing"              # A logical attempt already exists (any
                                       # state); provider execution suppressed.
    NOT_CLAIMABLE = "not_claimable"    # No row exists and none may be created:
                                       # legacy-protected projection, malformed
                                       # notify_error, foreign tenant, or
                                       # missing appointment (deliberately
                                       # indistinguishable, Rule 15).

    ALL = {CLAIMED, EXISTING, NOT_CLAIMABLE}


@dataclass(frozen=True)
class ClaimResult:
    """Immutable scalar claim result (approved contract): only identifiers
    and state cross back to the service — never a live ORM object, so no
    expired-attribute access can reopen a transaction at the provider
    boundary."""
    disposition: str
    channel: str
    attempt_id: Optional[uuid.UUID] = None      # CLAIMED / EXISTING only.
    existing_status: Optional[str] = None       # EXISTING only.


# Channel -> the appointment projection flag that must still be False for a
# claim to be eligible (monotonic sent flags: a true flag blocks re-claims).
_CHANNEL_FLAG_COLUMNS = {
    NotificationChannel.OFFICE_SMS: Appointment.office_sms_sent,
    NotificationChannel.OFFICE_EMAIL: Appointment.office_email_sent,
}


def claim_channel_attempt(
    db: Session,
    client_id: uuid.UUID,
    appointment_id: uuid.UUID,
    channel: str,
    allowed_stored_error_values: FrozenSet[str],
) -> ClaimResult:
    """
    Purpose: THE atomic per-channel claim (Patch 9A). One PostgreSQL
        statement — INSERT ... SELECT ... ON CONFLICT DO NOTHING RETURNING —
        enforces, in a single indivisible step: the appointment exists FOR
        THIS CLIENT; the channel's sent flag is False (protects legacy sent
        projections and post-9A sent outcomes alike); and notify_error is
        NULL or one of the caller-supplied allowed values (the approved
        vocabulary values carrying NO send_failed entry for this channel —
        so a legacy unknown outcome blocks, and any malformed value blocks
        because it is outside the closed set). ON CONFLICT DO NOTHING makes
        the unique index the race arbiter WITHOUT raising 23505, so a
        losing transaction stays healthy and usable (an ordinary ORM INSERT
        would abort the transaction until rollback — forbidden by the
        approved plan).
    Inputs:  ids; channel (NotificationChannel value); the channel-specific
        allowed stored-error values, computed by notification_service (the
        vocabulary's single owner, Rule 3) and passed in.
    Returns: frozen ClaimResult — claimed / existing(+state) / not_claimable.
    Database effects: at most one INSERT into notification_attempts
        (uncommitted — the service owns the commit). The eligibility SELECT
        takes NO row locks.
    Possible failures: ValueError on an unknown channel (a bug upstream,
        surfaced loudly per Rule 4); database errors propagate.
    """
    if channel not in NotificationChannel.ALL:
        raise ValueError(f"Unknown notification channel: {channel!r}")

    flag_column = _CHANNEL_FLAG_COLUMNS[channel]

    stored_error_permits_claim = Appointment.notify_error.is_(None)
    if allowed_stored_error_values:
        stored_error_permits_claim = or_(
            stored_error_permits_claim,
            Appointment.notify_error.in_(sorted(allowed_stored_error_values)),
        )

    eligibility = (
        select(
            Appointment.id.label("appointment_id"),
            literal(channel).label("channel"),
            literal(NotificationAttemptStatus.SENDING).label("status"),
        )
        .where(
            Appointment.id == appointment_id,
            Appointment.client_id == client_id,   # Derived tenancy at write time.
            flag_column.is_(False),
            stored_error_permits_claim,
        )
    )

    statement = (
        pg_insert(NotificationAttempt)
        .from_select(["appointment_id", "channel", "status"], eligibility)
        .on_conflict_do_nothing(
            index_elements=["appointment_id", "channel"]
        )
        .returning(NotificationAttempt.id)
    )
    inserted = db.execute(statement).first()
    if inserted is not None:
        return ClaimResult(
            disposition=ClaimDisposition.CLAIMED,
            channel=channel,
            attempt_id=inserted[0],
        )

    # No row came back: either the unique index arbitrated against us
    # (a logical attempt already exists) or eligibility filtered the SELECT
    # to zero rows. The same still-usable transaction distinguishes them.
    existing = (
        db.query(NotificationAttempt)
        .join(Appointment,
              NotificationAttempt.appointment_id == Appointment.id)
        .filter(
            Appointment.id == appointment_id,
            Appointment.client_id == client_id,
            NotificationAttempt.channel == channel,
        )
        .first()
    )
    if existing is not None:
        return ClaimResult(
            disposition=ClaimDisposition.EXISTING,
            channel=channel,
            attempt_id=existing.id,
            existing_status=existing.status,
        )
    return ClaimResult(disposition=ClaimDisposition.NOT_CLAIMABLE,
                       channel=channel)


def get_attempts_by_appointment(
    db: Session,
    client_id: uuid.UUID,
    appointment_id: uuid.UUID,
) -> Dict[str, NotificationAttempt]:
    """
    Purpose: Load both logical channel attempts (0-2 rows) for one
        appointment, for projection recomputation. Tenant-joined: a foreign
        tenant's appointment id returns an empty dict.
    Returns: {channel: NotificationAttempt} — at most one row per channel
        by the unique index.
    Database effects: SELECT only; no locks (the caller holds the
        appointment row lock, which serializes concurrent projections).
    """
    rows = (
        db.query(NotificationAttempt)
        .join(Appointment,
              NotificationAttempt.appointment_id == Appointment.id)
        .filter(
            Appointment.id == appointment_id,
            Appointment.client_id == client_id,
        )
        .all()
    )
    return {row.channel: row for row in rows}


def cas_attempt_to_terminal(
    db: Session,
    client_id: uuid.UUID,
    attempt_id: uuid.UUID,
    new_status: str,
    resolved_at_utc: datetime,
) -> int:
    """
    Purpose: THE single status-guarded compare-and-set writer of terminal
        attempt states (Patch 9A). Transitions exactly sending -> sent or
        sending -> unknown; the WHERE guard makes every other transition —
        sent -> unknown, unknown -> sent, terminal -> terminal with a
        rewritten resolved_at — structurally unrepresentable, so a stale or
        repeated outcome writer can never alter a terminal row.
    Inputs:  ids; new_status (must be terminal); aware-UTC resolution
        instant injected by the caller (deterministic-test convention).
    Returns: rowcount — 1 = this caller performed the transition; 0 = the
        row is already terminal, missing, or foreign-tenant (the caller
        disambiguates with get_attempt_for_tenant if needed).
    Database effects: at most one UPDATE (uncommitted — the service owns
        the commit, atomically with the appointment projection).
    Possible failures: ValueError on a non-terminal target status (a bug
        upstream, surfaced loudly per Rule 4).
    """
    if new_status not in NotificationAttemptStatus.TERMINAL:
        raise ValueError(
            f"CAS target must be terminal, got: {new_status!r}"
        )
    tenant_appointments = select(Appointment.id).where(
        Appointment.client_id == client_id
    )
    statement = (
        update(NotificationAttempt)
        .where(
            NotificationAttempt.id == attempt_id,
            NotificationAttempt.status == NotificationAttemptStatus.SENDING,
            NotificationAttempt.appointment_id.in_(tenant_appointments),
        )
        .values(status=new_status, resolved_at=resolved_at_utc)
        .execution_options(synchronize_session=False)
    )
    return db.execute(statement).rowcount


def get_attempt_for_tenant(
    db: Session,
    client_id: uuid.UUID,
    attempt_id: uuid.UUID,
) -> Optional[NotificationAttempt]:
    """
    Purpose: rowcount-0 disambiguation after the CAS: row exists for this
        tenant -> already terminal; None -> missing or foreign tenant
        (deliberately indistinguishable, Rule 15).
    Database effects: SELECT only.
    """
    return (
        db.query(NotificationAttempt)
        .join(Appointment,
              NotificationAttempt.appointment_id == Appointment.id)
        .filter(
            NotificationAttempt.id == attempt_id,
            Appointment.client_id == client_id,
        )
        .first()
    )
