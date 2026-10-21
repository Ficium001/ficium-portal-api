-- 044_controls_approved_by_admins_only.sql
-- 040 let any member other than the maker approve "switch off four-eyes",
-- e.g. an analyst. Weakening a control must be approved by an institution
-- admin, like requesting it. (API also maps four_eyes.* to inst:settings so
-- the request only appears in admins' queues.)

begin;

create or replace function institution.approve_action(p_action_id uuid, p_note text default '')
  returns void language plpgsql security definer set search_path = '' as
$$
declare
  v_action  governance.action%rowtype;
  v_inst_id uuid;
  v_role    text;
begin
  v_inst_id := institution.get_my_institution_id();

  select * into v_action from governance.action
   where id = p_action_id and institution_id = v_inst_id and scope = 'institution';
  if not found then raise exception 'Action not found'; end if;
  if v_action.status <> 'pending' then
    raise exception 'Action is not pending: %', v_action.status;
  end if;
  if v_action.maker_id = institution.get_my_member_id() then
    raise exception 'Maker cannot approve own action (4-eyes control)';
  end if;
  if v_action.category like 'four_eyes.%'
     and not coalesce((select ctx.is_admin from institution.current_member_ctx() ctx), false) then
    raise exception 'Only an institution admin can approve a change to controls';
  end if;
  if v_action.expires_at < now() then
    update governance.action set status = 'expired', updated_at = now() where id = p_action_id;
    raise exception 'Action has expired';
  end if;

  select role::text into v_role from institution.member
   where auth_user_id = auth.uid() and institution_id = v_inst_id and active = true;

  update governance.action
     set status = 'approved', checker_id = institution.get_my_member_id(),
         checker_role = coalesce(v_role, 'admin'), checker_note = p_note,
         checked_at = now(), updated_at = now()
   where id = p_action_id;

  perform governance.execute(p_action_id);

  insert into audit.event (actor_id, actor_type, actor_role, institution_id, action,
    resource_type, resource_id, resource_label, outcome, outcome_note,
    governance_action_id, metadata)
  values (auth.uid(), 'member', coalesce(v_role, 'admin'), v_inst_id,
    v_action.category || '.approved', v_action.resource_type, v_action.resource_id,
    v_action.resource_label, 'success', p_note, p_action_id,
    jsonb_build_object('maker_id', v_action.maker_id, 'checker_id', institution.get_my_member_id()));
end
$$;


do $verify$
declare
  maker  uuid := 'bbb283cf-548d-476c-a190-9957526e2afa';  -- MCB admin
  nonadm uuid;
  cand   record;
  a_id uuid; bad text := ''; adm boolean;
begin
  -- find an active MCB member who is NOT an admin
  for cand in select auth_user_id from institution.member
               where institution_id = 'f192050a-dfdd-4da7-874e-c3db88f11e41'
                 and active and auth_user_id is not null and auth_user_id <> maker loop
    perform set_config('request.jwt.claims', json_build_object('sub', cand.auth_user_id)::text, true);
    select ctx.is_admin into adm from institution.current_member_ctx() ctx;
    if not coalesce(adm, true) then nonadm := cand.auth_user_id; exit; end if;
  end loop;
  if nonadm is null then raise notice '044: no non-admin member to test with, skipped'; return; end if;
  begin
    perform set_config('request.jwt.claims', json_build_object('sub', maker, 'role', 'authenticated')::text, true);
    set local role authenticated;
    a_id := governance.submit('four_eyes.disable', 'four_eyes_policy', null, '{"category":"benefit.create"}');
    perform set_config('request.jwt.claims', json_build_object('sub', nonadm, 'role', 'authenticated')::text, true);
    begin
      perform institution.approve_action(a_id, 'ok');
      bad := bad || 'non-admin approved a switch-off; ';
    exception when others then null; end;
    raise exception using errcode = 'P0002', message = 'rollback';
  exception
    when sqlstate 'P0002' then null;
    when others then bad := bad || 'ERROR: ' || sqlerrm || '; ';
  end;
  reset role;
  if bad <> '' then raise exception '044 verification failed: %', bad; end if;
end
$verify$;

commit;
