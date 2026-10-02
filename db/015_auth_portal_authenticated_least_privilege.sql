-- APPLIED to the Portal DB (egwobcajdlragubtkpqp) on 2026-10-02 as migration
-- auth_portal_authenticated_least_privilege_2026_10_02. Follows 013/014 and the portal-api change that stopped
-- every tenant-session write to auth_portal (members.py / approvals.py).
--
-- The signed-in role (`authenticated`) had full SIUD on every auth_portal table behind RLS-with-no-policies. Now:
-- no write privilege anywhere in the schema, and the ONLY readable thing is auth_users(id, is_active), because
-- list_members / get_member LEFT JOIN auth_users on exactly those two columns. No password_hash, mfa_secret,
-- session tokens, reset tokens or backup codes are reachable.
--
-- VERIFIED after the portal-api fix was deployed (rolled back): the member-list join works as a tenant session;
-- password_hash, mfa_secret, auth_sessions, UPDATE, INSERT and DELETE are all refused (42501); a tenant session
-- deactivating a member + changing email still reaches the login row through the 013/014 trigger.

REVOKE ALL ON ALL TABLES    IN SCHEMA auth_portal FROM authenticated;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA auth_portal FROM authenticated;
GRANT SELECT (id, is_active) ON auth_portal.auth_users TO authenticated;
