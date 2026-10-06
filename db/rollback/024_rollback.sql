-- ROLLBACK for 024_admin_consolidation_phase1 (Portal DB, egwobcajdlragubtkpqp).
-- Restores the exact pre-024 functions, policies, ledger location and staff group links.
-- Everything is read back from _rollback.snapshot WHERE migration = '024' (exact original text),
-- so this script cannot drift from what was actually replaced.
-- Run the matching code rollback FIRST (revert the Phase 2 PR): the old code reads admin.* tables,
-- which this script makes authoritative again. Safe any time before the old schemas are hard-dropped.
BEGIN;

-- 1. functions: re-create every original definition
DO $rb$
DECLARE r record;
BEGIN
  FOR r IN SELECT definition FROM _rollback.snapshot WHERE migration = '024' AND kind = 'function' ORDER BY id LOOP
    EXECUTE r.definition;
  END LOOP;
END
$rb$;

-- 2. revenue ledger back to its old schema (rows and links move with it)
ALTER TABLE portal_admin.commission_event SET SCHEMA admin;

-- 3. policies back to calling admin.*
ALTER POLICY audit_admin_select ON audit.event USING (admin.is_admin());
ALTER POLICY audit_insert ON audit.event WITH CHECK (
  (auth.role() = 'service_role'::text) OR admin.is_admin()
  OR (institution_id = ( SELECT ctx.institution_id FROM institution.current_member_ctx() ctx(member_id, institution_id, is_admin, member_role, modules))));
ALTER POLICY governance_platform_insert ON governance.action WITH CHECK ((scope = 'platform'::text) AND admin.is_admin());
ALTER POLICY governance_platform_select ON governance.action USING ((scope = 'platform'::text) AND admin.is_admin());
ALTER POLICY governance_platform_update ON governance.action USING ((scope = 'platform'::text) AND admin.has_permission('dual_control:approve'::text));

-- 4. staff group links back to what they were (the 2nd platform admin had none)
UPDATE portal_admin.admin_users p
   SET group_id = NULLIF(s.definition, 'NULL')::uuid
  FROM _rollback.snapshot s
 WHERE s.migration = '024' AND s.kind = 'row_group_id'
   AND s.object = 'portal_admin.admin_users.' || p.id::text;

-- 5. helper that did not exist before
DROP FUNCTION IF EXISTS portal_admin.get_user_display_name(uuid);

COMMIT;
-- Verify: SELECT pg_get_functiondef('portal_admin.is_admin()'::regprocedure);  -- should call admin.is_admin()
--         SELECT to_regclass('admin.commission_event');                         -- should resolve
