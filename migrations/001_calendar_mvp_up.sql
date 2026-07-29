-- migrations/001_calendar_mvp_up.sql
--
-- Mia Calendar MVP — forward migration.
-- ADDITIVE ONLY: creates two new tables and adds nullable columns to
-- conversations. No existing data is modified or destroyed, so this is safe
-- to run on the live Supabase database (Rule 15: no destructive changes).
--
-- Run in the Supabase SQL editor (or psql). Rollback: 001_calendar_mvp_down.sql

BEGIN;

-- Staff-published bookable slots (controlled "Model B" calendar).
CREATE TABLE IF NOT EXISTS appointment_slots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id UUID NOT NULL REFERENCES clients(id),
    provider_name TEXT NULL,
    service_key TEXT NULL,
    start_datetime TIMESTAMPTZ NOT NULL,
    end_datetime TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL DEFAULT 'available',
    held_until TIMESTAMPTZ NULL,
    held_by_conversation_id UUID NULL,
    created_at TIMESTAMPTZ DEFAULT now(),
    -- Slots must be well-formed and use only known statuses.
    CONSTRAINT appointment_slots_time_order CHECK (end_datetime > start_datetime),
    CONSTRAINT appointment_slots_status_valid CHECK (
        status IN ('available', 'held', 'booked', 'blocked', 'cancelled')
    )
);

-- The availability query is "slots for one client in a time window".
CREATE INDEX IF NOT EXISTS idx_appointment_slots_client_start
    ON appointment_slots (client_id, start_datetime);

-- Confirmed / pending appointments created from slots.
CREATE TABLE IF NOT EXISTS appointments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id UUID NOT NULL REFERENCES clients(id),
    slot_id UUID NOT NULL REFERENCES appointment_slots(id),
    conversation_id UUID NULL REFERENCES conversations(id),
    patient_name TEXT NOT NULL,
    patient_phone TEXT NOT NULL,
    patient_email TEXT NULL,
    new_or_returning TEXT NULL,
    reason TEXT NULL,
    urgency TEXT NOT NULL DEFAULT 'routine',
    start_datetime TIMESTAMPTZ NOT NULL,
    end_datetime TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    source TEXT NOT NULL DEFAULT 'mia_widget',
    office_sms_sent BOOLEAN NOT NULL DEFAULT false,
    office_email_sent BOOLEAN NOT NULL DEFAULT false,
    patient_sms_sent BOOLEAN NOT NULL DEFAULT false,
    notify_error TEXT NULL,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now(),
    CONSTRAINT appointments_time_order CHECK (end_datetime > start_datetime),
    CONSTRAINT appointments_status_valid CHECK (
        status IN ('pending', 'confirmed', 'cancelled', 'completed', 'no_show')
    )
);

CREATE INDEX IF NOT EXISTS idx_appointments_client_start
    ON appointments (client_id, start_datetime);
CREATE INDEX IF NOT EXISTS idx_appointments_conversation
    ON appointments (conversation_id);

-- Booking dialog state on the existing conversations table.
-- Nullable + defaulted so every existing row remains valid untouched.
ALTER TABLE conversations
    ADD COLUMN IF NOT EXISTS booking_state TEXT NOT NULL DEFAULT 'none',
    ADD COLUMN IF NOT EXISTS booking_preferred_date TEXT NULL,
    ADD COLUMN IF NOT EXISTS booking_time_preference TEXT NULL,
    ADD COLUMN IF NOT EXISTS booking_offered_slot_ids JSONB NULL,
    ADD COLUMN IF NOT EXISTS booking_selected_slot_id UUID NULL;

COMMIT;
