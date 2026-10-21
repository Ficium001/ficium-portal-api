-- 042_benefits_through_four_eyes.sql
-- Benefits were only partly under four-eyes: "guaranteed" ones went through
-- governance, ordinary ones were inserted/updated/deactivated directly by the
-- API, and bank users also held direct INSERT/UPDATE on institution.benefit.
-- With the Setup > Controls switch (040) that would have shown "a second
-- person approves" while most benefit changes had no second person.
-- Now every benefit change goes through governance.submit, so the bank's
-- switch decides. The executor handles benefits itself because the legacy one
-- hard-codes is_guaranteed = true on create (it would have turned every
-- ordinary benefit into a guaranteed one).

begin;

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
revoke all on function governance.execute(uuid) from public, anon, authenticated;

drop policy if exists benefit_insert_own on institution.benefit;
drop policy if exists benefit_update_own on institution.benefit;
revoke insert, update, delete, truncate on institution.benefit from authenticated, anon;

do $verify$
declare
  maker   uuid := 'bbb283cf-548d-476c-a190-9957526e2afa';
  checker uuid := '0777be2a-de18-4b82-86aa-05de958d710e';
  a_id uuid; b_id uuid; g boolean; bad text := '';
  cat uuid := (select id from institution.benefit_cat where code = 'reward' limit 1);
begin
  if cat is null or not exists (select 1 from institution.member where auth_user_id = maker) then
    raise notice '042: test data absent, skipped'; return;
  end if;
  begin
    perform set_config('request.jwt.claims', json_build_object('sub', maker, 'role', 'authenticated')::text, true);
    set local role authenticated;
    begin
      insert into institution.benefit (institution_id, cat_id, title)
      values ((select institution_id from institution.member where auth_user_id = maker), cat, 'direct');
      bad := bad || 'direct benefit insert; ';
    exception when insufficient_privilege then null; end;

    -- ordinary benefit, four-eyes on (default): queued, then created NOT guaranteed
    a_id := governance.submit('benefit.create', 'benefit', null, jsonb_build_object(
      'cat_id', cat, 'title', '042 verify', 'value_amount', 1, 'value_unit', 'mur',
      'is_guaranteed', false));
    if (select status from governance.action where id = a_id) <> 'pending' then
      bad := bad || 'ordinary benefit skipped four-eyes; ';
    end if;
    perform set_config('request.jwt.claims', json_build_object('sub', checker, 'role', 'authenticated')::text, true);
    perform institution.approve_action(a_id, 'ok');
    reset role;
    select id, is_guaranteed into b_id, g from institution.benefit where title = '042 verify';
    if b_id is null then bad := bad || 'benefit not created; ';
    elsif g then bad := bad || 'ordinary benefit became guaranteed; '; end if;

    raise exception using errcode = 'P0002', message = 'rollback';
  exception
    when sqlstate 'P0002' then null;
    when others then bad := bad || 'ERROR: ' || sqlerrm || '; ';
  end;
  reset role;
  if bad <> '' then raise exception '042 verification failed: %', bad; end if;
end
$verify$;

commit;
