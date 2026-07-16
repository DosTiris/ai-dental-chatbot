-- migrations/005_calendar_admin_credentials_down.sql
--
-- Reverses 005 ONLY: drops the calendar_admin_credentials table (its
-- indexes, constraints, and comments go with it). Touches nothing created
-- by 001, 002, 003, or 004.
--
-- DATA WARNING: dropping the table discards every stored credential HASH.
-- Nothing secret is lost — raw keys were never stored anywhere — but every
-- provisioned per-office key stops existing and would need re-provisioning
-- if 005 is later re-applied. Raw keys still configured in staff tools
-- become inert strings.
--
-- "IF EXISTS" ON PURPOSE (approved 004 convention): the DOWN direction is
-- safe-rollback code and must succeed even if 005 was never applied or was
-- already rolled back — a second run is a harmless no-op. The UP direction
-- has no such guard and fails loudly instead.
--
-- ORDER: revert (or stop) the Patch 5 application code BEFORE running this
-- script — deployed Patch 5 code queries this table on every Calendar-admin
-- request and would fail once it is gone. Reverting the code FIRST also
-- restores global-ADMIN_API_KEY access immediately, because Patch 4 code
-- never references this table.

BEGIN;

DROP TABLE IF EXISTS calendar_admin_credentials;

COMMIT;
