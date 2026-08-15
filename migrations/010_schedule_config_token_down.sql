-- migrations/010_schedule_config_token_down.sql
--
-- P4-B rollback: removes ONLY the single optimistic-concurrency token column
-- that 010 added to clients (its COMMENT drops automatically with the column).
-- The recurring-config sources of truth - clients.office_hours and
-- clients.settings["calendar"]["recurring"] - and every other clients column
-- are NOT touched: this rollback destroys no weekly-hours, slot_minutes, or
-- closures data, only the version token.
--
-- ROLLBACK ORDER (mirrors the 010 up header): roll back the APPLICATION code
-- FIRST - code that maps clients.schedule_config_updated_at and mounts the
-- recurring-schedule router breaks on every clients SELECT once the column is
-- gone - and only then run this file.
--
-- IF EXISTS so the rollback is safe to run even when 010 was never applied
-- (001/007/008/009 down-migration convention for drops).

BEGIN;

ALTER TABLE clients
    DROP COLUMN IF EXISTS schedule_config_updated_at;

COMMIT;
