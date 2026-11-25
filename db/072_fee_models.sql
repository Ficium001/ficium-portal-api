-- 072_fee_models.sql
-- Ficium's commission becomes a fee MODEL per rule, not a single rate.
--
-- db/058 gave every rule one basis: rate x booked amount. That works for a loan
-- (one disbursement) and fails for everything else: a savings plan that starts
-- at MUR 1,000 and adds MUR 500 a month earns MUR 5 at 0.5%, a term deposit is
-- worth more to the bank the longer it runs, and a regular saver who stops after
-- two months should not cost the bank a full year's fee.
--
-- A rule now carries:
--   basis          how the fee base is worked out (billing.compute_fee):
--     booked_amount   rate x booked amount                         (loans; the 058 behaviour)
--     annualised      rate x (first amount x lump_weight + monthly amount x months)
--                     params {months 1-60, lump_weight 0-1 = 1}    (regular savings / investment plans)
--     amount_x_term   rate per year x amount x min(term in years, max_years)
--                     params {max_years 1-30}                      (term deposits, bonds)
--     tiered          banded rates on the booked amount
--                     params {mode marginal|whole, tiers [{up_to|null, rate}]}
--     flat            a fixed amount per booked deal, params {amount}
--   min_fee/max_fee  floor and cap on the fee (MUR, not for flat)
--   clawback         months 0-36 and mode full|pro_rata: when the bank reports
--                    the account closed inside the window, an append-only
--                    credit (billing.commission_adjustment) gives back all or
--                    the unearned share of the fee
--
-- Every rule is checked by billing.rule_shape_error() (also a CHECK on the
-- table), so a malformed rule cannot exist however it got there. The fee is
-- computed in the database, once, at booking, by the same function the admin
-- site's preview calls, and the whole working is stored on the commission
-- event (calc), so a statement re-run later shows exactly what was charged and why.
--
-- Not built on purpose: fees on balances or on each contribution over time.
-- They need the bank to report the account every month and probably sit inside
-- the FSC's perimeter for investment intermediaries. Clawback covers the main
-- risk (paying for money that never arrives) without either.
--
-- Backwards compatible: every new function argument has a default, so the
-- running portal-api keeps working until the new code is deployed. The one
-- live rule (platform default 0.5% on booked amount) is unchanged.

begin;

-- ── 1. rule shape ────────────────────────────────────────────────────────────
-- A JSON number as numeric, or null for anything else (never a cast error).
create or replace function billing.json_num(p jsonb) returns numeric
  language sql immutable set search_path = '' as
