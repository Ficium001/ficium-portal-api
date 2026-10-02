-- APPLIED to the Portal DB (egwobcajdlragubtkpqp) on 2026-10-02 as migration
-- security_lockdown_exposed_functions_2026_10_02.
--
-- PROBLEM: 53 SECURITY DEFINER functions in the four schemas PostgREST exposes
-- (public, institution, portal_admin, auth_portal) were executable by `anon`, i.e. by
-- anyone holding the public key that ships in the SPA bundle. Confirmed live before
-- this change: POST /rest/v1/rpc/get_admin_metrics with that key returned 200 and
-- real admin counts. With email signup open + auto-confirm, "authenticated" was
-- effectively public too, so unused admin/maintenance functions are locked from it as well.
--
-- ANALYSIS (before changing anything):
--   * code search of ficium-portal, ficium-portal-api, ficium-auth (incl. workflows/scripts)
--   * Supabase API logs (24 h window): only the audit's own probes called /rest/v1/rpc
--   * DB-internal: RLS policies, other functions, triggers, views, column defaults,
--     check constraints, indexes, event triggers; all 54 functions owned by postgres
--   * ficium-auth calls auth_portal.get_member_module_permissions with the SERVICE key
--     (role service_role), so it is granted explicitly below
--
-- A. 31 functions: service_role + postgres only (no signed-in user needs them).
-- B. 22 functions: authenticated + service_role + postgres (RLS policies, portal-api tenant
--    sessions that SET LOCAL ROLE authenticated, and the SPA's get_user_groups/get_my_group).
-- anon: nothing. Trigger / event-trigger functions fire regardless of EXECUTE.
--
-- The migration aborts (rolls back) unless exactly 31 + 22 functions are found and no
-- SECURITY DEFINER function in an exposed schema remains executable by anon.
--
-- VERIFIED AFTER APPLY:
--   * privilege matrix: 31 x (service,postgres), 22 x (authenticated,service,postgres), 0 x anon
--   * live role tests: anon denied on 5/5 probes; logged-in user allowed on 7/7 kept
--     functions and denied on get_admin_metrics; service_role still runs get_member_module_permissions
--   * sweep of all 93 tables/views readable by `authenticated`, under real member claims:
--     93/93 ok before and 93/93 ok after
--   * over HTTPS with the public key: get_admin_metrics 200 -> 401 (42501); get_member_module_permissions
--     -> 401; admin_approve_dual_control -> 401; approvals_cast / esign_sign -> 404 (hidden)
--
-- ROLLBACK for one function:  GRANT EXECUTE ON FUNCTION <sig> TO authenticated;
-- FUTURE: new functions in these schemas still default to PUBLIC execute. Revoke explicitly
-- in every new migration (see db/README.md checklist), or flip default privileges.

DO $$
DECLARE
  r record;
  n_locked int := 0;
  n_kept   int := 0;
BEGIN
  FOR r IN
    SELECT p.oid::regprocedure AS sig
    FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE p.prosecdef AND (n.nspname, p.proname) IN (VALUES
      ('institution','_execute_action'), ('institution','approvals_advance'), ('institution','approvals_evaluate_stage'),
      ('institution','approvals_expire_overdue'), ('institution','approvals_seed_defaults'), ('institution','esign_expire_overdue'),
      ('institution','get_my_institution_id'), ('institution','get_my_member_id'), ('institution','has_module'),
      ('institution','has_role'), ('institution','is_active'), ('institution','is_ficium_admin'),
      ('institution','assign_default_member_group'), ('institution','enforce_member_group_tenant'),
      ('portal_admin','_execute_dual_control_action'), ('portal_admin','expire_dual_control_actions'),
      ('portal_admin','get_institutions'), ('portal_admin','my_permissions'), ('portal_admin','my_role_slug'),
      ('portal_admin','update_group_modules'), ('portal_admin','admin_submit_dual_control'),
      ('public','detect_portal_user_type'), ('public','get_admin_audit'), ('public','get_admin_dual_control'),
      ('public','get_admin_me'), ('public','get_admin_metrics'), ('public','get_admin_roles'),
      ('public','get_admin_sessions'), ('public','get_admin_users'), ('public','rls_auto_enable'),
      ('auth_portal','get_member_module_permissions'))
  LOOP
    EXECUTE format('REVOKE EXECUTE ON FUNCTION %s FROM PUBLIC, anon, authenticated', r.sig);
    EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO service_role, postgres', r.sig);
    n_locked := n_locked + 1;
  END LOOP;

  FOR r IN
    SELECT p.oid::regprocedure AS sig
    FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE p.prosecdef AND (n.nspname, p.proname) IN (VALUES
      ('institution','approvals_cast'), ('institution','approvals_is_eligible'), ('institution','approvals_route'),
      ('institution','approvals_withdraw'), ('institution','approve_action'), ('institution','reject_action'),
      ('institution','submit_for_approval'), ('institution','current_member_ctx'), ('institution','current_member_ctx_v2'),
      ('institution','esign_append_event'), ('institution','esign_create_envelope'), ('institution','esign_decline'),
      ('institution','esign_sign'), ('institution','get_institution_bid_request_ids'), ('institution','get_my_modules'),
      ('institution','get_my_products'),
      ('portal_admin','admin_approve_dual_control'), ('portal_admin','admin_reject_dual_control'),
      ('portal_admin','get_user_groups'), ('portal_admin','get_my_group'), ('portal_admin','has_permission'),
      ('portal_admin','is_admin'))
  LOOP
    EXECUTE format('REVOKE EXECUTE ON FUNCTION %s FROM PUBLIC, anon', r.sig);
    EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO authenticated, service_role, postgres', r.sig);
    n_kept := n_kept + 1;
  END LOOP;

  IF n_locked <> 31 OR n_kept <> 22 THEN
    RAISE EXCEPTION 'unexpected function counts: locked %, kept % (expected 31 and 22)', n_locked, n_kept;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
             WHERE p.prosecdef AND n.nspname IN ('public','institution','portal_admin','auth_portal')
               AND has_function_privilege('anon', p.oid, 'EXECUTE')) THEN
    RAISE EXCEPTION 'anon can still execute a SECURITY DEFINER function in an exposed schema';
  END IF;
END $$;
