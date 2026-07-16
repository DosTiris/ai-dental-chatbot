-- migrations/001_calendar_mvp_down.sql
--
-- Mia Calendar MVP — rollback migration.
--
-- WARNING (Rule 15 backup plan): dropping the tables destroys any slots and
-- appointments created since the up-migration. Before running this, export
-- them:
--   COPY appointments TO STDOUT WITH CSV HEADER;        -- via psql \copy
--   COPY appointment_slots TO STDOUT WITH CSV HEADER;
-- or use the Supabase table export UI.
--
-- The conversations columns are dropped last; they contain only transient
-- dialog state and are safe to lose.

BEGIN;

DROP TABLE IF EXISTS appointments;
DROP TABLE IF EXISTS appointment_slots;

ALTER TABLE conversations
    DROP COLUMN IF EXISTS booking_state,
    DROP COLUMN IF EXISTS booking_preferred_date,
    DROP COLUMN IF EXISTS booking_time_preference,
    DROP COLUMN IF EXISTS booking_offered_slot_ids,
    DROP COLUMN IF EXISTS booking_selected_slot_id;

COMMIT;
