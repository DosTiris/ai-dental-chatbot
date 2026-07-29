-- migrations/006_notification_attempts_up.sql
--
-- PATCH 9A (Senior Audit Recommended #1): notification-attempt ledger.
--
-- Creates the ONE table that makes office-notification duplicate
-- suppression a DATABASE invariant instead of a control-flow convention.
-- ADDITIVE ONLY: no existing table, column, constraint, index, or row is
-- touched. Legacy appointments keep their notification projection
-- (office_sms_sent / office_email_sent / notify_error) untouched; the
-- application's runtime legacy suppression (approved Patch 9A Option B)
-- protects them WITHOUT any backfill — this migration writes no data.
--
-- LOGICAL IDENTITY (approved): one row per (appointment_id, channel).
-- uq_notification_attempt_per_channel is the ATOMIC CLAIM ARBITER: the
-- application claims a channel with a single
--   INSERT ... SELECT ... ON CONFLICT DO NOTHING RETURNING id
-- so at most one provider execution can ever be initiated per
-- appointment/channel after the 9A cutover, across every process.
--
-- STATUS SEMANTICS (approved three-state machine):
--   sending — claim committed BEFORE the provider call; outcome not yet
--             recorded. May persist permanently after a crash: that is an
--             HONEST unresolved state, suppressed from any re-send until
--             Patch 9B adds recovery.
--   sent    — the provider API call returned successfully AND the outcome
--             transaction committed. It does NOT mean the SMS was
--             delivered, the email reached the inbox, the office opened
--             anything, or that delivery was confirmed in any way.
--   unknown — the provider call raised/timed out/failed to produce a
--             safely classifiable success. Deliberately NOT named
--             "failed": a timeout after provider acceptance is
--             indistinguishable from a rejection with the current SDK
--             usage, and this system never claims definite non-delivery.
--
-- ck_notification_attempt_resolution pairs the state with its timestamp:
-- sending has no resolved_at; sent/unknown must carry one.
--
-- TENANT OWNERSHIP (approved derived-tenancy design): there is NO
-- client_id column on purpose. Ownership derives through the appointment
-- row; every application read/update joins appointments and filters by
-- appointments.client_id, so the database cannot represent a
-- client/appointment mismatch (and the separate cross-office FK audit
-- finding is not silently combined here).
--
-- DELIBERATELY EXCLUDED (approved 9A minimization): notification_type,
-- notification_version, attempts, available_at, claimed_at (in 9A the
-- claim IS row creation — created_at records it), updated_at,
-- last_error_code, provider_message_id, payload/body/subject/recipient
-- snapshots, and every worker/retry field. No patient channel is
-- representable: the channel CHECK admits only the two office channels.
--
-- NO "IF NOT EXISTS" ON PURPOSE (002/003/004/005 convention): applying
-- this migration twice must fail loudly, never half-apply silently.
--
-- ROLLOUT ORDER (documented in docs/INTEGRATION.md — the no-overlap
-- cutover is REQUIRED): apply 001 -> 002 -> 003 -> 004 -> 005 -> 006,
-- stop/drain every pre-9A instance, deploy the Patch 9A application code,
-- verify no pre-9A instance remains, then resume traffic. Overlapping old
-- and new notification executors are NOT duplicate-safe.

BEGIN;

CREATE TABLE notification_attempts (
    id             UUID PRIMARY KEY NOT NULL DEFAULT gen_random_uuid(),
    appointment_id UUID NOT NULL,
    channel        TEXT NOT NULL,
    status         TEXT NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at    TIMESTAMPTZ NULL,
    CONSTRAINT fk_notification_attempts_appointment
        FOREIGN KEY (appointment_id) REFERENCES appointments(id)
        ON DELETE RESTRICT,
    CONSTRAINT ck_notification_attempt_channel
        CHECK (channel IN ('office_sms', 'office_email')),
    CONSTRAINT ck_notification_attempt_status
        CHECK (status IN ('sending', 'sent', 'unknown')),
    CONSTRAINT ck_notification_attempt_resolution
        CHECK (
            (status = 'sending' AND resolved_at IS NULL)
            OR (status <> 'sending' AND resolved_at IS NOT NULL)
        )
);

-- The atomic claim arbiter: one logical attempt per appointment/channel.
-- Its leading column also serves every application lookup path, so no
-- additional ordinary index is required.
CREATE UNIQUE INDEX uq_notification_attempt_per_channel
    ON notification_attempts (appointment_id, channel);

COMMENT ON TABLE notification_attempts IS
    'Office-notification attempt ledger (Patch 9A). One row per '
    'appointment/channel; the unique index is the atomic claim arbiter '
    'for duplicate suppression. Tenant ownership derives through '
    'appointments (no client_id column by design). Stores no message '
    'bodies, recipients, provider identifiers, or error text.';

COMMENT ON COLUMN notification_attempts.status IS
    'sending = claim committed, outcome unrecorded (may persist after a '
    'crash; honest unresolved state until Patch 9B). sent = provider API '
    'call returned successfully and the outcome transaction committed — '
    'NOT a delivery confirmation. unknown = the provider call did not '
    'produce a safely classifiable success; never asserted as definite '
    'non-delivery.';

COMMENT ON COLUMN notification_attempts.resolved_at IS
    'UTC instant of the single sending -> sent/unknown transition. NULL '
    'exactly while status = sending (enforced by '
    'ck_notification_attempt_resolution). Immutable once written.';

COMMIT;
