-- migrations/009_notification_settings_token_up.sql
--
-- P6-A (Office Portal notification destination management): the ONE
-- optimistic-concurrency token column for the portal's notification-settings
-- writer. ADDITIVE ONLY: no existing table, column, constraint, index, or row
-- is touched, and NO default is set, so every existing clients row starts with
-- a NULL token (the "office has never saved destinations from the portal"
-- state the compare-and-set treats NULL-safely).
--
-- WHY A DEDICATED TOKEN COLUMN (owner decisions D2/D3): the notification
-- DESTINATIONS keep their existing first-class source of truth
-- (clients.notification_email / clients.notification_phone) - they are NOT
-- moved into clients.settings JSONB (Option A rejected). The portal write path
-- needs a version token for the SAME race-safe compare-and-set the P3-B2 lead
-- workflow uses, and the clients table had none. This adds exactly that token
-- and nothing else.
--
-- NO CHECK CONSTRAINT ON PURPOSE (contract v1.1 C6): the "at least one
-- destination must remain configured" rule is a PORTAL-WRITE invariant only
-- (owner decision D6), enforced by the service. Encoding it as a schema CHECK
-- would (a) reject legacy rows that legitimately have BOTH destinations NULL
-- and (b) collide with the two existing send owners' "blank recipient =
-- channel off" behavior, which P6-A must not change. The token is standalone,
-- so no paired-value CHECK applies.
--
-- create_all() INTERACTION (matches the 008 rollout note): clients is mapped
-- on app.database.Base, so create_all() on a FRESH database creates this
-- column from the ORM mapping. On an EXISTING production clients table
-- create_all() cannot ALTER, so THIS migration is the sole production schema
-- authority. Rollout order: migrate FIRST, deploy the application code SECOND
-- (rollback order is the reverse - see the down file).
--
-- NO "IF NOT EXISTS" ON PURPOSE (003/004/005/006/008 convention): applying
-- this migration twice must fail loudly rather than silently no-op.

BEGIN;

ALTER TABLE clients
    ADD COLUMN notification_settings_updated_at TIMESTAMPTZ NULL;

COMMENT ON COLUMN clients.notification_settings_updated_at IS
    'P6-A optimistic-concurrency token for the portal notification-settings '
    'writer; server-owned, strictly advancing on every accepted write, NULL '
    'until the first portal save. Not a business value.';

COMMIT;
