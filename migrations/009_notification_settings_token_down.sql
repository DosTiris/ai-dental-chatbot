-- migrations/009_notification_settings_token_down.sql
--
-- P6-A rollback: removes ONLY the single optimistic-concurrency token column
-- that 009 added to clients (its COMMENT drops automatically with the column).
-- The notification DESTINATION columns (notification_email /
-- notification_phone) and every other clients column are NOT touched - this
-- rollback destroys no destination data, only the version token.
--
-- ROLLBACK ORDER (mirrors the 009 up header): roll back the APPLICATION code
-- FIRST - code that maps clients.notification_settings_updated_at and mounts
-- the notification-settings router breaks on every clients SELECT once the
-- column is gone - and only then run this file.
--
-- IF EXISTS so the rollback is safe to run even when 009 was never applied
-- (001/007/008 down-migration convention for drops).

BEGIN;

ALTER TABLE clients
    DROP COLUMN IF EXISTS notification_settings_updated_at;

COMMIT;
