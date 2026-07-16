-- migrations/002_calendar_integrity_hardening_up.sql
--
-- Mia Calendar — Patch 1 (database integrity), forward migration.
--
-- WHAT THIS ADDS
-- Two PARTIAL UNIQUE indexes on appointments that make the two critical
-- booking invariants true AT THE DATABASE LEVEL, not merely in Python:
--
--   1. One ACTIVE (non-cancelled) appointment per conversation.
--      Closes the race where two concurrent finalize requests for the SAME
--      conversation each pass the application pre-check, lock DIFFERENT
--      slot rows, and both insert (Senior Audit, Critical #1).
--
--   2. One ACTIVE (non-cancelled) appointment per slot.
--      A backstop: the slot row lock is still the primary same-slot defense,
--      but if any future code path inserts without taking that lock, the
--      database now refuses the duplicate instead of silently accepting it.
--
-- WHY "status <> 'cancelled'":
--   Cancellation must reopen the slot for legitimate rebooking (by the same
--   OR a different conversation). Cancelled rows are excluded from both
--   indexes so history is preserved while rebooking stays legal.
--   'completed' and 'no_show' rows remain covered on purpose: they consumed
--   their slot, and a second active appointment on it is still invalid.
--
-- WHY "conversation_id IS NOT NULL":
--   Staff-created appointments (source = staff_admin) have no conversation.
--   PostgreSQL would not unique-compare NULLs anyway; the predicate makes
--   the intent explicit and keeps the index small.
--
-- DELIBERATELY NO "IF NOT EXISTS":
--   This is a numbered migration. If an object with either name already
--   exists, or the schema has drifted, this migration MUST fail loudly
--   rather than half-apply (Constitution Rule 15).
--
-- PRE-FLIGHT NOTE:
--   If the target database already contains rows violating either invariant
--   (two active appointments for one conversation or one slot), CREATE
--   UNIQUE INDEX fails with the offending duplicate reported by PostgreSQL.
--   That is the correct behavior: resolve the duplicate rows manually,
--   then re-run. Do not weaken the index to accommodate corrupt data.
--
-- ADDITIVE ONLY: no table, column, or row is modified or destroyed.
-- Rollback: 002_calendar_integrity_hardening_down.sql
-- Depends on: 001_calendar_mvp_up.sql (appointments table must exist).

BEGIN;

-- Invariant 1: one active appointment per conversation.
CREATE UNIQUE INDEX uq_active_appointment_per_conversation
    ON appointments (conversation_id)
    WHERE conversation_id IS NOT NULL AND status <> 'cancelled';

-- Invariant 2: one active appointment per slot (database-level backstop).
CREATE UNIQUE INDEX uq_active_appointment_per_slot
    ON appointments (slot_id)
    WHERE status <> 'cancelled';

COMMIT;
