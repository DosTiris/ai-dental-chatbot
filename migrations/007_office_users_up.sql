-- migrations/007_office_users_up.sql
--
-- P2 (Office Portal auth foundation): the ONE application-owned mapping that
-- binds an authenticated Supabase Auth user to exactly one Mia office.
-- ADDITIVE ONLY: no existing table, column, constraint, index, or row is
-- touched. Auth identities themselves live in Supabase Auth (auth.users);
-- passwords are NEVER stored in the Mia schema.
--
-- TENANT-BINDING CONTRACT (approved P2 design):
--   * auth_user_id is the verified JWT "sub" claim (Supabase auth.users.id).
--   * uq_office_users_auth_user makes one auth user bindable to at most ONE
--     office in V1 ("one user cannot silently bind to multiple clients").
--   * client_id is deliberately NOT unique: a future second staff user for
--     the same office is an INSERT, not a redesign.
--   * NO foreign key to auth.users ON PURPOSE: that table lives in the
--     Supabase-managed auth schema, is absent from the disposable local test
--     database, and cross-schema coupling would make this migration
--     untestable offline. Existence of the auth user is the provisioning
--     runbook's responsibility (docs/PORTAL_AUTH_SETUP.md); a dangling
--     auth_user_id can never authenticate because verification requires a
--     token SIGNED for that subject.
--   * ON DELETE RESTRICT (005 convention): clients are deactivated, never
--     hard-deleted; a manual delete must first clean up bindings explicitly.
--
-- ROLE VOCABULARY (Rule 4/16 closed vocabulary): only 'office_admin' exists
-- in V1; the CHECK makes unknown roles impossible to persist.
--
-- ck_office_users_active_not_deactivated mirrors 005: an ACTIVE binding must
-- not carry a deactivation instant. One-directional on purpose: an inactive
-- binding MAY have deactivated_at NULL (temporary disable without asserting
-- a deactivation time).
--
-- NO "IF NOT EXISTS" ON PURPOSE (002..006 convention): applying this
-- migration twice must fail loudly, never half-apply silently.
--
-- ROLLOUT ORDER: apply 001 -> ... -> 006 -> 007 BEFORE deploying the P2
-- application code - the ORM model and the portal authorization owner
-- reference this table, and deploying code first would 401 every portal
-- request (fail closed, but pointlessly).

BEGIN;

CREATE TABLE office_users (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    auth_user_id   UUID NOT NULL,
    client_id      UUID NOT NULL REFERENCES clients(id) ON DELETE RESTRICT,
    role           TEXT NOT NULL DEFAULT 'office_admin',
    active         BOOLEAN NOT NULL DEFAULT true,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    deactivated_at TIMESTAMPTZ NULL,
    CONSTRAINT ck_office_users_role
        CHECK (role IN ('office_admin')),
    CONSTRAINT ck_office_users_active_not_deactivated
        CHECK (NOT (active AND deactivated_at IS NOT NULL))
);

-- The authentication lookup path: at most one binding per auth user (V1).
CREATE UNIQUE INDEX uq_office_users_auth_user
    ON office_users (auth_user_id);

-- Operator/admin listing path ("which portal users does this office have?").
CREATE INDEX ix_office_users_client_id
    ON office_users (client_id);

-- F-P2-1: this table is the authoritative auth-user -> office binding and
-- must be invisible to browser-facing database roles.
--
-- ENABLE (not FORCE) row level security with ZERO policies:
--   * Supabase Data API roles (anon, authenticated) are neither the table
--     owner nor BYPASSRLS, so with no policy they see nothing and can
--     change nothing (default deny).
--   * The Mia backend connects as the migration-running owner role, and a
--     table OWNER is exempt from non-FORCED RLS - so application SQL keeps
--     working with no policy needed. FORCE is deliberately NOT used.
--   * Local disposable test databases run as a superuser, which bypasses
--     RLS entirely - also unaffected.
ALTER TABLE office_users ENABLE ROW LEVEL SECURITY;

-- Defense in depth: strip table privileges from the browser roles so even a
-- future accidental policy cannot expose the binding. PUBLIC always exists;
-- anon/authenticated exist on Supabase but NOT on the owner-local harness,
-- so those two are revoked conditionally (the DO block keeps this one
-- migration file runnable, unchanged, in BOTH environments).
REVOKE ALL ON TABLE office_users FROM PUBLIC;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL ON TABLE office_users FROM anon;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        REVOKE ALL ON TABLE office_users FROM authenticated;
    END IF;
END $$;

COMMENT ON TABLE office_users IS
    'Office Portal tenant binding (P2). Maps one Supabase Auth user (JWT '
    'sub) to exactly one Mia client/office in V1. Stores NO passwords and '
    'NO tokens; Supabase Auth owns credentials and reset flows.';

COMMENT ON COLUMN office_users.auth_user_id IS
    'Supabase auth.users.id — the verified JWT sub claim. No FK on purpose '
    '(auth schema is provider-managed and absent from local test DBs); the '
    'provisioning runbook guarantees existence.';

COMMENT ON COLUMN office_users.deactivated_at IS
    'UTC instant the binding was deactivated. Must be NULL while '
    'active=true (enforced by CHECK). The application additionally rejects '
    'any binding with deactivated_at set, failing closed against manual '
    'corruption (005 convention).';

COMMIT;
