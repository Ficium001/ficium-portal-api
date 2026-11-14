-- 063_four_eyes_show_the_change.sql
-- Four-eyes approvers (and the maker reviewing their own request) see WHAT changes: each field
-- before and after, with names instead of ids. Until now the queue showed raw payload keys
-- ("member id: 3f2a...", "field: member_role", "value: checker") and never the value being replaced.
--
--   * governance.describe_change() reads the record as it is now and returns
--       { label, before, rows: [{ field, before, after }], described }
--     `before` is a machine snapshot of the fields the request will change.
--   * governance.submit() freezes it on the request at submission: change (the rows),
--     payload_before (the snapshot) and resource_label. The approver sees exactly what the
--     maker saw, even if names change later.
--   * institution.approve_action() refuses if the record changed after the request was made
--     (snapshot no longer matches): nobody approves something other than what was requested.
--   * A description failure never blocks a request: it falls back to the raw fields with
--     described = false, and the screen says so.
-- Pending requests are backfilled (their "before" is the state at migration time).

begin;

alter table governance.action add column if not exists change jsonb;

create or replace function governance._module_label(k text) returns text
language sql stable set search_path = pg_catalog as $$
  select coalesce(
    (select m.label from catalog.module m where m.key = regexp_replace(k, '^inst:', '')),
    initcap(replace(regexp_replace(k, '^inst:', ''), '_', ' ')))
$$;

create or replace function governance._modules(a jsonb) returns text
language sql stable set search_path = pg_catalog as $$
  select coalesce(nullif(string_agg(governance._module_label(x), ', ' order by governance._module_label(x)), ''), 'none')
    from jsonb_array_elements_text(coalesce(a, '[]'::jsonb)) x
$$;

create or replace function governance._products(a jsonb) returns text
language sql stable set search_path = pg_catalog as $$
  select case when a is null or jsonb_typeof(a) <> 'array' or jsonb_array_length(a) = 0 then 'All products'
    else coalesce(string_agg(coalesce(p.label, x), ', ' order by coalesce(p.label, x)), 'All products') end
    from jsonb_array_elements_text(coalesce(nullif(a, 'null'::jsonb), '[]'::jsonb)) x
    left join catalog.product p on p.id::text = x
$$;

create or replace function governance._group_label(p_id text) returns text
language sql stable set search_path = pg_catalog as $$
  select coalesce((select g.label from institution."group" g where g.id::text = p_id), case when p_id is null then 'none' else 'unknown group' end)
$$;

create or replace function governance._money(v text) returns text
language sql immutable set search_path = pg_catalog as $$
  select case when v is null or v = '' then null
              else 'MUR ' || to_char(v::numeric, 'FM999,999,999,990.00') end
$$;

create or replace function governance._row(f text, b text, a text) returns jsonb
language sql immutable set search_path = pg_catalog as $$
  select jsonb_build_object('field', f, 'before', b, 'after', a)
$$;

create or replace function governance.describe_change(p_category text, p_institution uuid, p_payload jsonb)
returns jsonb
language plpgsql stable security definer
set search_path = pg_catalog
as $$
declare
  p jsonb := coalesce(p_payload, '{}'::jsonb);
  m institution.member%rowtype;
  g institution."group"%rowtype;
  b institution.benefit%rowtype;
  v_label text;
  v_before jsonb;
  v_rows jsonb := '[]'::jsonb;
  k text; v text; ov text;
  added text; removed text;
  fld_names constant jsonb := '{"full_name":"Name","email":"Email","member_role":"Role"}';
  ben_names constant jsonb := '{"title":"Title","cat_id":"Category","product_id":"Product","description":"Description",
    "value_display":"Value shown","value_amount":"Amount","value_unit":"Unit","is_guaranteed":"Guaranteed",
    "conditions":"Conditions","valid_from":"Valid from","valid_until":"Valid until","is_active":"Active"}';
  ben_keys constant text[] := array['title','cat_id','product_id','description','value_display','value_amount',
    'value_unit','is_guaranteed','conditions','valid_from','valid_until','is_active'];
  n int;
