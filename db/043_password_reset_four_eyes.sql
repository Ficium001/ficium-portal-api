-- 043_password_reset_four_eyes.sql
-- Resetting a colleague's password was one admin, no approval, and that admin
-- saw the new temporary password: account takeover by one person. Also, the
-- temporary password of an approved NEW user (provision-user) went to whoever
-- called first, including the maker.
-- Now:
--   * user.reset_password is a platform-locked four-eyes action.
--   * A temporary password is released ONCE, ONLY to the checker who approved
--     the action: governance.action.credential_released_at is claimed in the
--     same transaction that writes the password hash (API, service session).
--   * The primary admin's password cannot be reset this way (trigger).

begin;

insert into governance.four_eyes_catalog
  (category, label, screen, maker_module, locked, default_on, locked_reason, sort_order)
values ('user.reset_password', 'Reset a user''s password', 'Setup › Users', 'inst:team',
        true, true, 'A new password gives access to someone''s account; a second person always approves.', 45)
on conflict (category) do nothing;

alter table governance.action add column if not exists credential_released_at timestamptz;

create or replace function governance.execute(p_action_id uuid)
  returns void language plpgsql security definer set search_path = '' as
$$
declare
  a governance.action%rowtype;
  v_target governance.four_eyes_catalog%rowtype;
  v_field  text;
  v_n      int;
