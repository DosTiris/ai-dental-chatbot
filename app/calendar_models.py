# app/calendar_models.py
#
# OWNER OF: calendar data structures (tables, statuses, state names).
# This file defines WHAT the calendar stores. It contains no business logic.
#
# Constitution notes:
#   - Rule 3 (Single Source of Truth): every status string and booking state
#     name used anywhere in the calendar system is defined HERE and only here.
#   - Rule 14 (State Machine Discipline): valid booking states and transitions
#     are documented at the bottom of this file.
#
# DESIGN DECISION (found while mentally testing the roadmap):
#   The original roadmap proposed BOTH an `appointment_holds` table AND
#   `held_until` fields on `appointment_slots`. That is duplicate state — two
#   owners for one fact ("is this slot held?") — and the two can drift apart.
#   Per Rule 3, holds live ONLY on the slot row (status/held_until/held_by).
#   A separate holds table is not needed until multi-slot carts exist.

import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID

from app.database import Base


# ---------------------------------------------------------------------------
# Slot statuses — the ONLY valid values for appointment_slots.status.
# ---------------------------------------------------------------------------
class SlotStatus:
    AVAILABLE = "available"   # Bookable, shown to patients.
    HELD = "held"             # Temporarily reserved by one conversation.
    BOOKED = "booked"         # A confirmed appointment occupies it.
    BLOCKED = "blocked"       # Staff removed it from booking (lunch, meeting).
    CANCELLED = "cancelled"   # Staff deleted it; kept for audit instead of hard delete.

    ALL = {AVAILABLE, HELD, BOOKED, BLOCKED, CANCELLED}


# ---------------------------------------------------------------------------
# Appointment statuses — the ONLY valid values for appointments.status.
# ---------------------------------------------------------------------------
class AppointmentStatus:
    PENDING = "pending"       # Booked by Mia; office has not confirmed yet.
    CONFIRMED = "confirmed"   # Office confirmed (or auto-confirm is enabled).
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    NO_SHOW = "no_show"

    ALL = {PENDING, CONFIRMED, CANCELLED, COMPLETED, NO_SHOW}


# ---------------------------------------------------------------------------
# Booking conversation states — the ONLY valid values for
# conversations.booking_state. See transition table at bottom of file.
# ---------------------------------------------------------------------------
class BookingState:
    NONE = "none"                                   # Not in a booking flow.
    WAITING_FOR_DATE = "waiting_for_date"
    WAITING_FOR_TIME_PREFERENCE = "waiting_for_time_preference"
    WAITING_FOR_SLOT_SELECTION = "waiting_for_slot_selection"
    WAITING_FOR_CONFIRMATION = "waiting_for_confirmation"
    BOOKED = "booked"                               # Terminal; cleared to NONE after reply.

    ALL = {
        NONE,
        WAITING_FOR_DATE,
        WAITING_FOR_TIME_PREFERENCE,
        WAITING_FOR_SLOT_SELECTION,
        WAITING_FOR_CONFIRMATION,
        BOOKED,
    }


# ---------------------------------------------------------------------------
# Notification channels — the ONLY valid values for
# notification_attempts.channel (Patch 9A). Office channels only: no patient
# channel is representable, mirroring the migration-006 CHECK exactly
# (Patch 2D policy made structural).
# ---------------------------------------------------------------------------
class NotificationChannel:
    OFFICE_SMS = "office_sms"
    OFFICE_EMAIL = "office_email"

    ALL = {OFFICE_SMS, OFFICE_EMAIL}


# ---------------------------------------------------------------------------
# Notification attempt statuses — the ONLY valid values for
# notification_attempts.status (Patch 9A). Approved three-state machine:
#
#   (no row) -> SENDING   the atomic claim; committed BEFORE the provider
#                         call. May persist permanently after a crash —
#                         an HONEST unresolved state, suppressed from any
#                         re-send until Patch 9B adds recovery.
#   SENDING  -> SENT      the provider API call returned successfully AND
#                         the outcome transaction committed. NOT a delivery
#                         confirmation: it does not mean the SMS was
#                         delivered, the inbox received the email, or the
#                         office opened anything.
#   SENDING  -> UNKNOWN   the provider call raised/timed out/did not
#                         produce a safely classifiable success. There is
#                         deliberately NO "failed" state: with the current
#                         SDK usage a timeout after provider acceptance is
#                         indistinguishable from a rejection, and this
#                         system never claims definite non-delivery.
#
# SENT and UNKNOWN are terminal and immutable (resolved_at included).
# ---------------------------------------------------------------------------
class NotificationAttemptStatus:
    SENDING = "sending"
    SENT = "sent"
    UNKNOWN = "unknown"

    ALL = {SENDING, SENT, UNKNOWN}
    TERMINAL = {SENT, UNKNOWN}