begin
  if p_category in ('user.update','user.assign_group','user.deactivate','user.reactivate','user.reset_password') then
    select * into m from institution.member where id = (p->>'member_id')::uuid and institution_id = p_institution;
    if not found then raise exception 'member not found'; end if;
    v_label := coalesce(nullif(m.full_name, ''), m.email);
  end if;

  case p_category
  when 'user.create' then
    v_label := coalesce(nullif(btrim(concat_ws(' ', p->>'first_name', p->>'last_name')), ''), p->>'email');
    v_rows := jsonb_build_array(
      governance._row('Name', null, btrim(concat_ws(' ', p->>'first_name', p->>'last_name'))),
      governance._row('Email', null, p->>'email'),
      governance._row('Username', null, p->>'username'),
      governance._row('Role', null, p->>'member_role'),
      governance._row('Group', null, governance._group_label(p->>'custom_group_id')));

  when 'user.update' then
    k := p->>'field';
    ov := case k when 'full_name' then m.full_name when 'email' then m.email when 'member_role' then m.member_role end;
    v_before := jsonb_build_object(k, ov);
    v_rows := jsonb_build_array(governance._row(coalesce(fld_names->>k, k), ov, p->>'value'));

  when 'user.assign_group' then
    v_before := jsonb_build_object('custom_group_id', m.custom_group_id, 'member_role', m.member_role);
    v_rows := jsonb_build_array(
      governance._row('Group', governance._group_label(m.custom_group_id::text), governance._group_label(p->>'custom_group_id')),
      governance._row('Role', m.member_role, coalesce(p->>'member_role', m.member_role)));

  when 'user.deactivate', 'user.reactivate' then
    v_before := jsonb_build_object('active', m.active);
    v_rows := jsonb_build_array(governance._row('Access',
      case when m.active then 'Active' else 'Deactivated' end,
      case when p_category = 'user.reactivate' then 'Active' else 'Deactivated: can no longer sign in' end));

  when 'user.reset_password' then
    v_before := jsonb_build_object('active', m.active, 'email', m.email);
    v_rows := jsonb_build_array(
      governance._row('Password', 'Current password', 'New temporary password, shown once to the approver'),
      governance._row('Email', m.email, m.email));

  when 'group.create' then
    v_label := p->>'label';
    v_rows := jsonb_build_array(
      governance._row('Name', null, p->>'label'),
      governance._row('Description', null, nullif(p->>'description', '')),
      governance._row('Access', null, governance._modules(p->'module_permissions')),
      governance._row('Products', null, governance._products(p->'product_scope')));

  when 'group.update_modules', 'group.delete' then
    select * into g from institution."group" where id = (p->>'group_id')::uuid and institution_id = p_institution;
    if not found then raise exception 'group not found'; end if;
    v_label := g.label;
    v_before := jsonb_build_object('module_permissions', (select coalesce(jsonb_agg(x order by x), '[]') from unnest(g.module_permissions) x),
                                 'product_scope', to_jsonb(g.product_scope));
    if p_category = 'group.delete' then
      select count(*) into n from institution.member where custom_group_id = g.id;
      v_rows := jsonb_build_array(
        governance._row('Group', g.label, 'Deleted'),
        governance._row('Members in it', n::text, case when n > 0 then n::text || ' (must be moved first)' else '0' end),
        governance._row('Access it gave', governance._modules(v_before->'module_permissions'), null));
    else
      select string_agg(governance._module_label(x), ', ' order by governance._module_label(x)) into added
        from jsonb_array_elements_text(coalesce(p->'module_permissions', '[]')) x
       where not (x = any (coalesce(g.module_permissions, '{}')));
      select string_agg(governance._module_label(x), ', ' order by governance._module_label(x)) into removed
        from unnest(coalesce(g.module_permissions, '{}')) x
       where not (coalesce(p->'module_permissions', '[]') ? x);
      v_rows := jsonb_build_array(governance._row('Access', governance._modules(v_before->'module_permissions'), governance._modules(p->'module_permissions')));
      if added is not null then v_rows := v_rows || governance._row('Access added', null, added); end if;
      if removed is not null then v_rows := v_rows || governance._row('Access removed', removed, null); end if;
      if p ? 'product_scope' then
        v_rows := v_rows || governance._row('Products', governance._products(to_jsonb(g.product_scope)), governance._products(p->'product_scope'));
      end if;
    end if;

  when 'benefit.create' then
    v_label := p->>'title';
    v_rows := jsonb_build_array(
      governance._row('Title', null, p->>'title'),
      governance._row('Category', null, (select c.label from institution.benefit_cat c where c.id::text = p->>'cat_id')),
      governance._row('Product', null, coalesce((select pr.label from catalog.product pr where pr.id::text = p->>'product_id'), 'All products')),
      governance._row('Value shown', null, nullif(p->>'value_display', '')),
      governance._row('Amount', null, nullif(concat_ws(' ', p->>'value_amount', p->>'value_unit'), '')),
      governance._row('Guaranteed', null, case when (p->>'is_guaranteed')::boolean then 'Yes' else 'No' end),
      governance._row('Valid', null, concat_ws(' to ', coalesce(p->>'valid_from', 'now'), coalesce(p->>'valid_until', 'no end date'))),
      governance._row('Conditions', null, nullif(p->>'conditions', '')),
      governance._row('Description', null, nullif(p->>'description', '')));

  when 'benefit.update', 'benefit.delete' then
    select * into b from institution.benefit where id = (p->>'benefit_id')::uuid and institution_id = p_institution;
    if not found then raise exception 'benefit not found'; end if;
    v_label := b.title;
    v_before := (select jsonb_object_agg(key, value) from jsonb_each(to_jsonb(b)) where key = any (ben_keys));
    if p_category = 'benefit.delete' then
      v_rows := jsonb_build_array(governance._row('Benefit', b.title, 'Removed: no longer offered with new bids'));
    else
      foreach k in array ben_keys loop
        continue when not (p ? k);
        ov := v_before->>k; v := p->>k;
        continue when ov is not distinct from v;
        if k = 'cat_id' then
          ov := (select c.label from institution.benefit_cat c where c.id::text = ov);
          v  := (select c.label from institution.benefit_cat c where c.id::text = v);
        elsif k = 'product_id' then
          ov := coalesce((select pr.label from catalog.product pr where pr.id::text = ov), 'All products');
          v  := coalesce((select pr.label from catalog.product pr where pr.id::text = v), 'All products');
        elsif k in ('is_guaranteed', 'is_active') then
          ov := case when ov::boolean then 'Yes' else 'No' end; v := case when v::boolean then 'Yes' else 'No' end;
        end if;
        v_rows := v_rows || governance._row(ben_names->>k, ov, v);
      end loop;
      if jsonb_array_length(v_rows) = 0 then v_rows := jsonb_build_array(governance._row('Changes', null, 'No field changes')); end if;
    end if;

  when 'bid.submit' then
    v_label := 'Offer on request ' || left(p->>'request_id', 8);
    v_rows := jsonb_build_array(
      governance._row('Amount offered', null, governance._money(p->>'amount_offered')),
      governance._row('Interest rate', null, to_char((p->>'rate')::numeric * 100, 'FM990.00') || '% ' || coalesce(p->>'rate_type', 'fixed')),
      governance._row('Term', null, (p->>'term_months') || ' months'),
      governance._row('Conditions', null, nullif(coalesce(p->'conditions'->>'notes', ''), '')));

  when 'bid.withdraw' then
    select jsonb_build_object('status', bd.status, 'rate', bd.rate, 'amount_offered', bd.amount_offered, 'term_months', bd.term_months)
      into v_before from marketplace.bid bd where bd.id = (p->>'bid_id')::uuid and bd.institution_id = p_institution;
    if v_before is null then raise exception 'bid not found'; end if;
    v_label := 'Offer on request ' || (select left(bd.request_id::text, 8) from marketplace.bid bd where bd.id = (p->>'bid_id')::uuid);
    v_rows := jsonb_build_array(
      governance._row('Offer', format('%s at %s%% for %s months (%s)', governance._money(v_before->>'amount_offered'),
                       to_char((v_before->>'rate')::numeric * 100, 'FM990.00'), v_before->>'term_months', v_before->>'status'),
                      'Withdrawn: the client no longer sees it'),
      governance._row('Reason', null, p->>'reason'));

  when 'four_eyes.disable' then
    select c.label into v_label from governance.four_eyes_catalog c where c.category = p->>'category';
    v_before := jsonb_build_object('enabled', governance.four_eyes_required(p_institution, p->>'category'));
    v_rows := jsonb_build_array(governance._row('Second approval for "' || coalesce(v_label, p->>'category') || '"',
      case when (v_before->>'enabled')::boolean then 'On' else 'Off' end,
      'Off: one person can do it alone'));
    v_label := 'Control: ' || coalesce(v_label, p->>'category');

  else
    for k, v in select key, value #>> '{}' from jsonb_each(p) where jsonb_typeof(value) in ('string','number','boolean') loop
      continue when k in ('institution_id', 'submitted_via');
      v_rows := v_rows || governance._row(initcap(replace(k, '_', ' ')), null, v);
    end loop;
  end case;

  -- Drop rows with nothing to show on either side.
  select coalesce(jsonb_agg(r), '[]') into v_rows from jsonb_array_elements(v_rows) r
   where coalesce(r->>'before', r->>'after') is not null;
  return jsonb_build_object('label', v_label, 'before', v_before, 'rows', v_rows, 'described', true);
