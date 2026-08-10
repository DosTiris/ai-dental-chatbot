-- migrations/007_office_users_down.sql
--
-- P2 rollback: removes the office_users tenant-binding table.
--
-- WARNING (Rule 15 backup plan): dropping the table destroys every portal
-- user->office binding created since the up-migration. Export first:
--   COPY office_users TO STDOUT WITH CSV HEADER;   -- via psql \copy
-- Supabase Auth users themselves are NOT touched by this rollback; after a
-- rollback they simply cannot resolve to any office (every portal request
-- fails closed with 401).
--
-- IF EXISTS so the rollback is safe to run even when 007 was never applied
-- (001 down-migration convention for drops).

BEGIN;

DROP TABLE IF EXISTS office_users;

COMMIT;
