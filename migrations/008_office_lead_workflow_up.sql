-- migrations/008_office_lead_workflow_up.sql
--
-- P3-B2-S1 (office workflow schema foundation): the office-OWNED workflow
-- columns for leads, kept STRICTLY SEPARATE from Mia's system-owned
-- Conversation.lead_status. ADDITIVE ONLY: no existing table, column,
-- constraint, index, or row is touched, and NO default makes an existing
-- row appear office-modified (every new value starts NULL on every
-- existing row; the CHECKs below are trivially satisfied by NULLs, so
-- adding them rescans but can never fail on legacy data).
--
-- WHY A SEPARATE FIELD (approved Outcome C, recon against parent
-- 34dcd1a685ff0a0e5b3fac7aa6800d72969a36bf):
--   * lead_status is written by Mia's intake flow ("new" at creation,
--     "completed" by mark_lead_completed_silently in app/routes/chat.py),
--     and every routing gate tests it against "completed" only. Recon
--     proved a manual value stored there can be silently rewritten to
--     "completed" on later patient activity and makes a completed intake
--     look incomplete to routing. Office workflow therefore lives HERE,
--     and Mia never reads or writes these columns.
--   * COMPATIBILITY DEPENDENCY (documented follow-up, NOT changed by 008):
--     the legacy operator endpoint POST /admin/leads/status still writes
--     'new'/'contacted'/'booked'/'closed' into lead_status. 008 does not
--     touch or break that endpoint. A separate approved patch must later
--     move operator manual workflow writes onto office_status - without
--     letting the operator modify Mia's system intake state - before the
--     office-workflow transition is considered complete.
--
-- VOCABULARY (Rule 4/16 closed vocabulary): office_status is one of
-- 'contacted', 'booked', 'closed'; NULL = no current office workflow
-- status. 'new' is deliberately NOT an office value - clearing
-- office_status back to NULL replaces that concept, so the word "new"
-- keeps exactly one meaning (intake not finished) in lead_status.
--
-- TIMESTAMP CONTRACT (approved): the *_updated_at columns are server-owned
-- concurrency/version tokens, not decorative metadata. The later
-- application layer must ADVANCE the matching token on EVERY mutation -
-- including a clear back to NULL - and must never reset a token to NULL:
--     value NULL     + token NULL     = never office-modified;
--     value NULL     + token non-NULL = previously modified, now cleared;
--     value non-NULL + token non-NULL = current office state.
-- The two token CHECKs are therefore ONE-DIRECTIONAL on purpose: a present
-- value requires its token, but a NULL value may keep one.
--
-- NOTE SHAPE: ONE current plain-text note per lead in V1 (no history
-- table: the portal must never return an unbounded per-lead history -
-- audit finding A2's rule). A present note is trimmed-non-empty and at
-- most 2000 characters; the application layer must validate the same
-- rules independently before writing (defense in depth, 005/007
-- convention of database CHECK plus application refusal).
--
-- BACKFILL: NONE, on purpose. Legacy operator-written lead_status values
-- ('contacted'/'booked'/'closed') stay exactly as they are. Before any
-- eventual production migration, the activation runbook requires a
-- read-only production inventory of lead_status values; if any
-- contacted/booked/closed rows exist, STOP for an explicit owner decision.
-- No migration may rewrite them silently.
--
-- NO "IF NOT EXISTS" ON PURPOSE (002..007 convention): applying this
-- migration twice must fail loudly (duplicate column), never half-apply
-- silently.
--
-- ROLLOUT ORDER (mirrors 007): apply 008 BEFORE deploying application
-- code that maps these columns - the ORM names every mapped column in its
-- SELECTs, so deploying code first would break every Conversation query.
-- ROLLBACK ORDER is the reverse: roll the application code back FIRST,
-- only then consider 008's down migration.

BEGIN;

ALTER TABLE conversations
    ADD COLUMN office_status TEXT NULL,
    ADD COLUMN office_status_updated_at TIMESTAMPTZ NULL,
    ADD COLUMN office_note TEXT NULL,
    ADD COLUMN office_note_updated_at TIMESTAMPTZ NULL;

-- Closed office vocabulary; NULL allowed (= no current office status).
ALTER TABLE conversations
    ADD CONSTRAINT ck_conversations_office_status_vocab
    CHECK (office_status IS NULL
           OR office_status IN ('contacted', 'booked', 'closed'));

-- A present status must carry its version token (one-directional - see
-- the timestamp contract above: a cleared status keeps its last token).
ALTER TABLE conversations
    ADD CONSTRAINT ck_conversations_office_status_has_ts
    CHECK (office_status IS NULL OR office_status_updated_at IS NOT NULL);

-- A present note is trimmed-non-empty and bounded (at most 2000 chars).
ALTER TABLE conversations
    ADD CONSTRAINT ck_conversations_office_note_shape
    CHECK (office_note IS NULL
           OR (btrim(office_note) <> ''
               AND char_length(office_note) <= 2000));

-- A present note must carry its version token (one-directional, as above).
ALTER TABLE conversations
    ADD CONSTRAINT ck_conversations_office_note_has_ts
    CHECK (office_note IS NULL OR office_note_updated_at IS NOT NULL);

COMMENT ON COLUMN conversations.office_status IS
    'Office-owned workflow status (P3-B2-S1). Closed vocabulary contacted/'
    'booked/closed; NULL = no current office status. Mia never reads or '
    'writes this column; system intake state stays in lead_status.';

COMMENT ON COLUMN conversations.office_status_updated_at IS
    'Server-owned concurrency/version token for office_status. Advanced on '
    'every mutation including a clear back to NULL; never reset to NULL '
    '(CHECK is one-directional on purpose).';

COMMENT ON COLUMN conversations.office_note IS
    'One current office-entered plain-text note per lead (V1). Trimmed, '
    'non-empty when present, at most 2000 characters. Mia never reads or '
    'writes this column.';

COMMENT ON COLUMN conversations.office_note_updated_at IS
    'Server-owned concurrency/version token for office_note. Advanced on '
    'every mutation including a clear back to NULL; never reset to NULL '
    '(CHECK is one-directional on purpose).';

COMMIT;
