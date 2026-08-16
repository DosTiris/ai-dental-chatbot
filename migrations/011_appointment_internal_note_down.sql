-- migrations/011_appointment_internal_note_down.sql
--
-- Reverses 011: removes the office-internal note column (and, with it, its
-- CHECK constraint - PostgreSQL drops column constraints with the column).
-- DATA LOSS BY DESIGN: any stored internal notes are destroyed; that is what
-- reversing this migration means, and the constitution requires it to be
-- stated rather than hidden.
--
-- ROLLBACK ORDER (mirrors the 011 up header): roll back the APPLICATION code
-- FIRST - code that maps appointments.internal_note breaks on every
-- appointments SELECT once the column is gone - and only then run this file.
--
-- IF EXISTS so the rollback is safe to run even when 011 was never applied
-- (001/007/008/009/010 down-migration convention for drops).

BEGIN;

ALTER TABLE appointments
    DROP COLUMN IF EXISTS internal_note;

COMMIT;
