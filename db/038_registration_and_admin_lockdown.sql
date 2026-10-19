-- 038_registration_and_admin_lockdown.sql
-- CRITICAL (found 2026-10-19). Any self-registered Supabase Auth user could:
--   * insert themselves into portal_admin.admin_users as an ACTIVE super_admin
--     (policy admin_users_insert WITH CHECK true)  -> platform admin takeover
--   * insert themselves as an admin member of ANY institution, e.g. MCB
--     (institution_members_insert_self checked only auth_user_id)
--   * create an institution pre-approved, with every module
--     (institutions_insert_self WITH CHECK true; only RETURNING was gated)
--   * forge rows in the WORM admin audit log (audit_insert WITH CHECK true)
--   * repoint a compliance doc / notification into another tenant
--     (doc_update_service USING true; mark_read WITH CHECK true)
-- Every legitimate write to these tables runs on the service session
-- (BYPASSRLS), except self-registration, which keeps working below.
-- Self-verifying: replays every attack and the real registration path,
-- aborts unless attacks fail and registration succeeds. Nothing persists.

begin;

-- 1. Platform admin tables: no client-role writes at all.
drop policy if exists admin_users_insert on portal_admin.admin_users;
drop policy if exists audit_insert      on portal_admin.admin_audit_log;
revoke insert, update, delete, truncate on portal_admin.admin_users     from authenticated, anon;
revoke insert, update, delete, truncate on portal_admin.admin_audit_log from authenticated, anon;

-- 2. anon reads nothing on identity tables (RLS already denied; remove the grant).
revoke all on institution.institution, institution.member from anon;
revoke all on portal_admin.admin_audit_log, portal_admin.admin_dual_control_actions,
              portal_admin.admin_roles, portal_admin.admin_sessions,
              portal_admin.admin_users, portal_admin.user_groups from anon;

-- 3. Self-registration: an institution can only be born unapproved, owned by the caller.
create or replace function institution.jwt_email() returns text
  language sql stable set search_path = '' as
$$ select lower(nullif(auth.jwt() ->> 'email', '')) $$;
revoke all on function institution.jwt_email() from public, anon;
grant execute on function institution.jwt_email() to authenticated, service_role;

drop policy if exists institutions_insert_self on institution.institution;
create policy institutions_insert_self on institution.institution
  for insert to authenticated
  with check (
        approved = false and approved_at is null and approved_by is null
    and onboarding_stage  = 'registered'
    and compliance_status = 'not_submitted'
    and suspended_at is null and offboarded_at is null
    and deployment_model  = 'saas'
    and modules in ('[]'::jsonb, '["marketplace"]'::jsonb)
    and institution.jwt_email() is not null
    and lower(primary_contact_email) = institution.jwt_email()
  );

-- 4. First (primary) admin of an institution the caller just registered — nothing else.
create or replace function institution.can_bootstrap_primary_admin(p_institution uuid)
  returns boolean language sql stable security definer set search_path = '' as
$$
  select exists (
    select 1 from institution.institution i
     where i.id = p_institution
       and i.approved = false
       and i.onboarding_stage = 'registered'
       and lower(i.primary_contact_email) = institution.jwt_email()
       and not exists (select 1 from institution.member m where m.institution_id = i.id)
  )
$$;
revoke all on function institution.can_bootstrap_primary_admin(uuid) from public, anon;
grant execute on function institution.can_bootstrap_primary_admin(uuid) to authenticated;

drop policy if exists institution_members_insert_self on institution.member;
create policy institution_members_insert_self on institution.member
  for insert to authenticated
  with check (
        auth_user_id = auth.uid()
    and lower(email) = institution.jwt_email()
    and is_primary_admin
    and institution.can_bootstrap_primary_admin(institution_id)
  );
-- RESTRICTIVE: also closes inserts through the permissive tenant_isolation
-- policy (members adding arbitrary users to their own bank, skipping dual control).
-- Member provisioning goes through approvals (service session) only.
drop policy if exists member_insert_bootstrap_only on institution.member;
create policy member_insert_bootstrap_only on institution.member
  as restrictive for insert to authenticated
  with check (
        auth_user_id = auth.uid()
    and is_primary_admin
    and institution.can_bootstrap_primary_admin(institution_id)
  );

-- 5. Compliance docs: status changes are admin decisions on the service session.
drop policy if exists doc_update_service on institution.doc;
revoke update on institution.doc from authenticated, anon;

-- 6. Notifications: mark-read may not move a row to another tenant.
drop policy if exists institution_members_mark_read on public.portal_notifications;
create policy institution_members_mark_read on public.portal_notifications
  for update to authenticated
  using      (institution_id in (select m.institution_id from institution.member m where m.auth_user_id = auth.uid()))
  with check (institution_id in (select m.institution_id from institution.member m where m.auth_user_id = auth.uid()));

-- 7. Verify. Attacks must fail; registration must succeed; nothing persists.
do $verify$
declare
  uid  uuid := gen_random_uuid();
  em   text := 'verify-' || uid || '@example.invalid';
  victim uuid := (select id from institution.institution where approved order by created_at limit 1);
  iid  uuid;
  bad  text := '';
begin
  perform set_config('request.jwt.claims',
    json_build_object('sub', uid, 'role', 'authenticated', 'email', em)::text, true);
  set local role authenticated;

  begin
    insert into portal_admin.admin_users(auth_user_id,email,display_name,role_slug,status,created_by)
    values (uid, em, 'x', 'super_admin', 'active', uid);
    bad := bad || 'admin_users insert; ';
  exception when insufficient_privilege then null; end;

  begin
    insert into portal_admin.admin_audit_log(actor_id,action_category,event_label,resource_type,outcome)
    values (uid,'x','x','x','success');
    bad := bad || 'audit forge; ';
  exception when insufficient_privilege then null; end;

  if victim is not null then
    begin
      insert into institution.member(institution_id,auth_user_id,email,role,is_primary_admin,active)
      values (victim, uid, em, 'admin', true, true);
      bad := bad || 'member into approved institution; ';
    exception when insufficient_privilege then null; end;
  end if;

  begin
    insert into institution.institution(name,legal_name,institution_type,country,approved,onboarding_stage,primary_contact_email)
    values ('v','v','bank','MU',true,'approved',em);
    bad := bad || 'self-approved institution; ';
  exception when insufficient_privilege then null; end;

  -- Legitimate registration, exactly as the portal sends it, then rolled back.
  begin
    insert into institution.institution(name,legal_name,institution_type,country,deployment_model,modules,
         onboarding_stage,compliance_status,approved,primary_contact_email,primary_contact_name)
    values ('v','v','bank','MU','saas','["marketplace"]','registered','not_submitted',false,em,'v')
    returning id into iid;
    insert into institution.member(institution_id,auth_user_id,email,full_name,role,is_primary_admin,active)
    values (iid, uid, em, 'v', 'admin', true, true);
    begin
      insert into institution.member(institution_id,auth_user_id,email,role,is_primary_admin,active)
      values (iid, gen_random_uuid(), em, 'admin', true, true);
      bad := bad || 'second member via bootstrap; ';
    exception when insufficient_privilege then null; end;
    raise exception using errcode = 'P0002', message = 'registration ok';
  exception
    when sqlstate 'P0002' then null;
    when others then bad := bad || 'REGISTRATION BROKEN: ' || sqlerrm || '; ';
  end;

  reset role;
  if bad <> '' then raise exception '038 verification failed: %', bad; end if;
end
$verify$;

commit;
