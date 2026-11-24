-- =============================================================================
-- 066 — Least-privilege cleanup from the 2026-11-24 database audit.
--
-- Three leftovers that nothing uses, closed so a later mistake cannot open them:
--
--   1. integration.admin_command_log had RLS off. Only postgres holds grants on
--      it and the integration schema is not usable by API roles, so it was not
--      reachable; RLS is enabled (not forced) to match every other table.
--   2. _retired_workflow_20261006 tables still granted SELECT/INSERT/UPDATE/
--      DELETE to authenticated. The schema itself is not usable by authenticated,
--      so the grants were dead; they are revoked ahead of dropping the schema.
--   3. authenticated had USAGE on auth_portal (ficium-auth's tables: users,
--      sessions, MFA backup codes, reset tokens). No auth_portal table is
--      granted to authenticated, so tenant sessions could not read it anyway;
--      ficium-auth and the API's service_session() do not run as
--      authenticated. Revoked: the identity store is not part of the tenant
--      surface.
-- =============================================================================

alter table integration.admin_command_log enable row level security;

revoke all on all tables in schema _retired_workflow_20261006 from authenticated;

revoke usage on schema auth_portal from authenticated;