class AppointmentSlot(Base):
    """
    One bookable time window created by office staff (controlled "Model B" calendar).

    Rows are created only through the admin calendar route. Mia never invents
    slots; it can only book what staff explicitly published.
    """

    __tablename__ = "appointment_slots"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # Client isolation (Rule 15): every query against this table MUST filter
    # by client_id. The repository layer enforces that.
    client_id = Column(UUID(as_uuid=True), ForeignKey("clients.id"), nullable=False, index=True)

    # Display-only provider label for the MVP (e.g. "Dr. Sorrentino").
    # A real providers table is deliberately deferred (Rule 17 — no premature
    # complexity). Nothing schedules "by provider" yet; this is just wording.
    provider_name = Column(String, nullable=True)

    # Optional service key from Mia's service library (e.g. "cleaning/checkup").
    # Null means "any service may book this slot".
    service_key = Column(String, nullable=True)

    # Stored in UTC (timestamptz). All display/parsing converts through the
    # client's timezone — see calendar_settings_service.resolve_client_timezone.
    start_datetime = Column(DateTime(timezone=True), nullable=False, index=True)
    end_datetime = Column(DateTime(timezone=True), nullable=False)

    status = Column(String, nullable=False, server_default=SlotStatus.AVAILABLE,
                    default=SlotStatus.AVAILABLE)

    # Hold bookkeeping. Meaning:
    #   status == "held" AND held_until >= now  -> actively held (not bookable
    #                                              by anyone else).
    #   status == "held" AND held_until <  now  -> EXPIRED hold. Treated as
    #     available everywhere (lazy reclaim — no cron job needed for the MVP;
    #     documented in appointment_hold_service).
    held_until = Column(DateTime(timezone=True), nullable=True)
    held_by_conversation_id = Column(UUID(as_uuid=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=text("now()"))


class Appointment(Base):
    """
    One confirmed (or pending-staff-confirmation) booking created by Mia
    or by staff through the admin route.
    """

    __tablename__ = "appointments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    client_id = Column(UUID(as_uuid=True), ForeignKey("clients.id"), nullable=False, index=True)

    # The slot this appointment consumed. Kept as a link (not a copy) so the
    # admin calendar can show which slot is taken and cancellation can free it.
    slot_id = Column(UUID(as_uuid=True), ForeignKey("appointment_slots.id"), nullable=False)

    # The Mia conversation that produced the booking. Used to (a) prevent one
    # conversation from booking twice and (b) audit trail.
    conversation_id = Column(UUID(as_uuid=True), ForeignKey("conversations.id"), nullable=True, index=True)

    patient_name = Column(String, nullable=False)
    patient_phone = Column(String, nullable=False)
    patient_email = Column(String, nullable=True)
    new_or_returning = Column(String, nullable=True)   # "new" / "returning" / None(unknown)
    reason = Column(String, nullable=True)             # Mia's service reason category
    urgency = Column(String, nullable=False, server_default="routine", default="routine")

    # Copied from the slot at booking time so the appointment remains readable
    # even if the slot row is later edited. This is the one justified copy.
    start_datetime = Column(DateTime(timezone=True), nullable=False)
    end_datetime = Column(DateTime(timezone=True), nullable=False)

    status = Column(String, nullable=False, server_default=AppointmentStatus.PENDING,
                    default=AppointmentStatus.PENDING)

    # PATCH 4 (Senior Audit Critical #4): staff-confirmation audit timestamp.
    # Records the UTC instant of the FIRST successful STAFF pending ->
    # confirmed action (booking_service.confirm_appointment is the only
    # writer). NULL means "never staff-confirmed": appointments created
    # directly as CONFIRMED (require_staff_confirmation = false) keep NULL on
    # purpose, and re-confirming an already-confirmed appointment preserves
    # the original value byte-for-byte. Mirrors
    # migrations/004_staff_confirmation_up.sql EXACTLY (Rule 3; the migration
    # test proves they stay in sync).
    confirmed_at = Column(DateTime(timezone=True), nullable=True)

    source = Column(String, nullable=False, server_default="mia_widget", default="mia_widget")

    # Notification outcome bookkeeping (Rule 16 — failure must be visible).
    # If the appointment saved but a message failed, these fields say so.
    office_sms_sent = Column(Boolean, nullable=False, server_default="false", default=False)
    office_email_sent = Column(Boolean, nullable=False, server_default="false", default=False)
    patient_sms_sent = Column(Boolean, nullable=False, server_default="false", default=False)
    notify_error = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=text("now()"))
    updated_at = Column(DateTime(timezone=True), server_default=text("now()"))

    # DATABASE-ENFORCED BOOKING INVARIANTS (Patch 1 — Senior Audit Critical #1).
    # These mirror migrations/002_calendar_integrity_hardening_up.sql EXACTLY
    # (Rule 3: one definition; the migration test proves they stay in sync).
    #
    #   uq_active_appointment_per_conversation — one NON-CANCELLED appointment
    #     per conversation. Closes the race where two concurrent finalize
    #     requests both pass the application pre-check, lock DIFFERENT slots,
    #     and both insert. The application check remains as the fast path;
    #     this index is the guarantee.
    #
    #   uq_active_appointment_per_slot — one NON-CANCELLED appointment per
    #     slot. Backstop for the slot row lock: holds even if a future code
    #     path inserts without locking.
    #
    # Cancelled rows are excluded so cancellation -> rebooking (same or
    # different conversation) stays legal. BOTH dialect predicates are given:
    # postgresql_where for production, sqlite_where for any SQLite-backed
    # create_all() — without sqlite_where, SQLite would silently build a FULL
    # unique index and wrongly block rebooking after cancellation.
    __table_args__ = (
        Index(
            "uq_active_appointment_per_conversation",
            "conversation_id",
            unique=True,
            postgresql_where=text("conversation_id IS NOT NULL AND status <> 'cancelled'"),
            sqlite_where=text("conversation_id IS NOT NULL AND status <> 'cancelled'"),
        ),
        Index(
            "uq_active_appointment_per_slot",
            "slot_id",
            unique=True,
            postgresql_where=text("status <> 'cancelled'"),
            sqlite_where=text("status <> 'cancelled'"),
        ),
    )


