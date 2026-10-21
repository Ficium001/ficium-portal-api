-- 040_four_eyes_switch.sql
-- Four-eyes (maker-checker) becomes switchable per action, per institution,
-- from Setup > Controls, without becoming silently weakenable:
--   * governance.four_eyes_catalog  every action type, the screen it lives on,
--     its default, and whether the platform LOCKS it on (cannot be switched off)
--   * governance.four_eyes_policy   one on/off row per institution per action
--   * governance.submit             the single choke point: four-eyes on -> queue
--     for a checker (unchanged); off -> execute now, recorded as control='single'
--     and audited. Single-control execution also checks the maker's module.
--   * Switching OFF is itself a locked four-eyes action (four_eyes.disable);
--     switching ON is immediate. Both audited; admins only.
--   * Unknown categories are treated as locked on (fail closed).
-- Closes, found while building this (2026-10-19):
--   * bank users could INSERT marketplace.bid / bid_allocation directly via the
--     Supabase API, skipping four-eyes; bids now only come from the executor.
--   * bank users could INSERT/UPDATE governance.action directly, i.e. forge an
--     "approved" record with any checker. Now: no inserts; updates limited to
--     the execution columns the post-approval API step writes.
--   * user.update / user.deactivate / user.reactivate had NO database executor:
--     approving them raised "No executor for category", and the browser was
--     meant to apply the change in a second call afterwards. Approval and
--     execution are now one server-side transaction; the direct
--     PATCH/deactivate/reactivate endpoints submit for approval instead.
-- Self-verifying against live data; the checks roll back.

begin;

-- 1. Catalog ---------------------------------------------------------------
create table if not exists governance.four_eyes_catalog (
  category     text primary key check (category ~ '^[a-z_]+\.[a-z_]+$'),
  label        text not null,
  screen       text not null,
  maker_module text,                       -- null = institution admins only
  locked       boolean not null default false,
  default_on   boolean not null default true,
  locked_reason text,
  sort_order   int not null default 100,
  check (not locked or (default_on and locked_reason is not null))
);

insert into governance.four_eyes_catalog
  (category, label, screen, maker_module, locked, default_on, locked_reason, sort_order) values
  ('bid.submit',           'Submit a bid',              'Marketplace',      'inst:marketplace', true,  true, 'A bid is a binding price offer to a customer.', 10),
  ('bid.withdraw',         'Withdraw a bid',            'Bids',             'inst:bids',        false, true, null, 20),
  ('benefit.create',       'Add a benefit',             'Setup › Products', 'inst:benefits',    false, true, null, 30),
  ('benefit.update',       'Change a benefit',          'Setup › Products', 'inst:benefits',    false, true, null, 31),
  ('benefit.delete',       'Remove a benefit',          'Setup › Products', 'inst:benefits',    false, true, null, 32),
  ('user.create',          'Add a user',                'Setup › Users',    'inst:team',        true,  true, 'Who can access the bank''s portal always needs a second person.', 40),
  ('user.assign_group',    'Change a user''s group',    'Setup › Users',    'inst:team',        true,  true, 'Changing someone''s access always needs a second person.', 41),
  ('user.update',          'Edit a user',               'Setup › Users',    'inst:team',        true,  true, 'Changing a user''s identity or role always needs a second person.', 42),
  ('user.deactivate',      'Deactivate a user',         'Setup › Users',    'inst:team',        true,  true, 'Removing someone''s access always needs a second person.', 43),
  ('user.reactivate',      'Reactivate a user',         'Setup › Users',    'inst:team',        true,  true, 'Restoring someone''s access always needs a second person.', 44),
  ('group.create',         'Create a group',            'Setup › Users',    'inst:team',        true,  true, 'Groups grant access; changes always need a second person.', 50),
  ('group.update_modules', 'Change a group''s access',  'Setup › Users',    'inst:team',        true,  true, 'Groups grant access; changes always need a second person.', 51),
  ('group.delete',         'Delete a group',            'Setup › Users',    'inst:team',        true,  true, 'Groups grant access; changes always need a second person.', 52),
  ('four_eyes.disable',    'Switch off four-eyes',      'Setup › Controls', null,               true,  true, 'Weakening a control always needs a second person.', 90)