begin
  select * into a from governance.action where id = p_action_id;
  if not found then raise exception 'Action % not found', p_action_id; end if;

  if a.category = 'four_eyes.disable' then
    select * into v_target from governance.four_eyes_catalog
     where category = a.payload ->> 'category';
    if not found then raise exception 'Unknown control %', a.payload ->> 'category'; end if;
    if v_target.locked then raise exception 'Four-eyes on % is locked by the platform', v_target.category; end if;
    insert into governance.four_eyes_policy (institution_id, category, enabled, changed_by, via_action_id)
    values (a.institution_id, v_target.category, false, a.checker_id, a.id)
    on conflict (institution_id, category) do update
      set enabled = false, changed_by = excluded.changed_by,
          changed_at = now(), via_action_id = excluded.via_action_id;

  elsif a.category = 'user.update' then
    v_field := a.payload ->> 'field';
    if v_field is null or v_field not in ('full_name', 'email', 'member_role') then
      raise exception 'Invalid field %', v_field;
    end if;
    if v_field = 'member_role'
       and (a.payload ->> 'value') not in ('maker', 'checker', 'viewer', 'analyst') then
      raise exception 'Invalid role %', a.payload ->> 'value';
    end if;
    execute format('update institution.member set %I = $1, updated_at = now()
                     where id = $2 and institution_id = $3', v_field)
      using a.payload ->> 'value', (a.payload ->> 'member_id')::uuid, a.institution_id;
    get diagnostics v_n = row_count;
    if v_n <> 1 then raise exception 'Member not found'; end if;

  elsif a.category in ('user.deactivate', 'user.reactivate') then
    update institution.member
       set active = (a.category = 'user.reactivate'),
           deactivated_at = case when a.category = 'user.deactivate' then now() else null end,
           activated_at   = case when a.category = 'user.reactivate' then now() else activated_at end,
           updated_at = now()
     where id = (a.payload ->> 'member_id')::uuid and institution_id = a.institution_id;
    get diagnostics v_n = row_count;
    if v_n <> 1 then raise exception 'Member not found'; end if;

  elsif a.category = 'user.reset_password' then
    -- The password itself is set by the API when the CHECKER releases it
    -- (argon2 lives in the API). Validate now; stay execution_status=pending.
    if not exists (select 1 from institution.member m
                    where m.id = (a.payload ->> 'member_id')::uuid
                      and m.institution_id = a.institution_id
                      and m.auth_user_id is not null
                      and not m.is_primary_admin) then
      raise exception 'Member not found, not yet signed up, or primary admin';
    end if;
    return;

  elsif a.category = 'benefit.create' then
    if nullif(a.payload ->> 'cat_id', '') is null then
      raise exception 'A benefit needs a category';
    end if;
    insert into institution.benefit (
      institution_id, product_id, cat_id, title, description, value_display,
      value_amount, value_unit, is_guaranteed, conditions, valid_from, valid_until,
      is_active, created_by)
    values (
      a.institution_id,
      nullif(a.payload ->> 'product_id', '')::uuid,
      (a.payload ->> 'cat_id')::uuid,
      a.payload ->> 'title',
      nullif(a.payload ->> 'description', ''),
      nullif(a.payload ->> 'value_display', ''),
      nullif(a.payload ->> 'value_amount', '')::numeric,
      nullif(a.payload ->> 'value_unit', ''),
      coalesce((a.payload ->> 'is_guaranteed')::boolean, false),
      nullif(a.payload ->> 'conditions', ''),
      nullif(a.payload ->> 'valid_from', '')::date,
      nullif(a.payload ->> 'valid_until', '')::date,
      true,
      a.maker_id);

  elsif a.category = 'benefit.update' then
    update institution.benefit
       set product_id    = nullif(a.payload ->> 'product_id', '')::uuid,
           cat_id        = coalesce(nullif(a.payload ->> 'cat_id', '')::uuid, cat_id),
           title         = coalesce(nullif(a.payload ->> 'title', ''), title),
           description   = nullif(a.payload ->> 'description', ''),
           value_display = nullif(a.payload ->> 'value_display', ''),
           value_amount  = nullif(a.payload ->> 'value_amount', '')::numeric,
           value_unit    = nullif(a.payload ->> 'value_unit', ''),
           is_guaranteed = coalesce((a.payload ->> 'is_guaranteed')::boolean, is_guaranteed),
           conditions    = nullif(a.payload ->> 'conditions', ''),
           valid_from    = nullif(a.payload ->> 'valid_from', '')::date,
           valid_until   = nullif(a.payload ->> 'valid_until', '')::date,
           is_active     = coalesce((a.payload ->> 'is_active')::boolean, is_active),
           updated_at    = now()
     where id = (a.payload ->> 'benefit_id')::uuid and institution_id = a.institution_id;
    get diagnostics v_n = row_count;
    if v_n <> 1 then raise exception 'Benefit not found'; end if;

  elsif a.category = 'benefit.delete' then
    update institution.benefit set is_active = false, updated_at = now()
     where id = (a.payload ->> 'benefit_id')::uuid and institution_id = a.institution_id;
    get diagnostics v_n = row_count;
    if v_n <> 1 then raise exception 'Benefit not found'; end if;

  else
    perform institution._execute_action(p_action_id);
  end if;

  update governance.action
     set execution_status = 'executed', executed_at = now(), updated_at = now()
   where id = p_action_id;
end
$$;


create or replace function governance.guard_member_action()
  returns trigger language plpgsql security definer set search_path = '' as
$$
begin
  if new.category in ('user.update', 'user.deactivate', 'user.reset_password')
     and exists (select 1 from institution.member m
                  where m.id = (new.payload ->> 'member_id')::uuid
                    and m.is_primary_admin) then
    raise exception 'The primary admin cannot be edited, deactivated or reset this way';
  end if;
  return new;
end
$$;
drop trigger if exists trg_guard_member_action on governance.action;
create trigger trg_guard_member_action
  before insert on governance.action
  for each row when (new.category in ('user.update', 'user.deactivate', 'user.reset_password'))
  execute function governance.guard_member_action();

do $verify$
declare
  maker   uuid := 'bbb283cf-548d-476c-a190-9957526e2afa';
  checker uuid := '0777be2a-de18-4b82-86aa-05de958d710e';
  target  uuid := (select id from institution.member
                    where auth_user_id is not null and not is_primary_admin
                      and institution_id = 'f192050a-dfdd-4da7-874e-c3db88f11e41'
                      and auth_user_id not in ('bbb283cf-548d-476c-a190-9957526e2afa','0777be2a-de18-4b82-86aa-05de958d710e')
                    limit 1);
  prim    uuid := (select id from institution.member where auth_user_id = maker and is_primary_admin);
  a_id uuid; st text; ex text; bad text := '';
begin
  if target is null then raise notice '043: no test member, skipped'; return; end if;
  begin
    perform set_config('request.jwt.claims', json_build_object('sub', maker, 'role', 'authenticated')::text, true);
    set local role authenticated;
    a_id := governance.submit('user.reset_password', 'member', target, jsonb_build_object('member_id', target));
    if (select status from governance.action where id = a_id) <> 'pending' then bad := bad || 'reset not queued; '; end if;
    if prim is not null then
      begin
        perform governance.submit('user.reset_password', 'member', prim, jsonb_build_object('member_id', prim));
        bad := bad || 'primary admin reset queued; ';
      exception when others then null; end;
    end if;
    perform set_config('request.jwt.claims', json_build_object('sub', checker, 'role', 'authenticated')::text, true);
    perform institution.approve_action(a_id, 'ok');
    reset role;
    select status, execution_status into st, ex from governance.action where id = a_id;
    if st <> 'approved' or ex = 'executed' then bad := bad || 'reset state wrong ('||st||'/'||coalesce(ex,'-')||'); '; end if;
    raise exception using errcode = 'P0002', message = 'rollback';
  exception
    when sqlstate 'P0002' then null;
    when others then bad := bad || 'ERROR: ' || sqlerrm || '; ';
  end;
  reset role;
  if bad <> '' then raise exception '043 verification failed: %', bad; end if;
end
$verify$;

commit;
