-- migrations/004_staff_confirmation_up.sql
--
-- PATCH 4 (Senior Audit Critical #4): staff confirmation transition.
--
-- Adds the single audit column for the supported pending -> confirmed
-- staff action. ADDITIVE ONLY: no existing row, column, constraint, or
-- index is touched; existing rows read back confirmed_at = NULL.
--
-- confirmed_at SEMANTICS (approved):
--   * Records the UTC instant of the FIRST successful STAFF
--     pending -> confirmed action, set by booking_service.confirm_appointment.
--   * NULL means "never staff-confirmed": appointments created directly as
--     'confirmed' (require_staff_confirmation = false) keep NULL on purpose,
--     and re-confirming an already-confirmed appointment preserves the
--     original value byte-for-byte.
--
-- NO "IF NOT EXISTS" ON PURPOSE (002/003 convention): applying this
-- migration twice must fail loudly, never half-apply silently.
--
-- ROLLOUT ORDER: apply 001 -> 002 -> 003 -> 004, all BEFORE deploying the
-- Patch 4 application code — the ORM model, confirm service, and admin
-- appointment views reference this column.

BEGIN;

ALTER TABLE appointments
    ADD COLUMN confirmed_at TIMESTAMPTZ NULL;

COMMENT ON COLUMN appointments.confirmed_at IS
    'UTC instant of the FIRST successful staff pending->confirmed action. '
    'NULL = never staff-confirmed (includes appointments created directly '
    'as confirmed via require_staff_confirmation=false). Preserved '
    'byte-for-byte on repeated confirmation.';

COMMIT;