$$ select case when jsonb_typeof(p) = 'number' then (p #>> '{}')::numeric end $$;

create or replace function billing.rule_shape_error(
  p_basis text, p_rate numeric, p_params jsonb, p_min_fee numeric, p_max_fee numeric,
  p_clawback_months integer, p_clawback_mode text)
  returns text language plpgsql immutable set search_path = '' as
$$
declare
  v_keys    text[];
  v_allowed text[];
  v_tier    jsonb;
  v_prev    numeric := 0;
  v_n       integer;
  v_i       integer := 0;
  v_num     numeric;
begin
  if p_basis is null or p_basis not in ('booked_amount', 'annualised', 'amount_x_term', 'tiered', 'flat') then
    return 'basis must be booked_amount, annualised, amount_x_term, tiered or flat';
  end if;
  if p_params is null or jsonb_typeof(p_params) <> 'object' then
    return 'params must be an object';
  end if;

  v_allowed := case p_basis
    when 'booked_amount' then array[]::text[]
    when 'annualised'    then array['months', 'lump_weight']
    when 'amount_x_term' then array['max_years']
    when 'tiered'        then array['mode', 'tiers']
    when 'flat'          then array['amount'] end;
  select coalesce(array_agg(k), array[]::text[]) into v_keys from jsonb_object_keys(p_params) k;
  if not v_keys <@ v_allowed then
    return format('params for %s may only contain: %s', p_basis,
                  coalesce(nullif(array_to_string(v_allowed, ', '), ''), 'nothing'));
  end if;

  -- rate: a fraction, max 5%, only where the basis multiplies by one
  if p_basis in ('booked_amount', 'annualised', 'amount_x_term') then
    if p_rate is null or p_rate < 0 or p_rate > 0.05 then
      return 'rate must be from 0 to 5%';
    end if;
  elsif p_rate is not null then
    return format('a %s rule has no single rate', p_basis);
  end if;

  if p_basis = 'annualised' then
    v_num := billing.json_num(p_params->'months');
    if v_num is null or v_num not between 1 and 60 or v_num <> trunc(v_num) then
      return 'months must be a whole number from 1 to 60';
    end if;
    if p_params ? 'lump_weight' then
      v_num := billing.json_num(p_params->'lump_weight');
      if v_num is null or v_num not between 0 and 1 then
        return 'lump_weight must be between 0 and 1';
      end if;
    end if;
  elsif p_basis = 'amount_x_term' then
    v_num := billing.json_num(p_params->'max_years');
    if v_num is null or v_num not between 1 and 30 then
      return 'max_years must be from 1 to 30';
    end if;
  elsif p_basis = 'flat' then
    v_num := billing.json_num(p_params->'amount');
    if v_num is null or v_num <= 0 or v_num > 1000000 or round(v_num, 2) <> v_num then
      return 'amount must be above 0 and at most 1,000,000, with at most 2 decimals';
    end if;
    if p_min_fee is not null or p_max_fee is not null then
      return 'a flat fee has no minimum or maximum';
    end if;
  elsif p_basis = 'tiered' then
    if coalesce(p_params->>'mode', '') not in ('marginal', 'whole') then
      return 'mode must be marginal or whole';
    end if;
    if jsonb_typeof(p_params->'tiers') is distinct from 'array' then
      return 'tiers must be a list';
    end if;
    v_n := jsonb_array_length(p_params->'tiers');
    if v_n not between 1 and 10 then
      return 'give 1 to 10 tiers';
    end if;
    for v_tier in select t from jsonb_array_elements(p_params->'tiers') t loop
      v_i := v_i + 1;
      if jsonb_typeof(v_tier) <> 'object' then
        return format('tier %s must be an object', v_i);
      end if;
      if not (select coalesce(array_agg(k), array[]::text[]) from jsonb_object_keys(v_tier) k) <@ array['up_to', 'rate'] then
        return format('tier %s may only have up_to and rate', v_i);
      end if;
      v_num := billing.json_num(v_tier->'rate');
      if v_num is null or v_num < 0 or v_num > 0.05 or round(v_num, 4) <> v_num then
        return format('tier %s rate must be from 0 to 5%% with at most 4 decimals', v_i);
      end if;
      if v_i < v_n then
        v_num := billing.json_num(v_tier->'up_to');
        if v_num is null or v_num <= v_prev then
          return format('tier %s needs an upper limit above the one before it', v_i);
        end if;
        v_prev := v_num;
      elsif v_tier ? 'up_to' and jsonb_typeof(v_tier->'up_to') <> 'null' then
        return 'the last tier has no upper limit';
      end if;
    end loop;
  end if;

  if p_min_fee is not null and (p_min_fee < 0 or round(p_min_fee, 2) <> p_min_fee) then
    return 'the minimum fee must be 0 or more, with at most 2 decimals';
  end if;
  if p_max_fee is not null and (p_max_fee <= 0 or round(p_max_fee, 2) <> p_max_fee) then
    return 'the maximum fee must be above 0, with at most 2 decimals';
  end if;
  if p_min_fee is not null and p_max_fee is not null and p_min_fee > p_max_fee then
    return 'the minimum fee cannot be above the maximum';
  end if;

  if p_clawback_months is null or p_clawback_months not between 0 and 36 then
    return 'clawback months must be from 0 to 36';
  end if;
  if p_clawback_months = 0 and p_clawback_mode is distinct from 'none' then
    return 'with no clawback months the clawback mode is none';
  end if;
  if p_clawback_months > 0 and p_clawback_mode not in ('full', 'pro_rata') then
    return 'clawback mode must be full or pro_rata';
  end if;
  return null;
end
$$;

alter table billing.commission_rule drop constraint if exists commission_rule_basis_check;
alter table billing.commission_rule drop constraint if exists commission_rule_rate_check;
alter table billing.commission_rule alter column rate drop not null;
alter table billing.commission_rule
  add column if not exists params          jsonb not null default '{}'::jsonb,
  add column if not exists min_fee         numeric(15,2),
  add column if not exists max_fee         numeric(15,2),
  add column if not exists currency        char(3) not null default 'MUR' check (currency ~ '^[A-Z]{3}$'),
  add column if not exists clawback_months smallint not null default 0,
  add column if not exists clawback_mode   text not null default 'none';
alter table billing.commission_rule add constraint commission_rule_shape check (
  billing.rule_shape_error(basis, rate, params, min_fee, max_fee, clawback_months, clawback_mode) is null);

-- ── 2. the calculation (pure; the booking and the admin preview both use it) ──
-- p_in: {amount, monthly_amount, term_months}. Raises FEE_INPUT_MISSING:<field>.
create or replace function billing.compute_fee(
  p_basis text, p_rate numeric, p_params jsonb, p_min_fee numeric, p_max_fee numeric, p_in jsonb)
  returns jsonb language plpgsql immutable set search_path = '' as
$$
declare
  v_amount  numeric := (p_in->>'amount')::numeric;
  v_monthly numeric := (p_in->>'monthly_amount')::numeric;
  v_term    numeric := (p_in->>'term_months')::numeric;
  v_base    numeric;
  v_raw     numeric;
  v_fee     numeric;
  v_years   numeric;
  v_steps   jsonb := '[]'::jsonb;
  v_tier    jsonb;
  v_lower   numeric := 0;
  v_upper   numeric;
  v_part    numeric;
  v_floor   boolean := false;
  v_cap     boolean := false;
begin
  if v_amount is null or v_amount <= 0 then
    raise exception using errcode = 'P0001', message = 'FEE_INPUT_MISSING:amount';
  end if;

  case p_basis
  when 'booked_amount' then
    v_base := v_amount;
    v_raw  := v_base * p_rate;
  when 'annualised' then
    if v_monthly is null or v_monthly < 0 then
      raise exception using errcode = 'P0001', message = 'FEE_INPUT_MISSING:monthly_amount';
    end if;
    v_base := v_amount * coalesce((p_params->>'lump_weight')::numeric, 1)
              + v_monthly * (p_params->>'months')::numeric;
    v_raw  := v_base * p_rate;
  when 'amount_x_term' then
    if v_term is null or v_term <= 0 then
      raise exception using errcode = 'P0001', message = 'FEE_INPUT_MISSING:term_months';
    end if;
    v_years := least(v_term / 12.0, (p_params->>'max_years')::numeric);
    v_base  := v_amount * v_years;
    v_raw   := v_base * p_rate;
  when 'flat' then
    v_base := null;
    v_raw  := (p_params->>'amount')::numeric;
  when 'tiered' then
    v_base := v_amount;
    v_raw  := 0;
    for v_tier in select t from jsonb_array_elements(p_params->'tiers') t loop
      v_upper := (v_tier->>'up_to')::numeric;   -- null on the last tier
      if p_params->>'mode' = 'whole' then
        if v_upper is null or v_amount <= v_upper then
          v_raw   := v_amount * (v_tier->>'rate')::numeric;
          v_steps := jsonb_build_array(jsonb_build_object(
                       'from', v_lower, 'up_to', v_upper, 'rate', (v_tier->>'rate')::numeric,
                       'on', v_amount, 'fee', round(v_raw, 2)));
          exit;
        end if;
      else
        v_part := least(v_amount, coalesce(v_upper, v_amount)) - v_lower;
        if v_part > 0 then
          v_raw   := v_raw + v_part * (v_tier->>'rate')::numeric;
          v_steps := v_steps || jsonb_build_object(
                       'from', v_lower, 'up_to', v_upper, 'rate', (v_tier->>'rate')::numeric,
                       'on', v_part, 'fee', round(v_part * (v_tier->>'rate')::numeric, 2));
        end if;
        exit when v_upper is null or v_amount <= v_upper;
      end if;
      v_lower := v_upper;
    end loop;
  else
    raise exception using errcode = 'P0001', message = 'FEE_BASIS_UNKNOWN';
  end case;

  v_raw := round(v_raw, 2);
  v_fee := v_raw;
  if p_min_fee is not null and v_fee < p_min_fee then v_fee := p_min_fee; v_floor := true; end if;
  if p_max_fee is not null and v_fee > p_max_fee then v_fee := p_max_fee; v_cap := true; end if;

  return jsonb_strip_nulls(jsonb_build_object(
    'basis', p_basis, 'rate', p_rate, 'params', p_params, 'min_fee', p_min_fee, 'max_fee', p_max_fee,
    'inputs', jsonb_strip_nulls(jsonb_build_object('amount', v_amount, 'monthly_amount', v_monthly,
                                                   'term_months', v_term)),
    'years', round(v_years, 4), 'base', round(v_base, 2),
    'tiers', case when p_basis = 'tiered' then v_steps end,
    'raw_fee', v_raw, 'fee', v_fee, 'floor_applied', v_floor, 'cap_applied', v_cap));
end
$$;

-- ── 3. resolver returns the whole rule ───────────────────────────────────────
-- Replaces 058's commission_for(), which returned only the rate (nothing else calls it).
drop function if exists billing.commission_for(uuid, text, date);
create or replace function billing.rule_for(p_institution_id uuid, p_product_code text, p_on date)
  returns billing.commission_rule language sql stable set search_path = '' as
$$
  select r.*
    from billing.commission_rule r
   where r.effective_from <= p_on
     and (r.ended_on is null or r.ended_on > p_on)
     and (r.institution_id is null or r.institution_id = p_institution_id)
     and (r.product_code   is null or r.product_code   = p_product_code)
   order by (r.institution_id is not null) desc, (r.product_code is not null) desc, r.effective_from desc
   limit 1
$$;

-- ── 4. writes (admin channel only) ───────────────────────────────────────────
drop function if exists billing.set_commission_rule(uuid, text, numeric, date, text, uuid, uuid, uuid);
create function billing.set_commission_rule(
  p_institution_id uuid, p_product_code text, p_rate numeric, p_effective_from date,
  p_reason text, p_command_id uuid, p_maker uuid, p_checker uuid,
  p_basis text default 'booked_amount', p_params jsonb default '{}'::jsonb,
  p_min_fee numeric default null, p_max_fee numeric default null,
  p_clawback_months integer default 0, p_clawback_mode text default 'none')
  returns billing.commission_rule language plpgsql set search_path = '' as
$$
declare
  v_today date := (now() at time zone 'Indian/Mauritius')::date;
  v_open  billing.commission_rule;
  v_new   billing.commission_rule;
  v_err   text;
begin
  if p_maker = p_checker then
    raise exception using errcode = 'P0001', message = 'COMMISSION_FOUR_EYES';
  end if;
  if p_basis in ('booked_amount', 'annualised', 'amount_x_term')
     and (p_rate is null or p_rate < 0 or p_rate > 0.05) then
    raise exception using errcode = 'P0001', message = 'COMMISSION_RATE_INVALID';
  end if;
  v_err := billing.rule_shape_error(p_basis, p_rate, coalesce(p_params, '{}'::jsonb), p_min_fee, p_max_fee,
                                    p_clawback_months, p_clawback_mode);
  if v_err is not null then
    raise exception using errcode = 'P0001', message = 'COMMISSION_RULE_INVALID: ' || v_err;
  end if;
  if p_effective_from is null or p_effective_from < v_today then
    raise exception using errcode = 'P0001', message = 'COMMISSION_DATE_IN_PAST';
  end if;
  if p_institution_id is not null
     and not exists (select 1 from institution.institution i where i.id = p_institution_id) then
    raise exception using errcode = 'P0001', message = 'COMMISSION_UNKNOWN_INSTITUTION';
  end if;
  if p_product_code is not null
     and not exists (select 1 from catalog.product p where p.code = p_product_code) then
    raise exception using errcode = 'P0001', message = 'COMMISSION_UNKNOWN_PRODUCT';
  end if;

  perform pg_advisory_xact_lock(hashtext('commission:' || coalesce(p_institution_id::text, '*')
                                         || ':' || coalesce(p_product_code, '*')));
  select * into v_open from billing.commission_rule r
   where r.ended_on is null
     and r.institution_id is not distinct from p_institution_id
     and r.product_code   is not distinct from p_product_code
   for update;
  if found then
    if v_open.effective_from >= p_effective_from then
      raise exception using errcode = 'P0001', message = 'COMMISSION_LATER_RULE_EXISTS';
    end if;
    update billing.commission_rule
       set ended_on = p_effective_from, ended_by = p_checker, end_command_id = p_command_id
     where id = v_open.id;
  end if;

  insert into billing.commission_rule (institution_id, product_code, rate, basis, params, min_fee, max_fee,
                                       clawback_months, clawback_mode, effective_from, reason,
                                       command_id, set_by, approved_by)
  values (p_institution_id, p_product_code, p_rate, p_basis, coalesce(p_params, '{}'::jsonb), p_min_fee, p_max_fee,
          p_clawback_months, p_clawback_mode, p_effective_from, btrim(p_reason),
          p_command_id, p_maker, p_checker)
  returning * into v_new;
  return v_new;
end
$$;

revoke all on function billing.json_num(jsonb) from public, anon, authenticated;
revoke all on function billing.rule_shape_error(text, numeric, jsonb, numeric, numeric, integer, text) from public, anon, authenticated;
revoke all on function billing.compute_fee(text, numeric, jsonb, numeric, numeric, jsonb) from public, anon, authenticated;
revoke all on function billing.rule_for(uuid, text, date) from public, anon, authenticated;
revoke all on function billing.set_commission_rule(uuid, text, numeric, date, text, uuid, uuid, uuid,
                                                   text, jsonb, numeric, numeric, integer, text) from public, anon, authenticated;

-- ── 5. what a booking records ────────────────────────────────────────────────
alter table marketplace.loan_booking
  add column if not exists monthly_amount numeric(18,2) check (monthly_amount is null or monthly_amount >= 0),
  add column if not exists term_months    integer check (term_months is null or term_months between 1 and 600),
  add column if not exists closed_on      date,
  add column if not exists closure_reason text check (closure_reason is null or char_length(closure_reason) between 5 and 500),
  add column if not exists closed_source  text check (closed_source is null or closed_source in ('portal', 'api')),
  add column if not exists closed_by      uuid,
  add column if not exists closed_api_key_id uuid;
alter table marketplace.loan_booking drop constraint if exists loan_booking_closed_consistent;
alter table marketplace.loan_booking add constraint loan_booking_closed_consistent check (
  (closed_on is null) = (closure_reason is null) and (closed_on is null) = (closed_source is null)
  and (closed_on is null or closed_on >= booked_on));

-- The commission event stores the fee, not a formula: commission_amt was
-- deal_amount x rate, which a flat or tiered fee is not. The table is empty.
alter table portal_admin.commission_event alter column commission_amt drop expression;
alter table portal_admin.commission_event alter column commission_rate drop default;
alter table portal_admin.commission_event alter column commission_rate drop not null;
alter table portal_admin.commission_event
  add column if not exists basis           text,
  add column if not exists fee_base        numeric(18,2),
  add column if not exists calc            jsonb,
  add column if not exists clawback_until  date,
  add column if not exists clawback_mode   text not null default 'none'
                                            check (clawback_mode in ('none', 'full', 'pro_rata')),
  add column if not exists clawback_months smallint not null default 0;

-- ── 6. clawback credits: append-only ─────────────────────────────────────────
create table if not exists billing.commission_adjustment (
  id             uuid primary key default gen_random_uuid(),
  commission_id  uuid not null references portal_admin.commission_event(id),
  pipeline_id    uuid not null references marketplace.loan_pipeline(id),
  institution_id uuid not null references institution.institution(id),
  kind           text not null check (kind in ('clawback')),
  amount         numeric(15,2) not null check (amount < 0),
  currency       char(3) not null,
  effective_on   date not null,
  reason         text not null check (char_length(btrim(reason)) between 5 and 500),
  calc           jsonb not null,
  created_at     timestamptz not null default now()
);
create unique index if not exists commission_adjustment_one_clawback
  on billing.commission_adjustment (commission_id) where kind = 'clawback';
create index if not exists commission_adjustment_inst_month
  on billing.commission_adjustment (institution_id, effective_on);
create index if not exists commission_adjustment_pipeline on billing.commission_adjustment (pipeline_id);
alter table billing.commission_adjustment enable row level security;
alter table billing.commission_adjustment force row level security;
revoke all on billing.commission_adjustment from public, anon, authenticated;

create or replace function billing.adjustment_is_append_only() returns trigger
  language plpgsql set search_path = '' as
$$
begin
  raise exception using errcode = 'P0001', message = 'COMMISSION_ADJUSTMENT_APPEND_ONLY';
end
$$;
revoke all on function billing.adjustment_is_append_only() from public, anon, authenticated;
drop trigger if exists commission_adjustment_append_only on billing.commission_adjustment;
create trigger commission_adjustment_append_only before update or delete on billing.commission_adjustment
  for each row execute function billing.adjustment_is_append_only();

-- ── 7. booking: fee from the rule's model ────────────────────────────────────
drop function if exists marketplace.record_loan_booking(uuid, uuid, text, date, numeric, date, text, text, uuid, uuid, boolean);
create function marketplace.record_loan_booking(
  p_pipeline_id uuid, p_institution_id uuid, p_core_reference text, p_booked_on date,
  p_disbursed_amount numeric, p_disbursed_on date, p_notes text, p_source text,
  p_actor_id uuid, p_api_key_id uuid, p_correction boolean default false,
  p_monthly_amount numeric default null, p_term_months integer default null)
 returns jsonb
 language plpgsql
 set search_path to 'marketplace', 'portal_admin', 'pg_temp'
as $function$
declare
    v_deal     marketplace.loan_pipeline%rowtype;
    v_prev     marketplace.loan_booking%rowtype;
    v_row      marketplace.loan_booking%rowtype;
    v_ref      text := btrim(p_core_reference);
    v_today    date := (now() at time zone 'Indian/Mauritius')::date;
    v_comm     text;
    v_product  text;
    v_currency text;
    v_rule     billing.commission_rule;
    v_calc     jsonb;
begin
    select * into v_deal from marketplace.loan_pipeline
     where id = p_pipeline_id and institution_id = p_institution_id
     for update;
    if not found then raise exception 'BOOKING_DEAL_NOT_FOUND'; end if;
    if v_deal.status <> 'completed' then raise exception 'BOOKING_DEAL_NOT_COMPLETE'; end if;

    if v_ref is null or v_ref = '' or char_length(v_ref) > 64 then
        raise exception 'BOOKING_REFERENCE_INVALID';
    end if;
    if p_disbursed_amount is null or p_disbursed_amount <= 0 then
        raise exception 'BOOKING_AMOUNT_INVALID';
    end if;
    if v_deal.deal_amount is not null and p_disbursed_amount > v_deal.deal_amount then
        raise exception 'BOOKING_AMOUNT_EXCEEDS_DEAL';
    end if;
    if p_monthly_amount is not null and (p_monthly_amount < 0 or p_monthly_amount > 1e12) then
        raise exception 'BOOKING_MONTHLY_INVALID';
    end if;
    if p_term_months is not null and p_term_months not between 1 and 600 then
        raise exception 'BOOKING_TERM_INVALID';
    end if;
    if p_booked_on is null or p_booked_on > v_today then
        raise exception 'BOOKING_DATE_INVALID';
    end if;
    if p_booked_on < v_deal.started_at::date then
        raise exception 'BOOKING_DATE_BEFORE_DEAL';
    end if;
    if p_disbursed_on is not null and (p_disbursed_on > v_today or p_disbursed_on < v_deal.started_at::date) then
        raise exception 'BOOKING_DISBURSED_DATE_INVALID';
    end if;
    if p_source not in ('portal', 'api') then raise exception 'BOOKING_SOURCE_INVALID'; end if;

    select * into v_prev from marketplace.loan_booking where pipeline_id = p_pipeline_id for update;
    if found and not p_correction then raise exception 'BOOKING_ALREADY_RECORDED'; end if;
    if not found and p_correction then raise exception 'BOOKING_NOT_FOUND'; end if;
    if found and v_prev.closed_on is not null then raise exception 'BOOKING_CLOSED'; end if;

    begin
        if p_correction then
            update marketplace.loan_booking
               set core_reference   = v_ref,
                   booked_on        = p_booked_on,
                   disbursed_amount = p_disbursed_amount,
                   disbursed_on     = p_disbursed_on,
                   monthly_amount   = p_monthly_amount,
                   term_months      = p_term_months,
                   notes            = p_notes,
                   source           = p_source,
                   recorded_by      = p_actor_id,
                   api_key_id       = p_api_key_id,
                   corrected_count  = corrected_count + 1,
                   updated_at       = now()
             where pipeline_id = p_pipeline_id
            returning * into v_row;
        else
            insert into marketplace.loan_booking
                (pipeline_id, institution_id, core_reference, booked_on, disbursed_amount,
                 disbursed_on, monthly_amount, term_months, notes, source, recorded_by, api_key_id)
            values
                (p_pipeline_id, p_institution_id, v_ref, p_booked_on, p_disbursed_amount,
                 p_disbursed_on, p_monthly_amount, p_term_months, p_notes, p_source, p_actor_id, p_api_key_id)
            returning * into v_row;
        end if;
    exception when unique_violation then
        raise exception 'BOOKING_REFERENCE_IN_USE';
    end;

    -- The rule in force on the booking date for this bank and product, and its model.
    select p.code, coalesce(r.currency, p.currency, 'MUR') into v_product, v_currency
      from marketplace.request r join catalog.product p on p.id = r.product_id
     where r.id = v_deal.request_id;
    v_rule := billing.rule_for(p_institution_id, v_product, p_booked_on);
    if v_rule.id is null then raise exception 'BOOKING_NO_COMMISSION_RULE'; end if;
    if v_currency is distinct from v_rule.currency
       and (v_rule.basis = 'flat' or v_rule.min_fee is not null or v_rule.max_fee is not null) then
        raise exception 'BOOKING_FEE_CURRENCY_MISMATCH';
    end if;
    v_calc := billing.compute_fee(v_rule.basis, v_rule.rate, v_rule.params, v_rule.min_fee, v_rule.max_fee,
                jsonb_build_object('amount', p_disbursed_amount, 'monthly_amount', p_monthly_amount,
                                   'term_months', coalesce(p_term_months, v_deal.deal_term_months)));

    insert into portal_admin.commission_event (pipeline_id, request_id, institution_id, deal_amount,
                                               commission_rate, commission_amt, currency, rule_id,
                                               basis, fee_base, calc, clawback_months, clawback_mode,
                                               clawback_until)
    values (p_pipeline_id, v_deal.request_id, p_institution_id, p_disbursed_amount,
            v_rule.rate, (v_calc->>'fee')::numeric, v_currency, v_rule.id,
            v_rule.basis, (v_calc->>'base')::numeric, v_calc, v_rule.clawback_months, v_rule.clawback_mode,
            case when v_rule.clawback_months > 0
                 then (p_booked_on + make_interval(months => v_rule.clawback_months))::date end)
    on conflict (pipeline_id) do update
        set deal_amount = excluded.deal_amount, commission_rate = excluded.commission_rate,
            commission_amt = excluded.commission_amt, currency = excluded.currency,
            rule_id = excluded.rule_id, basis = excluded.basis, fee_base = excluded.fee_base,
            calc = excluded.calc, clawback_months = excluded.clawback_months,
            clawback_mode = excluded.clawback_mode, clawback_until = excluded.clawback_until,
            updated_at = now()
        where portal_admin.commission_event.status = 'pending';
    select status into v_comm from portal_admin.commission_event where pipeline_id = p_pipeline_id;

    return jsonb_build_object(
        'booking', to_jsonb(v_row),
        'previous', case when p_correction then to_jsonb(v_prev) end,
        'commission_status', v_comm
    );
end;
$function$;
revoke all on function marketplace.record_loan_booking(uuid, uuid, text, date, numeric, date, text, text, uuid, uuid, boolean, numeric, integer) from public, anon, authenticated;

-- What a booking for this deal will need to send (no rates: the bank sees which
-- fields, not Ficium's price). Same resolver as the booking, on today's date.
create or replace function marketplace.booking_fee_inputs(p_pipeline_id uuid, p_institution_id uuid)
  returns jsonb language sql stable set search_path = '' as
$$
  select jsonb_build_object(
           'monthly_amount', coalesce(r.basis = 'annualised', false),
           'term_months',    coalesce(r.basis = 'amount_x_term', false))
    from marketplace.loan_pipeline lp
    join marketplace.request rq on rq.id = lp.request_id
    join catalog.product p on p.id = rq.product_id
    left join lateral billing.rule_for(lp.institution_id, p.code, (now() at time zone 'Indian/Mauritius')::date) r on true
   where lp.id = p_pipeline_id and lp.institution_id = p_institution_id
$$;
revoke all on function marketplace.booking_fee_inputs(uuid, uuid) from public, anon, authenticated;

-- ── 8. closure: the account ends; clawback inside the window ────────────────
create or replace function marketplace.record_booking_closure(
  p_pipeline_id uuid, p_institution_id uuid, p_closed_on date, p_reason text,
  p_source text, p_actor_id uuid, p_api_key_id uuid)
  returns jsonb language plpgsql set search_path = '' as
$$
declare
  v_today   date := (now() at time zone 'Indian/Mauritius')::date;
  v_bk      marketplace.loan_booking%rowtype;
  v_ev      portal_admin.commission_event%rowtype;
  v_reason  text := btrim(p_reason);
  v_elapsed integer;
  v_share   numeric;
  v_back    numeric;
  v_already numeric;
  v_adj     billing.commission_adjustment;
begin
  select * into v_bk from marketplace.loan_booking
   where pipeline_id = p_pipeline_id and institution_id = p_institution_id for update;
  if not found then raise exception 'BOOKING_NOT_FOUND'; end if;
  if v_bk.closed_on is not null then raise exception 'BOOKING_ALREADY_CLOSED'; end if;
  if p_closed_on is null or p_closed_on > v_today or p_closed_on < v_bk.booked_on then
    raise exception 'BOOKING_CLOSURE_DATE_INVALID';
  end if;
  if v_reason is null or char_length(v_reason) not between 5 and 500 then
    raise exception 'BOOKING_CLOSURE_REASON_INVALID';
  end if;
  if p_source not in ('portal', 'api') then raise exception 'BOOKING_SOURCE_INVALID'; end if;

  update marketplace.loan_booking
     set closed_on = p_closed_on, closure_reason = v_reason, closed_source = p_source,
         closed_by = p_actor_id, closed_api_key_id = p_api_key_id, updated_at = now()
   where pipeline_id = p_pipeline_id
  returning * into v_bk;

  select * into v_ev from portal_admin.commission_event where pipeline_id = p_pipeline_id for update;
  if found and v_ev.clawback_until is not null and p_closed_on < v_ev.clawback_until
     and v_ev.status <> 'waived' and coalesce(v_ev.commission_amt, 0) > 0 then
    -- whole months the account ran, counted from the booking date
    v_elapsed := (extract(year from age(p_closed_on, v_bk.booked_on)) * 12
                  + extract(month from age(p_closed_on, v_bk.booked_on)))::integer;
    v_share := case v_ev.clawback_mode
                 when 'full' then 1
                 else greatest(v_ev.clawback_months - v_elapsed, 0)::numeric / v_ev.clawback_months end;
    select coalesce(-sum(a.amount), 0) into v_already
      from billing.commission_adjustment a where a.commission_id = v_ev.id;
    v_back := least(round(v_ev.commission_amt * v_share, 2), v_ev.commission_amt - v_already);
    if v_back > 0 then
      insert into billing.commission_adjustment (commission_id, pipeline_id, institution_id, kind, amount,
                                                 currency, effective_on, reason, calc)
      values (v_ev.id, p_pipeline_id, p_institution_id, 'clawback', -v_back, v_ev.currency, p_closed_on,
              v_reason,
              jsonb_build_object('fee', v_ev.commission_amt, 'mode', v_ev.clawback_mode,
                                 'window_months', v_ev.clawback_months, 'months_run', v_elapsed,
                                 'share', round(v_share, 4), 'clawback', v_back,
                                 'booked_on', v_bk.booked_on, 'closed_on', p_closed_on,
                                 'commission_status', v_ev.status))
      returning * into v_adj;
    end if;
  end if;

  return jsonb_build_object(
    'booking', to_jsonb(v_bk),
    'clawback', case when v_adj.id is not null then jsonb_build_object(
                  'amount', v_adj.amount, 'currency', v_adj.currency, 'effective_on', v_adj.effective_on) end);
end
$$;
revoke all on function marketplace.record_booking_closure(uuid, uuid, date, text, text, uuid, uuid) from public, anon, authenticated;

alter table public.portal_notifications drop constraint if exists portal_notifications_kind_check;
alter table public.portal_notifications add constraint portal_notifications_kind_check check (kind = any (array[
  'bid_accepted', 'bid_rejected', 'bid_expired', 'request_closed', 'pipeline_advanced',
  'pipeline_approved', 'approval_needed', 'compliance_expiring', 'system', 'deal_booked',
  'deal_closed', 'booking_closed']));

-- ── 9. self-check (rolls the whole file back on any mismatch) ────────────────
do $verify$
declare
  v jsonb;
  r billing.commission_rule;
begin
  -- the live default still charges 0.5% of the booked amount
  r := billing.rule_for(gen_random_uuid(), 'home_loan', current_date);
  if r.rate is distinct from 0.005 or r.basis <> 'booked_amount' then raise exception '072: default rule changed'; end if;

  v := billing.compute_fee('booked_amount', 0.005, '{}', null, null, '{"amount": 950000}');
  if (v->>'fee')::numeric <> 4750 then raise exception '072: booked_amount %', v; end if;
  -- MUR 1,000 + 500/month, 1% on 12 months: (1000 + 6000) x 1% = 70, floor 250
  v := billing.compute_fee('annualised', 0.01, '{"months": 12}', 250, null,
                           '{"amount": 1000, "monthly_amount": 500}');
  if (v->>'base')::numeric <> 7000 or (v->>'raw_fee')::numeric <> 70 or (v->>'fee')::numeric <> 250
     or not (v->>'floor_applied')::boolean then raise exception '072: annualised %', v; end if;
  -- 0.2%/yr on 500,000 for 36 months, max 2 years: 500000 x 2 x 0.002 = 2000
  v := billing.compute_fee('amount_x_term', 0.002, '{"max_years": 2}', null, null,
                           '{"amount": 500000, "term_months": 36}');
  if (v->>'fee')::numeric <> 2000 then raise exception '072: amount_x_term %', v; end if;
  -- marginal: 1% to 100k, 0.5% to 500k, 0.25% above; on 600k = 1000 + 2000 + 250 = 3250
  v := billing.compute_fee('tiered', null,
         '{"mode": "marginal", "tiers": [{"up_to": 100000, "rate": 0.01}, {"up_to": 500000, "rate": 0.005}, {"rate": 0.0025}]}',
         null, 3000, '{"amount": 600000}');
  if (v->>'raw_fee')::numeric <> 3250 or (v->>'fee')::numeric <> 3000 or not (v->>'cap_applied')::boolean
     then raise exception '072: tiered marginal %', v; end if;
  -- whole: 600k falls in the last band, 0.25% on all of it = 1500
  v := billing.compute_fee('tiered', null,
         '{"mode": "whole", "tiers": [{"up_to": 100000, "rate": 0.01}, {"up_to": 500000, "rate": 0.005}, {"rate": 0.0025}]}',
         null, null, '{"amount": 600000}');
  if (v->>'fee')::numeric <> 1500 then raise exception '072: tiered whole %', v; end if;
  v := billing.compute_fee('flat', null, '{"amount": 350}', null, null, '{"amount": 1000}');
  if (v->>'fee')::numeric <> 350 then raise exception '072: flat %', v; end if;

  if billing.rule_shape_error('flat', 0.01, '{"amount": 350}', null, null, 0, 'none') is null
     or billing.rule_shape_error('annualised', 0.01, '{}', null, null, 0, 'none') is null
     or billing.rule_shape_error('booked_amount', 0.06, '{}', null, null, 0, 'none') is null
     or billing.rule_shape_error('booked_amount', 0.01, '{}', null, null, 6, 'none') is null
     or billing.rule_shape_error('tiered', null, '{"mode": "marginal", "tiers": [{"up_to": 5, "rate": 0.01}, {"up_to": 4, "rate": 0.01}, {"rate": 0}]}', null, null, 0, 'none') is null
     or billing.rule_shape_error('annualised', 0.01, '{"months": 12}', 250, 100, 0, 'none') is null
  then raise exception '072: a malformed rule was accepted'; end if;
  if billing.rule_shape_error('annualised', 0.01, '{"months": 12, "lump_weight": 0.5}', 250, 5000, 6, 'pro_rata') is not null
  then raise exception '072: a valid rule was refused'; end if;

  begin
    perform billing.compute_fee('annualised', 0.01, '{"months": 12}', null, null, '{"amount": 1000}');
    raise exception '072: missing monthly_amount not refused';
  exception when others then
    if sqlerrm <> 'FEE_INPUT_MISSING:monthly_amount' then raise; end if;
  end;
end
$verify$;

commit;