on conflict (category) do nothing;

alter table governance.four_eyes_catalog enable row level security;
drop policy if exists four_eyes_catalog_read on governance.four_eyes_catalog;
create policy four_eyes_catalog_read on governance.four_eyes_catalog for select to authenticated using (true);
revoke all on governance.four_eyes_catalog from public, anon, authenticated;
grant select on governance.four_eyes_catalog to authenticated;

-- 2. Per-institution policy -----------------------------------------------
create table if not exists governance.four_eyes_policy (
  institution_id uuid not null references institution.institution(id) on delete cascade,
  category       text not null references governance.four_eyes_catalog(category),
  enabled        boolean not null,
  changed_by     uuid,
  changed_at     timestamptz not null default now(),
  via_action_id  uuid references governance.action(id),
  primary key (institution_id, category)
);
alter table governance.four_eyes_policy enable row level security;
drop policy if exists four_eyes_policy_read on governance.four_eyes_policy;
create policy four_eyes_policy_read on governance.four_eyes_policy for select to authenticated
  using (institution_id = (select ctx.institution_id from institution.current_member_ctx() ctx));
revoke all on governance.four_eyes_policy from public, anon, authenticated;
grant select on governance.four_eyes_policy to authenticated;

-- 3. Record how each action was controlled ---------------------------------
alter table governance.action
  add column if not exists control text not null default 'four_eyes'
  check (control in ('four_eyes', 'single'));

-- 4. Is four-eyes required? (fail closed) ----------------------------------
create or replace function governance.four_eyes_required(p_institution uuid, p_category text)
  returns boolean language sql stable security definer set search_path = '' as
$$
  select coalesce(
    (select case when c.locked then true else coalesce(p.enabled, c.default_on) end
       from governance.four_eyes_catalog c
       left join governance.four_eyes_policy p
         on p.institution_id = p_institution and p.category = c.category
      where c.category = p_category),
    true)
$$;
revoke all on function governance.four_eyes_required(uuid, text) from public, anon;
grant execute on function governance.four_eyes_required(uuid, text) to authenticated;

-- 5. Executor: the framework's own actions, then the existing executor -----
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
  else
    perform institution._execute_action(p_action_id);
  end if;

  update governance.action
     set execution_status = 'executed', executed_at = now(), updated_at = now()
   where id = p_action_id;
end
$$;
revoke all on function governance.execute(uuid) from public, anon, authenticated;

-- 6. The choke point --------------------------------------------------------
create or replace function governance.submit(
  p_category text, p_resource_type text, p_resource_id uuid, p_payload jsonb,
  p_label text default '', p_risk text default 'medium')
  returns uuid language plpgsql security definer
  set search_path = 'institution', 'governance', 'admin' as
$$
declare
  v_ctx record;
  v_id  uuid;
  v_cat governance.four_eyes_catalog%rowtype;
