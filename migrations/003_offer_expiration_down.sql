-- migrations/003_offer_expiration_down.sql
--
-- Rollback for 003_offer_expiration_up.sql. Drops exactly the two Patch 2C
-- offer-state columns; no other column, index, or row is touched.
-- Migrations 001 and 002 are unaffected in both directions.
--
-- IF EXISTS is intentional here (unlike the up migration): rollback must be
-- safe to run whether or not the forward migration fully applied.
--
-- Data note: values in these columns are discarded. Pre-2C code never reads
-- them, so a code rollback is safe with or without running this script;
-- conversations mid-offer simply lose the expiry bound they also lacked
-- before Patch 2C.

BEGIN;

ALTER TABLE conversations
    DROP COLUMN IF EXISTS booking_offer_expires_at,
    DROP COLUMN IF EXISTS booking_effective_time_preference;

COMMIT;
