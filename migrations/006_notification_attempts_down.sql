-- migrations/006_notification_attempts_down.sql
--
-- Reverses 006 ONLY: drops the notification_attempts table (its indexes,
-- constraints, and comments go with it). Touches nothing created by 001,
-- 002, 003, 004, or 005.
--
-- DATA WARNING: dropping the table discards the entire attempt ledger —
-- every claim, sent, and unknown record created since 006 was applied.
-- The appointment rows' own notification projection (office_sms_sent,
-- office_email_sent, notify_error) is NOT touched and remains the
-- staff-visible record, so no data repair is needed after this rollback.
-- Duplicate suppression and the honest sending/unknown ledger states stop
-- existing until 006 is re-applied.
--
-- "IF EXISTS" ON PURPOSE (approved 004/005 convention): the DOWN
-- direction is safe-rollback code and must succeed even if 006 was never
-- applied or was already rolled back — a second run is a harmless no-op.
-- The UP direction has no such guard and fails loudly instead.
--
-- ORDER: revert (or stop) the Patch 9A application code BEFORE running
-- this script — deployed 9A code claims into and reads this table on the
-- booking-notification path and would fail once it is gone. Patch 8 code
-- never references this table, so reverting the code first restores the
-- pre-9A notification behavior immediately.

BEGIN;

DROP TABLE IF EXISTS notification_attempts;

COMMIT;
