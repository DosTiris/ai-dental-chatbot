-- migrations/004_staff_confirmation_down.sql
--
-- Reverses 004 ONLY: removes the confirmed_at audit column from
-- appointments. Touches nothing created by 001, 002, or 003.
--
-- DATA WARNING: dropping the column discards any staff-confirmation
-- timestamps recorded since 004 was applied — that is the point of this
-- rollback. Appointment STATUS values already flipped to 'confirmed'
-- remain 'confirmed' (a valid pre-Patch-4 value; rewriting them would
-- falsify staff actions).
--
-- "IF EXISTS" ON PURPOSE (approved): the DOWN direction is safe-rollback
-- code and must succeed even if 004 was never applied or was already
-- rolled back — a second run is a harmless no-op. The UP direction has no
-- such guard and fails loudly instead.
--
-- ORDER: revert (or stop) the Patch 4 application code BEFORE running this
-- script — deployed Patch 4 code queries confirmed_at and would fail once
-- the column is gone.

BEGIN;

ALTER TABLE appointments
    DROP COLUMN IF EXISTS confirmed_at;

COMMIT;