class CalendarAdminCredential(Base):
    """
    One per-office Calendar admin API credential (Patch 5 — Senior Audit
    Critical #2). Replaces the shared global ADMIN_API_KEY for every
    /admin/calendar/* route; the non-calendar /admin routes are unaffected.

    Mirrors migrations/005_calendar_admin_credentials_up.sql EXACTLY
    (Rule 3: one definition; the migration test proves they stay in sync).

    SECRET HANDLING: key_hash stores ONLY the SHA-256 digest (64 lowercase
    hex characters) of a raw key of the form mia_cal_ + token. Raw keys are
    never persisted; app/services/calendar_admin_auth.py is the single owner
    of generation, hashing, and validation.

    Multiple rows per client are allowed BY DESIGN so rotation can provision
    a new credential while the old one still works, then revoke the old one
    (active=false, revoked_at set).
    """

    __tablename__ = "calendar_admin_credentials"

    # DB-side default mirrors the migration (raw operator SQL inserts rely
    # on it); the client-side default serves ORM inserts in tests.
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
                server_default=text("gen_random_uuid()"))

    # ON DELETE RESTRICT (mirrors 005): the app never hard-deletes clients;
    # a manual delete must clean up credentials explicitly first (Rule 4).
    client_id = Column(UUID(as_uuid=True),
                       ForeignKey("clients.id", ondelete="RESTRICT"),
                       nullable=False)

    # VARCHAR(64), never CHAR(64) — bpchar blank-padding semantics are a trap.
    key_hash = Column(String(64), nullable=False)

    # Operator-facing name for the credential ("front-desk tool"), required
    # so rotation and revocation are auditable.
    label = Column(Text, nullable=False)

    active = Column(Boolean, nullable=False, server_default=text("true"),
                    default=True)
    created_at = Column(DateTime(timezone=True), nullable=False,
                        server_default=text("now()"))
    # UTC instant of revocation; NULL while active (enforced by CHECK below,
    # and re-checked in the application, failing closed against drift).
    revoked_at = Column(DateTime(timezone=True), nullable=True)

    # Uniqueness lives ONLY in the explicitly named index required by
    # migration 005 (approved condition 5: no duplicate uniqueness mechanism
    # such as unique=True on the column). The regex CHECK uses PostgreSQL
    # syntax on purpose: the calendar suite runs on PostgreSQL only (the
    # conftest safeguards enforce that) and production is PostgreSQL.
    __table_args__ = (
        CheckConstraint("key_hash ~ '^[0-9a-f]{64}$'",
                        name="ck_cal_admin_cred_key_hash_hex"),
        CheckConstraint("NOT (active AND revoked_at IS NOT NULL)",
                        name="ck_cal_admin_cred_active_not_revoked"),
        Index("uq_cal_admin_cred_key_hash", "key_hash", unique=True),
        Index("ix_cal_admin_cred_client_id", "client_id"),
    )


