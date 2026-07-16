-- migrations/005_calendar_admin_credentials_up.sql
--
-- PATCH 5 (Senior Audit Critical #2): per-office Calendar admin credentials.
--
-- Creates the ONE table that replaces the shared global ADMIN_API_KEY for
-- every Calendar-admin route. ADDITIVE ONLY: no existing table, column,
-- constraint, index, or row is touched. The non-calendar /admin routes keep
-- using the global key and do not read this table.
--
-- SECRET HANDLING (approved):
--   * key_hash stores ONLY the SHA-256 digest (64 lowercase hex chars) of a
--     raw key of the form  mia_cal_ + secrets.token_urlsafe(32).
--   * Raw keys are NEVER stored, never appear in this file, in logs, in
--     CHANGE_REPORT.md, or in source control. Provisioning procedure:
--     docs/INTEGRATION.md §8.
--
-- CONSTRAINT DECISIONS (approved):
--   * VARCHAR(64), not CHAR(64): PostgreSQL CHAR is blank-padded bpchar with
--     surprising trailing-space comparison and cast semantics; VARCHAR
--     stores exactly the 64 digest characters.
--   * ck_cal_admin_cred_key_hash_hex: its real job is catching the one
--     dangerous operator mistake — inserting the RAW key instead of the
--     hash. A raw key contains '_' and is 51 characters, so the CHECK
--     rejects it loudly instead of silently persisting a secret (Rule 16).
--   * ck_cal_admin_cred_active_not_revoked: an ACTIVE credential must not
--     carry a revocation instant. One-directional on purpose: an inactive
--     credential MAY have revoked_at NULL (temporary disable without
--     asserting a revocation time).
--   * ON DELETE RESTRICT: the application never hard-deletes clients (it
--     uses the active flag); if someone attempts a manual delete, RESTRICT
--     forces explicit credential cleanup first instead of a hidden cascade
--     (Rule 4) and preserves the audit trail.
--   * Multiple credentials per client are allowed BY DESIGN — rotation
--     provisions the new key while the old one still works.
--
-- gen_random_uuid() is built into PostgreSQL 13+ (local PG16 container and
-- Supabase both qualify); a database-side default matters here because
-- provisioning inserts are raw operator SQL, not ORM inserts.
--
-- NO "IF NOT EXISTS" ON PURPOSE (002/003/004 convention): applying this
-- migration twice must fail loudly, never half-apply silently.
--
-- ROLLOUT ORDER: apply 001 -> 002 -> 003 -> 004 -> 005, and provision
-- credentials (docs/INTEGRATION.md §8), all BEFORE deploying the Patch 5
-- application code — the ORM model and the authorization owner reference
-- this table, and deploying code first would lock staff out.

BEGIN;

CREATE TABLE calendar_admin_credentials (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id   UUID NOT NULL REFERENCES clients(id) ON DELETE RESTRICT,
    key_hash    VARCHAR(64) NOT NULL,
    label       TEXT NOT NULL,
    active      BOOLEAN NOT NULL DEFAULT true,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at  TIMESTAMPTZ NULL,
    CONSTRAINT ck_cal_admin_cred_key_hash_hex
        CHECK (key_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_cal_admin_cred_active_not_revoked
        CHECK (NOT (active AND revoked_at IS NOT NULL))
);

-- The authentication lookup path: exactly one credential per digest.
CREATE UNIQUE INDEX uq_cal_admin_cred_key_hash
    ON calendar_admin_credentials (key_hash);

-- Operator/admin listing path ("which credentials does this office have?").
CREATE INDEX ix_cal_admin_cred_client_id
    ON calendar_admin_credentials (client_id);

COMMENT ON TABLE calendar_admin_credentials IS
    'Per-office Calendar admin API credentials (Patch 5). Replaces the '
    'global ADMIN_API_KEY for /admin/calendar/* only. Stores SHA-256 '
    'digests; raw keys are never persisted.';

COMMENT ON COLUMN calendar_admin_credentials.key_hash IS
    'SHA-256 of the raw key (mia_cal_ + token), as 64 lowercase hex chars. '
    'NEVER the raw key itself — the hex CHECK rejects raw-key-shaped values.';

COMMENT ON COLUMN calendar_admin_credentials.revoked_at IS
    'UTC instant the credential was revoked. Must be NULL while active=true '
    '(enforced by CHECK). The application additionally rejects any credential '
    'with revoked_at set, failing closed against manual corruption.';

COMMIT;