exception when others then
  select coalesce(jsonb_agg(governance._row(initcap(replace(key, '_', ' ')), null, value #>> '{}')), '[]') into v_rows
    from jsonb_each(p) where jsonb_typeof(value) in ('string','number','boolean') and key not in ('institution_id','submitted_via');
  return jsonb_build_object('label', null, 'before', null, 'rows', v_rows, 'described', false, 'error', sqlerrm);
end $$;

revoke all on function governance.describe_change(text, uuid, jsonb), governance._module_label(text),
  governance._modules(jsonb), governance._products(jsonb), governance._group_label(text),
  governance._money(text), governance._row(text, text, text) from public, anon, authenticated;

-- Freeze the description on every request, at submission (every submitter goes through here).
create or replace function governance.submit(p_category text, p_resource_type text, p_resource_id uuid, p_payload jsonb, p_label text default ''::text, p_risk text default 'medium'::text)
 returns uuid
 language plpgsql
 security definer
 set search_path to 'institution', 'governance', 'admin'
as $function$
declare
  v_ctx record;
  v_id  uuid;
  v_cat governance.four_eyes_catalog%rowtype;
  v_d   jsonb;
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

  v_d := governance.describe_change(p_category, v_ctx.institution_id, p_payload);

  insert into governance.action
    (scope, category, label, risk, institution_id, maker_id, maker_role,
     resource_type, resource_id, payload, payload_before, resource_label, change)
  values
    ('institution', p_category, coalesce(p_label, ''), p_risk,
     v_ctx.institution_id, v_ctx.member_id, v_ctx.member_role,
     p_resource_type, p_resource_id, coalesce(p_payload, '{}'),
     nullif(v_d -> 'before', 'null'::jsonb), v_d ->> 'label',
     jsonb_build_object('rows', v_d -> 'rows', 'described', v_d -> 'described'))
  returning id into v_id;

  if governance.four_eyes_required(v_ctx.institution_id, p_category) then
    return v_id;
  end if;

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
$function$;

-- Refuse approval when the record no longer matches what the maker saw.
create or replace function institution.approve_action(p_action_id uuid, p_note text default ''::text)
 returns void
 language plpgsql
 security definer
 set search_path to ''
as $function$
declare
  v_action  governance.action%rowtype;
  v_inst_id uuid;
  v_role    text;
  v_now     jsonb;
begin
  v_inst_id := institution.get_my_institution_id();

  select * into v_action from governance.action
   where id = p_action_id and institution_id = v_inst_id and scope = 'institution'
   for update;
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
  if v_action.payload_before is not null then
    v_now := governance.describe_change(v_action.category, v_inst_id, v_action.payload);
    if (v_now ->> 'described')::boolean and nullif(v_now -> 'before', 'null'::jsonb) is distinct from v_action.payload_before then
      raise exception 'CHANGED_SINCE_REQUEST: this record changed after the request was made, so it is no longer what was asked for. Reject it and ask for the change again.';
    end if;
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
    jsonb_build_object('maker_id', v_action.maker_id, 'checker_id', institution.get_my_member_id(),
                       'change', v_action.change));
end
$function$;

-- Backfill requests still waiting (before = state now).
with x as (
  select id, governance.describe_change(category, institution_id, payload) as d
    from governance.action
   where status = 'pending' and change is null and scope = 'institution')
update governance.action a
   set payload_before = coalesce(a.payload_before, nullif(x.d -> 'before', 'null'::jsonb)),
       resource_label = coalesce(nullif(a.resource_label, ''), x.d ->> 'label'),
       change = jsonb_build_object('rows', x.d -> 'rows', 'described', x.d -> 'described', 'backfilled', true)
  from x
 where a.id = x.id;

commit;