class NotificationAttempt(Base):
    """
    One office-notification attempt ledger row (Patch 9A — Senior Audit
    Recommended #1). Logical identity: (appointment_id, channel), enforced
    by uq_notification_attempt_per_channel — the ATOMIC CLAIM ARBITER that
    makes duplicate suppression a database invariant. The application
    claims a channel with a single INSERT ... SELECT ... ON CONFLICT DO
    NOTHING RETURNING id (app/repositories/notification_attempt_repository
    is the only reader/writer, Rule 15).

    Mirrors migrations/006_notification_attempts_up.sql EXACTLY (Rule 3:
    one definition; the migration test proves they stay in sync).

    TENANT OWNERSHIP (approved derived-tenancy design): there is NO
    client_id column on purpose. Ownership derives through the appointment
    row; every repository operation joins appointments and filters by
    appointments.client_id, so the database cannot represent a
    client/appointment mismatch.

    PRIVACY: stores no message bodies, recipients, provider identifiers,
    or error text — only channel, status, and timestamps.

    sent means ONLY that the provider API call returned successfully and
    the outcome transaction committed — never that anything was delivered,
    received, or opened. See NotificationAttemptStatus for the full
    approved state machine.
    """

    __tablename__ = "notification_attempts"

    # DB-side default mirrors the migration; the client-side default serves
    # ORM inserts in tests (005 convention).
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
                server_default=text("gen_random_uuid()"))

    # ON DELETE RESTRICT (mirrors 006): appointments are never hard-deleted
    # by the app; a manual delete must clean up the ledger explicitly first
    # (Rule 4), preserving the audit trail.
    appointment_id = Column(UUID(as_uuid=True),
                            ForeignKey("appointments.id",
                                       ondelete="RESTRICT"),
                            nullable=False)

    # TEXT exactly — mirrors migration 006 (never String/VARCHAR, so the
    # parity test's type comparison cannot drift).
    channel = Column(Text, nullable=False)

    # NO default on purpose (approved): the claim supplies 'sending'
    # explicitly; an omission is a bug and must fail loudly (Rule 4).
    status = Column(Text, nullable=False)

    # UTC claim instant — in 9A the claim IS row creation.
    created_at = Column(DateTime(timezone=True), nullable=False,
                        server_default=text("now()"))

    # UTC instant of the single sending -> sent/unknown transition; NULL
    # exactly while sending (CHECK below). Immutable once written — the
    # status-guarded compare-and-set in the repository is the only writer.
    resolved_at = Column(DateTime(timezone=True), nullable=True)

    # Uniqueness lives ONLY in the explicitly named index required by
    # migration 006 (005 convention: no duplicate uniqueness mechanism such
    # as unique=True on a column). The CHECKs mirror 006 byte-for-byte in
    # intent; the migration test proves structural alignment.
    __table_args__ = (
        CheckConstraint("channel IN ('office_sms', 'office_email')",
                        name="ck_notification_attempt_channel"),
        CheckConstraint("status IN ('sending', 'sent', 'unknown')",
                        name="ck_notification_attempt_status"),
        CheckConstraint(
            "(status = 'sending' AND resolved_at IS NULL)"
            " OR (status <> 'sending' AND resolved_at IS NOT NULL)",
            name="ck_notification_attempt_resolution",
        ),
        Index("uq_notification_attempt_per_channel",
              "appointment_id", "channel", unique=True),
    )


# ---------------------------------------------------------------------------
# BOOKING STATE TRANSITIONS (Rule 14) — the complete, closed transition table.
# Any transition not listed here is a bug.
#
#   NONE
#     -> WAITING_FOR_DATE              (scheduling intent + intake complete,
#                                       and no date was parsed from the message)
#     -> WAITING_FOR_TIME_PREFERENCE   (date already parsed from the message)
#
#   WAITING_FOR_DATE
#     -> WAITING_FOR_TIME_PREFERENCE   (valid date received)
#     -> WAITING_FOR_DATE              (unparseable date; re-ask)
#
#   WAITING_FOR_TIME_PREFERENCE
#     -> WAITING_FOR_SLOT_SELECTION    (preference received AND slots exist)
#     -> WAITING_FOR_DATE              (no slots that day; ask for another day)
#
#   WAITING_FOR_SLOT_SELECTION
#     -> WAITING_FOR_CONFIRMATION      (offered slot chosen AND hold succeeded)
#     -> WAITING_FOR_SLOT_SELECTION    (unrecognized choice, or hold lost race;
#                                       fresh slots re-offered)
#     -> WAITING_FOR_TIME_PREFERENCE   (patient typed a NEW day instead —
#                                       "changing their answer" path)
#
#   WAITING_FOR_CONFIRMATION
#     -> BOOKED                        (patient said yes AND finalize succeeded)
#     -> WAITING_FOR_SLOT_SELECTION    (finalize failed: hold expired/taken)
#     -> WAITING_FOR_DATE              (patient said no; hold released)
#     -> WAITING_FOR_CONFIRMATION      (ambiguous answer; re-ask once question)
#
#   BOOKED
#     -> NONE                          (cleared immediately after the
#                                       confirmation reply is produced)
#
#   ANY STATE
#     -> NONE                          (emergency detected, or state cleanup)
# ---------------------------------------------------------------------------
