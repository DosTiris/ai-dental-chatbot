-- migrations/003_offer_expiration_up.sql
--
-- Mia Calendar — Patch 2C (stale offered-slot revalidation), forward
-- migration. Adds explicit offer-state storage to conversations
-- (Senior Audit Critical #8: a stale offered slot could bypass current
-- booking-policy rules; the pre-hold offer previously had NO lifetime).
--
--   booking_offer_expires_at (timestamptz, NULL)
--     Deadline for the PRE-HOLD offer only. Set when Mia displays slots
--     (now + BOOKING_OFFER_TTL_MINUTES, owned by booking_conversation.py).
--     Cleared when a hold succeeds — from that point the slot row's
--     held_until is the only active expiration authority.
--     Boundary contract: now < expires -> usable; now >= expires -> expired;
--     NULL while offered slot IDs exist -> treated as expired (safe).
--
--   booking_effective_time_preference (varchar, NULL)
--     The time preference the offer was ACTUALLY filtered with — "any" when
--     the offer was relaxed after the stored preference had no matches.
--     Survives through WAITING_FOR_CONFIRMATION so finalization revalidates
--     against what was truly offered; cleared after successful booking,
--     cancellation/reset, abandonment, or replacement by a new offer.
--
-- DELIBERATELY NO "IF NOT EXISTS": numbered migrations must fail loudly on
-- drift or double application (Rule 15).
-- ADDITIVE ONLY: both columns nullable, no backfill, no row is modified.
-- Rollback: 003_offer_expiration_down.sql.
-- Depends on: 001 (conversations booking columns). 001 and 002 unchanged.

BEGIN;

ALTER TABLE conversations
    ADD COLUMN booking_offer_expires_at timestamptz NULL,
    ADD COLUMN booking_effective_time_preference varchar NULL;

COMMIT;