begin
  select * into v_ctx from institution.current_member_ctx();
  if v_ctx.member_id is null then
    raise exception 'Not an active institution member';
  end if;

  if p_category like 'four_eyes.%' then
    if not v_ctx.is_admin then raise exception 'Only institution admins can change controls'; end if;
    select * into v_cat from governance.four_eyes_catalog where category = p_payload ->> 'category';
    if not found then raise exception 'Unknown control %', p_payload ->> 'category'; end if;
    if v_cat.locked then raise exception 'Four-eyes on % is locked by the platform', v_cat.category; end if;
  end if;

  insert into governance.action
    (scope, category, label, risk, institution_id, maker_id, maker_role,
     resource_type, resource_id, payload)
  values
    ('institution', p_category, coalesce(p_label, ''), p_risk,
     v_ctx.institution_id, v_ctx.member_id, v_ctx.member_role,
     p_resource_type, p_resource_id, coalesce(p_payload, '{}'))
  returning id into v_id;

  if governance.four_eyes_required(v_ctx.institution_id, p_category) then
    return v_id;                                   -- queued for a checker
  end if;

  -- Single control: the maker must hold the module this action belongs to.
  select * into v_cat from governance.four_eyes_catalog where category = p_category;
  if not v_ctx.is_admin
     and v_cat.maker_module is not null
     and not (v_cat.maker_module = any (coalesce(v_ctx.modules, '{}'))) then
    raise exception 'You do not have access to %', v_cat.label;
  end if;

  update governance.action
     set status = 'approved', control = 'single', checked_at = now(), updated_at = now()
   where id = v_id;
  perform governance.execute(v_id);

  insert into audit.event (actor_id, actor_type, actor_role, institution_id, action,
    resource_type, resource_id, outcome, governance_action_id, metadata)
  values (auth.uid(), 'member', v_ctx.member_role, v_ctx.institution_id,
    p_category || '.executed_single_control', p_resource_type, p_resource_id,
    'success', v_id, jsonb_build_object('maker_id', v_ctx.member_id, 'control', 'single'));
  return v_id;
end
$$;

-- 7. approve_action: run through the framework executor --------------------
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

-- 8. Switching ON is immediate (admins only), and cancels a pending "off" ---
create or replace function governance.enable_four_eyes(p_category text)
  returns void language plpgsql security definer set search_path = '' as
$$
declare v_ctx record;
begin
  select * into v_ctx from institution.current_member_ctx();
  if v_ctx.member_id is null or not v_ctx.is_admin then
    raise exception 'Only institution admins can change controls';
  end if;
  if not exists (select 1 from governance.four_eyes_catalog where category = p_category) then
    raise exception 'Unknown control %', p_category;
  end if;
  insert into governance.four_eyes_policy (institution_id, category, enabled, changed_by)
  values (v_ctx.institution_id, p_category, true, v_ctx.member_id)
  on conflict (institution_id, category) do update
    set enabled = true, changed_by = excluded.changed_by, changed_at = now(), via_action_id = null;
  update governance.action set status = 'cancelled', updated_at = now()
   where institution_id = v_ctx.institution_id and category = 'four_eyes.disable'
     and status = 'pending' and payload ->> 'category' = p_category;
  insert into audit.event (actor_id, actor_type, actor_role, institution_id, action,
    resource_type, outcome, metadata)
  values (auth.uid(), 'member', v_ctx.member_role, v_ctx.institution_id,
    'four_eyes.enabled', 'four_eyes_policy', 'success', jsonb_build_object('category', p_category));
end
$$;
revoke all on function governance.enable_four_eyes(text) from public, anon;
grant execute on function governance.enable_four_eyes(text) to authenticated;

-- 9. Close the bypasses -----------------------------------------------------
drop policy if exists marketplace_bid_insert on marketplace.bid;
drop policy if exists marketplace_bid_allocation_insert on marketplace.bid_allocation;
revoke insert on marketplace.bid, marketplace.bid_allocation from authenticated, anon;

drop policy if exists governance_inst_insert on governance.action;
drop policy if exists governance_platform_insert on governance.action;
revoke insert, delete, truncate on governance.action from authenticated, anon;
revoke update on governance.action from authenticated, anon;
grant update (execution_status, executed_at, execution_error) on governance.action to authenticated;

-- 10. Verify (all data created here is rolled back) -------------------------
do $verify$
declare
  maker   uuid := 'bbb283cf-548d-476c-a190-9957526e2afa';  -- mcbadmin (admin)
  checker uuid := '0777be2a-de18-4b82-86aa-05de958d710e';  -- mcbchecker (not admin)
  mcb     uuid := 'f192050a-dfdd-4da7-874e-c3db88f11e41';
  target  uuid := '8617bae3-a263-4c38-8db1-45968f58a4dc';  -- member edited in the test
  a_id uuid; st text; ctl text; ok boolean; bad text := ''; n int;
