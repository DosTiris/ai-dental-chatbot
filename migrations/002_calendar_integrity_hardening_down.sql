-- migrations/002_calendar_integrity_hardening_down.sql
--
-- Rollback for 002_calendar_integrity_hardening_up.sql.
-- Drops the two partial unique indexes and nothing else. No data is touched.
--
-- IF EXISTS is intentional here (unlike the up migration): rollback must be
-- safe to run whether or not the forward migration fully applied.
--
-- WARNING: after rolling back, the "one active appointment per conversation /
-- per slot" invariants are enforced ONLY by application code again. Do not
-- leave production in that state longer than necessary.

BEGIN;

DROP INDEX IF EXISTS uq_active_appointment_per_conversation;
DROP INDEX IF EXISTS uq_active_appointment_per_slot;

COMMIT;
