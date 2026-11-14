-- 062_security_advisor_hardening.sql
-- Supabase security advisor findings on the Portal DB (2026-11-14).
--
-- 1. 32 functions had no fixed search_path, among them SECURITY DEFINER functions that run with
--    elevated rights: institution._execute_action (executes every approved four-eyes action),
--    institution.submit_for_approval, the portal_admin dual-control functions and audit.log.
--    A definer function that resolves names through the caller's search_path can be redirected
--    to objects the caller controls. The value set here is the database default made explicit
--    (pg_catalog first, then public and extensions, pg_temp last), so resolution is unchanged.
-- 2. marketplace.bid_benefit had an INSERT policy WITH CHECK (true) for every role. No role has an
--    INSERT grant, so it was not exploitable; it would have become so the day someone added one.
--    Benefits are written only by the snapshot trigger inside the governance executor (table owner).

begin;

do $$
declare f record;
begin
  for f in
    select p.oid::regprocedure as sig
      from pg_proc p join pg_namespace n on n.oid = p.pronamespace
     where (n.nspname, p.proname) in (
             ('portal_admin','check_failed_logins'), ('marketplace','guard_bid_window'),
             ('auth_portal','set_updated_at'), ('institution','set_updated_at'),
             ('portal_admin','set_updated_at'), ('institution','doc_template_set_updated_at'),
             ('portal_admin','admin_reject_dual_control'),
             ('_retired_workflow_20261006','_caller_institution_id'),
             ('_retired_workflow_20261006','_is_admin'), ('portal_admin','get_institutions'),
             ('_retired_workflow_20261006','_set_updated_at'), ('public','set_updated_at'),
             ('institution','current_institution_id'), ('institution','_chain_audit_events'),
             ('portal_admin','_execute_dual_control_action'), ('marketplace','block_bid_event_mutation'),
             ('portal_admin','update_group_modules'), ('portal_admin','expire_dual_control_actions'),
             ('portal_admin','my_permissions'), ('admin','current_user_id'),
             ('admin','check_failed_logins'), ('portal_admin','admin_submit_dual_control'),
             ('audit','block_mutation'), ('portal_admin','my_role_slug'), ('audit','log'),
             ('institution','_execute_action'), ('portal_admin','get_user_groups'),
             ('portal_admin','admin_approve_dual_control'), ('institution','submit_for_approval'),
             ('autobid','rule_version_freeze'), ('marketplace','snapshot_bid_benefits'),
             ('public','detect_portal_user_type'))
       and p.proconfig is null
  loop
    execute format('alter function %s set search_path = pg_catalog, public, extensions, pg_temp', f.sig);
  end loop;
end $$;

alter policy bid_benefit_insert_trigger on marketplace.bid_benefit to postgres;

commit;