begin
  if not exists (select 1 from institution.member where auth_user_id in (maker, checker)) then
    raise notice '040: test members absent, verification skipped'; return;
  end if;
  begin
    -- default: on
    if not governance.four_eyes_required(mcb, 'benefit.create') then bad := bad || 'default not on; '; end if;
    if not governance.four_eyes_required(mcb, 'no.such_thing') then bad := bad || 'unknown not fail-closed; '; end if;

    -- maker asks to switch benefits.create off -> pending
    perform set_config('request.jwt.claims', json_build_object('sub', maker, 'role', 'authenticated')::text, true);
    set local role authenticated;
    a_id := governance.submit('four_eyes.disable', 'four_eyes_policy', null, '{"category":"benefit.create"}');
    select status into st from governance.action where id = a_id;
    if st <> 'pending' then bad := bad || 'disable not queued; '; end if;
    begin
      perform institution.approve_action(a_id, 'self');
      bad := bad || 'maker approved own disable; ';
    exception when others then null; end;

    -- locked control cannot even be requested
    begin
      perform governance.submit('four_eyes.disable', 'four_eyes_policy', null, '{"category":"user.create"}');
      bad := bad || 'locked control accepted; ';
    exception when others then null; end;

    -- direct writes are closed
    begin
      insert into governance.action (scope, category, institution_id, maker_id, resource_type, status)
      values ('institution', 'benefit.create', mcb, target, 'x', 'approved');
      bad := bad || 'forged action insert; ';
    exception when insufficient_privilege then null; end;
    begin
      update governance.action set status = 'approved' where id = a_id;
      bad := bad || 'forged action approval; ';
    exception when insufficient_privilege then null; end;
    begin
      insert into marketplace.bid (request_id, institution_id, rate, amount_offered, term_months)
      values (gen_random_uuid(), mcb, 0.05, 1000, 12);
      bad := bad || 'direct bid insert; ';
    exception when insufficient_privilege then null; end;

    -- checker approves -> policy off
    perform set_config('request.jwt.claims', json_build_object('sub', checker, 'role', 'authenticated')::text, true);
    perform institution.approve_action(a_id, 'ok');
    if governance.four_eyes_required(mcb, 'benefit.create') then bad := bad || 'disable did not apply; '; end if;

    -- now a benefit is created by one person, recorded as single control
    perform set_config('request.jwt.claims', json_build_object('sub', maker, 'role', 'authenticated')::text, true);
    a_id := governance.submit('benefit.create', 'benefit', null, jsonb_build_object(
      'cat_id', (select id from institution.benefit_cat where code = 'reward' limit 1),
      'title', '040 verify', 'value_amount', 1, 'value_unit', 'mur'));
    select status, control into st, ctl from governance.action where id = a_id;
    if st <> 'approved' or ctl <> 'single' then bad := bad || 'single control not applied ('||st||'/'||ctl||'); '; end if;

    -- switching on is immediate
    perform governance.enable_four_eyes('benefit.create');
    if not governance.four_eyes_required(mcb, 'benefit.create') then bad := bad || 'enable failed; '; end if;

    -- user.update now executes on approval, server-side
    a_id := governance.submit('user.update', 'member', target,
      jsonb_build_object('member_id', target, 'field', 'full_name', 'value', '040 verify name'));
    perform set_config('request.jwt.claims', json_build_object('sub', checker, 'role', 'authenticated')::text, true);
    perform institution.approve_action(a_id, 'ok');
    reset role;
    select count(*) into n from institution.member where id = target and full_name = '040 verify name';
    if n <> 1 then bad := bad || 'user.update not executed; '; end if;

    raise exception using errcode = 'P0002', message = 'rollback';
  exception
    when sqlstate 'P0002' then null;
    when others then bad := bad || 'ERROR: ' || sqlerrm || '; ';
  end;
  reset role;
  if bad <> '' then raise exception '040 verification failed: %', bad; end if;
end
$verify$;

commit;
