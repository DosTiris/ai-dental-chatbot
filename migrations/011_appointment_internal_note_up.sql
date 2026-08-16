-- migrations/011_appointment_internal_note_up.sql
--
-- PHASE 3A Slice 4B1 (Appointment Internal Notes - storage contract): ONE
-- nullable office-internal note column on appointments. ADDITIVE ONLY: no
-- existing table, column, constraint, index, or row is touched, and NO
-- default is set, so every existing appointments row remains valid with
-- internal_note = NULL ("no note has been written").
--
-- SEMANTICS (owner contract): office-internal administrative text attached
-- to one appointment. Optional. Plain text (never HTML-rendered or
-- interpreted). NEVER patient-facing: it must not appear in chatbot
-- responses, patient-facing booking responses, patient SMS/email, office
-- booking notifications, notification templates, public/widget APIs,
-- transcripts, confirmation wording, or exports. It is independent from
-- reason (which is NOT repurposed), from urgency, and from notification
-- state, and it affects no slot eligibility, availability, status, booking
-- policy, confirmation, cancellation, or inventory.
--
-- LENGTH PROTECTION (contract-exact shape): at most 2000 characters when
-- present. Deliberately NO btrim(...) <> '' clause (unlike the migration-008
-- office_note shape): the contract for THIS column specifies the length
-- check only, and blank-to-NULL normalization is owned by the application's
-- single normalization helper at the authenticated portal write boundaries.
--
-- create_all() INTERACTION (matches the 008/009/010 rollout note):
-- appointments is mapped on app.database.Base, so create_all() on a FRESH
-- database creates this column from the ORM mapping. On an EXISTING
-- production appointments table create_all() cannot ALTER, so THIS migration
-- is the sole production schema authority. Rollout order: migrate FIRST,
-- deploy the application code SECOND (rollback order is the reverse - see
-- the down file).
--
-- NO "IF NOT EXISTS" ON PURPOSE (003/004/005/006/008/009/010 convention):
-- applying this migration twice must fail loudly rather than silently no-op.

BEGIN;

ALTER TABLE appointments
    ADD COLUMN internal_note TEXT NULL;

-- A present note is bounded (at most 2000 characters). Blank-to-NULL
-- normalization is an application write-boundary rule, not a schema rule
-- (contract-exact check shape - see the header).
ALTER TABLE appointments
    ADD CONSTRAINT ck_appointments_internal_note_len
    CHECK (internal_note IS NULL
           OR char_length(internal_note) <= 2000);

COMMENT ON COLUMN appointments.internal_note IS
    'Slice 4B1 office-internal administrative note; optional plain text, '
    'at most 2000 chars, blank normalized to NULL by the application; '
    'NEVER patient-facing and never included in notifications, chat, '
    'public/widget APIs, or exports.';

COMMIT;
