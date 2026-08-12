-- migrations/008_office_lead_workflow_down.sql
--
-- P3-B2-S1 rollback: removes ONLY the four office-workflow columns that
-- 008 added to conversations (their CHECK constraints and comments drop
-- automatically with the columns). Every other conversations column -
-- including Mia's system-owned lead_status and all intake data - is NOT
-- touched by this rollback.
--
-- WARNING (Rule 15 backup plan): once any office has entered a workflow
-- status or note, dropping these columns DESTROYS that office-entered
-- data permanently. Export first, e.g. via psql \copy:
--   COPY (SELECT id, office_status, office_status_updated_at,
--                office_note, office_note_updated_at
--           FROM conversations
--          WHERE office_status IS NOT NULL
--             OR office_status_updated_at IS NOT NULL
--             OR office_note IS NOT NULL
--             OR office_note_updated_at IS NOT NULL)
--     TO STDOUT WITH CSV HEADER;
--
-- ROLLBACK ORDER (mirrors the 008 up header): roll back the application
-- code FIRST - code that maps these columns breaks on every Conversation
-- SELECT once they are gone - and only then run this file.
--
-- IF EXISTS so the rollback is safe to run even when 008 was never applied
-- (001/007 down-migration convention for drops).

BEGIN;

ALTER TABLE conversations
    DROP COLUMN IF EXISTS office_status,
    DROP COLUMN IF EXISTS office_status_updated_at,
    DROP COLUMN IF EXISTS office_note,
    DROP COLUMN IF EXISTS office_note_updated_at;

COMMIT;
