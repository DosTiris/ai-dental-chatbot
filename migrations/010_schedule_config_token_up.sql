-- migrations/010_schedule_config_token_up.sql
--
-- P4-B (Office Portal recurring-schedule management): the ONE
-- optimistic-concurrency token column for the portal's recurring-schedule
-- config writer (weekly office_hours + settings.calendar.recurring). ADDITIVE
-- ONLY: no existing table, column, constraint, index, or row is touched, and
-- NO default is set, so every existing clients row starts with a NULL token
-- (the "office has never saved a recurring config from the portal" state the
-- compare-and-set treats NULL-safely, and the F3 first-Save-before-Apply gate
-- keys off).
--
-- WHY A DEDICATED TOKEN COLUMN: the recurring config's sources of truth stay
-- exactly where chat.py already reads them - clients.office_hours (weekly
-- hours) and clients.settings["calendar"]["recurring"] (slot_minutes +
-- closures). Nothing is moved. The portal write path needs a version token for
-- the SAME race-safe compare-and-set the P6-A notification-settings writer uses,
-- and this is that token and nothing else. It mirrors 009's dedicated
-- notification_settings_updated_at; it is a SEPARATE column and never shares or
-- reuses the P6-A token.
--
-- create_all() INTERACTION (matches the 009 rollout note): clients is mapped on
-- app.database.Base, so create_all() on a FRESH database creates this column
-- from the ORM mapping (app/models.py). On an EXISTING clients table create_all
-- cannot ALTER, so THIS migration is the sole production schema authority.
-- Rollout order: migrate FIRST, deploy the application code SECOND (rollback
-- order is the reverse - see the down file).
--
-- NO "IF NOT EXISTS" ON PURPOSE (003/004/005/006/008/009 convention): applying
-- this migration twice must fail loudly rather than silently no-op.
--
-- NO CHECK / NO BACKFILL / NO OTHER SCHEMA CHANGE: exactly one nullable
-- TIMESTAMPTZ. office_hours and settings are NOT modified by this migration
-- (the migration-010 test byte-snapshots both columns before/after - contract
-- v1.1 F7/S13).

BEGIN;

ALTER TABLE clients
    ADD COLUMN schedule_config_updated_at TIMESTAMPTZ NULL;

COMMENT ON COLUMN clients.schedule_config_updated_at IS
    'P4-B optimistic-concurrency token for the portal recurring-schedule config '
    '(office_hours + settings.calendar.recurring); server-owned, strictly '
    'advancing on every accepted config write, NULL until the first save. Not a '
    'business value.';

COMMIT;
