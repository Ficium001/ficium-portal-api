-- Ficium Portal DB schema baseline. GENERATED: do not edit by hand.
-- Schema only (no rows), through 075_partitions_and_duplicate_indexes.sql. Regenerate with the
-- schema-baseline workflow on a schema-baseline/* branch; see db/README.md.

--
-- PostgreSQL database dump
--

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: audit; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA audit;

--
-- Name: auth_portal; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA auth_portal;

--
-- Name: autobid; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA autobid;

--
-- Name: billing; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA billing;

--
-- Name: catalog; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA catalog;

--
-- Name: entitlements; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA entitlements;

--
-- Name: governance; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA governance;

--
-- Name: institution; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA institution;

--
-- Name: integration; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA integration;

--
-- Name: marketplace; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA marketplace;

--
-- Name: ops; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA ops;

--
-- Name: portal_admin; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA portal_admin;

--
-- Name: public; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA IF NOT EXISTS public;

--
-- Name: SCHEMA public; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON SCHEMA public IS 'standard public schema';

--
-- Name: inst_role; Type: TYPE; Schema: institution; Owner: -
--

CREATE TYPE institution.inst_role AS ENUM (
    'super_admin',
    'admin',
    'analyst',
    'viewer',
    'compliance',
    'api_operator'
);

--
-- Name: stage_key_enum; Type: TYPE; Schema: institution; Owner: -
--

CREATE TYPE institution.stage_key_enum AS ENUM (
    'credit_docs',
    'offer_letter',
    'legal_review',
    'board_approval',
    'disbursement',
    'custom'
);

--
-- Name: pipeline_status_enum; Type: TYPE; Schema: marketplace; Owner: -
--

CREATE TYPE marketplace.pipeline_status_enum AS ENUM (
    'active',
    'completed',
    'withdrawn',
    'declined'
);

--
-- Name: stage_status_enum; Type: TYPE; Schema: marketplace; Owner: -
--

CREATE TYPE marketplace.stage_status_enum AS ENUM (
    'pending',
    'active',
    'awaiting_approval',
    'completed',
    'skipped',
    'blocked'
);

--
-- Name: action_risk; Type: TYPE; Schema: portal_admin; Owner: -
--

CREATE TYPE portal_admin.action_risk AS ENUM (
    'low',
    'medium',
    'high',
    'critical'
);

--
-- Name: admin_user_status; Type: TYPE; Schema: portal_admin; Owner: -
--

CREATE TYPE portal_admin.admin_user_status AS ENUM (
    'active',
    'locked',
    'suspended',
    'pending_mfa',
    'deactivated'
);

--
-- Name: audit_outcome; Type: TYPE; Schema: portal_admin; Owner: -
--

CREATE TYPE portal_admin.audit_outcome AS ENUM (
    'success',
    'rejected',
    'failed',
    'blocked',
    'expired',
    'logged'
);

--
-- Name: dual_control_status; Type: TYPE; Schema: portal_admin; Owner: -
--

CREATE TYPE portal_admin.dual_control_status AS ENUM (
    'pending',
    'approved',
    'rejected',
    'expired',
    'cancelled',
    'executed'
);

--
-- Name: block_mutation(); Type: FUNCTION; Schema: audit; Owner: -
--

CREATE FUNCTION audit.block_mutation() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog', 'public', 'extensions', 'pg_temp'
    AS $$
BEGIN
  RAISE EXCEPTION 'audit.event is append-only — updates and deletes are not permitted';
END;
$$;

--
-- Name: log(text, uuid, text, uuid, text, uuid, text, jsonb); Type: FUNCTION; Schema: audit; Owner: -
--

CREATE FUNCTION audit.log(p_action text, p_actor_id uuid DEFAULT NULL::uuid, p_actor_type text DEFAULT 'system'::text, p_institution_id uuid DEFAULT NULL::uuid, p_resource_type text DEFAULT NULL::text, p_resource_id uuid DEFAULT NULL::uuid, p_outcome text DEFAULT 'logged'::text, p_metadata jsonb DEFAULT '{}'::jsonb) RETURNS uuid
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public', 'extensions', 'pg_temp'
    AS $$
DECLARE v_id UUID;
BEGIN
  INSERT INTO audit.event
    (action, actor_id, actor_type, institution_id,
     resource_type, resource_id, outcome, metadata)
  VALUES
    (p_action, p_actor_id, p_actor_type, p_institution_id,
     p_resource_type, p_resource_id, p_outcome, p_metadata)
  RETURNING id INTO v_id;
  RETURN v_id;
END;
$$;

--
-- Name: get_member_module_permissions(text, uuid); Type: FUNCTION; Schema: auth_portal; Owner: -
--

CREATE FUNCTION auth_portal.get_member_module_permissions(p_email text, p_institution_id uuid) RETURNS text[]
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'auth_portal', 'institution'
    AS $$
  SELECT COALESCE(g.module_permissions, ARRAY[]::text[])
  FROM institution.member m
  LEFT JOIN institution.group g ON g.id = m.custom_group_id
  WHERE m.email = p_email
    AND m.institution_id = p_institution_id
  LIMIT 1;
$$;

--
-- Name: set_updated_at(); Type: FUNCTION; Schema: auth_portal; Owner: -
--

CREATE FUNCTION auth_portal.set_updated_at() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog', 'public', 'extensions', 'pg_temp'
    AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;

--
-- Name: ensure_execution_partition(date); Type: FUNCTION; Schema: autobid; Owner: -
--

CREATE FUNCTION autobid.ensure_execution_partition(p_month date) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'autobid', 'pg_temp'
    AS $$
declare
  part_start date := date_trunc('month', p_month)::date;
  part_end   date := (date_trunc('month', p_month) + interval '1 month')::date;
  part_name  text := format('execution_%s', to_char(part_start, 'YYYY_MM'));
begin
  if not exists (select 1 from pg_class c join pg_namespace n on n.oid = c.relnamespace
                  where n.nspname = 'autobid' and c.relname = part_name) then
    execute format('create table autobid.%I partition of autobid.execution
                      for values from (%L) to (%L)', part_name, part_start, part_end);
    execute format('alter table autobid.%I enable row level security', part_name);
    execute format('alter table autobid.%I force row level security', part_name);
  end if;
end $$;

--
-- Name: ensure_execution_partitions(); Type: FUNCTION; Schema: autobid; Owner: -
--

CREATE FUNCTION autobid.ensure_execution_partitions() RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'autobid', 'pg_temp'
    AS $$
BEGIN
  PERFORM autobid.ensure_execution_partition(current_date);
  PERFORM autobid.ensure_execution_partition((current_date + interval '1 month')::date);
  PERFORM autobid.ensure_execution_partition((current_date + interval '2 months')::date);
END $$;

--
-- Name: rule_version_freeze(); Type: FUNCTION; Schema: autobid; Owner: -
--

CREATE FUNCTION autobid.rule_version_freeze() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog', 'public', 'extensions', 'pg_temp'
    AS $$
BEGIN
  IF OLD.submitted_at IS NOT NULL AND (
       NEW.predicate  IS DISTINCT FROM OLD.predicate  OR
       NEW.action     IS DISTINCT FROM OLD.action     OR
       NEW.guardrails IS DISTINCT FROM OLD.guardrails OR
       NEW.version_no IS DISTINCT FROM OLD.version_no OR
       NEW.rule_id    IS DISTINCT FROM OLD.rule_id
  ) THEN
    RAISE EXCEPTION 'autobid.rule_version is immutable once submitted (version %)',
      OLD.version_no
      USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  RETURN NEW;
END $$;

--
-- Name: adjustment_is_append_only(); Type: FUNCTION; Schema: billing; Owner: -
--

CREATE FUNCTION billing.adjustment_is_append_only() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO ''
    AS $$
begin
  raise exception using errcode = 'P0001', message = 'COMMISSION_ADJUSTMENT_APPEND_ONLY';
end
$$;

--
-- Name: compute_fee(text, numeric, jsonb, numeric, numeric, jsonb); Type: FUNCTION; Schema: billing; Owner: -
--

CREATE FUNCTION billing.compute_fee(p_basis text, p_rate numeric, p_params jsonb, p_min_fee numeric, p_max_fee numeric, p_in jsonb) RETURNS jsonb
    LANGUAGE plpgsql IMMUTABLE
    SET search_path TO ''
    AS $$
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

--
-- Name: rule_shape_error(text, numeric, jsonb, numeric, numeric, integer, text); Type: FUNCTION; Schema: billing; Owner: -
--

CREATE FUNCTION billing.rule_shape_error(p_basis text, p_rate numeric, p_params jsonb, p_min_fee numeric, p_max_fee numeric, p_clawback_months integer, p_clawback_mode text) RETURNS text
    LANGUAGE plpgsql IMMUTABLE
    SET search_path TO ''
    AS $$
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

SET default_table_access_method = heap;

--
-- Name: commission_rule; Type: TABLE; Schema: billing; Owner: -
--

CREATE TABLE billing.commission_rule (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    institution_id uuid,
    product_code text,
    rate numeric(6,4),
    basis text DEFAULT 'booked_amount'::text NOT NULL,
    effective_from date NOT NULL,
    ended_on date,
    reason text NOT NULL,
    command_id uuid NOT NULL,
    set_by uuid NOT NULL,
    approved_by uuid NOT NULL,
    ended_by uuid,
    end_command_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    params jsonb DEFAULT '{}'::jsonb NOT NULL,
    min_fee numeric(15,2),
    max_fee numeric(15,2),
    currency character(3) DEFAULT 'MUR'::bpchar NOT NULL,
    clawback_months smallint DEFAULT 0 NOT NULL,
    clawback_mode text DEFAULT 'none'::text NOT NULL,
    CONSTRAINT commission_rule_check CHECK (((ended_on IS NULL) OR (ended_on > effective_from))),
    CONSTRAINT commission_rule_currency_check CHECK ((currency ~ '^[A-Z]{3}$'::text)),
    CONSTRAINT commission_rule_reason_check CHECK ((char_length(btrim(reason)) >= 10)),
    CONSTRAINT commission_rule_shape CHECK ((billing.rule_shape_error(basis, rate, params, min_fee, max_fee, (clawback_months)::integer, clawback_mode) IS NULL))
);

ALTER TABLE ONLY billing.commission_rule FORCE ROW LEVEL SECURITY;

--
-- Name: end_commission_rule(uuid, date, uuid, uuid, uuid); Type: FUNCTION; Schema: billing; Owner: -
--

CREATE FUNCTION billing.end_commission_rule(p_rule_id uuid, p_ended_on date, p_command_id uuid, p_maker uuid, p_checker uuid) RETURNS billing.commission_rule
    LANGUAGE plpgsql
    SET search_path TO ''
    AS $$
declare
  v_today date := (now() at time zone 'Indian/Mauritius')::date;
  v_row   billing.commission_rule;
begin
  if p_maker = p_checker then
    raise exception using errcode = 'P0001', message = 'COMMISSION_FOUR_EYES';
  end if;
  select * into v_row from billing.commission_rule where id = p_rule_id for update;
  if not found or v_row.ended_on is not null then
    raise exception using errcode = 'P0001', message = 'COMMISSION_RULE_NOT_OPEN';
  end if;
  if v_row.institution_id is null and v_row.product_code is null then
    raise exception using errcode = 'P0001', message = 'COMMISSION_DEFAULT_CANNOT_END';
  end if;
  if p_ended_on is null or p_ended_on < v_today or p_ended_on <= v_row.effective_from then
    raise exception using errcode = 'P0001', message = 'COMMISSION_DATE_IN_PAST';
  end if;
  update billing.commission_rule
     set ended_on = p_ended_on, ended_by = p_checker, end_command_id = p_command_id
   where id = p_rule_id
  returning * into v_row;
  return v_row;
end
$$;

--
-- Name: json_num(jsonb); Type: FUNCTION; Schema: billing; Owner: -
--

CREATE FUNCTION billing.json_num(p jsonb) RETURNS numeric
    LANGUAGE sql IMMUTABLE
    SET search_path TO ''
    AS $$ select case when jsonb_typeof(p) = 'number' then (p #>> '{}')::numeric end $$;

--
-- Name: rule_for(uuid, text, date); Type: FUNCTION; Schema: billing; Owner: -
--

CREATE FUNCTION billing.rule_for(p_institution_id uuid, p_product_code text, p_on date) RETURNS billing.commission_rule
    LANGUAGE sql STABLE
    SET search_path TO ''
    AS $$
  select r.*
    from billing.commission_rule r
   where r.effective_from <= p_on
     and (r.ended_on is null or r.ended_on > p_on)
     and (r.institution_id is null or r.institution_id = p_institution_id)
     and (r.product_code   is null or r.product_code   = p_product_code)
   order by (r.institution_id is not null) desc, (r.product_code is not null) desc, r.effective_from desc
   limit 1
$$;

--
-- Name: set_commission_rule(uuid, text, numeric, date, text, uuid, uuid, uuid, text, jsonb, numeric, numeric, integer, text); Type: FUNCTION; Schema: billing; Owner: -
--

CREATE FUNCTION billing.set_commission_rule(p_institution_id uuid, p_product_code text, p_rate numeric, p_effective_from date, p_reason text, p_command_id uuid, p_maker uuid, p_checker uuid, p_basis text DEFAULT 'booked_amount'::text, p_params jsonb DEFAULT '{}'::jsonb, p_min_fee numeric DEFAULT NULL::numeric, p_max_fee numeric DEFAULT NULL::numeric, p_clawback_months integer DEFAULT 0, p_clawback_mode text DEFAULT 'none'::text) RETURNS billing.commission_rule
    LANGUAGE plpgsql
    SET search_path TO ''
    AS $$
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

--
-- Name: product_id_for_app_type(text); Type: FUNCTION; Schema: catalog; Owner: -
--

CREATE FUNCTION catalog.product_id_for_app_type(p_app_type text) RETURNS uuid
    LANGUAGE sql STABLE
    SET search_path TO 'catalog', 'public'
    AS $$
  SELECT p.id FROM catalog.product p
  WHERE p.code = CASE p_app_type
    WHEN 'mortgage'            THEN 'home_loan'
    WHEN 'personal_loan'       THEN 'personal_loan'
    WHEN 'sme_loan'            THEN 'business_loan'
    WHEN 'business_loan'       THEN 'business_loan'
    WHEN 'business_account'    THEN 'business_loan'
    WHEN 'fixed_deposit'       THEN 'deposit'
    WHEN 'savings_account'     THEN 'savings'
    WHEN 'investment_account'  THEN 'savings'
    WHEN 'credit_card'         THEN 'credit_card'
    WHEN 'education_loan'      THEN 'education_loan'
    WHEN 'vehicle_loan'        THEN 'vehicle_loan'
    WHEN 'leasing'             THEN 'vehicle_loan'
    WHEN 'equities'            THEN 'equities'
    WHEN 'unit_trust'          THEN 'unit_trust'
    WHEN 'government_bonds'    THEN 'government_bonds'
    WHEN 'offshore_investment' THEN 'offshore_investment'
    WHEN 'savings_plan'        THEN 'savings_plan'
    WHEN 'mixed_portfolio'     THEN 'mixed_portfolio'
    ELSE 'personal_loan'
  END
  LIMIT 1;
$$;

--
-- Name: ensure_usage_partition(date); Type: FUNCTION; Schema: entitlements; Owner: -
--

CREATE FUNCTION entitlements.ensure_usage_partition(p_month date) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'entitlements', 'pg_temp'
    AS $$
declare
  part_start date := date_trunc('month', p_month)::date;
  part_end   date := (date_trunc('month', p_month) + interval '1 month')::date;
  part_name  text := format('usage_event_%s', to_char(part_start, 'YYYY_MM'));
begin
  if not exists (select 1 from pg_class c join pg_namespace n on n.oid = c.relnamespace
                  where n.nspname = 'entitlements' and c.relname = part_name) then
    execute format('create table entitlements.%I partition of entitlements.usage_event
                      for values from (%L) to (%L)', part_name, part_start, part_end);
    execute format('alter table entitlements.%I enable row level security', part_name);
    execute format('alter table entitlements.%I force row level security', part_name);
  end if;
end $$;

--
-- Name: ensure_usage_partitions(); Type: FUNCTION; Schema: entitlements; Owner: -
--

CREATE FUNCTION entitlements.ensure_usage_partitions() RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'entitlements', 'pg_temp'
    AS $$
BEGIN
  PERFORM entitlements.ensure_usage_partition(current_date);
  PERFORM entitlements.ensure_usage_partition((current_date + interval '1 month')::date);
  PERFORM entitlements.ensure_usage_partition((current_date + interval '2 months')::date);
END $$;

--
-- Name: _group_label(text); Type: FUNCTION; Schema: governance; Owner: -
--

CREATE FUNCTION governance._group_label(p_id text) RETURNS text
    LANGUAGE sql STABLE
    SET search_path TO 'pg_catalog'
    AS $$
  select coalesce((select g.label from institution."group" g where g.id::text = p_id), case when p_id is null then 'none' else 'unknown group' end)
$$;

--
-- Name: _module_label(text); Type: FUNCTION; Schema: governance; Owner: -
--

CREATE FUNCTION governance._module_label(k text) RETURNS text
    LANGUAGE sql STABLE
    SET search_path TO 'pg_catalog'
    AS $$
  select coalesce(
    (select m.label from catalog.module m where m.key = regexp_replace(k, '^inst:', '')),
    initcap(replace(regexp_replace(k, '^inst:', ''), '_', ' ')))
$$;

--
-- Name: _modules(jsonb); Type: FUNCTION; Schema: governance; Owner: -
--

CREATE FUNCTION governance._modules(a jsonb) RETURNS text
    LANGUAGE sql STABLE
    SET search_path TO 'pg_catalog'
    AS $$
  select coalesce(nullif(string_agg(governance._module_label(x), ', ' order by governance._module_label(x)), ''), 'none')
    from jsonb_array_elements_text(coalesce(a, '[]'::jsonb)) x
$$;

--
-- Name: _money(text); Type: FUNCTION; Schema: governance; Owner: -
--

CREATE FUNCTION governance._money(v text) RETURNS text
    LANGUAGE sql IMMUTABLE
    SET search_path TO 'pg_catalog'
    AS $$
  select case when v is null or v = '' then null
              else 'MUR ' || to_char(v::numeric, 'FM999,999,999,990.00') end
$$;

--
-- Name: _products(jsonb); Type: FUNCTION; Schema: governance; Owner: -
--

CREATE FUNCTION governance._products(a jsonb) RETURNS text
    LANGUAGE sql STABLE
    SET search_path TO 'pg_catalog'
    AS $$
  select case when a is null or jsonb_typeof(a) <> 'array' or jsonb_array_length(a) = 0 then 'All products'
    else coalesce(string_agg(coalesce(p.label, x), ', ' order by coalesce(p.label, x)), 'All products') end
    from jsonb_array_elements_text(coalesce(nullif(a, 'null'::jsonb), '[]'::jsonb)) x
    left join catalog.product p on p.id::text = x
$$;

--
-- Name: _row(text, text, text); Type: FUNCTION; Schema: governance; Owner: -
--

CREATE FUNCTION governance._row(f text, b text, a text) RETURNS jsonb
    LANGUAGE sql IMMUTABLE
    SET search_path TO 'pg_catalog'
    AS $$
  select jsonb_build_object('field', f, 'before', b, 'after', a)
$$;

--
-- Name: describe_change(text, uuid, jsonb); Type: FUNCTION; Schema: governance; Owner: -
--

CREATE FUNCTION governance.describe_change(p_category text, p_institution uuid, p_payload jsonb) RETURNS jsonb
    LANGUAGE plpgsql STABLE SECURITY DEFINER
    SET search_path TO 'pg_catalog'
    AS $$
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

  select coalesce(jsonb_agg(r), '[]') into v_rows from jsonb_array_elements(v_rows) r
   where coalesce(r->>'before', r->>'after') is not null;
  return jsonb_build_object('label', v_label, 'before', v_before, 'rows', v_rows, 'described', true);
exception when others then
  select coalesce(jsonb_agg(governance._row(initcap(replace(key, '_', ' ')), null, value #>> '{}')), '[]') into v_rows
    from jsonb_each(p) where jsonb_typeof(value) in ('string','number','boolean') and key not in ('institution_id','submitted_via');
  return jsonb_build_object('label', null, 'before', null, 'rows', v_rows, 'described', false, 'error', sqlerrm);
end $$;

--
-- Name: enable_four_eyes(text); Type: FUNCTION; Schema: governance; Owner: -
--

CREATE FUNCTION governance.enable_four_eyes(p_category text) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
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

--
-- Name: execute(uuid); Type: FUNCTION; Schema: governance; Owner: -
--

CREATE FUNCTION governance.execute(p_action_id uuid) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $_$
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
$_$;

--
-- Name: expire_stale_actions(); Type: FUNCTION; Schema: governance; Owner: -
--

CREATE FUNCTION governance.expire_stale_actions() RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'governance', 'audit', 'public'
    AS $$
DECLARE n integer;
BEGIN
  WITH expired AS (
    UPDATE governance.action
       SET status = 'expired', updated_at = now()
     WHERE status = 'pending' AND expires_at IS NOT NULL AND expires_at < now()
    RETURNING id, category, institution_id, resource_type, resource_id, maker_id, expires_at
  ), logged AS (
    INSERT INTO audit.event
      (actor_type, institution_id, action, resource_type, resource_id,
       outcome, outcome_note, governance_action_id, metadata)
    SELECT 'system', e.institution_id, e.category || '.expired', e.resource_type, e.resource_id,
           'expired', 'Expired without a second approver', e.id,
           jsonb_build_object('maker_id', e.maker_id, 'expires_at', e.expires_at)
      FROM expired e
    RETURNING 1
  )
  SELECT count(*) INTO n FROM expired;
  RETURN n;
END
$$;

--
-- Name: four_eyes_required(uuid, text); Type: FUNCTION; Schema: governance; Owner: -
--

CREATE FUNCTION governance.four_eyes_required(p_institution uuid, p_category text) RETURNS boolean
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO ''
    AS $$
  select coalesce(
    (select case when c.locked then true else coalesce(p.enabled, c.default_on) end
       from governance.four_eyes_catalog c
       left join governance.four_eyes_policy p
         on p.institution_id = p_institution and p.category = c.category
      where c.category = p_category),
    true)
$$;

--
-- Name: guard_member_action(); Type: FUNCTION; Schema: governance; Owner: -
--

CREATE FUNCTION governance.guard_member_action() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
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

--
-- Name: submit(text, text, uuid, jsonb, text, text); Type: FUNCTION; Schema: governance; Owner: -
--

CREATE FUNCTION governance.submit(p_category text, p_resource_type text, p_resource_id uuid, p_payload jsonb, p_label text DEFAULT ''::text, p_risk text DEFAULT 'medium'::text) RETURNS uuid
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'institution', 'governance', 'admin'
    AS $$
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
$$;

--
-- Name: pending_actions; Type: TABLE; Schema: institution; Owner: -
--

CREATE TABLE institution.pending_actions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    action_category text NOT NULL,
    action_status text DEFAULT 'pending'::text NOT NULL,
    maker_id uuid NOT NULL,
    maker_role text DEFAULT ''::text NOT NULL,
    institution_id uuid NOT NULL,
    initiated_at timestamp with time zone DEFAULT now() NOT NULL,
    resource_type text NOT NULL,
    resource_id uuid,
    payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    payload_before jsonb,
    checker_id uuid,
    checker_role text,
    checker_note text,
    checked_at timestamp with time zone,
    expires_at timestamp with time zone DEFAULT (now() + '7 days'::interval) NOT NULL,
    execution_status text DEFAULT 'pending'::text,
    executed_at timestamp with time zone,
    execution_error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT pending_actions_action_status_check CHECK ((action_status = ANY (ARRAY['pending'::text, 'approved'::text, 'rejected'::text, 'expired'::text, 'cancelled'::text]))),
    CONSTRAINT pending_actions_execution_status_check CHECK ((execution_status = ANY (ARRAY['pending'::text, 'executing'::text, 'executed'::text, 'failed'::text])))
);

ALTER TABLE ONLY institution.pending_actions FORCE ROW LEVEL SECURITY;

--
-- Name: _call_provision_user(institution.pending_actions); Type: FUNCTION; Schema: institution; Owner: -
--

CREATE FUNCTION institution._call_provision_user(p_action institution.pending_actions) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'institution', 'extensions'
    AS $$
DECLARE
  v_url  TEXT;
  v_body JSONB;
BEGIN
  v_url := current_setting('app.supabase_url', true)
    || '/functions/v1/provision-institution-user';

  v_body := jsonb_build_object(
    'action_id',       p_action.id,
    'institution_id',  p_action.institution_id,
    'email',           p_action.payload->>'email',
    'first_name',      p_action.payload->>'first_name',
    'last_name',       p_action.payload->>'last_name',
    'custom_group_id', p_action.payload->>'custom_group_id',
    'member_role',     p_action.payload->>'member_role'
  );

  PERFORM extensions.http_post(
    url     := v_url,
    body    := v_body::TEXT,
    headers := jsonb_build_object(
      'Content-Type',  'application/json',
      'Authorization', 'Bearer ' || current_setting('app.service_role_key', true)
    )
  );
END;
$$;

--
-- Name: _chain_audit_events(); Type: FUNCTION; Schema: institution; Owner: -
--

CREATE FUNCTION institution._chain_audit_events() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog', 'public', 'extensions', 'pg_temp'
    AS $$
DECLARE last_hash bytea;
BEGIN
  SELECT row_hash INTO last_hash FROM institution.audit_events
  ORDER BY created_at DESC, ctid DESC LIMIT 1;
  NEW.prev_hash := last_hash;
  NEW.row_hash  := digest(
    convert_to(
      coalesce(encode(last_hash,'hex'),'') || (to_jsonb(NEW) - 'row_hash')::text,
      'UTF8'
    ), 'sha256'
  );
  RETURN NEW;
END $$;

--
-- Name: _execute_action(uuid); Type: FUNCTION; Schema: institution; Owner: -
--

CREATE FUNCTION institution._execute_action(p_action_id uuid) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public', 'extensions', 'pg_temp'
    AS $_$
DECLARE
  a          governance.action%ROWTYPE;
  v_group_id UUID;
  v_members  INT;
  v_bid_id   UUID;
  v_product_id UUID;
  v_product_scope UUID[];
  v_invalid_product UUID;
  v_alloc    jsonb;
BEGIN
  SELECT * INTO a FROM governance.action WHERE id = p_action_id;
  IF NOT FOUND THEN RAISE EXCEPTION 'Action % not found', p_action_id; END IF;

  CASE a.category

    WHEN 'group.create' THEN
      IF (a.payload->>'slug') !~ '^[a-z0-9_]{2,40}$' THEN
        RAISE EXCEPTION 'Invalid group slug';
      END IF;

      v_product_scope := NULL;
      IF a.payload ? 'product_scope' AND jsonb_typeof(a.payload->'product_scope') = 'array' THEN
        v_product_scope := ARRAY(SELECT (jsonb_array_elements_text(a.payload->'product_scope'))::UUID);
        IF array_length(v_product_scope, 1) IS NOT NULL THEN
          SELECT ps INTO v_invalid_product
          FROM unnest(v_product_scope) ps
          WHERE NOT EXISTS (
            SELECT 1 FROM institution.product_config pc
            WHERE pc.institution_id = a.institution_id
              AND pc.product_id = ps
              AND pc.enabled = true
          )
          LIMIT 1;
          IF v_invalid_product IS NOT NULL THEN
            RAISE EXCEPTION 'Product % is not licensed for this institution', v_invalid_product;
          END IF;
        END IF;
      END IF;

      INSERT INTO institution."group"
        (institution_id, slug, label, description, module_permissions, product_scope, created_by)
      VALUES (
        a.institution_id, a.payload->>'slug',
        COALESCE(a.payload->>'label', a.payload->>'slug'),
        COALESCE(a.payload->>'description', ''),
        COALESCE(ARRAY(SELECT jsonb_array_elements_text(a.payload->'module_permissions')), '{}'::TEXT[]),
        v_product_scope,
        a.maker_id
      );

    WHEN 'group.update_modules' THEN
      v_product_scope := NULL;
      IF a.payload ? 'product_scope' AND jsonb_typeof(a.payload->'product_scope') = 'array' THEN
        v_product_scope := ARRAY(SELECT (jsonb_array_elements_text(a.payload->'product_scope'))::UUID);
        IF array_length(v_product_scope, 1) IS NOT NULL THEN
          SELECT ps INTO v_invalid_product
          FROM unnest(v_product_scope) ps
          WHERE NOT EXISTS (
            SELECT 1 FROM institution.product_config pc
            WHERE pc.institution_id = a.institution_id
              AND pc.product_id = ps
              AND pc.enabled = true
          )
          LIMIT 1;
          IF v_invalid_product IS NOT NULL THEN
            RAISE EXCEPTION 'Product % is not licensed for this institution', v_invalid_product;
          END IF;
        END IF;
      END IF;

      UPDATE institution."group"
      SET module_permissions = ARRAY(SELECT jsonb_array_elements_text(a.payload->'module_permissions')),
          product_scope = CASE
            WHEN a.payload ? 'product_scope' THEN v_product_scope
            ELSE product_scope   -- key absent in payload => leave untouched
          END
      WHERE id = (a.payload->>'group_id')::UUID AND institution_id = a.institution_id;
      IF NOT FOUND THEN RAISE EXCEPTION 'Group not found in this institution'; END IF;

    WHEN 'group.delete' THEN
      v_group_id := (a.payload->>'group_id')::UUID;
      SELECT count(*) INTO v_members FROM institution.member WHERE custom_group_id = v_group_id;
      IF v_members > 0 THEN
        RAISE EXCEPTION 'Group has % member(s) — reassign them first', v_members;
      END IF;
      DELETE FROM institution."group" WHERE id = v_group_id AND institution_id = a.institution_id;
      IF NOT FOUND THEN RAISE EXCEPTION 'Group not found in this institution'; END IF;

    WHEN 'user.create' THEN
      IF (a.payload->>'email') IS NULL THEN
        RAISE EXCEPTION 'user.create requires email in payload';
      END IF;
      IF (a.payload->>'custom_group_id') IS NULL THEN
        RAISE EXCEPTION 'user.create requires custom_group_id in payload';
      END IF;
      IF NOT EXISTS (
        SELECT 1 FROM institution."group"
        WHERE id = (a.payload->>'custom_group_id')::UUID AND institution_id = a.institution_id
      ) THEN
        RAISE EXCEPTION 'Group not found in this institution';
      END IF;

    WHEN 'user.assign_group' THEN
      IF NOT EXISTS (
        SELECT 1 FROM institution.member
        WHERE id = (a.payload->>'member_id')::UUID AND institution_id = a.institution_id
      ) THEN
        RAISE EXCEPTION 'Member not found in this institution';
      END IF;
      IF NOT EXISTS (
        SELECT 1 FROM institution."group"
        WHERE id = (a.payload->>'custom_group_id')::UUID AND institution_id = a.institution_id
      ) THEN
        RAISE EXCEPTION 'Group not found in this institution';
      END IF;
      UPDATE institution.member
      SET custom_group_id = (a.payload->>'custom_group_id')::UUID,
          member_role     = COALESCE(a.payload->>'member_role', member_role)
      WHERE id = (a.payload->>'member_id')::UUID AND institution_id = a.institution_id;

    WHEN 'bid.submit' THEN
      SELECT product_id INTO v_product_id
      FROM marketplace.request
      WHERE id = (a.payload->>'request_id')::UUID;

      INSERT INTO marketplace.bid
        (request_id, institution_id, rate, rate_type, amount_offered,
         term_months, conditions, submitted_via)
      VALUES (
        (a.payload->>'request_id')::UUID, a.institution_id,
        (a.payload->>'rate')::NUMERIC, COALESCE(a.payload->>'rate_type', 'fixed'),
        (a.payload->>'amount_offered')::NUMERIC, (a.payload->>'term_months')::INTEGER,
        COALESCE(a.payload->'conditions', '{}'::jsonb),
        COALESCE(a.payload->>'submitted_via', 'portal')
      ) RETURNING id INTO v_bid_id;
      -- Benefits are snapshotted by trigger trg_snapshot_bid_benefits (db/028).

      -- Structured per-product counter-allocation, for baskets (mixed_portfolio
      -- requests). Only present when the bidding officer specified a line-by-line
      -- split rather than the single blended rate/amount above.
      IF a.payload ? 'allocations' AND jsonb_typeof(a.payload->'allocations') = 'array' THEN
        FOR v_alloc IN SELECT * FROM jsonb_array_elements(a.payload->'allocations')
        LOOP
          INSERT INTO marketplace.bid_allocation (bid_id, product_id, amount_offered, rate, term_months)
          VALUES (
            v_bid_id,
            catalog.product_id_for_app_type(v_alloc ->> 'product_type'),
            (v_alloc ->> 'amount_offered')::NUMERIC,
            NULLIF(v_alloc ->> 'rate', '')::NUMERIC,
            NULLIF(v_alloc ->> 'term_months', '')::INTEGER
          );
        END LOOP;
      END IF;

    WHEN 'bid.withdraw' THEN
      UPDATE marketplace.bid
      SET status = 'withdrawn', withdrawn_at = now(), withdraw_reason = a.payload->>'reason'
      WHERE id = (a.payload->>'bid_id')::UUID AND institution_id = a.institution_id;

    WHEN 'benefit.create' THEN
      IF NULLIF(a.payload->>'cat_id', '') IS NULL THEN
        RAISE EXCEPTION 'benefit.create requires cat_id in payload';
      END IF;
      INSERT INTO institution.benefit (
        institution_id, product_id, cat_id,
        title, description, value_display, value_amount, value_unit,
        is_guaranteed, conditions, valid_from, valid_until,
        is_active, created_by
      ) VALUES (
        a.institution_id,
        NULLIF(a.payload->>'product_id', '')::UUID,
        NULLIF(a.payload->>'cat_id', '')::UUID,
        a.payload->>'title',
        NULLIF(a.payload->>'description', ''),
        NULLIF(a.payload->>'value_display', ''),
        NULLIF(a.payload->>'value_amount', '')::NUMERIC,
        NULLIF(a.payload->>'value_unit', ''),
        true,
        NULLIF(a.payload->>'conditions', ''),
        NULLIF(a.payload->>'valid_from',  '')::DATE,
        NULLIF(a.payload->>'valid_until', '')::DATE,
        true,
        a.maker_id
      );

    WHEN 'benefit.update' THEN
      UPDATE institution.benefit
      SET
        cat_id        = COALESCE(NULLIF(a.payload->>'cat_id',        '')::UUID, cat_id),
        title         = COALESCE(NULLIF(a.payload->>'title',         ''),       title),
        description   = NULLIF(a.payload->>'description',   ''),
        value_display = NULLIF(a.payload->>'value_display',  ''),
        value_amount  = NULLIF(a.payload->>'value_amount', '')::NUMERIC,
        value_unit    = NULLIF(a.payload->>'value_unit', ''),
        conditions    = NULLIF(a.payload->>'conditions',     ''),
        valid_from    = NULLIF(a.payload->>'valid_from',     '')::DATE,
        valid_until   = NULLIF(a.payload->>'valid_until',    '')::DATE,
        updated_at    = now()
      WHERE id = (a.payload->>'benefit_id')::UUID
        AND institution_id = a.institution_id;
      IF NOT FOUND THEN RAISE EXCEPTION 'Benefit not found'; END IF;

    WHEN 'benefit.delete' THEN
      UPDATE institution.benefit
      SET is_active = false, updated_at = now()
      WHERE id = (a.payload->>'benefit_id')::UUID
        AND institution_id = a.institution_id;
      IF NOT FOUND THEN RAISE EXCEPTION 'Benefit not found'; END IF;

    ELSE
      RAISE EXCEPTION 'No executor for category %', a.category;
  END CASE;
END;
$_$;

--
-- Name: approvals_advance(uuid, integer); Type: FUNCTION; Schema: institution; Owner: -
--

CREATE FUNCTION institution.approvals_advance(p_instance uuid, p_done_seq integer) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'institution', 'public'
    AS $$
DECLARE v_next record; v record;
BEGIN
  SELECT i.*, i.institution_id AS iid INTO v FROM approval_instance i WHERE i.id = p_instance FOR UPDATE;

  SELECT si.*, sd.sla_hours INTO v_next
  FROM approval_stage_instance si
  JOIN approval_stage_def sd ON sd.id = si.stage_def_id
  WHERE si.instance_id = p_instance AND si.seq = p_done_seq + 1;

  IF FOUND THEN
    UPDATE approval_stage_instance
       SET status = 'active', started_at = now(),
           due_at = CASE WHEN v_next.sla_hours IS NOT NULL
                         THEN now() + make_interval(hours => v_next.sla_hours) END
     WHERE id = v_next.id;
    UPDATE approval_instance SET current_seq = p_done_seq + 1 WHERE id = p_instance;
  ELSE
    UPDATE approval_instance SET status = 'approved', resolved_at = now() WHERE id = p_instance;
    INSERT INTO approval_outbox (institution_id, event, payload)
    VALUES (v.iid, 'approval.completed',
            jsonb_build_object('instance_id', p_instance,
                               'entity_type', v.entity_type, 'entity_id', v.entity_id));
  END IF;
END $$;

--
-- Name: approvals_cast(uuid, uuid, text, text, jsonb, uuid); Type: FUNCTION; Schema: institution; Owner: -
--

CREATE FUNCTION institution.approvals_cast(p_stage_instance uuid, p_actor uuid, p_action text, p_comment text DEFAULT NULL::text, p_checklist jsonb DEFAULT NULL::jsonb, p_acting_as uuid DEFAULT NULL::uuid) RETURNS text
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'institution', 'public'
    AS $$
DECLARE v record; v_effective uuid; v_result text;
BEGIN
  SELECT si.id AS stage_id, si.status AS stage_status, si.seq,
         sd.stage_type, sd.committee_id, sd.checklist AS checklist_def,
         i.id AS instance_id, i.status AS inst_status,
         i.entity_maker_id, i.institution_id
    INTO v
  FROM approval_stage_instance si
  JOIN approval_stage_def sd ON sd.id = si.stage_def_id
  JOIN approval_instance i ON i.id = si.instance_id
  WHERE si.id = p_stage_instance
  FOR UPDATE OF si;

  IF NOT FOUND THEN RAISE EXCEPTION 'APPROVALS_STAGE_NOT_FOUND'; END IF;
  IF v.inst_status <> 'in_progress' OR v.stage_status <> 'active' THEN
    RAISE EXCEPTION 'APPROVALS_STAGE_NOT_ACTIVE';
  END IF;

  v_effective := COALESCE(p_acting_as, p_actor);

  IF p_acting_as IS NOT NULL THEN
    PERFORM 1 FROM approval_delegation d
    WHERE d.institution_id = v.institution_id
      AND d.from_member = p_acting_as AND d.to_member = p_actor
      AND now() BETWEEN d.valid_from AND d.valid_to
      AND (d.scope = 'all' OR d.scope = v.committee_id::text
           OR d.scope IN (SELECT template_id::text FROM approval_instance WHERE id = v.instance_id));
    IF NOT FOUND THEN RAISE EXCEPTION 'APPROVALS_DELEGATION_INVALID'; END IF;
  END IF;

  IF p_action IN ('approve','reject','abstain') THEN
    IF v_effective = v.entity_maker_id OR p_actor = v.entity_maker_id THEN
      RAISE EXCEPTION 'APPROVALS_MAKER_RECUSED';
    END IF;
    IF NOT institution.approvals_is_eligible(p_stage_instance, v_effective) THEN
      RAISE EXCEPTION 'APPROVALS_NOT_ELIGIBLE';
    END IF;
    IF p_action IN ('reject') AND (p_comment IS NULL OR length(trim(p_comment)) = 0) THEN
      RAISE EXCEPTION 'APPROVALS_COMMENT_REQUIRED';
    END IF;
  END IF;

  INSERT INTO approval_action (stage_instance_id, actor_id, acting_as, action, comment, checklist_state)
  VALUES (p_stage_instance, p_actor, p_acting_as, p_action, p_comment, p_checklist);

  v_result := institution.approvals_evaluate_stage(p_stage_instance);
  RETURN v_result;
END $$;

--
-- Name: approvals_evaluate_stage(uuid); Type: FUNCTION; Schema: institution; Owner: -
--

CREATE FUNCTION institution.approvals_evaluate_stage(p_stage_instance uuid) RETURNS text
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'institution', 'public'
    AS $$
DECLARE
  v record; c record;
  n_approve int; n_reject int; n_eligible int; needed int;
  chair_vote text; v_new text := 'active'; v_checked int; v_required int;
BEGIN
  SELECT si.id, si.seq, si.instance_id, sd.stage_type, sd.committee_id,
         sd.checklist AS checklist_def, i.institution_id, i.entity_type, i.entity_id
    INTO v
  FROM approval_stage_instance si
  JOIN approval_stage_def sd ON sd.id = si.stage_def_id
  JOIN approval_instance i ON i.id = si.instance_id
  WHERE si.id = p_stage_instance FOR UPDATE OF si;

  SELECT count(*) FILTER (WHERE action = 'approve'),
         count(*) FILTER (WHERE action = 'reject')
    INTO n_approve, n_reject
  FROM approval_action WHERE stage_instance_id = p_stage_instance;

  IF v.stage_type = 'single' THEN
    IF n_reject > 0 THEN v_new := 'rejected';
    ELSIF n_approve >= 1 THEN v_new := 'approved'; END IF;

  ELSIF v.stage_type = 'dual' THEN
    IF n_reject > 0 THEN v_new := 'rejected';
    ELSIF n_approve >= 2 THEN v_new := 'approved'; END IF;

  ELSIF v.stage_type = 'committee' THEN
    SELECT * INTO c FROM approval_committee WHERE id = v.committee_id;
    SELECT count(*) INTO n_eligible FROM committee_member cm
    WHERE cm.committee_id = v.committee_id AND cm.is_voting
      AND cm.valid_from <= current_date
      AND (cm.valid_to IS NULL OR cm.valid_to >= current_date);

    needed := CASE c.quorum_type
      WHEN 'count'     THEN c.quorum_value::int
      WHEN 'fraction'  THEN GREATEST(1, ceil(n_eligible * c.quorum_value)::int)
      WHEN 'unanimous' THEN n_eligible END;

    IF n_approve >= needed THEN v_new := 'approved';
    ELSIF n_reject >= needed OR (c.quorum_type = 'unanimous' AND n_reject > 0) THEN
      v_new := 'rejected';
    ELSIF n_approve + n_reject >= n_eligible AND n_eligible > 0 THEN
      IF c.tie_break = 'chair' THEN
        SELECT a.action INTO chair_vote
        FROM approval_action a
        JOIN committee_member cm ON cm.member_id = COALESCE(a.acting_as, a.actor_id)
        WHERE a.stage_instance_id = p_stage_instance
          AND cm.committee_id = v.committee_id AND cm.role = 'chair'
          AND a.action IN ('approve','reject')
        ORDER BY a.created_at DESC LIMIT 1;
        v_new := CASE WHEN chair_vote = 'approve' THEN 'approved' ELSE 'rejected' END;
      ELSIF c.tie_break = 'reject' THEN v_new := 'rejected';
      ELSE v_new := 'rejected';
      END IF;
    END IF;

  ELSIF v.stage_type = 'checklist' THEN
    SELECT count(*) INTO v_required
    FROM jsonb_array_elements(v.checklist_def) e WHERE (e->>'required')::boolean;
    SELECT count(DISTINCT e.key) INTO v_checked
    FROM approval_action a,
         LATERAL jsonb_each_text(COALESCE(a.checklist_state,'{}'::jsonb)) e(key, val)
    WHERE a.stage_instance_id = p_stage_instance AND e.val = 'true';
    IF n_reject > 0 THEN v_new := 'rejected';
    ELSIF n_approve >= 1 AND v_checked >= v_required THEN v_new := 'approved'; END IF;

  ELSIF v.stage_type = 'external_hold' THEN
    IF n_reject > 0 THEN v_new := 'rejected';
    ELSIF n_approve >= 1 THEN v_new := 'approved'; END IF;
  END IF;

  IF v_new = 'active' THEN RETURN 'active'; END IF;

  UPDATE approval_stage_instance
     SET status = v_new, resolved_at = now() WHERE id = p_stage_instance;

  INSERT INTO approval_outbox (institution_id, event, payload)
  VALUES (v.institution_id, 'approval.stage.completed',
          jsonb_build_object('instance_id', v.instance_id, 'seq', v.seq, 'outcome', v_new));

  IF v_new = 'rejected' THEN
    UPDATE approval_instance SET status = 'rejected', resolved_at = now()
     WHERE id = v.instance_id;
    INSERT INTO approval_outbox (institution_id, event, payload)
    VALUES (v.institution_id, 'approval.rejected',
            jsonb_build_object('instance_id', v.instance_id,
                               'entity_type', v.entity_type, 'entity_id', v.entity_id));
    RETURN 'rejected';
  END IF;

  PERFORM institution.approvals_advance(v.instance_id, v.seq);
  RETURN 'approved';
END $$;

--
-- Name: approvals_expire_overdue(); Type: FUNCTION; Schema: institution; Owner: -
--

CREATE FUNCTION institution.approvals_expire_overdue() RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'institution', 'public'
    AS $$
DECLARE r record; n int := 0;
BEGIN
  FOR r IN
    SELECT si.id AS stage_id, si.instance_id, sd.on_sla_breach,
           sd.escalate_to_template_id, i.institution_id, i.entity_type, i.entity_id
    FROM approval_stage_instance si
    JOIN approval_stage_def sd ON sd.id = si.stage_def_id
    JOIN approval_instance i ON i.id = si.instance_id
    WHERE si.status = 'active' AND si.due_at IS NOT NULL AND si.due_at < now()
      AND i.status = 'in_progress'
    FOR UPDATE OF si SKIP LOCKED
  LOOP
    n := n + 1;
    IF r.on_sla_breach = 'notify' THEN
      UPDATE approval_stage_instance SET due_at = now() + interval '1 hour'
       WHERE id = r.stage_id;
      INSERT INTO approval_outbox (institution_id, event, payload)
      VALUES (r.institution_id, 'approval.sla.breached',
              jsonb_build_object('instance_id', r.instance_id, 'stage_id', r.stage_id));
    ELSIF r.on_sla_breach = 'auto_reject' THEN
      UPDATE approval_stage_instance SET status = 'expired', resolved_at = now()
       WHERE id = r.stage_id;
      UPDATE approval_instance SET status = 'expired', resolved_at = now()
       WHERE id = r.instance_id;
      INSERT INTO approval_outbox (institution_id, event, payload)
      VALUES (r.institution_id, 'approval.expired',
              jsonb_build_object('instance_id', r.instance_id));
    ELSIF r.on_sla_breach = 'escalate' THEN
      UPDATE approval_stage_instance SET status = 'expired', resolved_at = now()
       WHERE id = r.stage_id;
      UPDATE approval_instance SET status = 'escalated', resolved_at = now()
       WHERE id = r.instance_id;
      INSERT INTO approval_outbox (institution_id, event, payload)
      VALUES (r.institution_id, 'approval.escalated',
              jsonb_build_object('instance_id', r.instance_id,
                                 'escalate_to_template_id', r.escalate_to_template_id,
                                 'entity_type', r.entity_type, 'entity_id', r.entity_id));
    END IF;
  END LOOP;
  RETURN n;
END $$;

--
-- Name: approvals_is_eligible(uuid, uuid); Type: FUNCTION; Schema: institution; Owner: -
--

CREATE FUNCTION institution.approvals_is_eligible(p_stage_instance uuid, p_actor uuid) RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'institution', 'public'
    AS $$
DECLARE v record;
BEGIN
  SELECT si.id, sd.stage_type, sd.committee_id, sd.approver_role, i.institution_id
    INTO v
  FROM approval_stage_instance si
  JOIN approval_stage_def sd ON sd.id = si.stage_def_id
  JOIN approval_instance i ON i.id = si.instance_id
  WHERE si.id = p_stage_instance;
  IF NOT FOUND THEN RETURN false; END IF;

  IF v.stage_type = 'committee' THEN
    RETURN EXISTS (
      SELECT 1 FROM committee_member cm
      WHERE cm.committee_id = v.committee_id AND cm.member_id = p_actor
        AND cm.is_voting AND cm.valid_from <= current_date
        AND (cm.valid_to IS NULL OR cm.valid_to >= current_date));
  ELSIF v.stage_type IN ('single','dual','checklist','external_hold') THEN
    -- Was institution.user_role (never existed). Approvers are identified by
    -- auth uid, as everywhere else in the engine.
    RETURN EXISTS (
      SELECT 1 FROM institution.member m
      WHERE m.auth_user_id = p_actor AND m.institution_id = v.institution_id AND m.active
        AND (v.approver_role IS NULL OR m.member_role = v.approver_role));
  END IF;
  RETURN false;
END $$;

--
-- Name: approvals_route(uuid, text, uuid, uuid, jsonb, uuid, uuid); Type: FUNCTION; Schema: institution; Owner: -
--

CREATE FUNCTION institution.approvals_route(p_institution_id uuid, p_entity_type text, p_entity_id uuid, p_entity_maker uuid, p_snapshot jsonb, p_template_id uuid, p_doa_rule_id uuid) RETURNS uuid
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'institution', 'public'
    AS $$
DECLARE v_instance uuid; v_version int; v_stage record; v_first_due timestamptz;
        v_step record; v_voters int; v_needed int; v_available int; c record;
BEGIN
  PERFORM 1 FROM approval_template
   WHERE id = p_template_id AND institution_id = p_institution_id AND status = 'active';
  IF NOT FOUND THEN RAISE EXCEPTION 'APPROVALS_TEMPLATE_NOT_ACTIVE'; END IF;

  SELECT version INTO v_version FROM approval_template WHERE id = p_template_id;

  INSERT INTO approval_instance
    (institution_id, template_id, template_version, doa_rule_id,
     entity_type, entity_id, entity_maker_id, entity_snapshot)
  VALUES (p_institution_id, p_template_id, v_version, p_doa_rule_id,
          p_entity_type, p_entity_id, p_entity_maker, p_snapshot)
  RETURNING id INTO v_instance;

  FOR v_stage IN
    SELECT * FROM approval_stage_def WHERE template_id = p_template_id ORDER BY seq
  LOOP
    INSERT INTO approval_stage_instance (instance_id, seq, stage_def_id, status, started_at, due_at)
    VALUES (v_instance, v_stage.seq, v_stage.id,
            CASE WHEN v_stage.seq = 1 THEN 'active' ELSE 'pending' END,
            CASE WHEN v_stage.seq = 1 THEN now() END,
            CASE WHEN v_stage.seq = 1 AND v_stage.sla_hours IS NOT NULL
                 THEN now() + make_interval(hours => v_stage.sla_hours) END);
  END LOOP;

  -- 065: every step must be approvable by someone other than the maker.
  FOR v_step IN
    SELECT si.id, si.seq, sd.name, sd.stage_type, sd.committee_id, sd.approver_role
      FROM approval_stage_instance si
      JOIN approval_stage_def sd ON sd.id = si.stage_def_id
     WHERE si.instance_id = v_instance
     ORDER BY si.seq
  LOOP
    SELECT count(*) INTO v_available
      FROM member m
     WHERE m.institution_id = p_institution_id AND m.active
       AND m.auth_user_id IS DISTINCT FROM p_entity_maker
       AND institution.approvals_is_eligible(v_step.id, m.auth_user_id);

    IF v_step.stage_type = 'dual' THEN
      v_needed := 2;
    ELSIF v_step.stage_type = 'committee' THEN
      SELECT * INTO c FROM approval_committee WHERE id = v_step.committee_id;
      SELECT count(*) INTO v_voters FROM committee_member cm
       WHERE cm.committee_id = v_step.committee_id AND cm.is_voting
         AND cm.valid_from <= current_date
         AND (cm.valid_to IS NULL OR cm.valid_to >= current_date);
      v_needed := CASE c.quorum_type
        WHEN 'count'     THEN c.quorum_value::int
        WHEN 'fraction'  THEN GREATEST(1, ceil(v_voters * c.quorum_value)::int)
        WHEN 'unanimous' THEN v_voters END;
      v_needed := GREATEST(COALESCE(v_needed, 1), 1);
    ELSE
      v_needed := 1;
    END IF;

    IF v_available < v_needed THEN
      RAISE EXCEPTION 'APPROVALS_NO_APPROVER'
        USING DETAIL = format(
          'Step %s "%s" needs %s approver(s) other than the submitter%s; %s can act.',
          v_step.seq, v_step.name, v_needed,
          CASE WHEN v_step.stage_type = 'committee' THEN ' on its committee'
               WHEN v_step.approver_role IS NOT NULL
               THEN format(' with the %s role', v_step.approver_role)
               ELSE '' END,
          v_available);
    END IF;
  END LOOP;

  INSERT INTO approval_outbox (institution_id, event, payload)
  VALUES (p_institution_id, 'approval.requested',
          jsonb_build_object('instance_id', v_instance, 'entity_type', p_entity_type,
                             'entity_id', p_entity_id));
  RETURN v_instance;
END $$;

--
-- Name: approvals_seed_defaults(uuid, uuid); Type: FUNCTION; Schema: institution; Owner: -
--

CREATE FUNCTION institution.approvals_seed_defaults(p_institution_id uuid, p_admin uuid) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'institution', 'public'
    AS $$
DECLARE v_tpl uuid;
BEGIN
  INSERT INTO approval_template (institution_id, name, entity_type, status, created_by, approved_by)
  VALUES (p_institution_id, 'Default dual control', 'bid', 'active', p_admin, p_admin)
  ON CONFLICT (institution_id, name, version) DO NOTHING
  RETURNING id INTO v_tpl;
  IF v_tpl IS NULL THEN RETURN; END IF;

  INSERT INTO approval_stage_def (template_id, seq, name, stage_type, approver_role, sla_hours)
  VALUES (v_tpl, 1, 'Checker approval', 'dual', 'checker', 48);

  INSERT INTO doa_rule (institution_id, entity_type, priority, conditions, template_id, created_by)
  VALUES (p_institution_id, 'bid', 9999, '{}'::jsonb, v_tpl, p_admin)
  ON CONFLICT DO NOTHING;
END $$;

--
-- Name: approvals_withdraw(uuid, uuid, text); Type: FUNCTION; Schema: institution; Owner: -
--

CREATE FUNCTION institution.approvals_withdraw(p_instance uuid, p_actor uuid, p_reason text) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'institution', 'public'
    AS $$
DECLARE v record;
BEGIN
  SELECT * INTO v FROM approval_instance WHERE id = p_instance FOR UPDATE;
  IF NOT FOUND OR v.status <> 'in_progress' THEN
    RAISE EXCEPTION 'APPROVALS_NOT_WITHDRAWABLE';
  END IF;
  UPDATE approval_instance
     SET status = 'withdrawn', withdraw_reason = p_reason, resolved_at = now()
   WHERE id = p_instance;
  UPDATE approval_stage_instance SET status = 'skipped', resolved_at = now()
   WHERE instance_id = p_instance AND status IN ('active','pending');
  INSERT INTO approval_outbox (institution_id, event, payload)
  VALUES (v.institution_id, 'approval.withdrawn',
          jsonb_build_object('instance_id', p_instance, 'reason', p_reason,
                             'actor', p_actor));
END $$;

--
-- Name: approve_action(uuid, text); Type: FUNCTION; Schema: institution; Owner: -
--

CREATE FUNCTION institution.approve_action(p_action_id uuid, p_note text DEFAULT ''::text) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
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
$$;

--
-- Name: assign_default_member_group(); Type: FUNCTION; Schema: institution; Owner: -
--

CREATE FUNCTION institution.assign_default_member_group() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'institution', 'portal_admin'
    AS $$
BEGIN
  IF NEW.group_id IS NULL THEN
    IF NEW.is_primary_admin THEN
      SELECT id INTO NEW.group_id FROM portal_admin.user_groups WHERE slug = 'institution_admin' LIMIT 1;
    ELSE
      SELECT id INTO NEW.group_id FROM portal_admin.user_groups WHERE slug = 'bank_officer' LIMIT 1;
    END IF;
  END IF;
  RETURN NEW;
END;
$$;

--
-- Name: current_institution_id(); Type: FUNCTION; Schema: institution; Owner: -
--

CREATE FUNCTION institution.current_institution_id() RETURNS uuid
    LANGUAGE sql STABLE
    SET search_path TO 'pg_catalog', 'public', 'extensions', 'pg_temp'
    AS $$
  SELECT NULLIF(
    current_setting('request.jwt.claims', true)::jsonb ->> 'institution_id', ''
  )::uuid
$$;

--
-- Name: current_member_ctx(); Type: FUNCTION; Schema: institution; Owner: -
--

CREATE FUNCTION institution.current_member_ctx() RETURNS TABLE(member_id uuid, institution_id uuid, is_admin boolean, member_role text, modules text[])
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'institution', 'portal_admin'
    AS $$
  SELECT m.id, m.institution_id,
         (m.is_primary_admin OR COALESCE(pg.slug, '') = 'institution_admin') AS is_admin,
         COALESCE(m.member_role, 'maker') AS member_role,
         COALESCE(cg.module_permissions, pg.module_permissions, '{}'::TEXT[]) AS modules
    FROM institution.member m
    LEFT JOIN institution."group"        cg ON cg.id = m.custom_group_id
    LEFT JOIN portal_admin.user_groups   pg ON pg.id = m.group_id
   WHERE m.auth_user_id = auth.uid() AND m.active = true
   LIMIT 1;
$$;

--
-- Name: current_member_ctx_v2(); Type: FUNCTION; Schema: institution; Owner: -
--

CREATE FUNCTION institution.current_member_ctx_v2() RETURNS TABLE(member_id uuid, institution_id uuid, is_admin boolean, member_role text, modules text[], products uuid[])
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'institution', 'portal_admin'
    AS $$
  SELECT m.id, m.institution_id,
         (m.is_primary_admin OR COALESCE(pg.slug, '') = 'institution_admin') AS is_admin,
         COALESCE(m.member_role, 'maker') AS member_role,
         COALESCE(cg.module_permissions, pg.module_permissions, '{}'::TEXT[]) AS modules,
         cg.product_scope AS products   -- NULL = unrestricted (all licensed products)
    FROM institution.member m
    LEFT JOIN institution."group"        cg ON cg.id = m.custom_group_id
    LEFT JOIN portal_admin.user_groups   pg ON pg.id = m.group_id
   WHERE m.auth_user_id = auth.uid() AND m.active = true
   LIMIT 1;
$$;

--
-- Name: doc_template_set_updated_at(); Type: FUNCTION; Schema: institution; Owner: -
--

CREATE FUNCTION institution.doc_template_set_updated_at() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog', 'public', 'extensions', 'pg_temp'
    AS $$
begin
  new.updated_at := now();
  return new;
end $$;

--
-- Name: enforce_member_group_tenant(); Type: FUNCTION; Schema: institution; Owner: -
--

CREATE FUNCTION institution.enforce_member_group_tenant() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'institution'
    AS $$
BEGIN
  IF NEW.custom_group_id IS NOT NULL THEN
    IF NOT EXISTS (
      SELECT 1 FROM institution."group"
      WHERE id = NEW.custom_group_id
        AND institution_id = NEW.institution_id
    ) THEN
      RAISE EXCEPTION 'Cross-tenant group assignment blocked';
    END IF;
  END IF;
  RETURN NEW;
END;
$$;

--
-- Name: esign_append_event(uuid, uuid, text, jsonb, inet, text); Type: FUNCTION; Schema: institution; Owner: -
--

CREATE FUNCTION institution.esign_append_event(p_envelope uuid, p_signer uuid, p_event text, p_detail jsonb DEFAULT '{}'::jsonb, p_ip inet DEFAULT NULL::inet, p_ua text DEFAULT NULL::text) RETURNS text
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'institution', 'public'
    AS $$
DECLARE v_prev text; v_hash text;
BEGIN
  SELECT hash INTO v_prev FROM esign_event
  WHERE envelope_id = p_envelope ORDER BY id DESC LIMIT 1;
  v_prev := COALESCE(v_prev, 'GENESIS');
  v_hash := encode(sha256(convert_to(
              v_prev || p_event || COALESCE(p_signer::text,'') ||
              COALESCE(p_detail::text,'{}') || now()::text, 'UTF8')), 'hex');
  INSERT INTO esign_event (envelope_id, signer_id, event, detail, ip, user_agent, prev_hash, hash)
  VALUES (p_envelope, p_signer, p_event, p_detail, p_ip, p_ua, v_prev, v_hash);
  RETURN v_hash;
END $$;

--
-- Name: esign_create_envelope(uuid, text, uuid, uuid, text, text, text, timestamp with time zone, uuid, jsonb); Type: FUNCTION; Schema: institution; Owner: -
--

CREATE FUNCTION institution.esign_create_envelope(p_institution_id uuid, p_entity_type text, p_entity_id uuid, p_approval_instance uuid, p_title text, p_document_path text, p_document_sha256 text, p_expires_at timestamp with time zone, p_created_by uuid, p_signers jsonb) RETURNS uuid
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'institution', 'public'
    AS $$
DECLARE v_env uuid; s jsonb;
BEGIN
  IF p_entity_type IN ('offer_letter','investment_mandate') THEN
    PERFORM 1 FROM approval_instance
     WHERE id = p_approval_instance AND institution_id = p_institution_id
       AND status = 'approved';
    IF NOT FOUND THEN RAISE EXCEPTION 'ESIGN_APPROVAL_NOT_GRANTED'; END IF;
  END IF;

  INSERT INTO esign_envelope
    (institution_id, entity_type, entity_id, approval_instance_id, title,
     document_path, document_sha256, expires_at, created_by)
  VALUES (p_institution_id, p_entity_type, p_entity_id, p_approval_instance, p_title,
          p_document_path, p_document_sha256, p_expires_at, p_created_by)
  RETURNING id INTO v_env;

  FOR s IN SELECT * FROM jsonb_array_elements(p_signers) LOOP
    INSERT INTO esign_signer
      (envelope_id, party, sign_order, display_name, email, user_ref, token_sha256)
    VALUES (v_env, s->>'party', (s->>'sign_order')::int, s->>'display_name',
            s->>'email', NULLIF(s->>'user_ref','')::uuid, s->>'token_sha256');
  END LOOP;

  PERFORM institution.esign_append_event(v_env, NULL, 'created',
    jsonb_build_object('document_sha256', p_document_sha256));
  RETURN v_env;
END $$;

--
-- Name: esign_decline(uuid, text, inet, text); Type: FUNCTION; Schema: institution; Owner: -
--

CREATE FUNCTION institution.esign_decline(p_signer uuid, p_reason text, p_ip inet, p_ua text) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'institution', 'public'
    AS $$
DECLARE v record;
BEGIN
  SELECT sg.*, e.id AS env_id INTO v
  FROM esign_signer sg JOIN esign_envelope e ON e.id = sg.envelope_id
  WHERE sg.id = p_signer FOR UPDATE OF sg, e;
  IF NOT FOUND THEN RAISE EXCEPTION 'ESIGN_SIGNER_NOT_FOUND'; END IF;
  IF p_reason IS NULL OR length(trim(p_reason)) = 0 THEN
    RAISE EXCEPTION 'ESIGN_REASON_REQUIRED'; END IF;
  UPDATE esign_signer SET status = 'declined', decline_reason = p_reason WHERE id = p_signer;
  UPDATE esign_envelope SET status = 'declined' WHERE id = v.env_id;
  PERFORM institution.esign_append_event(v.env_id, p_signer, 'declined',
    jsonb_build_object('reason', p_reason), p_ip, p_ua);
  INSERT INTO institution.approval_outbox (institution_id, event, payload)
  SELECT institution_id, 'esign.declined',
         jsonb_build_object('envelope_id', id, 'entity_type', entity_type,
                            'entity_id', entity_id, 'reason', p_reason)
  FROM esign_envelope WHERE id = v.env_id;
END $$;

--
-- Name: esign_expire_overdue(); Type: FUNCTION; Schema: institution; Owner: -
--

CREATE FUNCTION institution.esign_expire_overdue() RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'institution', 'public'
    AS $$
DECLARE r record; n int := 0;
BEGIN
  FOR r IN SELECT id, institution_id FROM esign_envelope
           WHERE status IN ('sent','partially_signed') AND expires_at < now()
           FOR UPDATE SKIP LOCKED
  LOOP
    n := n + 1;
    UPDATE esign_envelope SET status = 'expired' WHERE id = r.id;
    PERFORM institution.esign_append_event(r.id, NULL, 'expired', '{}');
    INSERT INTO approval_outbox (institution_id, event, payload)
    VALUES (r.institution_id, 'esign.expired', jsonb_build_object('envelope_id', r.id));
  END LOOP;
  RETURN n;
END $$;

--
-- Name: esign_sign(uuid, inet, text); Type: FUNCTION; Schema: institution; Owner: -
--

CREATE FUNCTION institution.esign_sign(p_signer uuid, p_ip inet, p_ua text) RETURNS text
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'institution', 'public'
    AS $$
DECLARE v record; v_pending int; v_env_status text;
BEGIN
  SELECT sg.*, e.id AS env_id, e.status AS env_status, e.expires_at
    INTO v
  FROM esign_signer sg JOIN esign_envelope e ON e.id = sg.envelope_id
  WHERE sg.id = p_signer FOR UPDATE OF sg, e;

  IF NOT FOUND THEN RAISE EXCEPTION 'ESIGN_SIGNER_NOT_FOUND'; END IF;
  IF v.env_status NOT IN ('sent','partially_signed') THEN
    RAISE EXCEPTION 'ESIGN_ENVELOPE_NOT_SIGNABLE'; END IF;
  IF v.expires_at < now() THEN RAISE EXCEPTION 'ESIGN_ENVELOPE_EXPIRED'; END IF;
  IF v.status = 'signed' THEN RAISE EXCEPTION 'ESIGN_ALREADY_SIGNED'; END IF;
  IF v.otp_verified_at IS NULL OR v.otp_verified_at < now() - interval '10 minutes' THEN
    RAISE EXCEPTION 'ESIGN_OTP_NOT_VERIFIED';
  END IF;
  PERFORM 1 FROM esign_signer
   WHERE envelope_id = v.env_id AND sign_order < v.sign_order AND status <> 'signed';
  IF FOUND THEN RAISE EXCEPTION 'ESIGN_OUT_OF_ORDER'; END IF;

  UPDATE esign_signer
     SET status = 'signed', signed_at = now(), signed_ip = p_ip, signed_ua = p_ua
   WHERE id = p_signer;

  PERFORM institution.esign_append_event(v.env_id, p_signer,
    CASE WHEN v.party = 'institution' THEN 'countersigned' ELSE 'signed' END,
    jsonb_build_object('sign_order', v.sign_order), p_ip, p_ua);

  SELECT count(*) INTO v_pending FROM esign_signer
   WHERE envelope_id = v.env_id AND status <> 'signed';

  v_env_status := CASE WHEN v_pending = 0 THEN 'completed' ELSE 'partially_signed' END;
  UPDATE esign_envelope
     SET status = v_env_status,
         completed_at = CASE WHEN v_pending = 0 THEN now() END
   WHERE id = v.env_id;

  IF v_pending = 0 THEN
    INSERT INTO institution.approval_outbox (institution_id, event, payload)
    SELECT institution_id, 'esign.completed',
           jsonb_build_object('envelope_id', id, 'entity_type', entity_type, 'entity_id', entity_id)
    FROM esign_envelope WHERE id = v.env_id;
  END IF;
  RETURN v_env_status;
END $$;

--
-- Name: get_institution_bid_request_ids(uuid); Type: FUNCTION; Schema: institution; Owner: -
--

CREATE FUNCTION institution.get_institution_bid_request_ids(p_institution_id uuid) RETURNS TABLE(request_id uuid)
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'marketplace'
    AS $$
  SELECT DISTINCT b.request_id
  FROM marketplace.bid b
  WHERE b.institution_id = p_institution_id;
$$;

--
-- Name: get_my_institution_id(); Type: FUNCTION; Schema: institution; Owner: -
--

CREATE FUNCTION institution.get_my_institution_id() RETURNS uuid
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'institution', 'public'
    AS $$
  SELECT institution_id FROM institution.member
  WHERE auth_user_id = auth.uid() AND active = true
  LIMIT 1;
$$;

--
-- Name: get_my_member_id(); Type: FUNCTION; Schema: institution; Owner: -
--

CREATE FUNCTION institution.get_my_member_id() RETURNS uuid
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO ''
    AS $$
  SELECT id FROM institution.member
  WHERE auth_user_id = auth.uid() AND active = true
  LIMIT 1;
$$;

--
-- Name: get_my_modules(); Type: FUNCTION; Schema: institution; Owner: -
--

CREATE FUNCTION institution.get_my_modules() RETURNS text[]
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'institution', 'portal_admin'
    AS $$
  SELECT COALESCE(
    (SELECT g.module_permissions
       FROM institution.member im
       JOIN institution."group" g ON g.id = im.custom_group_id
      WHERE im.auth_user_id = auth.uid()),
    (SELECT ug.module_permissions
       FROM institution.member im
       JOIN portal_admin.user_groups ug ON ug.id = im.group_id
      WHERE im.auth_user_id = auth.uid()),
    '{}'::TEXT[]
  );
$$;

--
-- Name: get_my_products(); Type: FUNCTION; Schema: institution; Owner: -
--

CREATE FUNCTION institution.get_my_products() RETURNS uuid[]
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'institution', 'portal_admin'
    AS $$
  SELECT
    (SELECT g.product_scope
       FROM institution.member im
       JOIN institution."group" g ON g.id = im.custom_group_id
      WHERE im.auth_user_id = auth.uid());
$$;

--
-- Name: has_module(text); Type: FUNCTION; Schema: institution; Owner: -
--

CREATE FUNCTION institution.has_module(p_module text) RETURNS boolean
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO ''
    AS $$
  SELECT exists (
    SELECT 1 FROM institution.institution
    WHERE id = institution.get_my_institution_id()
      AND modules ? p_module
  );
$$;

--
-- Name: has_role(text); Type: FUNCTION; Schema: institution; Owner: -
--

CREATE FUNCTION institution.has_role(p_role text) RETURNS boolean
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO ''
    AS $$
  SELECT exists (
    SELECT 1 FROM institution.member
    WHERE auth_user_id = auth.uid()
      AND institution_id = institution.get_my_institution_id()
      AND role::text = p_role
      AND active = true
  );
$$;

--
-- Name: is_active(); Type: FUNCTION; Schema: institution; Owner: -
--

CREATE FUNCTION institution.is_active() RETURNS boolean
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO ''
    AS $$
  SELECT exists (
    SELECT 1 FROM institution.institution
    WHERE id = institution.get_my_institution_id()
      AND approved = true
      AND onboarding_stage = 'approved'
      AND suspended_at IS NULL
  );
$$;

--
-- Name: is_ficium_admin(); Type: FUNCTION; Schema: institution; Owner: -
--

CREATE FUNCTION institution.is_ficium_admin() RETURNS boolean
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO ''
    AS $$
  SELECT coalesce(
    (auth.jwt() -> 'app_metadata' ->> 'role') = 'ficium_admin', false
  );
$$;

--
-- Name: reject_action(uuid, text); Type: FUNCTION; Schema: institution; Owner: -
--

CREATE FUNCTION institution.reject_action(p_action_id uuid, p_note text) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
DECLARE
  v_action  governance.action%ROWTYPE;
  v_inst_id uuid;
  v_role    text;
BEGIN
  v_inst_id := institution.get_my_institution_id();

  SELECT * INTO v_action
  FROM governance.action
  WHERE id = p_action_id AND institution_id = v_inst_id AND scope = 'institution';

  IF NOT FOUND THEN RAISE EXCEPTION 'Action not found'; END IF;
  IF v_action.status <> 'pending' THEN RAISE EXCEPTION 'Action is not pending'; END IF;
  IF v_action.maker_id = institution.get_my_member_id() THEN
    RAISE EXCEPTION 'Maker cannot reject own action';
  END IF;

  SELECT role::text INTO v_role
  FROM institution.member
  WHERE auth_user_id = auth.uid() AND institution_id = v_inst_id AND active = true;

  UPDATE governance.action
  SET status = 'rejected', checker_id = institution.get_my_member_id(),
      checker_role = COALESCE(v_role,'admin'), checker_note = p_note,
      checked_at = now(), updated_at = now()
  WHERE id = p_action_id;

  INSERT INTO audit.event (
    actor_id, actor_type, actor_role, institution_id, action,
    resource_type, resource_id, resource_label, outcome, outcome_note,
    governance_action_id, metadata
  ) VALUES (
    auth.uid(), 'member', COALESCE(v_role,'admin'), v_inst_id,
    v_action.category || '.rejected', v_action.resource_type, v_action.resource_id,
    v_action.resource_label, 'rejected', p_note, p_action_id,
    jsonb_build_object('maker_id', v_action.maker_id, 'checker_id', institution.get_my_member_id())
  );
END;
$$;

--
-- Name: doc_type; Type: TABLE; Schema: institution; Owner: -
--

CREATE TABLE institution.doc_type (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    code text NOT NULL,
    label text NOT NULL,
    description text,
    is_mandatory boolean DEFAULT true NOT NULL,
    applies_to text[],
    sort_order integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: required_doc_types(uuid); Type: FUNCTION; Schema: institution; Owner: -
--

CREATE FUNCTION institution.required_doc_types(p_institution uuid) RETURNS SETOF institution.doc_type
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO ''
    AS $$
  select dt.* from institution.doc_type dt
    join institution.institution i on i.id = p_institution
   where dt.is_mandatory
     and (dt.applies_to is null or i.institution_type = any (dt.applies_to))
   order by dt.sort_order
$$;

--
-- Name: set_updated_at(); Type: FUNCTION; Schema: institution; Owner: -
--

CREATE FUNCTION institution.set_updated_at() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog', 'public', 'extensions', 'pg_temp'
    AS $$
BEGIN NEW.updated_at = NOW(); RETURN NEW; END;
$$;

--
-- Name: submit_for_approval(text, text, uuid, jsonb); Type: FUNCTION; Schema: institution; Owner: -
--

CREATE FUNCTION institution.submit_for_approval(p_action_category text, p_resource_type text, p_resource_id uuid, p_payload jsonb) RETURNS uuid
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public', 'extensions', 'pg_temp'
    AS $$
  SELECT governance.submit(p_action_category, p_resource_type, p_resource_id, p_payload);
$$;

--
-- Name: sync_member_login_state(); Type: FUNCTION; Schema: institution; Owner: -
--

CREATE FUNCTION institution.sync_member_login_state() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
BEGIN
  IF NEW.auth_user_id IS NULL THEN
    RETURN NEW;
  END IF;
  IF NEW.active IS DISTINCT FROM OLD.active THEN
    UPDATE auth_portal.auth_users SET is_active = NEW.active, updated_at = now() WHERE id = NEW.auth_user_id;
  END IF;
  IF NEW.email IS DISTINCT FROM OLD.email AND NEW.email IS NOT NULL THEN
    UPDATE auth_portal.auth_users SET email = lower(btrim(NEW.email)), updated_at = now() WHERE id = NEW.auth_user_id;
  END IF;
  RETURN NEW;
END $$;

--
-- Name: _admin_command_log_worm(); Type: FUNCTION; Schema: integration; Owner: -
--

CREATE FUNCTION integration._admin_command_log_worm() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog'
    AS $$
BEGIN RAISE EXCEPTION 'integration.admin_command_log is append-only' USING ERRCODE = '42501'; END $$;

--
-- Name: app_type_for_allocation(uuid, uuid); Type: FUNCTION; Schema: integration; Owner: -
--

CREATE FUNCTION integration.app_type_for_allocation(p_request_id uuid, p_product_id uuid) RETURNS text
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'integration'
    AS $$
  WITH app_types(t, ord) AS (
    SELECT * FROM unnest(ARRAY['sme_loan','personal_loan','mortgage','fixed_deposit','savings_account','credit_card','business_account',
      'investment_account','leasing','overdraft','business_loan','equities','unit_trust','savings_plan','government_bonds',
      'offshore_investment','mixed_portfolio']) WITH ORDINALITY),
  cands(t, pri, ord) AS (
    SELECT x.a ->> 'product_type', 1, x.n FROM integration.request_shadow s, jsonb_array_elements(s.allocations) WITH ORDINALITY x(a, n) WHERE s.request_id = p_request_id
    UNION ALL SELECT r.params ->> 'app_product_type', 2, 0 FROM marketplace.request r WHERE r.id = p_request_id
    UNION ALL SELECT p.code, 3, 0 FROM catalog.product p WHERE p.id = p_product_id
    UNION ALL SELECT a.t, 4, a.ord FROM app_types a)
  SELECT coalesce(
    (SELECT c.t FROM cands c JOIN app_types a ON a.t = c.t WHERE catalog.product_id_for_app_type(c.t) = p_product_id ORDER BY c.pri, c.ord LIMIT 1),
    (SELECT p.code FROM catalog.product p WHERE p.id = p_product_id))
$$;

--
-- Name: app_type_for_product(text); Type: FUNCTION; Schema: integration; Owner: -
--

CREATE FUNCTION integration.app_type_for_product(p_code text) RETURNS text
    LANGUAGE sql IMMUTABLE
    SET search_path TO 'pg_catalog'
    AS $$
  SELECT CASE WHEN t = ANY (ARRAY['sme_loan','personal_loan','mortgage','fixed_deposit','savings_account','credit_card',
                                  'business_account','investment_account','leasing','overdraft','business_loan','equities',
                                  'unit_trust','savings_plan','government_bonds','offshore_investment','mixed_portfolio'])
              THEN t END
    FROM (SELECT CASE p_code WHEN 'home_loan' THEN 'mortgage' WHEN 'deposit' THEN 'fixed_deposit'
                             WHEN 'savings' THEN 'savings_account' WHEN 'vehicle_loan' THEN 'leasing'
                             ELSE p_code END AS t) x;
$$;

--
-- Name: apply_chat_message(jsonb); Type: FUNCTION; Schema: integration; Owner: -
--

CREATE FUNCTION integration.apply_chat_message(p_env jsonb) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'integration', 'marketplace'
    AS $$
DECLARE d jsonb := p_env -> 'data';
BEGIN
  IF d ->> 'sender_side' <> 'borrower' THEN RAISE EXCEPTION 'apply_chat_message: borrower messages only'; END IF;
  INSERT INTO marketplace.request_message (id, request_id, institution_id, sender_side, kind, template_code, params, body, created_at)
  VALUES ((d ->> 'message_id')::uuid, (d ->> 'request_id')::uuid, (d ->> 'institution_id')::uuid, 'borrower',
          CASE WHEN d ->> 'template_code' IS NULL THEN 'free' ELSE 'structured' END, d ->> 'template_code',
          coalesce(d -> 'params', '{}'::jsonb), d ->> 'body', (d ->> 'sent_at')::timestamptz)
  ON CONFLICT (id) DO NOTHING;
END $$;

--
-- Name: apply_request_live(uuid); Type: FUNCTION; Schema: integration; Owner: -
--

CREATE FUNCTION integration.apply_request_live(p_request_id uuid) RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'integration', 'marketplace'
    AS $$
DECLARE s integration.request_shadow%ROWTYPE; existed boolean;
BEGIN
  SELECT * INTO s FROM integration.request_shadow WHERE request_id = p_request_id;
  IF NOT FOUND THEN RETURN false; END IF;
  existed := EXISTS (SELECT 1 FROM marketplace.request WHERE id = p_request_id);
  PERFORM marketplace.ingest_request(
    s.request_id, s.anon_borrower_id, s.product_type, s.amount, s.term_months, s.max_rate,
    s.decision_deadline, s.status, s.created_at, coalesce(s.phase1, '{}'::jsonb),
    s.allocation_mode, s.allocations);
  RETURN NOT existed;
END $$;

--
-- Name: apply_request_published(jsonb); Type: FUNCTION; Schema: integration; Owner: -
--

CREATE FUNCTION integration.apply_request_published(p_env jsonb) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'integration'
    AS $$
DECLARE d jsonb := p_env -> 'data';
BEGIN
  IF d ? 'phase1' AND (d -> 'phase1') ? 'employer' THEN
    RAISE EXCEPTION 'employer must never be received';      -- belt and braces: the contract already rejects it
  END IF;
  INSERT INTO integration.request_shadow AS s
    (request_id, anon_borrower_id, product_type, amount, currency, term_months, max_rate, decision_deadline,
     allocation_mode, allocations, phase1, created_at, last_event_id, last_sequence)
  VALUES ((d ->> 'request_id')::uuid, (d ->> 'anon_borrower_id')::uuid, d ->> 'product_type', (d ->> 'amount')::numeric,
          d ->> 'currency', NULLIF(d ->> 'term_months', '')::int, NULLIF(d ->> 'max_rate', '')::numeric,
          NULLIF(d ->> 'decision_deadline', '')::timestamptz, d ->> 'allocation_mode',
          coalesce(d -> 'allocations', '[]'::jsonb), d -> 'phase1', (d ->> 'created_at')::timestamptz,
          p_env ->> 'id', (p_env ->> 'sequence')::bigint)
  ON CONFLICT (request_id) DO UPDATE SET
    anon_borrower_id = EXCLUDED.anon_borrower_id, product_type = EXCLUDED.product_type, amount = EXCLUDED.amount,
    currency = EXCLUDED.currency, term_months = EXCLUDED.term_months, max_rate = EXCLUDED.max_rate,
    decision_deadline = EXCLUDED.decision_deadline, allocation_mode = EXCLUDED.allocation_mode,
    allocations = EXCLUDED.allocations, phase1 = EXCLUDED.phase1, created_at = EXCLUDED.created_at,
    last_event_id = EXCLUDED.last_event_id, last_sequence = EXCLUDED.last_sequence, updated_at = now();   -- status untouched
END $$;

--
-- Name: apply_request_status_changed(jsonb); Type: FUNCTION; Schema: integration; Owner: -
--

CREATE FUNCTION integration.apply_request_status_changed(p_env jsonb) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'integration'
    AS $$
DECLARE d jsonb := p_env -> 'data';
BEGIN
  UPDATE integration.request_shadow
     SET status = d ->> 'status', last_event_id = p_env ->> 'id', last_sequence = (p_env ->> 'sequence')::bigint, updated_at = now()
   WHERE request_id = (d ->> 'request_id')::uuid;
  IF NOT FOUND THEN
    -- Ordered delivery makes this impossible in normal operation; failing makes the sender retry instead of losing it.
    RAISE EXCEPTION 'status change for a request that was never published: %', d ->> 'request_id';
  END IF;
END $$;

--
-- Name: backfill_bid_events(); Type: FUNCTION; Schema: integration; Owner: -
--

CREATE FUNCTION integration.backfill_bid_events() RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'integration'
    AS $$
DECLARE r record; n int := 0;
BEGIN
  FOR r IN SELECT b.id FROM marketplace.bid b WHERE b.status IN ('submitted','under_review') ORDER BY b.submitted_at LOOP
    IF NOT EXISTS (SELECT 1 FROM integration.outbox o WHERE o.aggregate_id = r.id::text) THEN
      PERFORM integration.publish_bid(r.id); n := n + 1;
    END IF;
  END LOOP;
  RETURN n;
END $$;

--
-- Name: benefit_value(numeric, text, text); Type: FUNCTION; Schema: integration; Owner: -
--

CREATE FUNCTION integration.benefit_value(p_amount numeric, p_unit text, p_currency text) RETURNS jsonb
    LANGUAGE sql IMMUTABLE
    SET search_path TO 'pg_catalog'
    AS $$
  SELECT CASE
    WHEN p_amount IS NULL OR p_amount <= 0 OR p_unit IS NULL THEN '{}'::jsonb
    WHEN p_unit = 'pct_pa' AND p_amount <= 10
      THEN jsonb_build_object('value_amount', round(p_amount / 100, 8), 'value_unit', 'rate_pa')
    WHEN p_unit = 'pct' AND p_amount <= 100
      THEN jsonb_build_object('value_amount', round(p_amount / 100, 8), 'value_unit', 'fee_fraction')
    WHEN p_unit = 'mur' AND p_currency = 'MUR'
      THEN jsonb_build_object('value_amount', p_amount, 'value_unit', 'amount')
    ELSE '{}'::jsonb
  END
$$;

--
-- Name: build_bid_payload(uuid); Type: FUNCTION; Schema: integration; Owner: -
--

CREATE FUNCTION integration.build_bid_payload(p_bid_id uuid) RETURNS jsonb
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'integration'
    AS $$
  SELECT jsonb_build_object(
    'bid_id', b.id, 'request_id', b.request_id, 'institution_id', b.institution_id,
    'institution_name', i.name, 'institution_logo_url', i.logo_url,
    'status', b.status, 'rate', b.rate, 'rate_type', b.rate_type, 'rate_valid_days', b.rate_valid_days,
    'amount_offered', b.amount_offered, 'currency', r.currency, 'term_months', b.term_months,
    'conditions', coalesce(b.conditions, '{}'::jsonb), 'fee_structure', coalesce(b.fee_structure, '{}'::jsonb),
    'benefits', coalesce((SELECT jsonb_agg(jsonb_strip_nulls(jsonb_build_object('title', bb.title, 'value_display', bb.value_display,
                 'is_guaranteed', bb.is_guaranteed, 'cat_code', bb.cat_code)
                 || integration.benefit_value(bb.value_amount, bb.value_unit, r.currency)) ORDER BY bb.is_guaranteed DESC, bb.id)
               FROM marketplace.bid_benefit bb WHERE bb.bid_id = b.id), '[]'::jsonb),
    'allocations', coalesce((SELECT jsonb_agg(jsonb_strip_nulls(jsonb_build_object(
                 'product_type', integration.app_type_for_allocation(b.request_id, ba.product_id), 'product_label', cp.label,
                 'amount_offered', ba.amount_offered, 'rate', ba.rate, 'term_months', ba.term_months)) ORDER BY ba.product_id, ba.amount_offered)
               FROM marketplace.bid_allocation ba JOIN catalog.product cp ON cp.id = ba.product_id WHERE ba.bid_id = b.id), '[]'::jsonb),
    'submitted_at', b.submitted_at, 'expires_at', b.expires_at)
  FROM marketplace.bid b
  JOIN institution.institution i ON i.id = b.institution_id
  JOIN marketplace.request r ON r.id = b.request_id
  WHERE b.id = p_bid_id
$$;

--
-- Name: build_deal_documents_payload(uuid); Type: FUNCTION; Schema: integration; Owner: -
--

CREATE FUNCTION integration.build_deal_documents_payload(p_pipeline_id uuid) RETURNS jsonb
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'integration'
    AS $$
  SELECT jsonb_build_object(
    'pipeline_id', lp.id, 'request_id', lp.request_id, 'bid_id', lp.bid_id, 'updated_at', now(),
    'documents', coalesce((
      SELECT jsonb_agg(jsonb_build_object(
          'requirement_id', r.id, 'name', r.name, 'description', r.description,
          'provided_by', r.provided_by, 'mandatory', r.mandatory, 'stage_label', sd.label,
          'status', dd.status,
          'review_note', CASE WHEN dd.status IN ('rejected','waived') THEN dd.review_note END,
          'document_type', r.document_type, 'max_age_days', r.max_age_days)
        ORDER BY r.position, r.name)
        FROM marketplace.deal_document dd
        JOIN institution.doc_requirement r ON r.id = dd.requirement_id
        LEFT JOIN institution.pipeline_stage_def sd ON sd.id = r.stage_def_id
       WHERE dd.pipeline_id = lp.id), '[]'::jsonb))
  FROM marketplace.loan_pipeline lp
  WHERE lp.id = p_pipeline_id AND lp.bid_id IS NOT NULL;
$$;

--
-- Name: build_deal_progress_payload(uuid); Type: FUNCTION; Schema: integration; Owner: -
--

CREATE FUNCTION integration.build_deal_progress_payload(p_pipeline_id uuid) RETURNS jsonb
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'integration'
    AS $$
  SELECT jsonb_build_object(
    'pipeline_id', lp.id, 'request_id', lp.request_id, 'bid_id', lp.bid_id,
    'status', lp.status::text,
    'deal', jsonb_build_object('amount', lp.deal_amount, 'rate', lp.deal_rate, 'term_months', lp.deal_term_months),
    'started_at', lp.started_at, 'completed_at', lp.completed_at, 'updated_at', now(),
    'stages', coalesce((
      SELECT jsonb_agg(jsonb_build_object(
          'position', row_number, 'label', label, 'status', status,
          'started_at', started_at, 'completed_at', completed_at, 'due_at', sla_due_at) ORDER BY row_number)
        FROM (SELECT row_number() OVER (ORDER BY psi.position) AS row_number,
                     coalesce(nullif(psd.borrower_label, ''), psd.label) AS label,
                     psi.status::text AS status, psi.started_at, psi.completed_at, psi.sla_due_at
                FROM marketplace.pipeline_stage_instance psi
                JOIN institution.pipeline_stage_def psd ON psd.id = psi.stage_def_id
               WHERE psi.pipeline_id = lp.id AND psd.borrower_visible) v), '[]'::jsonb))
  FROM marketplace.loan_pipeline lp
  WHERE lp.id = p_pipeline_id AND lp.bid_id IS NOT NULL;
$$;

--
-- Name: build_market_intelligence(); Type: FUNCTION; Schema: integration; Owner: -
--

CREATE FUNCTION integration.build_market_intelligence() RETURNS jsonb
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'marketplace', 'catalog', 'integration'
    AS $$
  WITH live AS (
    SELECT b.*, p.code AS product_code, r.product_id
      FROM marketplace.bid b JOIN marketplace.request r ON r.id = b.request_id JOIN catalog.product p ON p.id = r.product_id
     WHERE b.status IN ('submitted', 'accepted') AND b.created_at > now() - interval '90 days'),
  rates AS (
    SELECT jsonb_strip_nulls(jsonb_build_object(
      'product_code', integration.app_type_for_product(product_code),
      'bid_count', count(DISTINCT id), 'request_count', count(DISTINCT request_id),
      'min_rate', round(min(rate)::numeric, 6), 'max_rate', round(max(rate)::numeric, 6),
      'avg_rate', round(avg(rate)::numeric, 6),
      'p25_rate', round((percentile_cont(0.25) WITHIN GROUP (ORDER BY rate))::numeric, 6),
      'p75_rate', round((percentile_cont(0.75) WITHIN GROUP (ORDER BY rate))::numeric, 6))) AS j
      FROM live GROUP BY product_code),
  outcomes AS (
    SELECT jsonb_strip_nulls(jsonb_build_object(
      'product_code', integration.app_type_for_product(p.code), 'total_acceptances', count(*),
      'avg_winning_rate', round(avg(b.rate)::numeric, 6), 'min_winning_rate', round(min(b.rate)::numeric, 6),
      'max_winning_rate', round(max(b.rate)::numeric, 6), 'avg_winning_amount', round(avg(b.amount_offered)::numeric, 0),
      'avg_winning_term_months', greatest(1, round(avg(b.term_months))::int),
      'rate_vs_market_avg', round((avg(b.rate) - (SELECT avg(l.rate) FROM live l WHERE l.product_id = r.product_id))::numeric, 6))) AS j
      FROM marketplace.request r JOIN marketplace.bid b ON b.id = r.winning_bid_id JOIN catalog.product p ON p.id = r.product_id
     WHERE r.accepted_at > now() - interval '90 days' GROUP BY p.code, r.product_id),
  comp AS (
    SELECT jsonb_build_object('product_code', integration.app_type_for_product(product_code),
      'active_institutions', count(DISTINCT institution_id),
      'avg_bids_per_request', round((count(*)::numeric / nullif(count(DISTINCT request_id), 0)), 1),
      'max_bids_per_request', max(per_req)) AS j
      FROM (SELECT l.*, count(*) OVER (PARTITION BY l.request_id) AS per_req FROM live l) x GROUP BY product_code)
  SELECT jsonb_build_object('as_of', current_date,
    'rates', coalesce((SELECT jsonb_agg(j ORDER BY j ->> 'product_code') FROM rates WHERE j ? 'product_code'), '[]'::jsonb),
    'outcomes', coalesce((SELECT jsonb_agg(j ORDER BY j ->> 'product_code') FROM outcomes WHERE j ? 'product_code'), '[]'::jsonb),
    'competitiveness', coalesce((SELECT jsonb_agg(j ORDER BY j ->> 'product_code') FROM comp
                                  WHERE j ->> 'product_code' IS NOT NULL), '[]'::jsonb));
$$;

--
-- Name: claim_batch(integer, integer); Type: FUNCTION; Schema: integration; Owner: -
--

CREATE FUNCTION integration.claim_batch(p_limit integer DEFAULT 20, p_lease_seconds integer DEFAULT 60) RETURNS TABLE(id text, envelope jsonb, attempts integer)
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'integration'
    AS $$
BEGIN
  RETURN QUERY
  UPDATE integration.outbox o
     SET status = 'sending',
         lease_until = now() + make_interval(secs => p_lease_seconds),
         attempts = o.attempts + 1
   WHERE o.id IN (
     SELECT x.id FROM integration.outbox x
      WHERE ((x.status = 'pending' AND x.next_attempt_at <= now())
          OR (x.status = 'sending' AND x.lease_until < now()))
        AND NOT EXISTS (SELECT 1 FROM integration.outbox e
                         WHERE e.aggregate_id = x.aggregate_id AND e.sequence < x.sequence AND e.status <> 'delivered')
      ORDER BY x.created_at
      LIMIT greatest(1, least(p_limit, 100))
      FOR UPDATE SKIP LOCKED)
  RETURNING o.id, o.envelope, o.attempts;
END $$;

--
-- Name: defer(text); Type: FUNCTION; Schema: integration; Owner: -
--

CREATE FUNCTION integration.defer(p_id text) RETURNS void
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'integration'
    AS $$
  UPDATE integration.outbox
     SET status = 'pending', attempts = greatest(attempts - 1, 0), lease_until = NULL,
         next_attempt_at = now() + interval '1 minute' * (1 + random() * 0.2),
         last_error = 'held: the receiving side has paused this channel'
   WHERE id = p_id AND status = 'sending';
$$;

--
-- Name: enqueue(text, text, jsonb, text); Type: FUNCTION; Schema: integration; Owner: -
--

CREATE FUNCTION integration.enqueue(p_type text, p_aggregate_id text, p_data jsonb, p_source text) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'integration'
    AS $$
DECLARE
  v_seq bigint;
  v_id  text := 'evt_' || replace(gen_random_uuid()::text, '-', '');
  v_env jsonb;
BEGIN
  IF p_source NOT IN ('borrower','institution') THEN
    RAISE EXCEPTION 'integration.enqueue: bad source %', p_source;
  END IF;
  INSERT INTO integration.aggregate_sequence AS s (aggregate_id, last_sequence)
  VALUES (p_aggregate_id, 1)
  ON CONFLICT (aggregate_id) DO UPDATE SET last_sequence = s.last_sequence + 1
  RETURNING last_sequence INTO v_seq;

  v_env := jsonb_build_object(
    'id', v_id, 'type', p_type, 'version', 1, 'source', p_source,
    'occurred_at', to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'),
    'aggregate_id', p_aggregate_id, 'sequence', v_seq, 'data', coalesce(p_data, '{}'::jsonb));

  INSERT INTO integration.outbox (id, type, aggregate_id, sequence, envelope)
  VALUES (v_id, p_type, p_aggregate_id, v_seq, v_env);
  RETURN v_env;
END $$;

--
-- Name: is_phase2(uuid); Type: FUNCTION; Schema: integration; Owner: -
--

CREATE FUNCTION integration.is_phase2(p_pipeline_id uuid) RETURNS boolean
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'integration', 'marketplace'
    AS $$
  SELECT EXISTS (
    SELECT 1 FROM marketplace.loan_pipeline lp
      JOIN integration.acceptance_log a ON a.bid_id = lp.bid_id AND a.status_code = 200
     WHERE lp.id = p_pipeline_id);
$$;

--
-- Name: mark_delivered(text); Type: FUNCTION; Schema: integration; Owner: -
--

CREATE FUNCTION integration.mark_delivered(p_id text) RETURNS void
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'integration'
    AS $$
  UPDATE integration.outbox
     SET status = 'delivered', delivered_at = now(), lease_until = NULL, last_error = NULL
   WHERE id = p_id;
$$;

--
-- Name: mark_failed(text, text); Type: FUNCTION; Schema: integration; Owner: -
--

CREATE FUNCTION integration.mark_failed(p_id text, p_error text) RETURNS text
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'integration'
    AS $$
DECLARE
  v_attempts integer; v_created timestamptz; v_delay interval; v_status text;
BEGIN
  SELECT o.attempts, o.created_at INTO v_attempts, v_created FROM integration.outbox o WHERE o.id = p_id;
  IF NOT FOUND THEN RETURN 'missing'; END IF;
  v_delay := CASE v_attempts WHEN 1 THEN interval '30 seconds' WHEN 2 THEN interval '2 minutes'
                             WHEN 3 THEN interval '10 minutes' ELSE interval '1 hour' END;
  v_delay := v_delay * (1 + random() * 0.1);
  v_status := CASE WHEN now() + v_delay > v_created + interval '24 hours' THEN 'dead' ELSE 'pending' END;
  UPDATE integration.outbox
     SET status = v_status, next_attempt_at = now() + v_delay, lease_until = NULL,
         last_error = left(p_error, 1000)
   WHERE id = p_id;
  RETURN v_status;
END $$;

--
-- Name: publish_bid(uuid); Type: FUNCTION; Schema: integration; Owner: -
--

CREATE FUNCTION integration.publish_bid(p_bid_id uuid) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'integration'
    AS $$
DECLARE st text; rid uuid; d jsonb; n int; ever_placed boolean; last_type text; last_data jsonb;
BEGIN
  SELECT b.status, b.request_id INTO st, rid FROM marketplace.bid b WHERE b.id = p_bid_id;
  IF NOT FOUND THEN RETURN; END IF;
  ever_placed := EXISTS (SELECT 1 FROM integration.outbox o WHERE o.aggregate_id = p_bid_id::text AND o.type = 'bid.placed');
  SELECT o.type, o.envelope -> 'data' INTO last_type, last_data FROM integration.outbox o
   WHERE o.aggregate_id = p_bid_id::text ORDER BY o.sequence DESC LIMIT 1;
  IF st IN ('submitted','under_review') THEN
    d := integration.build_bid_payload(p_bid_id);
    IF last_type IN ('bid.placed','bid.updated') AND last_data = d THEN RETURN; END IF;   -- nothing a borrower sees changed
    UPDATE integration.outbox o SET envelope = jsonb_set(o.envelope, '{data}', d)
     WHERE o.id = (SELECT x.id FROM integration.outbox x WHERE x.aggregate_id = p_bid_id::text ORDER BY x.sequence DESC LIMIT 1)
       AND o.type IN ('bid.placed','bid.updated') AND o.status = 'pending' AND o.attempts = 0;
    GET DIAGNOSTICS n = ROW_COUNT;
    IF n = 0 THEN
      PERFORM integration.enqueue(CASE WHEN NOT ever_placed OR last_type = 'bid.withdrawn' THEN 'bid.placed' ELSE 'bid.updated' END,
                                  p_bid_id::text, d, 'institution');
    END IF;
  ELSIF st IN ('withdrawn','rejected','expired') THEN
    IF ever_placed AND last_type IS DISTINCT FROM 'bid.withdrawn' THEN
      PERFORM integration.enqueue('bid.withdrawn', p_bid_id::text,
              jsonb_build_object('bid_id', p_bid_id, 'request_id', rid, 'reason', st, 'at', now()), 'institution');
    END IF;
  END IF;
END $$;

--
-- Name: publish_deal_documents(uuid); Type: FUNCTION; Schema: integration; Owner: -
--

CREATE FUNCTION integration.publish_deal_documents(p_pipeline_id uuid) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'integration'
    AS $$
DECLARE d jsonb; last_docs jsonb;
BEGIN
  IF NOT integration.is_phase2(p_pipeline_id) THEN RETURN; END IF;
  d := integration.build_deal_documents_payload(p_pipeline_id);
  IF d IS NULL THEN RETURN; END IF;
  SELECT o.envelope -> 'data' -> 'documents' INTO last_docs FROM integration.outbox o
   WHERE o.aggregate_id = p_pipeline_id::text AND o.type = 'deal.documents_updated'
   ORDER BY o.sequence DESC LIMIT 1;
  IF last_docs = d -> 'documents' THEN RETURN; END IF;
  PERFORM integration.enqueue('deal.documents_updated', p_pipeline_id::text, d, 'institution');
END $$;

--
-- Name: publish_deal_progress(uuid); Type: FUNCTION; Schema: integration; Owner: -
--

CREATE FUNCTION integration.publish_deal_progress(p_pipeline_id uuid) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'integration'
    AS $$
DECLARE d jsonb; last_d jsonb;
BEGIN
  IF NOT integration.is_phase2(p_pipeline_id) THEN RETURN; END IF;
  d := integration.build_deal_progress_payload(p_pipeline_id);
  IF d IS NULL THEN RETURN; END IF;
  SELECT o.envelope -> 'data' INTO last_d FROM integration.outbox o
   WHERE o.aggregate_id = p_pipeline_id::text AND o.type = 'deal.progress_updated'
   ORDER BY o.sequence DESC LIMIT 1;
  IF last_d - 'updated_at' = d - 'updated_at' THEN RETURN; END IF;
  PERFORM integration.enqueue('deal.progress_updated', p_pipeline_id::text, d, 'institution');
END $$;

--
-- Name: publish_market_intelligence(); Type: FUNCTION; Schema: integration; Owner: -
--

CREATE FUNCTION integration.publish_market_intelligence() RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'integration'
    AS $$
DECLARE d jsonb := integration.build_market_intelligence(); last_d jsonb;
BEGIN
  SELECT o.envelope -> 'data' INTO last_d FROM integration.outbox o
   WHERE o.aggregate_id = 'market' AND o.type = 'market.intelligence' ORDER BY o.sequence DESC LIMIT 1;
  IF last_d IS NOT NULL AND last_d - 'as_of' = d - 'as_of' AND last_d ->> 'as_of' = d ->> 'as_of' THEN
    RETURN false;
  END IF;
  PERFORM integration.enqueue('market.intelligence', 'market', d, 'institution');
  RETURN true;
END $$;

--
-- Name: record_inbox(text, text, text, text, bigint); Type: FUNCTION; Schema: integration; Owner: -
--

CREATE FUNCTION integration.record_inbox(p_event_id text, p_type text, p_source text, p_aggregate_id text, p_sequence bigint) RETURNS text
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'integration'
    AS $$
DECLARE v_last bigint; v_outcome text;
BEGIN
  PERFORM 1 FROM integration.inbox i WHERE i.event_id = p_event_id;
  IF FOUND THEN RETURN 'duplicate'; END IF;

  SELECT w.last_sequence INTO v_last FROM integration.inbox_watermark w
   WHERE w.aggregate_id = p_aggregate_id FOR UPDATE;
  IF v_last IS NOT NULL AND p_sequence <= v_last THEN
    v_outcome := 'stale';
  ELSE
    v_outcome := 'apply';
    INSERT INTO integration.inbox_watermark (aggregate_id, last_sequence)
    VALUES (p_aggregate_id, p_sequence)
    ON CONFLICT (aggregate_id) DO UPDATE SET last_sequence = excluded.last_sequence;
  END IF;

  INSERT INTO integration.inbox (event_id, type, source, aggregate_id, sequence, outcome)
  VALUES (p_event_id, p_type, p_source, p_aggregate_id, p_sequence, v_outcome)
  ON CONFLICT (event_id) DO NOTHING;
  IF NOT FOUND THEN RETURN 'duplicate'; END IF;
  RETURN v_outcome;
END $$;

--
-- Name: trg_bid_child_emit(); Type: FUNCTION; Schema: integration; Owner: -
--

CREATE FUNCTION integration.trg_bid_child_emit() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'integration'
    AS $$
DECLARE bid uuid := CASE WHEN TG_OP = 'DELETE' THEN OLD.bid_id ELSE NEW.bid_id END;
BEGIN
  BEGIN PERFORM integration.publish_bid(bid);
  EXCEPTION WHEN OTHERS THEN INSERT INTO integration.emit_error (aggregate_id, stage, error) VALUES (bid::text, TG_TABLE_NAME || ' ' || TG_OP, left(SQLERRM, 500));
  END;
  RETURN NULL;
END $$;

--
-- Name: trg_bid_emit(); Type: FUNCTION; Schema: integration; Owner: -
--

CREATE FUNCTION integration.trg_bid_emit() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'integration'
    AS $$
BEGIN
  BEGIN PERFORM integration.publish_bid(NEW.id);
  EXCEPTION WHEN OTHERS THEN INSERT INTO integration.emit_error (aggregate_id, stage, error) VALUES (NEW.id::text, 'bid ' || TG_OP, left(SQLERRM, 500));
  END;
  RETURN NULL;
END $$;

--
-- Name: trg_deal_documents_emit(); Type: FUNCTION; Schema: integration; Owner: -
--

CREATE FUNCTION integration.trg_deal_documents_emit() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'integration'
    AS $$
DECLARE pid uuid;
BEGIN
  IF TG_TABLE_NAME = 'deal_document' THEN
    pid := NEW.pipeline_id;
  ELSE
    SELECT lp.id INTO pid FROM marketplace.loan_pipeline lp WHERE lp.bid_id = NEW.bid_id;
  END IF;
  IF pid IS NULL THEN RETURN NULL; END IF;
  BEGIN PERFORM integration.publish_deal_documents(pid);
  EXCEPTION WHEN OTHERS THEN
    INSERT INTO integration.emit_error (aggregate_id, stage, error)
    VALUES (pid::text, TG_TABLE_NAME || ' ' || TG_OP, left(SQLERRM, 500));
  END;
  RETURN NULL;
END $$;

--
-- Name: trg_deal_progress_emit(); Type: FUNCTION; Schema: integration; Owner: -
--

CREATE FUNCTION integration.trg_deal_progress_emit() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'integration'
    AS $$
DECLARE pid uuid;
BEGIN
  IF TG_TABLE_NAME = 'loan_pipeline' THEN
    pid := NEW.id;
  ELSIF TG_TABLE_NAME = 'pipeline_stage_instance' THEN
    pid := NEW.pipeline_id;
  ELSE
    SELECT lp.id INTO pid FROM marketplace.loan_pipeline lp WHERE lp.bid_id = NEW.bid_id;
  END IF;
  IF pid IS NULL THEN RETURN NULL; END IF;
  BEGIN PERFORM integration.publish_deal_progress(pid);
  EXCEPTION WHEN OTHERS THEN
    INSERT INTO integration.emit_error (aggregate_id, stage, error)
    VALUES (pid::text, 'progress ' || TG_TABLE_NAME || ' ' || TG_OP, left(SQLERRM, 500));
  END;
  RETURN NULL;
END $$;

--
-- Name: trg_requirement_emit(); Type: FUNCTION; Schema: integration; Owner: -
--

CREATE FUNCTION integration.trg_requirement_emit() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'integration'
    AS $$
DECLARE pid uuid;
BEGIN
  FOR pid IN SELECT DISTINCT dd.pipeline_id FROM marketplace.deal_document dd
               JOIN marketplace.loan_pipeline lp ON lp.id = dd.pipeline_id
              WHERE dd.requirement_id = NEW.id AND lp.status = 'active' LOOP
    BEGIN PERFORM integration.publish_deal_documents(pid);
    EXCEPTION WHEN OTHERS THEN
      INSERT INTO integration.emit_error (aggregate_id, stage, error)
      VALUES (pid::text, 'doc_requirement ' || TG_OP, left(SQLERRM, 500));
    END;
  END LOOP;
  RETURN NULL;
END $$;

--
-- Name: accept_bid(uuid, uuid, uuid, jsonb); Type: FUNCTION; Schema: marketplace; Owner: -
--

CREATE FUNCTION marketplace.accept_bid(p_request_id uuid, p_bid_id uuid, p_consumer_id uuid, p_phase2 jsonb) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'marketplace', 'institution', 'public'
    AS $$
declare
  v_inst_id     uuid;
  v_inst        record;
  v_bid         record;
  v_req         record;
  v_pipeline_id uuid;
  v_repeat      boolean := false;
begin
  select * into v_req from marketplace.request
   where id = p_request_id and consumer_id = p_consumer_id
   for update;
  if not found then
    raise exception 'Request % not found or not owned by consumer', p_request_id;
  end if;

  select * into v_bid from marketplace.bid
   where id = p_bid_id and request_id = p_request_id;
  if v_bid.id is null then
    raise exception 'Bid % not found on request %', p_bid_id, p_request_id;
  end if;

  if v_req.status = 'accepted' then
    if v_req.winning_bid_id = p_bid_id then
      v_repeat := true;
    else
      raise exception 'Request % has already accepted another offer', p_request_id;
    end if;
  elsif v_req.status <> 'bidding' then
    raise exception 'Request % is not open for acceptance (status %)', p_request_id, v_req.status;
  elsif v_bid.status not in ('submitted', 'under_review') then
    raise exception 'Bid % cannot be accepted (status %)', p_bid_id, v_bid.status;
  end if;

  v_inst_id := v_bid.institution_id;

  if not v_repeat then
    update marketplace.bid set status = 'accepted', updated_at = now() where id = p_bid_id;

    update marketplace.bid
       set status = 'rejected',
           rejection_reason = 'Another offer was accepted by the client',
           rejected_at = now(), updated_at = now()
     where request_id = p_request_id and id <> p_bid_id
       and status not in ('withdrawn', 'rejected');

    update marketplace.request
       set status = 'accepted', winning_bid_id = p_bid_id,
           accepted_at = now(), updated_at = now()
     where id = p_request_id;

    insert into marketplace.bid_acceptance (
      request_id, bid_id, institution_id,
      full_name, email, phone, address, date_of_birth, document_number)
    values (
      p_request_id, p_bid_id, v_inst_id,
      p_phase2 ->> 'full_name',  p_phase2 ->> 'email',
      p_phase2 ->> 'phone',      p_phase2 ->> 'address',
      (p_phase2 ->> 'date_of_birth')::date,
      p_phase2 ->> 'document_number')
    on conflict (request_id) do nothing;
  end if;

  -- Same transaction as the acceptance: if this raises, nothing above commits.
  -- Idempotent, so a repeat returns the existing deal (or heals a lost one).
  v_pipeline_id := marketplace.create_pipeline_from_acceptance(
    p_request_id, p_bid_id, v_inst_id, p_consumer_id);

  select name, legal_name, primary_contact_name,
         primary_contact_email, primary_contact_phone, logo_url
    into v_inst
    from institution.institution where id = v_inst_id;

  return jsonb_build_object(
    'institution_id',   v_inst_id,
    'institution_name', v_inst.name,
    'legal_name',       v_inst.legal_name,
    'contact_person',   v_inst.primary_contact_name,
    'contact_email',    v_inst.primary_contact_email,
    'contact_phone',    v_inst.primary_contact_phone,
    'logo_url',         v_inst.logo_url,
    'pipeline_id',      v_pipeline_id,
    'rate',             v_bid.rate,
    'rate_type',        v_bid.rate_type,
    'amount_offered',   v_bid.amount_offered,
    'term_months',      v_bid.term_months,
    'repeat',           v_repeat);
end;
$$;

--
-- Name: advance_pipeline_stage(uuid, uuid, uuid, text, boolean); Type: FUNCTION; Schema: marketplace; Owner: -
--

CREATE FUNCTION marketplace.advance_pipeline_stage(p_pipeline_id uuid, p_stage_inst_id uuid, p_actor_id uuid, p_notes text DEFAULT NULL::text, p_is_approval boolean DEFAULT false) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'marketplace', 'institution', 'admin', 'public'
    AS $$
DECLARE
  v_pipeline  record;
  v_stage     record;
  v_def       record;
  v_next_id   uuid;
  v_result    jsonb;
BEGIN
  SELECT * INTO v_pipeline FROM marketplace.loan_pipeline WHERE id = p_pipeline_id;
  IF NOT FOUND THEN RAISE EXCEPTION 'Pipeline % not found', p_pipeline_id; END IF;
  IF v_pipeline.status <> 'active' THEN
    RAISE EXCEPTION 'Pipeline % is not active (status: %)', p_pipeline_id, v_pipeline.status;
  END IF;

  SELECT si.*, sd.requires_maker_checker, sd.stage_key, sd.sla_hours, sd.label
  INTO v_stage
  FROM marketplace.pipeline_stage_instance si
  JOIN institution.pipeline_stage_def       sd ON sd.id = si.stage_def_id
  WHERE si.id = p_stage_inst_id AND si.pipeline_id = p_pipeline_id;

  IF NOT FOUND THEN RAISE EXCEPTION 'Stage instance % not found', p_stage_inst_id; END IF;

  -- ── Maker-checker path ────────────────────────────────────────────────────
  IF v_stage.requires_maker_checker THEN
    IF NOT p_is_approval THEN
      -- Maker submits for approval
      IF v_stage.status NOT IN ('active', 'awaiting_approval') THEN
        RAISE EXCEPTION 'Stage is % — cannot submit', v_stage.status;
      END IF;
      UPDATE marketplace.pipeline_stage_instance
      SET    status       = 'awaiting_approval',
             submitted_by = p_actor_id,
             submitted_at = now(),
             notes        = COALESCE(p_notes, notes),
             updated_at   = now()
      WHERE  id = p_stage_inst_id;

      RETURN jsonb_build_object('status', 'awaiting_approval', 'stage_id', p_stage_inst_id);
    ELSE
      -- Checker approves
      IF v_stage.status <> 'awaiting_approval' THEN
        RAISE EXCEPTION 'Stage must be awaiting_approval to approve (is: %)', v_stage.status;
      END IF;
      IF v_stage.submitted_by = p_actor_id THEN
        RAISE EXCEPTION 'Maker and checker cannot be the same person';
      END IF;
      UPDATE marketplace.pipeline_stage_instance
      SET    status      = 'completed',
             approved_by = p_actor_id,
             approved_at = now(),
             completed_at= now(),
             updated_at  = now()
      WHERE  id = p_stage_inst_id;
    END IF;

  ELSE
    -- ── Standard (no maker-checker) ────────────────────────────────────────
    IF v_stage.status <> 'active' THEN
      RAISE EXCEPTION 'Stage is % — cannot advance', v_stage.status;
    END IF;
    UPDATE marketplace.pipeline_stage_instance
    SET    status        = 'completed',
           submitted_by  = p_actor_id,
           submitted_at  = now(),
           completed_at  = now(),
           notes         = COALESCE(p_notes, notes),
           updated_at    = now()
    WHERE  id = p_stage_inst_id;
  END IF;

  -- ── Activate the next stage (or complete the pipeline) ────────────────────
  SELECT id INTO v_next_id
  FROM marketplace.pipeline_stage_instance
  WHERE pipeline_id = p_pipeline_id
    AND position    = v_stage.position + 1
    AND status      = 'pending';

  IF v_next_id IS NOT NULL THEN
    UPDATE marketplace.pipeline_stage_instance
    SET    status     = 'active',
           started_at = now(),
           updated_at = now()
    WHERE  id = v_next_id;

    UPDATE marketplace.loan_pipeline
    SET    current_stage_id = v_next_id, updated_at = now()
    WHERE  id = p_pipeline_id;

    v_result := jsonb_build_object('status', 'advanced', 'next_stage_id', v_next_id);
  ELSE
    -- All stages done — pipeline complete
    UPDATE marketplace.loan_pipeline
    SET    status       = 'completed',
           completed_at = now(),
           updated_at   = now()
    WHERE  id = p_pipeline_id;

    -- Fire commission event if disbursement stage
    IF v_stage.stage_key = 'disbursement' THEN
      INSERT INTO portal_admin.commission_event (
        pipeline_id, request_id, institution_id, deal_amount
      )
      SELECT p_pipeline_id, v_pipeline.request_id,
             v_pipeline.institution_id, v_pipeline.deal_amount
      FROM marketplace.loan_pipeline WHERE id = p_pipeline_id
      ON CONFLICT (pipeline_id) DO NOTHING;
    END IF;

    v_result := jsonb_build_object('status', 'completed', 'pipeline_id', p_pipeline_id);
  END IF;

  RETURN v_result;
END;
$$;

--
-- Name: block_bid_event_mutation(); Type: FUNCTION; Schema: marketplace; Owner: -
--

CREATE FUNCTION marketplace.block_bid_event_mutation() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog', 'public', 'extensions', 'pg_temp'
    AS $$
BEGIN
  RAISE EXCEPTION 'marketplace.bid_event is append-only';
END;
$$;

--
-- Name: booking_fee_inputs(uuid, uuid); Type: FUNCTION; Schema: marketplace; Owner: -
--

CREATE FUNCTION marketplace.booking_fee_inputs(p_pipeline_id uuid, p_institution_id uuid) RETURNS jsonb
    LANGUAGE sql STABLE
    SET search_path TO ''
    AS $$
  select jsonb_build_object(
           'monthly_amount', coalesce(r.basis = 'annualised', false),
           'term_months',    coalesce(r.basis = 'amount_x_term', false))
    from marketplace.loan_pipeline lp
    join marketplace.request rq on rq.id = lp.request_id
    join catalog.product p on p.id = rq.product_id
    left join lateral billing.rule_for(lp.institution_id, p.code, (now() at time zone 'Indian/Mauritius')::date) r on true
   where lp.id = p_pipeline_id and lp.institution_id = p_institution_id
$$;

--
-- Name: chat_thread_state(uuid, uuid); Type: FUNCTION; Schema: marketplace; Owner: -
--

CREATE FUNCTION marketplace.chat_thread_state(p_request_id uuid, p_institution_id uuid) RETURNS TABLE(is_open boolean, is_winner boolean)
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'marketplace'
    AS $$
  SELECT NOT EXISTS (SELECT 1 FROM marketplace.bid b WHERE b.request_id = p_request_id AND b.status = 'accepted')
         OR EXISTS (SELECT 1 FROM marketplace.bid b WHERE b.request_id = p_request_id AND b.status = 'accepted'
                      AND b.institution_id = p_institution_id),
         EXISTS (SELECT 1 FROM marketplace.bid b WHERE b.request_id = p_request_id AND b.status = 'accepted'
                   AND b.institution_id = p_institution_id);
$$;

--
-- Name: close_deal(uuid, uuid, text, text, uuid); Type: FUNCTION; Schema: marketplace; Owner: -
--

CREATE FUNCTION marketplace.close_deal(p_pipeline_id uuid, p_institution_id uuid, p_outcome text, p_reason text, p_actor uuid) RETURNS jsonb
    LANGUAGE plpgsql
    SET search_path TO 'marketplace', 'institution', 'public', 'pg_temp'
    AS $$
DECLARE d marketplace.loan_pipeline%ROWTYPE; v_inst uuid;
BEGIN
  IF p_outcome NOT IN ('declined', 'withdrawn') THEN RAISE EXCEPTION 'DEAL_OUTCOME_INVALID'; END IF;
  IF p_reason IS NULL OR char_length(btrim(p_reason)) < 5 THEN RAISE EXCEPTION 'DEAL_REASON_REQUIRED'; END IF;
  SELECT * INTO d FROM marketplace.loan_pipeline
   WHERE id = p_pipeline_id AND institution_id = p_institution_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'DEAL_NOT_FOUND'; END IF;
  IF d.status <> 'active' THEN RAISE EXCEPTION 'DEAL_NOT_ACTIVE'; END IF;

  UPDATE marketplace.loan_pipeline
     SET status = p_outcome::pipeline_status_enum, closed_reason = btrim(p_reason),
         closed_at = now(), closed_by = p_actor, updated_at = now()
   WHERE id = d.id;

  -- Stop any approval still running. The deal is no longer active, so the
  -- outcome trigger leaves it alone.
  FOR v_inst IN
    SELECT psi.approval_instance_id FROM marketplace.pipeline_stage_instance psi
      JOIN institution.approval_instance ai ON ai.id = psi.approval_instance_id
     WHERE psi.pipeline_id = d.id AND ai.status = 'in_progress'
  LOOP
    PERFORM institution.approvals_withdraw(v_inst, p_actor, 'Deal ' || p_outcome || ': ' || btrim(p_reason));
  END LOOP;

  UPDATE marketplace.pipeline_stage_instance
     SET status = 'skipped', updated_at = now()
   WHERE pipeline_id = d.id AND status IN ('pending', 'active', 'awaiting_approval');

  INSERT INTO public.portal_notifications (institution_id, kind, title, body, link, metadata)
  VALUES (d.institution_id, 'deal_closed',
          CASE p_outcome WHEN 'declined' THEN 'Deal declined' ELSE 'Deal withdrawn' END,
          btrim(p_reason), '/deals/' || d.id,
          jsonb_build_object('pipeline_id', d.id, 'outcome', p_outcome));

  RETURN jsonb_build_object('pipeline_id', d.id, 'status', p_outcome, 'reason', btrim(p_reason));
END $$;

--
-- Name: close_expired_windows(); Type: FUNCTION; Schema: marketplace; Owner: -
--

CREATE FUNCTION marketplace.close_expired_windows() RETURNS TABLE(request_id uuid, new_status text, bid_count bigint)
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'marketplace', 'governance', 'integration', 'public'
    AS $$
DECLARE v_row record;
BEGIN
  FOR v_row IN
    WITH expired AS (
      SELECT r.id, r.source, COUNT(b.id) FILTER (WHERE b.status NOT IN ('withdrawn','rejected')) AS active_bids
        FROM marketplace.request r
        LEFT JOIN marketplace.bid b ON b.request_id = r.id
       WHERE r.status = 'bidding' AND r.bid_window_closes_at < now()
       GROUP BY r.id, r.source
    ), updated AS (
      UPDATE marketplace.request r
         SET status = CASE WHEN e.active_bids > 0 THEN 'closed' ELSE 'expired' END, updated_at = now()
        FROM expired e WHERE r.id = e.id
      RETURNING r.id, CASE WHEN e.active_bids > 0 THEN 'closed' ELSE 'expired' END AS new_status, e.active_bids, e.source
    )
    SELECT u.id, u.new_status, u.active_bids, u.source FROM updated u
  LOOP
    UPDATE governance.action SET status = 'expired', updated_at = now()
     WHERE category = 'bid.submit' AND status = 'pending' AND payload->>'request_id' = v_row.id::text;
    IF v_row.source = 'app' THEN
      PERFORM integration.enqueue('request.window_closed', v_row.id::text, jsonb_build_object(
        'request_id', v_row.id,
        'outcome', CASE WHEN v_row.active_bids > 0 THEN 'offers' ELSE 'no_offers' END,
        'offer_count', v_row.active_bids, 'closed_at', now()), 'institution');
    END IF;
    request_id := v_row.id; new_status := v_row.new_status; bid_count := v_row.active_bids;
    RETURN NEXT;
  END LOOP;
END;
$$;

--
-- Name: create_pipeline_from_acceptance(uuid, uuid, uuid, uuid); Type: FUNCTION; Schema: marketplace; Owner: -
--

CREATE FUNCTION marketplace.create_pipeline_from_acceptance(p_request_id uuid, p_bid_id uuid, p_institution_id uuid, p_consumer_id uuid) RETURNS uuid
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
declare
  v_template_id uuid;
  v_product_id  uuid;
  v_pipeline_id uuid;
  v_bid         record;
  v_first_stage uuid;
begin
  -- Not licensed for deals: no deal, by design (commercial decision, unchanged).
  if not coalesce((select i.modules from institution.institution i
                    where i.id = p_institution_id), '[]'::jsonb) ? 'pipeline' then
    return null;
  end if;

  -- Idempotent: one deal per accepted bid.
  select id into v_pipeline_id from marketplace.loan_pipeline where bid_id = p_bid_id;
  if found then
    return v_pipeline_id;
  end if;

  select r.product_id into v_product_id from marketplace.request r where r.id = p_request_id;
  v_template_id := marketplace.deal_template_for(p_institution_id, v_product_id);
  if v_template_id is null then
    raise exception using
      errcode = 'P0001',
      message = 'DEAL_TEMPLATE_MISSING: no active deal template with stages for this product';
  end if;

  select amount_offered, rate, term_months into v_bid
    from marketplace.bid where id = p_bid_id;

  insert into marketplace.loan_pipeline (
    request_id, bid_id, institution_id, consumer_id,
    template_id, deal_amount, deal_rate, deal_term_months)
  values (
    p_request_id, p_bid_id, p_institution_id, p_consumer_id,
    v_template_id, v_bid.amount_offered, v_bid.rate, v_bid.term_months)
  returning id into v_pipeline_id;

  insert into marketplace.pipeline_stage_instance (pipeline_id, stage_def_id, position, status, sla_due_at)
  select v_pipeline_id, s.id, s.position, 'pending',
         case when s.sla_hours is null then null else now() + make_interval(hours => s.sla_hours) end
    from institution.pipeline_stage_def s
   where s.template_id = v_template_id and s.is_active;

  update marketplace.pipeline_stage_instance
     set status = 'active', started_at = now()
   where id = (select id from marketplace.pipeline_stage_instance
                where pipeline_id = v_pipeline_id
                order by position limit 1)
  returning id into v_first_stage;

  if v_first_stage is null then  -- unreachable given the resolver; belt and braces
    raise exception 'DEAL_TEMPLATE_MISSING: template % has no active stage', v_template_id;
  end if;

  update marketplace.loan_pipeline set current_stage_id = v_first_stage where id = v_pipeline_id;
  return v_pipeline_id;
end
$$;

--
-- Name: deal_document_act(uuid, uuid, text, uuid, text, text, text, text, bigint); Type: FUNCTION; Schema: marketplace; Owner: -
--

CREATE FUNCTION marketplace.deal_document_act(p_document_id uuid, p_institution_id uuid, p_action text, p_actor uuid, p_note text DEFAULT NULL::text, p_storage_path text DEFAULT NULL::text, p_file_name text DEFAULT NULL::text, p_mime_type text DEFAULT NULL::text, p_file_size bigint DEFAULT NULL::bigint) RETURNS jsonb
    LANGUAGE plpgsql
    SET search_path TO 'marketplace', 'institution', 'pg_temp'
    AS $$
DECLARE
    d   marketplace.deal_document%ROWTYPE;
    lp  marketplace.loan_pipeline%ROWTYPE;
    v_note text := NULLIF(btrim(COALESCE(p_note, '')), '');
BEGIN
    SELECT * INTO d FROM marketplace.deal_document
     WHERE id = p_document_id AND institution_id = p_institution_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'DOC_NOT_FOUND'; END IF;
    SELECT * INTO lp FROM marketplace.loan_pipeline WHERE id = d.pipeline_id;
    IF lp.status <> 'active' THEN RAISE EXCEPTION 'DOC_DEAL_CLOSED'; END IF;

    IF p_action = 'receive' THEN
        IF d.status = 'verified' THEN RAISE EXCEPTION 'DOC_ALREADY_VERIFIED'; END IF;
        IF p_storage_path IS NULL OR p_file_name IS NULL THEN RAISE EXCEPTION 'DOC_FILE_REQUIRED'; END IF;
        UPDATE marketplace.deal_document
           SET status = 'received', storage_path = p_storage_path, file_name = p_file_name,
               mime_type = p_mime_type, file_size = p_file_size,
               uploaded_by = p_actor, uploaded_at = now(),
               reviewed_by = NULL, reviewed_at = NULL, review_note = NULL, updated_at = now()
         WHERE id = d.id;
    ELSIF p_action = 'verify' THEN
        IF d.status <> 'received' THEN RAISE EXCEPTION 'DOC_NOT_RECEIVED'; END IF;
        IF d.uploaded_by = p_actor THEN RAISE EXCEPTION 'DOC_FOUR_EYES'; END IF;
        UPDATE marketplace.deal_document
           SET status = 'verified', reviewed_by = p_actor, reviewed_at = now(),
               review_note = v_note, updated_at = now()
         WHERE id = d.id;
    ELSIF p_action = 'reject' THEN
        IF d.status <> 'received' THEN RAISE EXCEPTION 'DOC_NOT_RECEIVED'; END IF;
        IF v_note IS NULL THEN RAISE EXCEPTION 'DOC_NOTE_REQUIRED'; END IF;
        UPDATE marketplace.deal_document
           SET status = 'rejected', reviewed_by = p_actor, reviewed_at = now(),
               review_note = v_note, updated_at = now()
         WHERE id = d.id;
    ELSIF p_action = 'waive' THEN
        IF d.status IN ('verified', 'waived') THEN RAISE EXCEPTION 'DOC_NOT_WAIVABLE'; END IF;
        IF v_note IS NULL THEN RAISE EXCEPTION 'DOC_NOTE_REQUIRED'; END IF;
        UPDATE marketplace.deal_document
           SET status = 'waived', reviewed_by = p_actor, reviewed_at = now(),
               review_note = v_note, updated_at = now()
         WHERE id = d.id;
    ELSE
        RAISE EXCEPTION 'DOC_ACTION_INVALID';
    END IF;

    INSERT INTO marketplace.deal_document_event (document_id, action, actor_id, note, file_name, storage_path)
    VALUES (d.id, p_action, p_actor, v_note,
            CASE WHEN p_action = 'receive' THEN p_file_name END,
            CASE WHEN p_action = 'receive' THEN p_storage_path END);

    SELECT * INTO d FROM marketplace.deal_document WHERE id = d.id;
    RETURN to_jsonb(d);
END $$;

--
-- Name: deal_template_for(uuid, uuid); Type: FUNCTION; Schema: marketplace; Owner: -
--

CREATE FUNCTION marketplace.deal_template_for(p_institution_id uuid, p_product_id uuid) RETURNS uuid
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO ''
    AS $$
  select t.id
    from institution.pipeline_template t
   where t.institution_id = p_institution_id
     and t.is_active
     and (t.product_code is null
          or t.product_code = (select p.code from catalog.product p where p.id = p_product_id))
     and exists (select 1 from institution.pipeline_stage_def s
                  where s.template_id = t.id and s.is_active)
   order by (t.product_code is not null) desc, t.is_default desc, t.created_at
   limit 1
$$;

--
-- Name: decline_request(uuid, uuid, uuid, text); Type: FUNCTION; Schema: marketplace; Owner: -
--

CREATE FUNCTION marketplace.decline_request(p_request_id uuid, p_institution_id uuid, p_actor uuid, p_reason text) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'marketplace', 'integration'
    AS $$
DECLARE r marketplace.request%ROWTYPE; v_reason text := nullif(btrim(p_reason), ''); v_at timestamptz := now();
BEGIN
  SELECT * INTO r FROM marketplace.request WHERE id = p_request_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'DECLINE_NOT_FOUND'; END IF;
  IF r.status NOT IN ('open', 'bidding') THEN RAISE EXCEPTION 'DECLINE_NOT_OPEN'; END IF;
  IF EXISTS (SELECT 1 FROM marketplace.bid b WHERE b.request_id = p_request_id AND b.institution_id = p_institution_id
              AND b.status IN ('submitted', 'under_review')) THEN
    RAISE EXCEPTION 'DECLINE_HAS_BID';
  END IF;
  IF char_length(v_reason) > 500 THEN RAISE EXCEPTION 'DECLINE_REASON_TOO_LONG'; END IF;
  INSERT INTO marketplace.request_decline (request_id, institution_id, reason, declined_by, declined_at)
  VALUES (p_request_id, p_institution_id, v_reason, p_actor, v_at)
  ON CONFLICT (request_id, institution_id) DO NOTHING;
  IF NOT FOUND THEN RAISE EXCEPTION 'DECLINE_ALREADY'; END IF;
  IF r.source = 'app' THEN
    PERFORM integration.enqueue('request.declined', p_request_id::text,
      jsonb_build_object('request_id', p_request_id, 'reason', v_reason, 'declined_at', v_at), 'institution');
  END IF;
  RETURN jsonb_build_object('request_id', p_request_id, 'declined_at', v_at);
END $$;

--
-- Name: ensure_deal_documents(uuid); Type: FUNCTION; Schema: marketplace; Owner: -
--

CREATE FUNCTION marketplace.ensure_deal_documents(p_pipeline_id uuid) RETURNS integer
    LANGUAGE plpgsql
    SET search_path TO 'marketplace', 'institution', 'pg_temp'
    AS $$
DECLARE n integer;
BEGIN
    INSERT INTO marketplace.deal_document (pipeline_id, requirement_id, institution_id)
    SELECT lp.id, r.id, lp.institution_id
      FROM marketplace.loan_pipeline lp
      JOIN institution.doc_requirement r
        ON r.template_id = lp.template_id AND r.institution_id = lp.institution_id AND r.is_active
     WHERE lp.id = p_pipeline_id
    ON CONFLICT (pipeline_id, requirement_id) DO NOTHING;
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
END $$;

--
-- Name: expire_bids_on_request_end(); Type: FUNCTION; Schema: marketplace; Owner: -
--

CREATE FUNCTION marketplace.expire_bids_on_request_end() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'marketplace'
    AS $$
begin
  update marketplace.bid
     set status = 'expired'
   where request_id = new.id
     and status in ('draft', 'submitted', 'under_review');
  return null;
end
$$;

--
-- Name: guard_bid_compliance(); Type: FUNCTION; Schema: marketplace; Owner: -
--

CREATE FUNCTION marketplace.guard_bid_compliance() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
declare c record;
begin
  select can_bid, is_approved, missing_docs into c
    from institution.compliance where institution_id = new.institution_id;
  if not found or not c.can_bid then
    raise exception using
      errcode = 'P0001',
      message = 'This institution cannot bid yet: '
                || case when not coalesce(c.is_approved, false)
                        then 'it is not approved or is suspended'
                        else 'required documents missing or not approved ('
                             || array_to_string(c.missing_docs, ', ') || ')' end;
  end if;
  return new;
end
$$;

--
-- Name: guard_bid_deal_template(); Type: FUNCTION; Schema: marketplace; Owner: -
--

CREATE FUNCTION marketplace.guard_bid_deal_template() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
declare v_product uuid;
begin
  if coalesce((select i.modules from institution.institution i
                where i.id = new.institution_id), '[]'::jsonb) ? 'pipeline' then
    select r.product_id into v_product from marketplace.request r where r.id = new.request_id;
    if marketplace.deal_template_for(new.institution_id, v_product) is null then
      raise exception using
        errcode = 'P0001',
        message = 'This institution cannot bid on this product yet: set up an active deal '
                  || 'template with at least one stage for it (Setup > Deal templates)';
    end if;
  end if;
  return new;
end
$$;

--
-- Name: guard_bid_window(); Type: FUNCTION; Schema: marketplace; Owner: -
--

CREATE FUNCTION marketplace.guard_bid_window() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog', 'public', 'extensions', 'pg_temp'
    AS $$
DECLARE
  r_status   text;
  r_closes   timestamptz;
BEGIN
  SELECT status, bid_window_closes_at
  INTO   r_status, r_closes
  FROM   marketplace.request
  WHERE  id = NEW.request_id;

  IF r_status IS NULL THEN
    RAISE EXCEPTION 'Request % not found', NEW.request_id;
  END IF;

  IF r_status <> 'bidding' THEN
    RAISE EXCEPTION 'Request % is not open for bidding (status: %)',
      NEW.request_id, r_status;
  END IF;

  IF r_closes IS NOT NULL AND r_closes < now() THEN
    RAISE EXCEPTION 'Bid window for request % has closed at %',
      NEW.request_id, r_closes;
  END IF;

  RETURN NEW;
END;
$$;

--
-- Name: guard_bid_withdraw(); Type: FUNCTION; Schema: marketplace; Owner: -
--

CREATE FUNCTION marketplace.guard_bid_withdraw() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog', 'marketplace'
    AS $$
begin
  if new.status = 'withdrawn' and old.status is distinct from 'withdrawn' then
    if old.status not in ('draft', 'submitted', 'under_review') then
      raise exception 'BID_NOT_WITHDRAWABLE: bid % is %', old.id, old.status
        using errcode = 'P0001';
    end if;
    new.withdrawn_at := coalesce(new.withdrawn_at, now());
  end if;
  return new;
end $$;

--
-- Name: ingest_app_request(uuid, uuid, text, numeric, integer, numeric, timestamp with time zone, text, timestamp with time zone, jsonb, text, jsonb); Type: FUNCTION; Schema: marketplace; Owner: -
--

CREATE FUNCTION marketplace.ingest_app_request(p_app_request_id uuid, p_consumer_id uuid, p_app_product_type text, p_amount numeric, p_term_months integer, p_max_rate numeric, p_deadline timestamp with time zone, p_status text, p_created_at timestamp with time zone, p_phase1 jsonb DEFAULT '{}'::jsonb, p_allocation_mode text DEFAULT NULL::text, p_allocations jsonb DEFAULT NULL::jsonb) RETURNS uuid
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'marketplace', 'catalog', 'public'
    AS $$
DECLARE
  v_product_id uuid; v_status text; v_close_at timestamptz; v_anon_id uuid; v_alloc jsonb; v_alloc_changed boolean;
BEGIN
  v_product_id := catalog.product_id_for_app_type(p_app_product_type);
  IF v_product_id IS NULL THEN
    RAISE EXCEPTION 'No catalog product for app type: %', p_app_product_type;
  END IF;
  v_anon_id := md5(p_consumer_id::text || ':ficium-anon-v1:')::uuid;
  v_status := CASE p_status
    WHEN 'open'      THEN 'bidding'
    WHEN 'accepted'  THEN 'accepted'
    WHEN 'cancelled' THEN 'cancelled'
    WHEN 'expired'   THEN 'expired'
    -- App 'closed' has no Portal equivalent (request_status_check rejects it). It is terminal and
    -- relistable exactly like 'expired', which is how the Portal already held the one such request.
    WHEN 'closed'    THEN 'expired'
    ELSE 'bidding'
  END;
  -- A bad or early App deadline must never break request_check (closes > opens): clamp.
  v_close_at := GREATEST(COALESCE(p_deadline, p_created_at + interval '72 hours'), p_created_at + interval '1 second');

  INSERT INTO marketplace.request AS r (
    id, consumer_id, consumer_ref, product_id, country, currency, amount, term_months,
    params, status, bid_window_opens_at, bid_window_closes_at,
    idempotency_key, source, metadata, created_at, allocation_mode
  ) VALUES (
    p_app_request_id, v_anon_id, LEFT(v_anon_id::text, 8), v_product_id, 'MU', 'MUR',
    p_amount, COALESCE(p_term_months, 12),
    jsonb_strip_nulls(jsonb_build_object(
      'app_product_type', p_app_product_type, 'max_rate', p_max_rate,
      'loan_purpose', p_phase1 -> 'loan_purpose', 'collateral_type', p_phase1 -> 'collateral_type',
      'collateral_sub', p_phase1 -> 'collateral_sub', 'ltv_pct', p_phase1 -> 'ltv_pct')),
    v_status, p_created_at, v_close_at, p_app_request_id::text, 'app',
    jsonb_strip_nulls(jsonb_build_object(
      'ficium_attested', true, 'kyc_verified', p_phase1 -> 'kyc_verified',
      'employment_status', p_phase1 -> 'employment_status', 'employment_type', p_phase1 -> 'employment_type',
      -- 'employer' deliberately excluded (re-identification risk).
      'years_employed', p_phase1 -> 'years_employed', 'gross_monthly_income', p_phase1 -> 'gross_monthly_income',
      'income_verified', p_phase1 -> 'income_verified', 'dsr_current_pct', p_phase1 -> 'dsr_current_pct',
      'dsr_post_pct', p_phase1 -> 'dsr_post_pct', 'net_worth_band', p_phase1 -> 'net_worth_band',
      'has_existing_loans', p_phase1 -> 'has_existing_loans', 'existing_monthly_repayment', p_phase1 -> 'existing_monthly_repayment',
      'existing_loan_balance', p_phase1 -> 'existing_loan_balance', 'loan_breakdown', p_phase1 -> 'loan_breakdown',
      'health_score', p_phase1 -> 'health_score', 'risk_score', p_phase1 -> 'risk_score',
      'affordability_score', p_phase1 -> 'affordability_score', 'risk_tier', p_phase1 -> 'risk_tier',
      'age', p_phase1 -> 'age', 'risk_appetite', p_phase1 -> 'risk_appetite',
      'investment_horizon', p_phase1 -> 'investment_horizon', 'liquidity_pref', p_phase1 -> 'liquidity_pref',
      'investment_style', p_phase1 -> 'investment_style', 'target_amount', p_phase1 -> 'target_amount',
      'monthly_contribution', p_phase1 -> 'monthly_contribution', 'investment_objective', p_phase1 -> 'investment_objective',
      'investment_product_answers', p_phase1 -> 'investment_product_answers')),
    p_created_at, p_allocation_mode
  )
  -- Match on id (the real identity). The old arbiter, idempotency_key, missed the one row created by the
  -- very first sync version (key 'app_sync_...'), which then collided on the primary key.
  ON CONFLICT (id) DO UPDATE SET
    consumer_id = EXCLUDED.consumer_id, consumer_ref = EXCLUDED.consumer_ref, status = EXCLUDED.status,
    amount = EXCLUDED.amount, term_months = EXCLUDED.term_months, params = EXCLUDED.params, metadata = EXCLUDED.metadata,
    -- Only repair the window start when it would otherwise break closes > opens.
    bid_window_opens_at = CASE WHEN EXCLUDED.bid_window_closes_at <= r.bid_window_opens_at
                               THEN EXCLUDED.bid_window_opens_at ELSE r.bid_window_opens_at END,
    bid_window_closes_at = EXCLUDED.bid_window_closes_at,
    allocation_mode = EXCLUDED.allocation_mode, updated_at = now()
  -- Skip no-op rewrites: before this, every unchanged row was rewritten on every 5-minute sync.
  WHERE (r.consumer_id, r.consumer_ref, r.status, r.amount, r.term_months, r.params, r.metadata, r.bid_window_closes_at, r.allocation_mode)
        IS DISTINCT FROM
        (EXCLUDED.consumer_id, EXCLUDED.consumer_ref, EXCLUDED.status, EXCLUDED.amount, EXCLUDED.term_months, EXCLUDED.params, EXCLUDED.metadata, EXCLUDED.bid_window_closes_at, EXCLUDED.allocation_mode)
     OR r.bid_window_opens_at >= EXCLUDED.bid_window_closes_at;

  IF p_allocations IS NOT NULL THEN
    WITH incoming AS (
      SELECT catalog.product_id_for_app_type(a ->> 'product_type') AS product_id,
             NULLIF(a ->> 'amount', '')::numeric AS amount, COALESCE((a ->> 'sort_order')::int, 0) AS sort_order
        FROM jsonb_array_elements(p_allocations) a),
    existing AS (SELECT product_id, amount, sort_order FROM marketplace.request_allocation WHERE request_id = p_app_request_id)
    SELECT EXISTS (SELECT * FROM incoming EXCEPT SELECT * FROM existing)
        OR EXISTS (SELECT * FROM existing EXCEPT SELECT * FROM incoming)
        OR (SELECT count(*) FROM incoming) <> (SELECT count(*) FROM existing)
      INTO v_alloc_changed;
    IF v_alloc_changed THEN  -- delete + re-insert only when the allocation set actually changed
      DELETE FROM marketplace.request_allocation WHERE request_id = p_app_request_id;
      FOR v_alloc IN SELECT * FROM jsonb_array_elements(p_allocations) LOOP
        INSERT INTO marketplace.request_allocation (request_id, product_id, amount, sort_order)
        VALUES (p_app_request_id, catalog.product_id_for_app_type(v_alloc ->> 'product_type'),
                NULLIF(v_alloc ->> 'amount', '')::numeric, COALESCE((v_alloc ->> 'sort_order')::int, 0));
      END LOOP;
    END IF;
  END IF;
  RETURN p_app_request_id;
END;
$$;

--
-- Name: ingest_request(uuid, uuid, text, numeric, integer, numeric, timestamp with time zone, text, timestamp with time zone, jsonb, text, jsonb); Type: FUNCTION; Schema: marketplace; Owner: -
--

CREATE FUNCTION marketplace.ingest_request(p_app_request_id uuid, p_consumer_id uuid, p_app_product_type text, p_amount numeric, p_term_months integer, p_max_rate numeric, p_deadline timestamp with time zone, p_status text, p_created_at timestamp with time zone, p_phase1 jsonb DEFAULT '{}'::jsonb, p_allocation_mode text DEFAULT NULL::text, p_allocations jsonb DEFAULT NULL::jsonb) RETURNS uuid
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'marketplace', 'catalog', 'public'
    AS $$
DECLARE
  v_product_id uuid; v_status text; v_close_at timestamptz; v_anon_id uuid; v_alloc jsonb; v_alloc_changed boolean;
BEGIN
  v_product_id := catalog.product_id_for_app_type(p_app_product_type);
  IF v_product_id IS NULL THEN
    RAISE EXCEPTION 'No catalog product for app type: %', p_app_product_type;
  END IF;
  v_anon_id := p_consumer_id;  -- contract id, as given (db/034)
  v_status := CASE p_status
    WHEN 'open'      THEN 'bidding'
    WHEN 'accepted'  THEN 'accepted'
    WHEN 'cancelled' THEN 'cancelled'
    WHEN 'expired'   THEN 'expired'
    -- App 'closed' has no Portal equivalent (request_status_check rejects it). It is terminal and
    -- relistable exactly like 'expired', which is how the Portal already held the one such request.
    WHEN 'closed'    THEN 'expired'
    ELSE 'bidding'
  END;
  -- A bad or early App deadline must never break request_check (closes > opens): clamp.
  v_close_at := GREATEST(COALESCE(p_deadline, p_created_at + interval '72 hours'), p_created_at + interval '1 second');

  INSERT INTO marketplace.request AS r (
    id, consumer_id, consumer_ref, product_id, country, currency, amount, term_months,
    params, status, bid_window_opens_at, bid_window_closes_at,
    idempotency_key, source, metadata, created_at, allocation_mode
  ) VALUES (
    p_app_request_id, v_anon_id, LEFT(v_anon_id::text, 8), v_product_id, 'MU', 'MUR',
    p_amount, COALESCE(p_term_months, 12),
    jsonb_strip_nulls(jsonb_build_object(
      'app_product_type', p_app_product_type, 'max_rate', p_max_rate,
      'loan_purpose', p_phase1 -> 'loan_purpose', 'collateral_type', p_phase1 -> 'collateral_type',
      'collateral_sub', p_phase1 -> 'collateral_sub', 'ltv_pct', p_phase1 -> 'ltv_pct')),
    v_status, p_created_at, v_close_at, p_app_request_id::text, 'app',
    jsonb_strip_nulls(jsonb_build_object(
      'ficium_attested', true, 'kyc_verified', p_phase1 -> 'kyc_verified',
      'employment_status', p_phase1 -> 'employment_status', 'employment_type', p_phase1 -> 'employment_type',
      -- 'employer' deliberately excluded (re-identification risk).
      'years_employed', p_phase1 -> 'years_employed', 'gross_monthly_income', p_phase1 -> 'gross_monthly_income',
      'income_verified', p_phase1 -> 'income_verified', 'dsr_current_pct', p_phase1 -> 'dsr_current_pct',
      'dsr_post_pct', p_phase1 -> 'dsr_post_pct', 'net_worth_band', p_phase1 -> 'net_worth_band',
      'has_existing_loans', p_phase1 -> 'has_existing_loans', 'existing_monthly_repayment', p_phase1 -> 'existing_monthly_repayment',
      'existing_loan_balance', p_phase1 -> 'existing_loan_balance', 'loan_breakdown', p_phase1 -> 'loan_breakdown',
      'health_score', p_phase1 -> 'health_score', 'risk_score', p_phase1 -> 'risk_score',
      'affordability_score', p_phase1 -> 'affordability_score', 'risk_tier', p_phase1 -> 'risk_tier',
      'age', p_phase1 -> 'age', 'risk_appetite', p_phase1 -> 'risk_appetite',
      'investment_horizon', p_phase1 -> 'investment_horizon', 'liquidity_pref', p_phase1 -> 'liquidity_pref',
      'investment_style', p_phase1 -> 'investment_style', 'target_amount', p_phase1 -> 'target_amount',
      'monthly_contribution', p_phase1 -> 'monthly_contribution', 'investment_objective', p_phase1 -> 'investment_objective',
      'investment_product_answers', p_phase1 -> 'investment_product_answers')),
    p_created_at, p_allocation_mode
  )
  -- Match on id (the real identity). The old arbiter, idempotency_key, missed the one row created by the
  -- very first sync version (key 'app_sync_...'), which then collided on the primary key.
  ON CONFLICT (id) DO UPDATE SET
    consumer_id = EXCLUDED.consumer_id, consumer_ref = EXCLUDED.consumer_ref, status = EXCLUDED.status,
    amount = EXCLUDED.amount, term_months = EXCLUDED.term_months, params = EXCLUDED.params, metadata = EXCLUDED.metadata,
    -- Only repair the window start when it would otherwise break closes > opens.
    bid_window_opens_at = CASE WHEN EXCLUDED.bid_window_closes_at <= r.bid_window_opens_at
                               THEN EXCLUDED.bid_window_opens_at ELSE r.bid_window_opens_at END,
    bid_window_closes_at = EXCLUDED.bid_window_closes_at,
    allocation_mode = EXCLUDED.allocation_mode, updated_at = now()
  -- Skip no-op rewrites: before this, every unchanged row was rewritten on every 5-minute sync.
  WHERE (r.consumer_id, r.consumer_ref, r.status, r.amount, r.term_months, r.params, r.metadata, r.bid_window_closes_at, r.allocation_mode)
        IS DISTINCT FROM
        (EXCLUDED.consumer_id, EXCLUDED.consumer_ref, EXCLUDED.status, EXCLUDED.amount, EXCLUDED.term_months, EXCLUDED.params, EXCLUDED.metadata, EXCLUDED.bid_window_closes_at, EXCLUDED.allocation_mode)
     OR r.bid_window_opens_at >= EXCLUDED.bid_window_closes_at;

  IF p_allocations IS NOT NULL THEN
    WITH incoming AS (
      SELECT catalog.product_id_for_app_type(a ->> 'product_type') AS product_id,
             NULLIF(a ->> 'amount', '')::numeric AS amount, COALESCE((a ->> 'sort_order')::int, 0) AS sort_order
        FROM jsonb_array_elements(p_allocations) a),
    existing AS (SELECT product_id, amount, sort_order FROM marketplace.request_allocation WHERE request_id = p_app_request_id)
    SELECT EXISTS (SELECT * FROM incoming EXCEPT SELECT * FROM existing)
        OR EXISTS (SELECT * FROM existing EXCEPT SELECT * FROM incoming)
        OR (SELECT count(*) FROM incoming) <> (SELECT count(*) FROM existing)
      INTO v_alloc_changed;
    IF v_alloc_changed THEN  -- delete + re-insert only when the allocation set actually changed
      DELETE FROM marketplace.request_allocation WHERE request_id = p_app_request_id;
      FOR v_alloc IN SELECT * FROM jsonb_array_elements(p_allocations) LOOP
        INSERT INTO marketplace.request_allocation (request_id, product_id, amount, sort_order)
        VALUES (p_app_request_id, catalog.product_id_for_app_type(v_alloc ->> 'product_type'),
                NULLIF(v_alloc ->> 'amount', '')::numeric, COALESCE((v_alloc ->> 'sort_order')::int, 0));
      END LOOP;
    END IF;
  END IF;
  RETURN p_app_request_id;
END;
$$;

--
-- Name: FUNCTION ingest_request(p_app_request_id uuid, p_consumer_id uuid, p_app_product_type text, p_amount numeric, p_term_months integer, p_max_rate numeric, p_deadline timestamp with time zone, p_status text, p_created_at timestamp with time zone, p_phase1 jsonb, p_allocation_mode text, p_allocations jsonb); Type: COMMENT; Schema: marketplace; Owner: -
--

COMMENT ON FUNCTION marketplace.ingest_request(p_app_request_id uuid, p_consumer_id uuid, p_app_product_type text, p_amount numeric, p_term_months integer, p_max_rate numeric, p_deadline timestamp with time zone, p_status text, p_created_at timestamp with time zone, p_phase1 jsonb, p_allocation_mode text, p_allocations jsonb) IS 'db/034: ingest_app_request taking the contract anon_borrower_id as given (no md5). Fed only by contract events.';

--
-- Name: on_facility_approval_record_decision(); Type: FUNCTION; Schema: marketplace; Owner: -
--

CREATE FUNCTION marketplace.on_facility_approval_record_decision() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'marketplace', 'institution', 'public', 'pg_temp'
    AS $$
DECLARE v_stage uuid; v_pid uuid; v_reason text; v_actor uuid;
BEGIN
  IF NEW.status NOT IN ('approved', 'rejected') THEN RETURN NEW; END IF;
  SELECT psi.id, psi.pipeline_id INTO v_stage, v_pid
    FROM marketplace.pipeline_stage_instance psi WHERE psi.approval_instance_id = NEW.id;
  IF NOT FOUND THEN RETURN NEW; END IF;
  SELECT a.comment, a.actor_id INTO v_reason, v_actor
    FROM institution.approval_action a
    JOIN institution.approval_stage_instance si ON si.id = a.stage_instance_id
   WHERE si.instance_id = NEW.id
     AND a.action = CASE NEW.status WHEN 'approved' THEN 'approve' ELSE 'reject' END
   ORDER BY a.created_at DESC LIMIT 1;
  INSERT INTO marketplace.credit_decision
      (pipeline_id, institution_id, stage_instance_id, source, outcome, reason,
       decided_at, recorded_by, approval_instance_id)
  VALUES (v_pid, NEW.institution_id, v_stage, 'engine',
          CASE NEW.status WHEN 'approved' THEN 'approved' ELSE 'declined' END,
          COALESCE(v_reason, CASE NEW.status WHEN 'rejected' THEN 'Credit approval rejected.' END),
          COALESCE(NEW.resolved_at, now()), v_actor, NEW.id);
  RETURN NEW;
END $$;

--
-- Name: on_facility_approval_resolved(); Type: FUNCTION; Schema: marketplace; Owner: -
--

CREATE FUNCTION marketplace.on_facility_approval_resolved() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'marketplace', 'institution', 'public', 'pg_temp'
    AS $$
DECLARE
  s        marketplace.pipeline_stage_instance%ROWTYPE;
  d        marketplace.loan_pipeline%ROWTYPE;
  v_next   uuid;
  v_reason text;
  v_esc    uuid;
  v_new    uuid;
BEGIN
  SELECT * INTO s FROM marketplace.pipeline_stage_instance
   WHERE approval_instance_id = NEW.id FOR UPDATE;
  IF NOT FOUND THEN RETURN NEW; END IF;
  SELECT * INTO d FROM marketplace.loan_pipeline WHERE id = s.pipeline_id FOR UPDATE;
  -- Deal already closed (e.g. declined while in approval), or stage moved on.
  IF d.status <> 'active' OR s.status <> 'awaiting_approval' THEN RETURN NEW; END IF;

  IF NEW.status = 'approved' THEN
    UPDATE marketplace.pipeline_stage_instance
       SET status = 'completed', approved_at = now(), completed_at = now(), updated_at = now()
     WHERE id = s.id;
    SELECT id INTO v_next FROM marketplace.pipeline_stage_instance
     WHERE pipeline_id = s.pipeline_id AND position > s.position AND status = 'pending'
     ORDER BY position LIMIT 1;
    IF v_next IS NOT NULL THEN
      UPDATE marketplace.pipeline_stage_instance
         SET status = 'active', started_at = now(), updated_at = now() WHERE id = v_next;
      UPDATE marketplace.loan_pipeline SET current_stage_id = v_next, updated_at = now() WHERE id = d.id;
    ELSE
      UPDATE marketplace.loan_pipeline
         SET status = 'completed', completed_at = now(), updated_at = now() WHERE id = d.id;
    END IF;
    INSERT INTO public.portal_notifications (institution_id, kind, title, body, link, metadata)
    VALUES (d.institution_id, 'pipeline_approved', 'Credit approval granted',
            'The facility was approved and the deal moved to its next stage.',
            '/deals/' || d.id, jsonb_build_object('pipeline_id', d.id, 'approval_instance_id', NEW.id));

  ELSIF NEW.status = 'rejected' THEN
    SELECT a.comment INTO v_reason
      FROM institution.approval_action a
      JOIN institution.approval_stage_instance si ON si.id = a.stage_instance_id
     WHERE si.instance_id = NEW.id AND a.action = 'reject'
     ORDER BY a.created_at DESC LIMIT 1;
    UPDATE marketplace.pipeline_stage_instance
       SET status = 'blocked', rejection_reason = v_reason, updated_at = now() WHERE id = s.id;
    UPDATE marketplace.pipeline_stage_instance
       SET status = 'skipped', updated_at = now()
     WHERE pipeline_id = d.id AND status = 'pending';
    UPDATE marketplace.loan_pipeline
       SET status = 'declined', closed_reason = COALESCE(v_reason, 'Credit approval rejected.'),
           closed_at = now(), updated_at = now()
     WHERE id = d.id;
    INSERT INTO public.portal_notifications (institution_id, kind, title, body, link, metadata)
    VALUES (d.institution_id, 'deal_closed', 'Credit approval rejected',
            'The facility was rejected and the deal is declined.',
            '/deals/' || d.id, jsonb_build_object('pipeline_id', d.id, 'approval_instance_id', NEW.id));

  ELSIF NEW.status = 'escalated' THEN
    SELECT sd.escalate_to_template_id INTO v_esc
      FROM institution.approval_stage_instance si
      JOIN institution.approval_stage_def sd ON sd.id = si.stage_def_id
     WHERE si.instance_id = NEW.id AND si.status = 'expired'
     ORDER BY si.seq DESC LIMIT 1;
    IF v_esc IS NOT NULL THEN
      v_new := institution.approvals_route(NEW.institution_id, 'facility', NEW.entity_id,
                 NEW.entity_maker_id, NEW.entity_snapshot, v_esc, NEW.doa_rule_id);
      UPDATE marketplace.pipeline_stage_instance
         SET approval_instance_id = v_new, updated_at = now() WHERE id = s.id;
      INSERT INTO public.portal_notifications (institution_id, kind, title, body, link, metadata)
      VALUES (d.institution_id, 'approval_needed', 'Credit approval escalated',
              'The approval passed its deadline and was escalated.',
              '/deals/' || d.id, jsonb_build_object('pipeline_id', d.id, 'approval_instance_id', v_new));
    ELSE
      UPDATE marketplace.pipeline_stage_instance
         SET status = 'active', updated_at = now() WHERE id = s.id;
    END IF;

  ELSE  -- expired, withdrawn: back to the officer to resubmit
    UPDATE marketplace.pipeline_stage_instance
       SET status = 'active', updated_at = now() WHERE id = s.id;
    INSERT INTO public.portal_notifications (institution_id, kind, title, body, link, metadata)
    VALUES (d.institution_id, 'approval_needed',
            CASE NEW.status WHEN 'expired' THEN 'Credit approval expired' ELSE 'Credit approval withdrawn' END,
            'Resubmit the facility for credit approval from the deal.',
            '/deals/' || d.id, jsonb_build_object('pipeline_id', d.id, 'approval_instance_id', NEW.id));
  END IF;
  RETURN NEW;
END $$;

--
-- Name: record_booking_closure(uuid, uuid, date, text, text, uuid, uuid); Type: FUNCTION; Schema: marketplace; Owner: -
--

CREATE FUNCTION marketplace.record_booking_closure(p_pipeline_id uuid, p_institution_id uuid, p_closed_on date, p_reason text, p_source text, p_actor_id uuid, p_api_key_id uuid) RETURNS jsonb
    LANGUAGE plpgsql
    SET search_path TO ''
    AS $$
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

--
-- Name: record_credit_decision(uuid, uuid, text, jsonb, text, text, timestamp with time zone, text, uuid, uuid); Type: FUNCTION; Schema: marketplace; Owner: -
--

CREATE FUNCTION marketplace.record_credit_decision(p_pipeline_id uuid, p_institution_id uuid, p_outcome text, p_conditions jsonb, p_reason text, p_reference text, p_decided_at timestamp with time zone, p_source text, p_actor uuid, p_api_key uuid) RETURNS jsonb
    LANGUAGE plpgsql
    SET search_path TO 'marketplace', 'institution', 'public', 'pg_temp'
    AS $$
DECLARE
  d       marketplace.loan_pipeline%ROWTYPE;
  s       record;
  v_next  uuid;
  v_id    uuid;
  v_conds jsonb := COALESCE(p_conditions, '[]'::jsonb);
  v_deal  text  := 'active';
BEGIN
  IF p_source NOT IN ('portal', 'api') THEN RAISE EXCEPTION 'CREDIT_SOURCE_INVALID'; END IF;
  IF p_outcome NOT IN ('approved', 'approved_with_conditions', 'referred_back', 'declined') THEN
    RAISE EXCEPTION 'CREDIT_OUTCOME_INVALID';
  END IF;
  IF p_reference IS NULL OR char_length(btrim(p_reference)) NOT BETWEEN 1 AND 64 THEN
    RAISE EXCEPTION 'CREDIT_REFERENCE_INVALID';
  END IF;
  IF jsonb_typeof(v_conds) <> 'array' THEN RAISE EXCEPTION 'CREDIT_CONDITIONS_INVALID'; END IF;
  IF p_outcome = 'approved_with_conditions' AND jsonb_array_length(v_conds) = 0 THEN
    RAISE EXCEPTION 'CREDIT_CONDITIONS_REQUIRED';
  END IF;
  IF p_outcome <> 'approved_with_conditions' AND jsonb_array_length(v_conds) > 0 THEN
    RAISE EXCEPTION 'CREDIT_CONDITIONS_UNEXPECTED';
  END IF;
  IF p_outcome IN ('declined', 'referred_back')
     AND (p_reason IS NULL OR char_length(btrim(p_reason)) < 5) THEN
    RAISE EXCEPTION 'CREDIT_REASON_REQUIRED';
  END IF;
  IF p_decided_at IS NOT NULL AND p_decided_at > now() + interval '5 minutes' THEN
    RAISE EXCEPTION 'CREDIT_DATE_INVALID';
  END IF;

  SELECT * INTO d FROM marketplace.loan_pipeline
   WHERE id = p_pipeline_id AND institution_id = p_institution_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'CREDIT_DEAL_NOT_FOUND'; END IF;
  IF d.status <> 'active' THEN RAISE EXCEPTION 'CREDIT_DEAL_NOT_ACTIVE'; END IF;
  IF p_decided_at IS NOT NULL AND p_decided_at < d.created_at THEN
    RAISE EXCEPTION 'CREDIT_DATE_INVALID';
  END IF;

  SELECT psi.id, psi.position, psd.decision_source INTO s
    FROM marketplace.pipeline_stage_instance psi
    JOIN institution.pipeline_stage_def psd ON psd.id = psi.stage_def_id
   WHERE psi.pipeline_id = d.id AND psi.status = 'active' AND psd.approval_route
   ORDER BY psi.position LIMIT 1
   FOR UPDATE OF psi;
  IF NOT FOUND THEN RAISE EXCEPTION 'CREDIT_STAGE_NOT_ACTIVE'; END IF;
  IF s.decision_source <> 'external' THEN RAISE EXCEPTION 'CREDIT_STAGE_INTERNAL'; END IF;

  INSERT INTO marketplace.credit_decision
      (pipeline_id, institution_id, stage_instance_id, source, outcome, conditions,
       reason, external_reference, decided_at, recorded_by, api_key_id)
  VALUES (d.id, d.institution_id, s.id, p_source, p_outcome, v_conds,
          NULLIF(btrim(p_reason), ''), btrim(p_reference), COALESCE(p_decided_at, now()),
          p_actor, p_api_key)
  RETURNING id INTO v_id;

  IF p_outcome IN ('approved', 'approved_with_conditions') THEN
    UPDATE marketplace.pipeline_stage_instance
       SET status = 'completed', approved_at = now(), completed_at = now(),
           rejection_reason = NULL, updated_at = now()
     WHERE id = s.id;
    SELECT id INTO v_next FROM marketplace.pipeline_stage_instance
     WHERE pipeline_id = d.id AND position > s.position AND status = 'pending'
     ORDER BY position LIMIT 1;
    IF v_next IS NOT NULL THEN
      UPDATE marketplace.pipeline_stage_instance
         SET status = 'active', started_at = now(), updated_at = now() WHERE id = v_next;
      UPDATE marketplace.loan_pipeline SET current_stage_id = v_next, updated_at = now()
       WHERE id = d.id;
    ELSE
      UPDATE marketplace.loan_pipeline
         SET status = 'completed', completed_at = now(), updated_at = now() WHERE id = d.id;
      v_deal := 'completed';
    END IF;
    INSERT INTO public.portal_notifications (institution_id, kind, title, body, link, metadata)
    VALUES (d.institution_id, 'pipeline_approved',
            CASE p_outcome WHEN 'approved' THEN 'Credit approved'
                           ELSE 'Credit approved with conditions' END,
            'Credit reference ' || btrim(p_reference) || '.',
            '/deals/' || d.id, jsonb_build_object('pipeline_id', d.id, 'credit_decision_id', v_id));

  ELSIF p_outcome = 'referred_back' THEN
    UPDATE marketplace.pipeline_stage_instance
       SET rejection_reason = btrim(p_reason), updated_at = now() WHERE id = s.id;
    INSERT INTO public.portal_notifications (institution_id, kind, title, body, link, metadata)
    VALUES (d.institution_id, 'approval_needed', 'Credit referred back', btrim(p_reason),
            '/deals/' || d.id, jsonb_build_object('pipeline_id', d.id, 'credit_decision_id', v_id));

  ELSE
    UPDATE marketplace.pipeline_stage_instance
       SET status = 'blocked', rejection_reason = btrim(p_reason), updated_at = now()
     WHERE id = s.id;
    UPDATE marketplace.pipeline_stage_instance
       SET status = 'skipped', updated_at = now()
     WHERE pipeline_id = d.id AND status = 'pending';
    UPDATE marketplace.loan_pipeline
       SET status = 'declined', closed_reason = btrim(p_reason), closed_at = now(),
           closed_by = p_actor, updated_at = now()
     WHERE id = d.id;
    v_deal := 'declined';
    INSERT INTO public.portal_notifications (institution_id, kind, title, body, link, metadata)
    VALUES (d.institution_id, 'deal_closed', 'Credit declined', btrim(p_reason),
            '/deals/' || d.id, jsonb_build_object('pipeline_id', d.id, 'credit_decision_id', v_id));
  END IF;

  RETURN jsonb_build_object(
    'id', v_id, 'pipeline_id', d.id, 'stage_id', s.id, 'outcome', p_outcome,
    'conditions', v_conds, 'reason', NULLIF(btrim(p_reason), ''),
    'external_reference', btrim(p_reference),
    'decided_at', COALESCE(p_decided_at, now()), 'source', p_source,
    'deal_status', v_deal, 'next_stage_id', v_next);
END $$;

--
-- Name: record_loan_booking(uuid, uuid, text, date, numeric, date, text, text, uuid, uuid, boolean, numeric, integer); Type: FUNCTION; Schema: marketplace; Owner: -
--

CREATE FUNCTION marketplace.record_loan_booking(p_pipeline_id uuid, p_institution_id uuid, p_core_reference text, p_booked_on date, p_disbursed_amount numeric, p_disbursed_on date, p_notes text, p_source text, p_actor_id uuid, p_api_key_id uuid, p_correction boolean DEFAULT false, p_monthly_amount numeric DEFAULT NULL::numeric, p_term_months integer DEFAULT NULL::integer) RETURNS jsonb
    LANGUAGE plpgsql
    SET search_path TO 'marketplace', 'portal_admin', 'pg_temp'
    AS $$
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
$$;

--
-- Name: relist_request(uuid, uuid, integer); Type: FUNCTION; Schema: marketplace; Owner: -
--

CREATE FUNCTION marketplace.relist_request(p_request_id uuid, p_consumer_id uuid, p_extend_hours integer DEFAULT 72) RETURNS uuid
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'marketplace', 'public'
    AS $$
DECLARE
  v_old    marketplace.request%ROWTYPE;
  v_new_id uuid;
BEGIN
  SELECT * INTO v_old
  FROM marketplace.request
  WHERE id = p_request_id AND consumer_id = p_consumer_id;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'Request % not found or not owned by consumer', p_request_id;
  END IF;

  IF v_old.status NOT IN ('expired', 'closed') THEN
    RAISE EXCEPTION 'Only expired or closed requests can be relisted (status: %)', v_old.status;
  END IF;

  INSERT INTO marketplace.request (
    id, consumer_id, consumer_ref, product_id,
    country, currency, amount, term_months,
    params, status, metadata,
    bid_window_opens_at, bid_window_closes_at,
    idempotency_key, source,
    relist_count, relisted_from, created_at
  )
  SELECT
    gen_random_uuid(),
    v_old.consumer_id, v_old.consumer_ref, v_old.product_id,
    v_old.country, v_old.currency, v_old.amount, v_old.term_months,
    v_old.params, 'bidding', v_old.metadata,
    now(), now() + (p_extend_hours || ' hours')::interval,
    gen_random_uuid()::text, v_old.source,
    v_old.relist_count + 1, v_old.id, now()
  RETURNING id INTO v_new_id;

  RETURN v_new_id;
END;
$$;

--
-- Name: request_message; Type: TABLE; Schema: marketplace; Owner: -
--

CREATE TABLE marketplace.request_message (
    id uuid NOT NULL,
    request_id uuid NOT NULL,
    institution_id uuid NOT NULL,
    sender_side text NOT NULL,
    kind text NOT NULL,
    template_code text,
    params jsonb DEFAULT '{}'::jsonb NOT NULL,
    body text NOT NULL,
    member_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT request_message_body_check CHECK (((char_length(body) >= 1) AND (char_length(body) <= 2000))),
    CONSTRAINT request_message_check CHECK (((kind = 'structured'::text) = (template_code IS NOT NULL))),
    CONSTRAINT request_message_check1 CHECK (((sender_side = 'institution'::text) OR (member_id IS NULL))),
    CONSTRAINT request_message_kind_check CHECK ((kind = ANY (ARRAY['structured'::text, 'free'::text]))),
    CONSTRAINT request_message_sender_side_check CHECK ((sender_side = ANY (ARRAY['borrower'::text, 'institution'::text])))
);

--
-- Name: send_institution_message(uuid, uuid, uuid, text, jsonb, text); Type: FUNCTION; Schema: marketplace; Owner: -
--

CREATE FUNCTION marketplace.send_institution_message(p_request_id uuid, p_institution_id uuid, p_member_id uuid, p_template_code text, p_params jsonb, p_body text) RETURNS marketplace.request_message
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'marketplace', 'integration'
    AS $$
DECLARE st record; m marketplace.request_message%ROWTYPE; r marketplace.request%ROWTYPE;
BEGIN
  SELECT * INTO r FROM marketplace.request WHERE id = p_request_id;
  IF NOT FOUND THEN RAISE EXCEPTION 'CHAT_REQUEST_NOT_FOUND'; END IF;
  SELECT * INTO st FROM marketplace.chat_thread_state(p_request_id, p_institution_id);
  IF NOT st.is_open THEN RAISE EXCEPTION 'CHAT_CLOSED'; END IF;
  IF p_template_code IS NULL AND NOT st.is_winner THEN RAISE EXCEPTION 'CHAT_FREE_TEXT_NOT_ALLOWED'; END IF;
  INSERT INTO marketplace.request_message (id, request_id, institution_id, sender_side, kind, template_code, params, body, member_id)
  VALUES (gen_random_uuid(), p_request_id, p_institution_id, 'institution',
          CASE WHEN p_template_code IS NULL THEN 'free' ELSE 'structured' END, p_template_code,
          coalesce(p_params, '{}'::jsonb), p_body, p_member_id)
  RETURNING * INTO m;
  IF r.source = 'app' THEN
    PERFORM integration.enqueue('chat.message', p_request_id::text, jsonb_strip_nulls(jsonb_build_object(
      'message_id', m.id, 'request_id', m.request_id, 'institution_id', m.institution_id, 'sender_side', 'institution',
      'template_code', m.template_code, 'params', m.params, 'body', m.body, 'sent_at', m.created_at,
      'free_text_allowed', st.is_winner)), 'institution');
  END IF;
  RETURN m;
END $$;

--
-- Name: snapshot_bid_benefits(); Type: FUNCTION; Schema: marketplace; Owner: -
--

CREATE FUNCTION marketplace.snapshot_bid_benefits() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog', 'public', 'extensions', 'pg_temp'
    AS $$
BEGIN
  INSERT INTO marketplace.bid_benefit (
    bid_id, benefit_id, title, description, value_display, is_guaranteed, cat_code,
    value_amount, value_unit
  )
  SELECT NEW.id, b.id, b.title, b.description, b.value_display, b.is_guaranteed, bc.code,
         b.value_amount, b.value_unit
  FROM institution.benefit b
  JOIN institution.benefit_cat bc ON bc.id = b.cat_id
  WHERE b.institution_id = NEW.institution_id
    AND b.is_active = true
    AND (b.valid_from  IS NULL OR b.valid_from  <= CURRENT_DATE)
    AND (b.valid_until IS NULL OR b.valid_until >= CURRENT_DATE)
    AND (b.product_id IS NULL
         OR b.product_id = (SELECT product_id FROM marketplace.request WHERE id = NEW.request_id));
  RETURN NEW;
END;
$$;

--
-- Name: stage_documents_outstanding(uuid); Type: FUNCTION; Schema: marketplace; Owner: -
--

CREATE FUNCTION marketplace.stage_documents_outstanding(p_stage_instance_id uuid) RETURNS integer
    LANGUAGE sql STABLE
    SET search_path TO 'marketplace', 'institution', 'pg_temp'
    AS $$
    SELECT count(*)::int
      FROM marketplace.pipeline_stage_instance psi
      JOIN marketplace.deal_document dd ON dd.pipeline_id = psi.pipeline_id
      JOIN institution.doc_requirement r ON r.id = dd.requirement_id
     WHERE psi.id = p_stage_instance_id
       AND r.stage_def_id = psi.stage_def_id
       AND r.mandatory
       AND dd.status NOT IN ('verified', 'waived');
$$;

--
-- Name: strip_employer(); Type: FUNCTION; Schema: marketplace; Owner: -
--

CREATE FUNCTION marketplace.strip_employer() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO ''
    AS $$
BEGIN
  NEW.metadata := NEW.metadata - 'employer';
  RETURN NEW;
END $$;

--
-- Name: trg_pipeline_documents(); Type: FUNCTION; Schema: marketplace; Owner: -
--

CREATE FUNCTION marketplace.trg_pipeline_documents() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'marketplace'
    AS $$
BEGIN
  PERFORM marketplace.ensure_deal_documents(NEW.id);
  RETURN NULL;
END $$;

--
-- Name: _execute_dual_control_action(uuid); Type: FUNCTION; Schema: portal_admin; Owner: -
--

CREATE FUNCTION portal_admin._execute_dual_control_action(p_action_id uuid) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public', 'extensions', 'pg_temp'
    AS $$
DECLARE
  v_action portal_admin.admin_dual_control_actions%ROWTYPE;
  v_payload JSONB;
BEGIN
  SELECT * INTO v_action FROM portal_admin.admin_dual_control_actions WHERE id = p_action_id;
  v_payload := v_action.payload;

  UPDATE portal_admin.admin_dual_control_actions
  SET status = 'executed', executed_at = now()
  WHERE id = p_action_id;

  CASE v_action.action_category

    WHEN 'user.suspend' THEN
      UPDATE portal_admin.admin_users SET
        status='suspended', suspended_at=now(), suspended_by=v_action.checker_id,
        suspension_reason=v_payload->>'suspension_reason', updated_at=now()
      WHERE id = (v_payload->>'admin_user_id')::UUID;

    WHEN 'user.unlock' THEN
      UPDATE portal_admin.admin_users SET
        status='active', locked_at=NULL, locked_reason=NULL,
        failed_login_count=0, updated_at=now()
      WHERE id = (v_payload->>'admin_user_id')::UUID;

    WHEN 'user.reset_password' THEN
      UPDATE portal_admin.admin_users SET force_password_reset=TRUE, updated_at=now()
      WHERE id = (v_payload->>'admin_user_id')::UUID;

    WHEN 'user.role_change' THEN
      UPDATE portal_admin.admin_users SET role_slug=v_payload->>'new_role_slug', updated_at=now()
      WHERE id = (v_payload->>'admin_user_id')::UUID;

    WHEN 'user.deactivate' THEN
      UPDATE portal_admin.admin_users SET status='deactivated', updated_at=now()
      WHERE id = (v_payload->>'admin_user_id')::UUID;
      UPDATE portal_admin.admin_sessions SET is_active=FALSE, ended_at=now(), end_reason='forced'
      WHERE admin_user_id = (v_payload->>'admin_user_id')::UUID AND is_active=TRUE;

    WHEN 'user.force_logout' THEN
      UPDATE portal_admin.admin_sessions SET is_active=FALSE, ended_at=now(), end_reason='forced'
      WHERE admin_user_id = (v_payload->>'admin_user_id')::UUID AND is_active=TRUE;

    WHEN 'group.create' THEN
      INSERT INTO portal_admin.user_groups
        (slug,label,description,user_type,module_permissions,is_system,created_by)
      VALUES (v_payload->>'slug', v_payload->>'label', COALESCE(v_payload->>'description',''),
        COALESCE(v_payload->>'user_type','institution'),
        ARRAY(SELECT jsonb_array_elements_text(v_payload->'module_permissions')), FALSE, v_action.checker_id)
      ON CONFLICT (slug) DO NOTHING;

    WHEN 'group.update_modules' THEN
      UPDATE portal_admin.user_groups SET
        module_permissions=ARRAY(SELECT jsonb_array_elements_text(v_payload->'module_permissions')), updated_at=now()
      WHERE id=(v_payload->>'group_id')::UUID AND is_system=FALSE;

    WHEN 'role.create' THEN
      INSERT INTO portal_admin.user_groups
        (slug,label,description,module_permissions,is_system,created_by)
      VALUES (v_payload->>'slug', v_payload->>'label', COALESCE(v_payload->>'description',''),
        ARRAY(SELECT jsonb_array_elements_text(v_payload->'permissions')), FALSE, v_action.checker_id)
      ON CONFLICT (slug) DO NOTHING;

    WHEN 'institution.approve' THEN
      UPDATE institution.institution SET
        approved=TRUE, onboarding_stage='approved', updated_at=now()
      WHERE id=(v_payload->>'institution_id')::UUID;

    WHEN 'institution.suspend' THEN
      UPDATE institution.institution SET
        approved=FALSE, onboarding_stage='suspended', updated_at=now()
      WHERE id=(v_payload->>'institution_id')::UUID;

    WHEN 'institution.modules_update' THEN
      UPDATE institution.institution SET
        modules=ARRAY(SELECT jsonb_array_elements_text(v_payload->'modules')), updated_at=now()
      WHERE id=(v_payload->>'institution_id')::UUID;

    ELSE
      INSERT INTO portal_admin.admin_audit_log
        (action_category,event_label,dual_control_id,outcome,outcome_note)
      VALUES ('execution.unknown','Unknown action category: '||v_action.action_category,
              p_action_id,'logged','No executor registered for this category');

  END CASE;

  EXCEPTION WHEN OTHERS THEN
    UPDATE portal_admin.admin_dual_control_actions
    SET execution_error = SQLERRM, status = 'approved'
    WHERE id = p_action_id;
    RAISE;
END;
$$;

--
-- Name: admin_approve_dual_control(uuid, text); Type: FUNCTION; Schema: portal_admin; Owner: -
--

CREATE FUNCTION portal_admin.admin_approve_dual_control(p_action_id uuid, p_note text DEFAULT NULL::text) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public', 'extensions', 'pg_temp'
    AS $$
DECLARE
  v_admin   portal_admin.admin_users%ROWTYPE;
  v_action  portal_admin.admin_dual_control_actions%ROWTYPE;
  v_session portal_admin.admin_sessions%ROWTYPE;
BEGIN
  SELECT * INTO v_admin FROM portal_admin.admin_users
  WHERE auth_user_id = auth.uid() AND status = 'active' LIMIT 1;
  IF NOT FOUND THEN RAISE EXCEPTION 'Unauthorised'; END IF;

  SELECT * INTO v_action FROM portal_admin.admin_dual_control_actions
  WHERE id = p_action_id AND status = 'pending' FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'Action not found or not pending'; END IF;

  -- Block self-approval
  IF v_action.maker_id = v_admin.id THEN
    INSERT INTO portal_admin.admin_audit_log
      (actor_id, actor_email, actor_role, action_category, event_label, dual_control_id, outcome, outcome_note)
    VALUES
      (v_admin.id, v_admin.email, v_admin.role_slug, 'dual_control.self_approval_blocked',
       'Self-approval attempt blocked', p_action_id, 'blocked',
       'Maker cannot approve their own action');
    RAISE EXCEPTION 'Self-approval is not permitted';
  END IF;

  -- Block expired
  IF v_action.expires_at < now() THEN
    RAISE EXCEPTION 'Action has expired';
  END IF;

  -- Check checker has dual_control:approve permission
  IF NOT portal_admin.has_permission('dual_control:approve') THEN
    RAISE EXCEPTION 'Insufficient permissions to approve dual-control actions';
  END IF;

  SELECT * INTO v_session FROM portal_admin.admin_sessions
  WHERE admin_user_id = v_admin.id AND is_active = TRUE
  ORDER BY last_active_at DESC LIMIT 1;

  -- Approve
  UPDATE portal_admin.admin_dual_control_actions SET
    status       = 'approved',
    checker_id   = v_admin.id,
    checker_email= v_admin.email,
    checker_role = v_admin.role_slug,
    checker_note = p_note,
    checker_ip   = COALESCE(v_session.ip_address, '0.0.0.0'::INET),
    checked_at   = now()
  WHERE id = p_action_id;

  -- Execute the action
  PERFORM portal_admin._execute_dual_control_action(p_action_id);

  -- Audit
  INSERT INTO portal_admin.admin_audit_log
    (actor_id, actor_email, actor_role, actor_ip, action_category, event_label,
     resource_type, resource_id, dual_control_id, outcome, outcome_note)
  VALUES
    (v_admin.id, v_admin.email, v_admin.role_slug,
     COALESCE(v_session.ip_address, '0.0.0.0'::INET),
     v_action.action_category, 'Dual-control action approved: ' || v_action.action_label,
     v_action.resource_type, v_action.resource_id, p_action_id, 'success', p_note);
END;
$$;

--
-- Name: admin_reject_dual_control(uuid, text); Type: FUNCTION; Schema: portal_admin; Owner: -
--

CREATE FUNCTION portal_admin.admin_reject_dual_control(p_action_id uuid, p_note text) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public', 'extensions', 'pg_temp'
    AS $$
DECLARE
  v_admin  portal_admin.admin_users%ROWTYPE;
  v_action portal_admin.admin_dual_control_actions%ROWTYPE;
BEGIN
  SELECT * INTO v_admin FROM portal_admin.admin_users
  WHERE auth_user_id = auth.uid() AND status = 'active' LIMIT 1;
  IF NOT FOUND THEN RAISE EXCEPTION 'Unauthorised'; END IF;

  SELECT * INTO v_action FROM portal_admin.admin_dual_control_actions
  WHERE id = p_action_id AND status = 'pending' FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'Action not found or not pending'; END IF;

  IF v_action.maker_id = v_admin.id THEN RAISE EXCEPTION 'Cannot reject your own action — use cancel instead'; END IF;

  UPDATE portal_admin.admin_dual_control_actions SET
    status        = 'rejected',
    checker_id    = v_admin.id,
    checker_email = v_admin.email,
    checker_role  = v_admin.role_slug,
    checker_note  = p_note,
    checked_at    = now()
  WHERE id = p_action_id;

  INSERT INTO portal_admin.admin_audit_log
    (actor_id, actor_email, actor_role, action_category, event_label,
     resource_type, resource_id, dual_control_id, outcome, outcome_note)
  VALUES
    (v_admin.id, v_admin.email, v_admin.role_slug,
     v_action.action_category, 'Dual-control action rejected: ' || v_action.action_label,
     v_action.resource_type, v_action.resource_id, p_action_id, 'rejected', p_note);
END;
$$;

--
-- Name: admin_submit_dual_control(text, text, text, text, uuid, text, jsonb, jsonb); Type: FUNCTION; Schema: portal_admin; Owner: -
--

CREATE FUNCTION portal_admin.admin_submit_dual_control(action_category text, action_label text, risk text, resource_type text, resource_id uuid DEFAULT NULL::uuid, resource_label text DEFAULT NULL::text, payload jsonb DEFAULT '{}'::jsonb, payload_before jsonb DEFAULT NULL::jsonb) RETURNS uuid
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public', 'extensions', 'pg_temp'
    AS $$
DECLARE
  v_admin     portal_admin.admin_users%ROWTYPE;
  v_action_id UUID;
  v_session   portal_admin.admin_sessions%ROWTYPE;
BEGIN
  SELECT * INTO v_admin FROM portal_admin.admin_users
  WHERE auth_user_id = auth.uid() AND status = 'active'
  LIMIT 1;
  IF NOT FOUND THEN RAISE EXCEPTION 'Unauthorised'; END IF;

  SELECT * INTO v_session FROM portal_admin.admin_sessions
  WHERE admin_user_id = v_admin.id AND is_active = TRUE
  ORDER BY last_active_at DESC LIMIT 1;

  INSERT INTO portal_admin.admin_dual_control_actions (
    action_category, action_label, risk, maker_id, maker_email, maker_role,
    maker_ip, resource_type, resource_id, resource_label, payload, payload_before
  ) VALUES (
    action_category, action_label, risk::portal_admin.action_risk,
    v_admin.id, v_admin.email, v_admin.role_slug,
    COALESCE(v_session.ip_address, '0.0.0.0'::INET),
    resource_type, resource_id, resource_label, payload, payload_before
  ) RETURNING id INTO v_action_id;

  INSERT INTO portal_admin.admin_audit_log (
    actor_id, actor_email, actor_role, actor_ip,
    action_category, event_label, resource_type, resource_id,
    dual_control_id, outcome, outcome_note
  ) VALUES (
    v_admin.id, v_admin.email, v_admin.role_slug,
    COALESCE(v_session.ip_address, '0.0.0.0'::INET),
    action_category, 'Dual-control action submitted: ' || action_label,
    resource_type, resource_id, v_action_id,
    'logged', 'Awaiting second admin approval'
  );

  RETURN v_action_id;
END;
$$;

--
-- Name: check_failed_logins(); Type: FUNCTION; Schema: portal_admin; Owner: -
--

CREATE FUNCTION portal_admin.check_failed_logins() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog', 'public', 'extensions', 'pg_temp'
    AS $$
BEGIN
  IF NEW.failed_login_count >= 5 AND OLD.status = 'active' THEN
    NEW.status     := 'locked';
    NEW.locked_at  := now();
    NEW.locked_reason := 'Exceeded 5 failed login attempts — auto-locked';
    INSERT INTO portal_admin.admin_audit_log
      (actor_id, action_category, event_label, resource_type, resource_id, outcome, outcome_note)
    VALUES
      (NEW.id, 'security.auto_lock', 'Account auto-locked after 5 failed logins', 'admin_user', NEW.id, 'success', 'Threshold: 5 attempts');
  END IF;
  RETURN NEW;
END;
$$;

--
-- Name: expire_dual_control_actions(); Type: FUNCTION; Schema: portal_admin; Owner: -
--

CREATE FUNCTION portal_admin.expire_dual_control_actions() RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public', 'extensions', 'pg_temp'
    AS $$
DECLARE
  expired_count INT;
BEGIN
  WITH expired AS (
    UPDATE portal_admin.admin_dual_control_actions
    SET status = 'expired'
    WHERE status = 'pending' AND expires_at < now()
    RETURNING id, action_label, maker_id
  )
  INSERT INTO portal_admin.admin_audit_log
    (action_category, event_label, resource_type, resource_id, outcome, outcome_note)
  SELECT
    'dual_control.expire',
    'Dual control action expired: ' || action_label,
    'admin_dual_control_action',
    id,
    'expired',
    'TTL elapsed without checker decision'
  FROM expired;

  GET DIAGNOSTICS expired_count = ROW_COUNT;
  RETURN expired_count;
END;
$$;

--
-- Name: get_institutions(); Type: FUNCTION; Schema: portal_admin; Owner: -
--

CREATE FUNCTION portal_admin.get_institutions() RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public', 'extensions', 'pg_temp'
    AS $$
BEGIN
  IF NOT portal_admin.is_admin() THEN
    RAISE EXCEPTION 'Unauthorized';
  END IF;
  RETURN (
    SELECT jsonb_agg(row_to_json(i))
    FROM (
      SELECT
        i.id, i.name, i.legal_name, i.institution_type,
        i.onboarding_stage, i.compliance_status,
        i.approved, i.approved_at, i.suspended_at, i.suspension_reason,
        i.primary_contact_name, i.primary_contact_email,
        i.website, i.country, i.regulator, i.modules,
        i.created_at, i.updated_at,
        COALESCE(
          (SELECT COUNT(*) FROM institution.member m WHERE m.institution_id = i.id AND m.status = 'active'),
          0
        ) AS member_count
      FROM institution.institution i
      ORDER BY i.created_at DESC
    ) i
  );
END;
$$;

--
-- Name: get_my_group(); Type: FUNCTION; Schema: portal_admin; Owner: -
--

CREATE FUNCTION portal_admin.get_my_group() RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'portal_admin', 'institution', 'pg_temp'
    AS $$
DECLARE v_group portal_admin.user_groups%ROWTYPE;
BEGIN
  SELECT g.* INTO v_group FROM portal_admin.user_groups g
    JOIN portal_admin.admin_users u ON u.group_id = g.id
   WHERE u.auth_user_id = auth.uid() AND u.status = 'active' LIMIT 1;
  IF FOUND THEN RETURN row_to_json(v_group)::jsonb; END IF;

  SELECT g.* INTO v_group FROM portal_admin.user_groups g
    JOIN institution.member m ON m.group_id = g.id
   WHERE m.auth_user_id = auth.uid() LIMIT 1;
  IF FOUND THEN RETURN row_to_json(v_group)::jsonb; END IF;
  RETURN NULL;
END;
$$;

--
-- Name: get_user_display_name(uuid); Type: FUNCTION; Schema: portal_admin; Owner: -
--

CREATE FUNCTION portal_admin.get_user_display_name(p_auth_user_id uuid) RETURNS text
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'portal_admin', 'pg_temp'
    AS $$
  SELECT COALESCE(display_name, email) FROM portal_admin.admin_users WHERE auth_user_id = p_auth_user_id LIMIT 1
$$;

--
-- Name: get_user_groups(); Type: FUNCTION; Schema: portal_admin; Owner: -
--

CREATE FUNCTION portal_admin.get_user_groups() RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public', 'extensions', 'pg_temp'
    AS $$
BEGIN
  RETURN (
    SELECT jsonb_agg(
      jsonb_build_object(
        'id', g.id, 'slug', g.slug, 'label', g.label, 'description', g.description,
        'user_type', g.user_type, 'module_permissions', g.module_permissions,
        'is_system', g.is_system,
        'member_count', (
          SELECT COUNT(*) FROM portal_admin.admin_users u
          WHERE u.group_id = g.id AND u.status != 'deactivated'
        ) + (
          SELECT COUNT(*) FROM institution.member m
          WHERE m.group_id = g.id AND m.active = TRUE
        ),
        'created_by', g.created_by, 'created_at', g.created_at, 'updated_at', g.updated_at
      )
      ORDER BY g.user_type, g.is_system DESC, g.label
    )
    FROM portal_admin.user_groups g
  );
END;
$$;

--
-- Name: has_permission(text); Type: FUNCTION; Schema: portal_admin; Owner: -
--

CREATE FUNCTION portal_admin.has_permission(p_key text) RETURNS boolean
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'portal_admin', 'pg_temp'
    AS $$
  SELECT EXISTS (
    SELECT 1
      FROM portal_admin.admin_users u
      JOIN portal_admin.admin_roles r
        ON r.id = COALESCE(u.custom_role_id, (SELECT r2.id FROM portal_admin.admin_roles r2 WHERE r2.slug = u.role_slug))
     WHERE u.auth_user_id = auth.uid()
       AND u.status = 'active'
       AND ('*' = ANY(r.permissions) OR p_key = ANY(r.permissions)))
$$;

--
-- Name: is_admin(); Type: FUNCTION; Schema: portal_admin; Owner: -
--

CREATE FUNCTION portal_admin.is_admin() RETURNS boolean
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'portal_admin', 'pg_temp'
    AS $$
  SELECT EXISTS (SELECT 1 FROM portal_admin.admin_users WHERE auth_user_id = auth.uid() AND status = 'active')
$$;

--
-- Name: my_permissions(); Type: FUNCTION; Schema: portal_admin; Owner: -
--

CREATE FUNCTION portal_admin.my_permissions() RETURNS text[]
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public', 'extensions', 'pg_temp'
    AS $$
  SELECT COALESCE(
    (SELECT permissions FROM portal_admin.admin_roles
     WHERE slug = portal_admin.my_role_slug() LIMIT 1),
    '{}'::TEXT[]
  )
$$;

--
-- Name: my_role_slug(); Type: FUNCTION; Schema: portal_admin; Owner: -
--

CREATE FUNCTION portal_admin.my_role_slug() RETURNS text
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public', 'extensions', 'pg_temp'
    AS $$
  SELECT role_slug FROM portal_admin.admin_users
  WHERE auth_user_id = auth.uid() AND status = 'active'
  LIMIT 1
$$;

--
-- Name: set_updated_at(); Type: FUNCTION; Schema: portal_admin; Owner: -
--

CREATE FUNCTION portal_admin.set_updated_at() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog', 'public', 'extensions', 'pg_temp'
    AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$;

--
-- Name: update_group_modules(uuid, text[]); Type: FUNCTION; Schema: portal_admin; Owner: -
--

CREATE FUNCTION portal_admin.update_group_modules(p_group_id uuid, p_module_permissions text[]) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public', 'extensions', 'pg_temp'
    AS $$
BEGIN
  IF auth.role() != 'service_role' THEN
    RAISE EXCEPTION 'Unauthorised — service role only';
  END IF;

  UPDATE portal_admin.user_groups
  SET module_permissions = p_module_permissions, updated_at = now()
  WHERE id = p_group_id AND is_system = FALSE;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'Group not found or is a system group';
  END IF;
END;
$$;

--
-- Name: detect_portal_user_type(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.detect_portal_user_type(p_auth_user_id uuid) RETURNS text
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public', 'extensions', 'pg_temp'
    AS $$
DECLARE v_type TEXT;
BEGIN
  SELECT 'admin' INTO v_type
  FROM portal_admin.admin_users
  WHERE auth_user_id = p_auth_user_id AND status = 'active'
  LIMIT 1;
  IF FOUND THEN RETURN v_type; END IF;

  SELECT 'institution' INTO v_type
  FROM institution.member
  WHERE auth_user_id = p_auth_user_id AND active = TRUE
  LIMIT 1;
  IF FOUND THEN RETURN v_type; END IF;

  RETURN 'unknown';
END;
$$;

--
-- Name: get_admin_audit(integer, text, text); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.get_admin_audit(p_limit integer DEFAULT 100, p_outcome text DEFAULT NULL::text, p_category text DEFAULT NULL::text) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'portal_admin', 'public'
    AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM portal_admin.admin_users WHERE auth_user_id = auth.uid() AND status = 'active')
  THEN RAISE EXCEPTION 'Unauthorised'; END IF;
  RETURN (SELECT jsonb_agg(to_jsonb(e) ORDER BY e.created_at DESC)
    FROM (SELECT * FROM portal_admin.admin_audit_log
      WHERE (p_outcome IS NULL OR outcome = p_outcome::portal_admin.audit_outcome)
        AND (p_category IS NULL OR action_category ILIKE p_category || '%')
      ORDER BY created_at DESC LIMIT p_limit) e);
END; $$;

--
-- Name: get_admin_dual_control(text); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.get_admin_dual_control(p_status text DEFAULT 'pending'::text) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'portal_admin', 'public'
    AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM portal_admin.admin_users WHERE auth_user_id = auth.uid() AND status = 'active')
  THEN RAISE EXCEPTION 'Unauthorised'; END IF;
  RETURN (SELECT jsonb_agg(to_jsonb(a) ORDER BY a.initiated_at DESC)
    FROM portal_admin.admin_dual_control_actions a
    WHERE p_status = 'all' OR a.status = p_status::portal_admin.dual_control_status);
END; $$;

--
-- Name: get_admin_me(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.get_admin_me() RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'portal_admin', 'public'
    AS $$
DECLARE v_uid UUID; v_result JSONB;
BEGIN
  v_uid := auth.uid();
  SELECT to_jsonb(u) INTO v_result FROM portal_admin.admin_users u
  WHERE u.auth_user_id = v_uid AND u.status = 'active' LIMIT 1;
  RETURN v_result;
END; $$;

--
-- Name: get_admin_metrics(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.get_admin_metrics() RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'portal_admin', 'public'
    AS $$
DECLARE
  v_result JSONB;
BEGIN
  SELECT jsonb_build_object(
    'total_admins',    (SELECT COUNT(*) FROM portal_admin.admin_users),
    'active_admins',   (SELECT COUNT(*) FROM portal_admin.admin_users WHERE status = 'active'),
    'locked_accounts', (SELECT COUNT(*) FROM portal_admin.admin_users WHERE status = 'locked'),
    'active_sessions', (SELECT COUNT(*) FROM portal_admin.admin_sessions WHERE is_active = TRUE),
    'pending_dc',      (SELECT COUNT(*) FROM portal_admin.admin_dual_control_actions WHERE status = 'pending'),
    'recent_audit',    (
      SELECT jsonb_agg(jsonb_build_object('outcome', outcome))
      FROM (
        SELECT outcome FROM portal_admin.admin_audit_log
        ORDER BY created_at DESC LIMIT 100
      ) sub
    )
  ) INTO v_result;

  RETURN v_result;
END;
$$;

--
-- Name: get_admin_roles(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.get_admin_roles() RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'portal_admin', 'public'
    AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM portal_admin.admin_users WHERE auth_user_id = auth.uid() AND status = 'active')
  THEN RAISE EXCEPTION 'Unauthorised'; END IF;
  RETURN (SELECT jsonb_agg(to_jsonb(r) ORDER BY r.is_system DESC, r.slug) FROM portal_admin.admin_roles r);
END; $$;

--
-- Name: get_admin_sessions(boolean); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.get_admin_sessions(p_active_only boolean DEFAULT false) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'portal_admin', 'public'
    AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM portal_admin.admin_users WHERE auth_user_id = auth.uid() AND status = 'active')
  THEN RAISE EXCEPTION 'Unauthorised'; END IF;
  RETURN (SELECT jsonb_agg(jsonb_build_object('id',s.id,'admin_user_id',s.admin_user_id,
    'ip_address',s.ip_address,'user_agent',s.user_agent,'country',s.country,'city',s.city,
    'started_at',s.started_at,'last_active_at',s.last_active_at,'ended_at',s.ended_at,
    'end_reason',s.end_reason,'is_active',s.is_active,'admin_email',u.email,
    'admin_name',u.display_name,'admin_role',u.role_slug) ORDER BY s.last_active_at DESC)
    FROM portal_admin.admin_sessions s JOIN portal_admin.admin_users u ON u.id = s.admin_user_id
    WHERE NOT p_active_only OR s.is_active = TRUE LIMIT 200);
END; $$;

--
-- Name: get_admin_users(text); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.get_admin_users(p_status text DEFAULT NULL::text) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'portal_admin', 'public'
    AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM portal_admin.admin_users WHERE auth_user_id = auth.uid() AND status = 'active')
  THEN RAISE EXCEPTION 'Unauthorised'; END IF;
  RETURN (SELECT jsonb_agg(to_jsonb(u) ORDER BY u.created_at DESC) FROM portal_admin.admin_users u
    WHERE p_status IS NULL OR u.status = p_status::portal_admin.admin_user_status);
END; $$;

--
-- Name: rls_auto_enable(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.rls_auto_enable() RETURNS event_trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog'
    AS $$
DECLARE
  cmd record;
BEGIN
  FOR cmd IN
    SELECT *
    FROM pg_event_trigger_ddl_commands()
    WHERE command_tag IN ('CREATE TABLE', 'CREATE TABLE AS', 'SELECT INTO')
      AND object_type IN ('table','partitioned table')
  LOOP
     IF cmd.schema_name IS NOT NULL AND cmd.schema_name IN ('public') AND cmd.schema_name NOT IN ('pg_catalog','information_schema') AND cmd.schema_name NOT LIKE 'pg_toast%' AND cmd.schema_name NOT LIKE 'pg_temp%' THEN
      BEGIN
        EXECUTE format('alter table if exists %s enable row level security', cmd.object_identity);
        RAISE LOG 'rls_auto_enable: enabled RLS on %', cmd.object_identity;
      EXCEPTION
        WHEN OTHERS THEN
          RAISE LOG 'rls_auto_enable: failed to enable RLS on %', cmd.object_identity;
      END;
     ELSE
        RAISE LOG 'rls_auto_enable: skip % (either system schema or not in enforced list: %.)', cmd.object_identity, cmd.schema_name;
     END IF;
  END LOOP;
END;
$$;

--
-- Name: set_updated_at(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.set_updated_at() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog', 'public', 'extensions', 'pg_temp'
    AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END;
$$;

--
-- Name: event; Type: TABLE; Schema: audit; Owner: -
--

CREATE TABLE audit.event (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    occurred_at timestamp with time zone DEFAULT now() NOT NULL,
    actor_id uuid,
    actor_type text DEFAULT 'system'::text NOT NULL,
    actor_email text,
    actor_role text,
    actor_ip inet,
    actor_user_agent text,
    institution_id uuid,
    action text NOT NULL,
    resource_type text,
    resource_id uuid,
    resource_label text,
    outcome text DEFAULT 'logged'::text NOT NULL,
    outcome_note text,
    governance_action_id uuid,
    session_id uuid,
    request_id text,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT event_actor_type_check CHECK ((actor_type = ANY (ARRAY['consumer'::text, 'member'::text, 'admin'::text, 'system'::text, 'api'::text]))),
    CONSTRAINT event_outcome_check CHECK ((outcome = ANY (ARRAY['success'::text, 'failed'::text, 'blocked'::text, 'rejected'::text, 'expired'::text, 'logged'::text])))
);

ALTER TABLE ONLY audit.event FORCE ROW LEVEL SECURITY;

--
-- Name: auth_audit_events; Type: TABLE; Schema: auth_portal; Owner: -
--

CREATE TABLE auth_portal.auth_audit_events (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid,
    institution_id uuid,
    event_type character varying(50) NOT NULL,
    outcome character varying(20) NOT NULL,
    ip_address character varying(45),
    user_agent text,
    event_metadata jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: auth_sessions; Type: TABLE; Schema: auth_portal; Owner: -
--

CREATE TABLE auth_portal.auth_sessions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    refresh_token_hash character varying(64) NOT NULL,
    ip_address character varying(45),
    user_agent text,
    device_fingerprint character varying(64),
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    last_used_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    revoked_at timestamp with time zone,
    revoke_reason character varying(100),
    mfa_verified boolean DEFAULT false NOT NULL
);

--
-- Name: auth_users; Type: TABLE; Schema: auth_portal; Owner: -
--

CREATE TABLE auth_portal.auth_users (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    institution_id uuid NOT NULL,
    email character varying(255) NOT NULL,
    email_verified boolean DEFAULT false NOT NULL,
    password_hash text NOT NULL,
    role character varying(50) DEFAULT 'analyst'::character varying NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    mfa_enabled boolean DEFAULT false NOT NULL,
    mfa_secret text,
    last_login_at timestamp with time zone,
    last_login_ip character varying(45),
    failed_attempts integer DEFAULT 0 NOT NULL,
    locked_until timestamp with time zone,
    password_changed_at timestamp with time zone DEFAULT now() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    username text,
    must_change_password boolean DEFAULT false NOT NULL
);

--
-- Name: email_verification_tokens; Type: TABLE; Schema: auth_portal; Owner: -
--

CREATE TABLE auth_portal.email_verification_tokens (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    token_hash character varying(64) NOT NULL,
    used boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL
);

--
-- Name: ip_allowlist; Type: TABLE; Schema: auth_portal; Owner: -
--

CREATE TABLE auth_portal.ip_allowlist (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    institution_id uuid NOT NULL,
    cidr character varying(50) NOT NULL,
    label character varying(100),
    created_by uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: mfa_backup_codes; Type: TABLE; Schema: auth_portal; Owner: -
--

CREATE TABLE auth_portal.mfa_backup_codes (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    code_hash character varying(64) NOT NULL,
    used_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: password_reset_tokens; Type: TABLE; Schema: auth_portal; Owner: -
--

CREATE TABLE auth_portal.password_reset_tokens (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    token_hash character varying(64) NOT NULL,
    used boolean DEFAULT false NOT NULL,
    ip_address character varying(45),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    used_at timestamp with time zone
);

--
-- Name: execution; Type: TABLE; Schema: autobid; Owner: -
--

CREATE TABLE autobid.execution (
    id bigint NOT NULL,
    institution_id uuid NOT NULL,
    rule_id uuid NOT NULL,
    rule_version_id uuid NOT NULL,
    loan_request_id uuid NOT NULL,
    outcome text NOT NULL,
    bid_id uuid,
    detail jsonb DEFAULT '{}'::jsonb NOT NULL,
    idempotency_key text NOT NULL,
    occurred_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT execution_outcome_check CHECK ((outcome = ANY (ARRAY['bid_placed'::text, 'suppressed_guardrail'::text, 'suppressed_exposure'::text, 'no_match'::text, 'error'::text])))
)
PARTITION BY RANGE (occurred_at);

ALTER TABLE ONLY autobid.execution FORCE ROW LEVEL SECURITY;

--
-- Name: execution_default; Type: TABLE; Schema: autobid; Owner: -
--

CREATE TABLE autobid.execution_default (
    id bigint NOT NULL,
    institution_id uuid NOT NULL,
    rule_id uuid NOT NULL,
    rule_version_id uuid NOT NULL,
    loan_request_id uuid NOT NULL,
    outcome text NOT NULL,
    bid_id uuid,
    detail jsonb DEFAULT '{}'::jsonb NOT NULL,
    idempotency_key text NOT NULL,
    occurred_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT execution_outcome_check CHECK ((outcome = ANY (ARRAY['bid_placed'::text, 'suppressed_guardrail'::text, 'suppressed_exposure'::text, 'no_match'::text, 'error'::text])))
);

ALTER TABLE ONLY autobid.execution_default FORCE ROW LEVEL SECURITY;

--
-- Name: execution_id_seq; Type: SEQUENCE; Schema: autobid; Owner: -
--

ALTER TABLE autobid.execution ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME autobid.execution_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

--
-- Name: exposure; Type: TABLE; Schema: autobid; Owner: -
--

CREATE TABLE autobid.exposure (
    institution_id uuid NOT NULL,
    open_bid_count integer DEFAULT 0 NOT NULL,
    open_principal_mur numeric(16,2) DEFAULT 0 NOT NULL,
    bids_today integer DEFAULT 0 NOT NULL,
    principal_today_mur numeric(16,2) DEFAULT 0 NOT NULL,
    today date DEFAULT CURRENT_DATE NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT exposure_bids_today_check CHECK ((bids_today >= 0)),
    CONSTRAINT exposure_open_bid_count_check CHECK ((open_bid_count >= 0)),
    CONSTRAINT exposure_open_principal_mur_check CHECK ((open_principal_mur >= (0)::numeric)),
    CONSTRAINT exposure_principal_today_mur_check CHECK ((principal_today_mur >= (0)::numeric))
);

ALTER TABLE ONLY autobid.exposure FORCE ROW LEVEL SECURITY;

--
-- Name: rule; Type: TABLE; Schema: autobid; Owner: -
--

CREATE TABLE autobid.rule (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    institution_id uuid NOT NULL,
    name text NOT NULL,
    description text,
    status text DEFAULT 'draft'::text NOT NULL,
    priority integer DEFAULT 100 NOT NULL,
    current_version_id uuid,
    created_by uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT rule_priority_check CHECK (((priority >= 1) AND (priority <= 10000))),
    CONSTRAINT rule_status_check CHECK ((status = ANY (ARRAY['draft'::text, 'pending_approval'::text, 'active'::text, 'paused'::text, 'rejected'::text, 'retired'::text])))
);

ALTER TABLE ONLY autobid.rule FORCE ROW LEVEL SECURITY;

--
-- Name: rule_version; Type: TABLE; Schema: autobid; Owner: -
--

CREATE TABLE autobid.rule_version (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    rule_id uuid NOT NULL,
    institution_id uuid NOT NULL,
    version_no integer NOT NULL,
    predicate jsonb NOT NULL,
    action jsonb NOT NULL,
    guardrails jsonb NOT NULL,
    submitted_by uuid,
    submitted_at timestamp with time zone,
    approved_by uuid,
    approved_at timestamp with time zone,
    rejected_by uuid,
    rejected_at timestamp with time zone,
    rejection_note text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT rule_version_check CHECK (((approved_by IS NULL) OR (submitted_by IS NULL) OR (approved_by <> submitted_by))),
    CONSTRAINT rule_version_version_no_check CHECK ((version_no >= 1))
);

ALTER TABLE ONLY autobid.rule_version FORCE ROW LEVEL SECURITY;

--
-- Name: commission_adjustment; Type: TABLE; Schema: billing; Owner: -
--

CREATE TABLE billing.commission_adjustment (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    commission_id uuid NOT NULL,
    pipeline_id uuid NOT NULL,
    institution_id uuid NOT NULL,
    kind text NOT NULL,
    amount numeric(15,2) NOT NULL,
    currency character(3) NOT NULL,
    effective_on date NOT NULL,
    reason text NOT NULL,
    calc jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT commission_adjustment_amount_check CHECK ((amount < (0)::numeric)),
    CONSTRAINT commission_adjustment_kind_check CHECK ((kind = 'clawback'::text)),
    CONSTRAINT commission_adjustment_reason_check CHECK (((char_length(btrim(reason)) >= 5) AND (char_length(btrim(reason)) <= 500)))
);

ALTER TABLE ONLY billing.commission_adjustment FORCE ROW LEVEL SECURITY;

--
-- Name: country; Type: TABLE; Schema: catalog; Owner: -
--

CREATE TABLE catalog.country (
    code character(2) NOT NULL,
    name text NOT NULL,
    currency character(3) NOT NULL,
    active boolean DEFAULT true NOT NULL
);

--
-- Name: currency; Type: TABLE; Schema: catalog; Owner: -
--

CREATE TABLE catalog.currency (
    code character(3) NOT NULL,
    name text NOT NULL,
    symbol text NOT NULL,
    decimal_places integer DEFAULT 2 NOT NULL
);

--
-- Name: module; Type: TABLE; Schema: catalog; Owner: -
--

CREATE TABLE catalog.module (
    key text NOT NULL,
    label text NOT NULL,
    description text DEFAULT ''::text NOT NULL,
    side text DEFAULT 'institution'::text NOT NULL,
    icon text,
    path text,
    sort_order integer DEFAULT 0 NOT NULL,
    active boolean DEFAULT true NOT NULL,
    CONSTRAINT module_side_check CHECK ((side = ANY (ARRAY['institution'::text, 'admin'::text, 'both'::text])))
);

--
-- Name: product; Type: TABLE; Schema: catalog; Owner: -
--

CREATE TABLE catalog.product (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    family_id uuid NOT NULL,
    code text NOT NULL,
    label text NOT NULL,
    description text DEFAULT ''::text NOT NULL,
    currency character(3) DEFAULT 'MUR'::bpchar NOT NULL,
    min_amount numeric(20,6),
    max_amount numeric(20,6),
    min_term_months integer,
    max_term_months integer,
    active boolean DEFAULT true NOT NULL,
    sort_order integer DEFAULT 0 NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT product_check CHECK (((min_amount IS NULL) OR (max_amount IS NULL) OR (min_amount <= max_amount)))
);

--
-- Name: product_document; Type: TABLE; Schema: catalog; Owner: -
--

CREATE TABLE catalog.product_document (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    product_id uuid NOT NULL,
    doc_key text NOT NULL,
    label text NOT NULL,
    description text DEFAULT ''::text NOT NULL,
    required boolean DEFAULT true NOT NULL,
    allowed_mime_types text[] DEFAULT '{}'::text[] NOT NULL,
    max_size_bytes integer,
    sort_order integer DEFAULT 0 NOT NULL
);

--
-- Name: product_eligibility; Type: TABLE; Schema: catalog; Owner: -
--

CREATE TABLE catalog.product_eligibility (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    product_id uuid NOT NULL,
    country character(2),
    rules jsonb DEFAULT '{}'::jsonb NOT NULL,
    description text DEFAULT ''::text NOT NULL,
    active boolean DEFAULT true NOT NULL
);

--
-- Name: product_family; Type: TABLE; Schema: catalog; Owner: -
--

CREATE TABLE catalog.product_family (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    code text NOT NULL,
    label text NOT NULL,
    description text DEFAULT ''::text NOT NULL,
    icon text,
    sort_order integer DEFAULT 0 NOT NULL,
    active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: product_parameter; Type: TABLE; Schema: catalog; Owner: -
--

CREATE TABLE catalog.product_parameter (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    product_id uuid NOT NULL,
    key text NOT NULL,
    label text NOT NULL,
    data_type text DEFAULT 'text'::text NOT NULL,
    required boolean DEFAULT false NOT NULL,
    options jsonb,
    validation jsonb,
    sort_order integer DEFAULT 0 NOT NULL,
    CONSTRAINT product_parameter_data_type_check CHECK ((data_type = ANY (ARRAY['text'::text, 'number'::text, 'boolean'::text, 'date'::text, 'select'::text, 'multiselect'::text])))
);

--
-- Name: product_rate_model; Type: TABLE; Schema: catalog; Owner: -
--

CREATE TABLE catalog.product_rate_model (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    product_id uuid NOT NULL,
    rate_type text DEFAULT 'fixed'::text NOT NULL,
    min_rate numeric(8,4),
    max_rate numeric(8,4),
    rate_unit text DEFAULT 'percent_per_annum'::text NOT NULL,
    compounding text DEFAULT 'monthly'::text NOT NULL,
    config jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT product_rate_model_compounding_check CHECK ((compounding = ANY (ARRAY['daily'::text, 'monthly'::text, 'quarterly'::text, 'annually'::text, 'none'::text]))),
    CONSTRAINT product_rate_model_rate_type_check CHECK ((rate_type = ANY (ARRAY['fixed'::text, 'variable'::text, 'range'::text, 'tiered'::text])))
);

--
-- Name: product_sla; Type: TABLE; Schema: catalog; Owner: -
--

CREATE TABLE catalog.product_sla (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    product_id uuid NOT NULL,
    bid_window_minutes integer DEFAULT 240 NOT NULL,
    auto_withdraw_minutes integer DEFAULT 300 NOT NULL,
    min_bids_required integer DEFAULT 1 NOT NULL,
    max_bids_allowed integer,
    CONSTRAINT product_sla_auto_withdraw_minutes_check CHECK ((auto_withdraw_minutes > 0)),
    CONSTRAINT product_sla_bid_window_minutes_check CHECK ((bid_window_minutes > 0))
);

--
-- Name: regulator; Type: TABLE; Schema: catalog; Owner: -
--

CREATE TABLE catalog.regulator (
    code text NOT NULL,
    name text NOT NULL,
    country text NOT NULL,
    website text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: institution_entitlement; Type: TABLE; Schema: entitlements; Owner: -
--

CREATE TABLE entitlements.institution_entitlement (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    institution_id uuid NOT NULL,
    module_code text NOT NULL,
    plan_tier text DEFAULT 'standard'::text NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    valid_from timestamp with time zone DEFAULT now() NOT NULL,
    valid_to timestamp with time zone,
    limits jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT institution_entitlement_check CHECK (((valid_to IS NULL) OR (valid_to > valid_from))),
    CONSTRAINT institution_entitlement_plan_tier_check CHECK ((plan_tier = ANY (ARRAY['trial'::text, 'standard'::text, 'professional'::text, 'enterprise'::text]))),
    CONSTRAINT institution_entitlement_status_check CHECK ((status = ANY (ARRAY['active'::text, 'trial'::text, 'suspended'::text, 'expired'::text])))
);

ALTER TABLE ONLY entitlements.institution_entitlement FORCE ROW LEVEL SECURITY;

--
-- Name: module; Type: TABLE; Schema: entitlements; Owner: -
--

CREATE TABLE entitlements.module (
    module_code text NOT NULL,
    name text NOT NULL,
    description text,
    billing_model text NOT NULL,
    metered_unit text,
    status text DEFAULT 'active'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT module_billing_model_check CHECK ((billing_model = ANY (ARRAY['flat'::text, 'metered'::text, 'flat_plus_metered'::text]))),
    CONSTRAINT module_check CHECK ((((billing_model = 'flat'::text) AND (metered_unit IS NULL)) OR ((billing_model <> 'flat'::text) AND (metered_unit IS NOT NULL)))),
    CONSTRAINT module_module_code_check CHECK ((module_code ~ '^[A-Z][A-Z0-9_]{1,39}$'::text)),
    CONSTRAINT module_status_check CHECK ((status = ANY (ARRAY['active'::text, 'retired'::text])))
);

ALTER TABLE ONLY entitlements.module FORCE ROW LEVEL SECURITY;

--
-- Name: usage_event; Type: TABLE; Schema: entitlements; Owner: -
--

CREATE TABLE entitlements.usage_event (
    id bigint NOT NULL,
    institution_id uuid NOT NULL,
    module_code text NOT NULL,
    unit text NOT NULL,
    quantity numeric DEFAULT 1 NOT NULL,
    idempotency_key text NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    occurred_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT usage_event_quantity_check CHECK ((quantity > (0)::numeric))
)
PARTITION BY RANGE (occurred_at);

ALTER TABLE ONLY entitlements.usage_event FORCE ROW LEVEL SECURITY;

--
-- Name: usage_event_default; Type: TABLE; Schema: entitlements; Owner: -
--

CREATE TABLE entitlements.usage_event_default (
    id bigint NOT NULL,
    institution_id uuid NOT NULL,
    module_code text NOT NULL,
    unit text NOT NULL,
    quantity numeric DEFAULT 1 NOT NULL,
    idempotency_key text NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    occurred_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT usage_event_quantity_check CHECK ((quantity > (0)::numeric))
);

ALTER TABLE ONLY entitlements.usage_event_default FORCE ROW LEVEL SECURITY;

--
-- Name: usage_event_id_seq; Type: SEQUENCE; Schema: entitlements; Owner: -
--

ALTER TABLE entitlements.usage_event ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME entitlements.usage_event_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

--
-- Name: action; Type: TABLE; Schema: governance; Owner: -
--

CREATE TABLE governance.action (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    scope text DEFAULT 'institution'::text NOT NULL,
    category text NOT NULL,
    label text DEFAULT ''::text NOT NULL,
    risk text DEFAULT 'medium'::text NOT NULL,
    institution_id uuid,
    maker_id uuid NOT NULL,
    maker_role text DEFAULT ''::text NOT NULL,
    maker_ip inet,
    maker_user_agent text,
    resource_type text NOT NULL,
    resource_id uuid,
    resource_label text,
    payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    payload_before jsonb,
    status text DEFAULT 'pending'::text NOT NULL,
    checker_id uuid,
    checker_role text,
    checker_note text,
    checker_ip inet,
    checked_at timestamp with time zone,
    execution_status text DEFAULT 'pending'::text NOT NULL,
    executed_at timestamp with time zone,
    execution_error text,
    expires_at timestamp with time zone DEFAULT (now() + '7 days'::interval) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    control text DEFAULT 'four_eyes'::text NOT NULL,
    credential_released_at timestamp with time zone,
    change jsonb,
    CONSTRAINT action_control_check CHECK ((control = ANY (ARRAY['four_eyes'::text, 'single'::text]))),
    CONSTRAINT action_execution_status_check CHECK ((execution_status = ANY (ARRAY['pending'::text, 'executing'::text, 'executed'::text, 'failed'::text, 'skipped'::text]))),
    CONSTRAINT action_risk_check CHECK ((risk = ANY (ARRAY['low'::text, 'medium'::text, 'high'::text, 'critical'::text]))),
    CONSTRAINT action_scope_check CHECK ((scope = ANY (ARRAY['institution'::text, 'platform'::text]))),
    CONSTRAINT action_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'approved'::text, 'rejected'::text, 'expired'::text, 'cancelled'::text]))),
    CONSTRAINT four_eyes CHECK (((checker_id IS NULL) OR (checker_id <> maker_id)))
);

ALTER TABLE ONLY governance.action FORCE ROW LEVEL SECURITY;

--
-- Name: four_eyes_catalog; Type: TABLE; Schema: governance; Owner: -
--

CREATE TABLE governance.four_eyes_catalog (
    category text NOT NULL,
    label text NOT NULL,
    screen text NOT NULL,
    maker_module text,
    locked boolean DEFAULT false NOT NULL,
    default_on boolean DEFAULT true NOT NULL,
    locked_reason text,
    sort_order integer DEFAULT 100 NOT NULL,
    CONSTRAINT four_eyes_catalog_category_check CHECK ((category ~ '^[a-z_]+\.[a-z_]+$'::text)),
    CONSTRAINT four_eyes_catalog_check CHECK (((NOT locked) OR (default_on AND (locked_reason IS NOT NULL))))
);

--
-- Name: four_eyes_policy; Type: TABLE; Schema: governance; Owner: -
--

CREATE TABLE governance.four_eyes_policy (
    institution_id uuid NOT NULL,
    category text NOT NULL,
    enabled boolean NOT NULL,
    changed_by uuid,
    changed_at timestamp with time zone DEFAULT now() NOT NULL,
    via_action_id uuid
);

--
-- Name: access_request; Type: TABLE; Schema: institution; Owner: -
--

CREATE TABLE institution.access_request (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    reference text DEFAULT upper(substr(replace((gen_random_uuid())::text, '-'::text, ''::text), 1, 10)) NOT NULL,
    institution_name text NOT NULL,
    institution_type text NOT NULL,
    country text NOT NULL,
    reg_number text,
    regulator text,
    website text,
    contact_name text NOT NULL,
    contact_email text NOT NULL,
    contact_phone text,
    contact_role text,
    message text,
    status text DEFAULT 'unverified'::text NOT NULL,
    verify_token_hash text,
    verify_expires_at timestamp with time zone,
    verified_at timestamp with time zone,
    reviewed_by uuid,
    reviewed_at timestamp with time zone,
    review_note text,
    provisioned_by uuid,
    provisioned_at timestamp with time zone,
    contract_reference text,
    institution_id uuid,
    submit_ip inet,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT access_request_check CHECK (((status <> 'provisioned'::text) OR ((institution_id IS NOT NULL) AND (provisioned_at IS NOT NULL)))),
    CONSTRAINT access_request_check1 CHECK (((status = ANY (ARRAY['unverified'::text, 'expired'::text])) OR (verified_at IS NOT NULL))),
    CONSTRAINT access_request_contact_email_check CHECK (((length(contact_email) >= 3) AND (length(contact_email) <= 254))),
    CONSTRAINT access_request_contact_name_check CHECK (((length(btrim(contact_name)) >= 2) AND (length(btrim(contact_name)) <= 200))),
    CONSTRAINT access_request_contact_phone_check CHECK ((length(contact_phone) <= 40)),
    CONSTRAINT access_request_contact_role_check CHECK ((length(contact_role) <= 100)),
    CONSTRAINT access_request_contract_reference_check CHECK ((length(contract_reference) <= 100)),
    CONSTRAINT access_request_country_check CHECK ((country ~ '^[A-Z]{2,3}$'::text)),
    CONSTRAINT access_request_institution_name_check CHECK (((length(btrim(institution_name)) >= 2) AND (length(btrim(institution_name)) <= 200))),
    CONSTRAINT access_request_institution_type_check CHECK ((institution_type = ANY (ARRAY['commercial_bank'::text, 'fintech'::text, 'micro_credit'::text, 'leasing'::text, 'insurance'::text, 'cooperative'::text, 'other'::text]))),
    CONSTRAINT access_request_message_check CHECK ((length(message) <= 2000)),
    CONSTRAINT access_request_reg_number_check CHECK ((length(reg_number) <= 100)),
    CONSTRAINT access_request_regulator_check CHECK ((length(regulator) <= 100)),
    CONSTRAINT access_request_review_note_check CHECK ((length(review_note) <= 500)),
    CONSTRAINT access_request_status_check CHECK ((status = ANY (ARRAY['unverified'::text, 'submitted'::text, 'in_review'::text, 'info_requested'::text, 'approved'::text, 'rejected'::text, 'provisioned'::text, 'expired'::text]))),
    CONSTRAINT access_request_website_check CHECK ((length(website) <= 300))
);

--
-- Name: TABLE access_request; Type: COMMENT; Schema: institution; Owner: -
--

COMMENT ON TABLE institution.access_request IS 'Institution applications (060). Record only: an institution and its first login are created by the four-eyes institution.provision admin command after the contract is signed.';

--
-- Name: api_key; Type: TABLE; Schema: institution; Owner: -
--

CREATE TABLE institution.api_key (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    institution_id uuid NOT NULL,
    label text NOT NULL,
    key_hmac text NOT NULL,
    scopes text[] DEFAULT '{}'::text[] NOT NULL,
    active boolean DEFAULT false NOT NULL,
    created_by uuid,
    mc_status text DEFAULT 'pending_approval'::text NOT NULL,
    requested_by uuid,
    approved_by uuid,
    approved_at timestamp with time zone,
    rejection_note text,
    last_used_at timestamp with time zone,
    expires_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    last_used_ip inet,
    revoked_at timestamp with time zone,
    revoked_by uuid,
    key_prefix text DEFAULT ''::text NOT NULL,
    key_hash text DEFAULT ''::text NOT NULL,
    CONSTRAINT chk_active_requires_approval CHECK (((NOT active) OR (mc_status = 'approved'::text))),
    CONSTRAINT chk_key_hmac_format CHECK ((key_hmac ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT institution_api_keys_mc_status_check CHECK ((mc_status = ANY (ARRAY['pending_approval'::text, 'approved'::text, 'rejected'::text])))
);

ALTER TABLE ONLY institution.api_key FORCE ROW LEVEL SECURITY;

--
-- Name: approval_action; Type: TABLE; Schema: institution; Owner: -
--

CREATE TABLE institution.approval_action (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    stage_instance_id uuid NOT NULL,
    actor_id uuid NOT NULL,
    acting_as uuid,
    action text NOT NULL,
    comment text,
    checklist_state jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT approval_action_action_check CHECK ((action = ANY (ARRAY['approve'::text, 'reject'::text, 'abstain'::text, 'recuse'::text, 'comment'::text, 'checklist_update'::text, 'delegate'::text, 'withdraw'::text, 'escalate'::text]))),
    CONSTRAINT approval_action_check CHECK (((action <> ALL (ARRAY['reject'::text, 'recuse'::text])) OR (comment IS NOT NULL)))
);

ALTER TABLE ONLY institution.approval_action FORCE ROW LEVEL SECURITY;

--
-- Name: approval_committee; Type: TABLE; Schema: institution; Owner: -
--

CREATE TABLE institution.approval_committee (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    institution_id uuid NOT NULL,
    name text NOT NULL,
    description text,
    quorum_type text NOT NULL,
    quorum_value numeric,
    tie_break text DEFAULT 'chair'::text NOT NULL,
    allow_abstain boolean DEFAULT true NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    created_by uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT approval_committee_check CHECK ((((quorum_type = 'count'::text) AND (quorum_value >= (1)::numeric)) OR ((quorum_type = 'fraction'::text) AND (quorum_value > (0)::numeric) AND (quorum_value <= (1)::numeric)) OR ((quorum_type = 'unanimous'::text) AND (quorum_value IS NULL)))),
    CONSTRAINT approval_committee_quorum_type_check CHECK ((quorum_type = ANY (ARRAY['count'::text, 'fraction'::text, 'unanimous'::text]))),
    CONSTRAINT approval_committee_status_check CHECK ((status = ANY (ARRAY['active'::text, 'retired'::text]))),
    CONSTRAINT approval_committee_tie_break_check CHECK ((tie_break = ANY (ARRAY['chair'::text, 'reject'::text, 'escalate'::text])))
);

ALTER TABLE ONLY institution.approval_committee FORCE ROW LEVEL SECURITY;

--
-- Name: approval_delegation; Type: TABLE; Schema: institution; Owner: -
--

CREATE TABLE institution.approval_delegation (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    institution_id uuid NOT NULL,
    from_member uuid NOT NULL,
    to_member uuid NOT NULL,
    scope text DEFAULT 'all'::text NOT NULL,
    reason text NOT NULL,
    valid_from timestamp with time zone NOT NULL,
    valid_to timestamp with time zone NOT NULL,
    approved_by uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT approval_delegation_check CHECK ((from_member <> to_member)),
    CONSTRAINT approval_delegation_check1 CHECK ((valid_to > valid_from))
);

ALTER TABLE ONLY institution.approval_delegation FORCE ROW LEVEL SECURITY;

--
-- Name: approval_instance; Type: TABLE; Schema: institution; Owner: -
--

CREATE TABLE institution.approval_instance (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    institution_id uuid NOT NULL,
    template_id uuid NOT NULL,
    template_version integer NOT NULL,
    doa_rule_id uuid NOT NULL,
    entity_type text NOT NULL,
    entity_id uuid NOT NULL,
    entity_maker_id uuid NOT NULL,
    entity_snapshot jsonb NOT NULL,
    current_seq integer DEFAULT 1 NOT NULL,
    status text DEFAULT 'in_progress'::text NOT NULL,
    withdraw_reason text,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    resolved_at timestamp with time zone,
    CONSTRAINT approval_instance_status_check CHECK ((status = ANY (ARRAY['in_progress'::text, 'approved'::text, 'rejected'::text, 'expired'::text, 'escalated'::text, 'withdrawn'::text])))
);

ALTER TABLE ONLY institution.approval_instance FORCE ROW LEVEL SECURITY;

--
-- Name: approval_outbox; Type: TABLE; Schema: institution; Owner: -
--

CREATE TABLE institution.approval_outbox (
    id bigint NOT NULL,
    institution_id uuid NOT NULL,
    event text NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    processed_at timestamp with time zone
);

ALTER TABLE ONLY institution.approval_outbox FORCE ROW LEVEL SECURITY;

--
-- Name: approval_outbox_id_seq; Type: SEQUENCE; Schema: institution; Owner: -
--

CREATE SEQUENCE institution.approval_outbox_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: approval_outbox_id_seq; Type: SEQUENCE OWNED BY; Schema: institution; Owner: -
--

ALTER SEQUENCE institution.approval_outbox_id_seq OWNED BY institution.approval_outbox.id;

--
-- Name: approval_stage_def; Type: TABLE; Schema: institution; Owner: -
--

CREATE TABLE institution.approval_stage_def (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    template_id uuid NOT NULL,
    seq integer NOT NULL,
    name text NOT NULL,
    stage_type text NOT NULL,
    committee_id uuid,
    approver_role text,
    checklist jsonb,
    sla_hours integer,
    on_sla_breach text DEFAULT 'notify'::text NOT NULL,
    escalate_to_template_id uuid,
    config jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT approval_stage_def_check CHECK (((stage_type <> 'committee'::text) OR (committee_id IS NOT NULL))),
    CONSTRAINT approval_stage_def_check1 CHECK (((stage_type <> ALL (ARRAY['single'::text, 'dual'::text])) OR (approver_role IS NOT NULL))),
    CONSTRAINT approval_stage_def_check2 CHECK (((stage_type <> 'checklist'::text) OR (checklist IS NOT NULL))),
    CONSTRAINT approval_stage_def_check3 CHECK (((on_sla_breach <> 'escalate'::text) OR (escalate_to_template_id IS NOT NULL))),
    CONSTRAINT approval_stage_def_on_sla_breach_check CHECK ((on_sla_breach = ANY (ARRAY['notify'::text, 'escalate'::text, 'auto_reject'::text]))),
    CONSTRAINT approval_stage_def_seq_check CHECK ((seq >= 1)),
    CONSTRAINT approval_stage_def_sla_hours_check CHECK (((sla_hours IS NULL) OR (sla_hours > 0))),
    CONSTRAINT approval_stage_def_stage_type_check CHECK ((stage_type = ANY (ARRAY['single'::text, 'dual'::text, 'committee'::text, 'checklist'::text, 'external_hold'::text])))
);

ALTER TABLE ONLY institution.approval_stage_def FORCE ROW LEVEL SECURITY;

--
-- Name: approval_stage_instance; Type: TABLE; Schema: institution; Owner: -
--

CREATE TABLE institution.approval_stage_instance (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    instance_id uuid NOT NULL,
    seq integer NOT NULL,
    stage_def_id uuid NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    started_at timestamp with time zone,
    due_at timestamp with time zone,
    resolved_at timestamp with time zone,
    CONSTRAINT approval_stage_instance_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'active'::text, 'approved'::text, 'rejected'::text, 'expired'::text, 'skipped'::text])))
);

ALTER TABLE ONLY institution.approval_stage_instance FORCE ROW LEVEL SECURITY;

--
-- Name: approval_template; Type: TABLE; Schema: institution; Owner: -
--

CREATE TABLE institution.approval_template (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    institution_id uuid NOT NULL,
    name text NOT NULL,
    entity_type text NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    status text DEFAULT 'draft'::text NOT NULL,
    created_by uuid NOT NULL,
    approved_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT approval_template_entity_type_check CHECK ((entity_type = ANY (ARRAY['bid'::text, 'offer_letter'::text, 'countersign'::text, 'investment_mandate'::text, 'api_key'::text, 'webhook'::text, 'user_admin'::text, 'autobid_rule'::text, 'custom'::text, 'facility'::text]))),
    CONSTRAINT approval_template_status_check CHECK ((status = ANY (ARRAY['draft'::text, 'pending_activation'::text, 'active'::text, 'retired'::text])))
);

ALTER TABLE ONLY institution.approval_template FORCE ROW LEVEL SECURITY;

--
-- Name: benefit; Type: TABLE; Schema: institution; Owner: -
--

CREATE TABLE institution.benefit (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    institution_id uuid NOT NULL,
    product_id uuid,
    cat_id uuid NOT NULL,
    title text NOT NULL,
    description text,
    value_display text,
    is_guaranteed boolean DEFAULT false NOT NULL,
    conditions text,
    valid_from date,
    valid_until date,
    is_active boolean DEFAULT true NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    value_amount numeric(14,4),
    value_unit text,
    CONSTRAINT ck_benefit_value CHECK ((((value_amount IS NULL) AND (value_unit IS NULL)) OR ((value_amount IS NOT NULL) AND (value_unit IS NOT NULL) AND (value_amount > (0)::numeric) AND (value_unit = ANY (ARRAY['pct_pa'::text, 'pct'::text, 'mur'::text])))))
);

--
-- Name: benefit_cat; Type: TABLE; Schema: institution; Owner: -
--

CREATE TABLE institution.benefit_cat (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    code text NOT NULL,
    label text NOT NULL,
    icon_key text,
    sort_order integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: committee_member; Type: TABLE; Schema: institution; Owner: -
--

CREATE TABLE institution.committee_member (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    committee_id uuid NOT NULL,
    member_id uuid NOT NULL,
    role text DEFAULT 'member'::text NOT NULL,
    is_voting boolean DEFAULT true NOT NULL,
    valid_from date DEFAULT CURRENT_DATE NOT NULL,
    valid_to date,
    created_by uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT committee_member_check CHECK (((valid_to IS NULL) OR (valid_to >= valid_from))),
    CONSTRAINT committee_member_role_check CHECK ((role = ANY (ARRAY['chair'::text, 'vice_chair'::text, 'member'::text, 'secretary'::text, 'observer'::text])))
);

ALTER TABLE ONLY institution.committee_member FORCE ROW LEVEL SECURITY;

--
-- Name: compliance_waiver; Type: TABLE; Schema: institution; Owner: -
--

CREATE TABLE institution.compliance_waiver (
    institution_id uuid NOT NULL,
    reason text NOT NULL,
    waived_until timestamp with time zone NOT NULL,
    granted_by uuid NOT NULL,
    granted_at timestamp with time zone DEFAULT now() NOT NULL,
    revoked_at timestamp with time zone,
    revoked_by uuid,
    CONSTRAINT compliance_waiver_check CHECK (((waived_until > granted_at) AND (waived_until <= (granted_at + '180 days'::interval)))),
    CONSTRAINT compliance_waiver_reason_check CHECK ((length(btrim(reason)) >= 10))
);

--
-- Name: doc; Type: TABLE; Schema: institution; Owner: -
--

CREATE TABLE institution.doc (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    institution_id uuid NOT NULL,
    doc_type_id uuid NOT NULL,
    storage_path text NOT NULL,
    file_name text NOT NULL,
    mime_type text,
    status text DEFAULT 'pending'::text NOT NULL,
    expiry_date date,
    rejection_reason text,
    reviewed_by uuid,
    reviewed_at timestamp with time zone,
    uploaded_by uuid,
    uploaded_at timestamp with time zone DEFAULT now() NOT NULL,
    reviewed_by_staff uuid,
    CONSTRAINT doc_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'approved'::text, 'rejected'::text, 'expired'::text])))
);

--
-- Name: institution; Type: TABLE; Schema: institution; Owner: -
--

CREATE TABLE institution.institution (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    name character varying(150) NOT NULL,
    legal_name character varying(150) NOT NULL,
    institution_type character varying(50) NOT NULL,
    reg_number character varying(40),
    regulator character varying(100),
    country character varying(3) NOT NULL,
    website character varying(255),
    deployment_model character varying(20) DEFAULT 'saas'::character varying NOT NULL,
    modules jsonb DEFAULT '{}'::jsonb NOT NULL,
    onboarding_stage character varying(50) DEFAULT 'registered'::character varying NOT NULL,
    compliance_status character varying(30) DEFAULT 'not_submitted'::character varying NOT NULL,
    compliance_notes text,
    approved boolean DEFAULT false NOT NULL,
    approved_at timestamp with time zone,
    suspended_at timestamp with time zone,
    suspension_reason text,
    primary_contact_name character varying(150),
    primary_contact_email character varying(255),
    primary_contact_phone character varying(20),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    approved_by uuid,
    tax_id text,
    incorporation_date date,
    logo_url text,
    timezone text DEFAULT 'Indian/Mauritius'::text NOT NULL,
    compliance_reviewed_at timestamp with time zone,
    compliance_reviewed_by uuid,
    suspended_by uuid,
    offboarded_at timestamp with time zone,
    notes text,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL
);

ALTER TABLE ONLY institution.institution FORCE ROW LEVEL SECURITY;

--
-- Name: compliance; Type: VIEW; Schema: institution; Owner: -
--

CREATE VIEW institution.compliance WITH (security_barrier='true') AS
 WITH base AS (
         SELECT i.id AS institution_id,
            ((i.approved = true) AND (i.suspended_at IS NULL)) AS is_approved,
            ARRAY( SELECT dt.code
                   FROM institution.required_doc_types(i.id) dt(id, code, label, description, is_mandatory, applies_to, sort_order, created_at)
                  WHERE (NOT (EXISTS ( SELECT 1
                           FROM institution.doc d
                          WHERE ((d.institution_id = i.id) AND (d.doc_type_id = dt.id) AND (d.status = 'approved'::text) AND ((d.expiry_date IS NULL) OR (d.expiry_date > CURRENT_DATE))))))) AS missing_docs,
            ( SELECT w.waived_until
                   FROM institution.compliance_waiver w
                  WHERE ((w.institution_id = i.id) AND (w.revoked_at IS NULL) AND (w.waived_until > now()))) AS waived_until
           FROM institution.institution i
          WHERE ((CURRENT_USER <> ALL (ARRAY['authenticated'::name, 'anon'::name])) OR (i.id = ( SELECT (NULLIF(((NULLIF(current_setting('request.jwt.claims'::text, true), ''::text))::jsonb ->> 'institution_id'::text), ''::text))::uuid AS "nullif")))
        )
 SELECT institution_id,
    is_approved,
    (cardinality(missing_docs) = 0) AS is_compliant,
    missing_docs,
    (is_approved AND ((cardinality(missing_docs) = 0) OR (waived_until IS NOT NULL))) AS can_bid,
    waived_until
   FROM base;

--
-- Name: doa_rule; Type: TABLE; Schema: institution; Owner: -
--

CREATE TABLE institution.doa_rule (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    institution_id uuid NOT NULL,
    entity_type text NOT NULL,
    priority integer NOT NULL,
    conditions jsonb DEFAULT '{}'::jsonb NOT NULL,
    template_id uuid NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    created_by uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT doa_rule_status_check CHECK ((status = ANY (ARRAY['active'::text, 'retired'::text])))
);

ALTER TABLE ONLY institution.doa_rule FORCE ROW LEVEL SECURITY;

--
-- Name: doc_generation; Type: TABLE; Schema: institution; Owner: -
--

CREATE TABLE institution.doc_generation (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    institution_id uuid NOT NULL,
    template_id uuid NOT NULL,
    template_version_id uuid NOT NULL,
    entity_type text DEFAULT 'loan_pipeline'::text NOT NULL,
    entity_id uuid NOT NULL,
    stage_instance_id uuid,
    data_snapshot jsonb DEFAULT '{}'::jsonb NOT NULL,
    output_docx_path text,
    output_pdf_path text,
    status text DEFAULT 'pending'::text NOT NULL,
    error text,
    esign_envelope_id uuid,
    generated_by uuid,
    generated_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT doc_generation_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'generating'::text, 'generated'::text, 'failed'::text])))
);

ALTER TABLE ONLY institution.doc_generation FORCE ROW LEVEL SECURITY;

--
-- Name: doc_requirement; Type: TABLE; Schema: institution; Owner: -
--

CREATE TABLE institution.doc_requirement (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    institution_id uuid NOT NULL,
    template_id uuid NOT NULL,
    stage_def_id uuid,
    name text NOT NULL,
    description text,
    provided_by text DEFAULT 'borrower'::text NOT NULL,
    mandatory boolean DEFAULT true NOT NULL,
    "position" integer DEFAULT 0 NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    document_type text,
    max_age_days integer,
    CONSTRAINT ck_doc_requirement_max_age CHECK (((max_age_days IS NULL) OR ((max_age_days >= 1) AND (max_age_days <= 3650)))),
    CONSTRAINT ck_doc_requirement_type CHECK (((document_type IS NULL) OR (document_type = ANY (ARRAY['payslip'::text, 'employment_letter'::text, 'tax_return'::text, 'bank_statement'::text, 'loan_statement'::text, 'credit_card_statement'::text, 'title_deed'::text, 'valuation_report'::text, 'land_registry_extract'::text, 'brn_certificate'::text, 'audited_accounts'::text, 'insurance_policy'::text, 'nic'::text, 'passport'::text, 'birth_certificate'::text, 'driving_licence'::text, 'other'::text])))),
    CONSTRAINT doc_requirement_description_check CHECK (((description IS NULL) OR (char_length(description) <= 500))),
    CONSTRAINT doc_requirement_name_check CHECK (((char_length(btrim(name)) >= 1) AND (char_length(btrim(name)) <= 120))),
    CONSTRAINT doc_requirement_provided_by_check CHECK ((provided_by = ANY (ARRAY['borrower'::text, 'bank'::text, 'third_party'::text])))
);

ALTER TABLE ONLY institution.doc_requirement FORCE ROW LEVEL SECURITY;

--
-- Name: doc_template; Type: TABLE; Schema: institution; Owner: -
--

CREATE TABLE institution.doc_template (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    institution_id uuid NOT NULL,
    product_id uuid,
    product_code text,
    code text NOT NULL,
    name text NOT NULL,
    description text,
    doc_category text DEFAULT 'other'::text NOT NULL,
    status text DEFAULT 'draft'::text NOT NULL,
    current_version integer DEFAULT 0 NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT doc_template_doc_category_check CHECK ((doc_category = ANY (ARRAY['loan_agreement'::text, 'facility_letter'::text, 'legal_agreement'::text, 'terms_conditions'::text, 'other'::text]))),
    CONSTRAINT doc_template_status_check CHECK ((status = ANY (ARRAY['draft'::text, 'pending_approval'::text, 'active'::text, 'retired'::text])))
);

ALTER TABLE ONLY institution.doc_template FORCE ROW LEVEL SECURITY;

--
-- Name: doc_template_version; Type: TABLE; Schema: institution; Owner: -
--

CREATE TABLE institution.doc_template_version (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    template_id uuid NOT NULL,
    institution_id uuid NOT NULL,
    version_no integer NOT NULL,
    storage_path text NOT NULL,
    file_name text NOT NULL,
    mime_type text DEFAULT 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'::text NOT NULL,
    file_size_bytes integer,
    checksum_sha256 text,
    merge_field_map jsonb DEFAULT '{}'::jsonb NOT NULL,
    change_note text,
    status text DEFAULT 'draft'::text NOT NULL,
    created_by uuid,
    approved_by uuid,
    approved_at timestamp with time zone,
    rejection_note text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT doc_template_version_status_check CHECK ((status = ANY (ARRAY['draft'::text, 'pending_approval'::text, 'published'::text, 'retired'::text, 'rejected'::text]))),
    CONSTRAINT doc_template_version_version_no_check CHECK ((version_no >= 1))
);

ALTER TABLE ONLY institution.doc_template_version FORCE ROW LEVEL SECURITY;

--
-- Name: esign_envelope; Type: TABLE; Schema: institution; Owner: -
--

CREATE TABLE institution.esign_envelope (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    institution_id uuid NOT NULL,
    entity_type text NOT NULL,
    entity_id uuid NOT NULL,
    approval_instance_id uuid,
    title text NOT NULL,
    document_path text NOT NULL,
    document_sha256 text NOT NULL,
    sealed_path text,
    sealed_sha256 text,
    status text DEFAULT 'draft'::text NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    created_by uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone,
    CONSTRAINT esign_envelope_entity_type_check CHECK ((entity_type = ANY (ARRAY['offer_letter'::text, 'investment_mandate'::text, 'custom'::text]))),
    CONSTRAINT esign_envelope_status_check CHECK ((status = ANY (ARRAY['draft'::text, 'sent'::text, 'partially_signed'::text, 'completed'::text, 'declined'::text, 'expired'::text, 'voided'::text])))
);

ALTER TABLE ONLY institution.esign_envelope FORCE ROW LEVEL SECURITY;

--
-- Name: esign_event; Type: TABLE; Schema: institution; Owner: -
--

CREATE TABLE institution.esign_event (
    id bigint NOT NULL,
    envelope_id uuid NOT NULL,
    signer_id uuid,
    event text NOT NULL,
    detail jsonb DEFAULT '{}'::jsonb NOT NULL,
    ip inet,
    user_agent text,
    prev_hash text NOT NULL,
    hash text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT esign_event_event_check CHECK ((event = ANY (ARRAY['created'::text, 'sent'::text, 'viewed'::text, 'otp_sent'::text, 'otp_verified'::text, 'otp_failed'::text, 'signed'::text, 'declined'::text, 'countersigned'::text, 'sealed'::text, 'voided'::text, 'expired'::text])))
);

ALTER TABLE ONLY institution.esign_event FORCE ROW LEVEL SECURITY;

--
-- Name: esign_event_id_seq; Type: SEQUENCE; Schema: institution; Owner: -
--

CREATE SEQUENCE institution.esign_event_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: esign_event_id_seq; Type: SEQUENCE OWNED BY; Schema: institution; Owner: -
--

ALTER SEQUENCE institution.esign_event_id_seq OWNED BY institution.esign_event.id;

--
-- Name: esign_signer; Type: TABLE; Schema: institution; Owner: -
--

CREATE TABLE institution.esign_signer (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    envelope_id uuid NOT NULL,
    party text NOT NULL,
    sign_order integer NOT NULL,
    display_name text NOT NULL,
    email text NOT NULL,
    user_ref uuid,
    token_sha256 text NOT NULL,
    otp_hash text,
    otp_expires_at timestamp with time zone,
    otp_attempts integer DEFAULT 0 NOT NULL,
    otp_verified_at timestamp with time zone,
    status text DEFAULT 'pending'::text NOT NULL,
    signed_at timestamp with time zone,
    signed_ip inet,
    signed_ua text,
    decline_reason text,
    CONSTRAINT esign_signer_party_check CHECK ((party = ANY (ARRAY['borrower'::text, 'institution'::text]))),
    CONSTRAINT esign_signer_sign_order_check CHECK ((sign_order >= 1)),
    CONSTRAINT esign_signer_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'viewed'::text, 'signed'::text, 'declined'::text])))
);

ALTER TABLE ONLY institution.esign_signer FORCE ROW LEVEL SECURITY;

--
-- Name: group; Type: TABLE; Schema: institution; Owner: -
--

CREATE TABLE institution."group" (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    institution_id uuid NOT NULL,
    slug text NOT NULL,
    label text NOT NULL,
    description text DEFAULT ''::text NOT NULL,
    module_permissions text[] DEFAULT '{}'::text[] NOT NULL,
    is_system boolean DEFAULT false NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    product_scope uuid[]
);

ALTER TABLE ONLY institution."group" FORCE ROW LEVEL SECURITY;

--
-- Name: COLUMN "group".product_scope; Type: COMMENT; Schema: institution; Owner: -
--

COMMENT ON COLUMN institution."group".product_scope IS 'Subset of catalog.product.id this group may access. NULL or empty = all products the institution is licensed for via institution.product_config.';

--
-- Name: institution_sla_config; Type: TABLE; Schema: institution; Owner: -
--

CREATE TABLE institution.institution_sla_config (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    institution_id uuid NOT NULL,
    product_id uuid,
    product_code text,
    bid_window_minutes integer,
    auto_withdraw_minutes integer,
    integration_mode text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY institution.institution_sla_config FORCE ROW LEVEL SECURITY;

--
-- Name: kyb_document; Type: TABLE; Schema: institution; Owner: -
--

CREATE TABLE institution.kyb_document (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    institution_id uuid NOT NULL,
    doc_type text NOT NULL,
    label text NOT NULL,
    storage_path text NOT NULL,
    mime_type text NOT NULL,
    file_size_bytes integer,
    status text DEFAULT 'pending'::text NOT NULL,
    rejection_reason text,
    reviewed_by uuid,
    reviewed_at timestamp with time zone,
    expires_at timestamp with time zone,
    uploaded_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT kyb_document_doc_type_check CHECK ((doc_type = ANY (ARRAY['certificate_of_incorporation'::text, 'business_registration'::text, 'regulator_license'::text, 'tax_certificate'::text, 'audited_accounts'::text, 'aml_policy'::text, 'beneficial_owner_declaration'::text, 'other'::text]))),
    CONSTRAINT kyb_document_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'under_review'::text, 'accepted'::text, 'rejected'::text])))
);

ALTER TABLE ONLY institution.kyb_document FORCE ROW LEVEL SECURITY;

--
-- Name: member; Type: TABLE; Schema: institution; Owner: -
--

CREATE TABLE institution.member (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    institution_id uuid NOT NULL,
    auth_user_id uuid NOT NULL,
    email character varying(255) NOT NULL,
    full_name character varying(150),
    role character varying(50) DEFAULT 'analyst'::character varying NOT NULL,
    is_primary_admin boolean DEFAULT false NOT NULL,
    active boolean DEFAULT true NOT NULL,
    invited_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    group_id uuid,
    custom_group_id uuid,
    member_role text,
    system_group_id uuid,
    invited_at timestamp with time zone,
    activated_at timestamp with time zone,
    deactivated_at timestamp with time zone,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT institution_members_member_role_check CHECK ((member_role = ANY (ARRAY['maker'::text, 'checker'::text, 'viewer'::text, 'api_operator'::text])))
);

ALTER TABLE ONLY institution.member FORCE ROW LEVEL SECURITY;

--
-- Name: onboarding_pref; Type: TABLE; Schema: institution; Owner: -
--

CREATE TABLE institution.onboarding_pref (
    institution_id uuid NOT NULL,
    deployment_model text NOT NULL,
    integration_mode text NOT NULL,
    it_contact_name text NOT NULL,
    it_contact_email text NOT NULL,
    webhook_url text,
    expected_volume text,
    submitted_by uuid,
    submitted_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT onboarding_pref_deployment_model_check CHECK ((deployment_model = ANY (ARRAY['saas'::text, 'paas'::text, 'on_prem'::text])))
);

--
-- Name: pipeline_stage_def; Type: TABLE; Schema: institution; Owner: -
--

CREATE TABLE institution.pipeline_stage_def (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    template_id uuid NOT NULL,
    institution_id uuid NOT NULL,
    stage_key institution.stage_key_enum DEFAULT 'custom'::institution.stage_key_enum NOT NULL,
    custom_key text,
    label text NOT NULL,
    description text,
    "position" smallint NOT NULL,
    sla_hours integer DEFAULT 48 NOT NULL,
    requires_maker_checker boolean DEFAULT false NOT NULL,
    requires_documents boolean DEFAULT false NOT NULL,
    borrower_label text,
    borrower_visible boolean DEFAULT true NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    approval_route boolean DEFAULT false NOT NULL,
    decision_source text DEFAULT 'internal'::text NOT NULL,
    CONSTRAINT chk_custom_key CHECK (((stage_key <> 'custom'::institution.stage_key_enum) OR (custom_key IS NOT NULL))),
    CONSTRAINT pipeline_stage_def_decision_source_check CHECK ((decision_source = ANY (ARRAY['internal'::text, 'external'::text])))
);

ALTER TABLE ONLY institution.pipeline_stage_def FORCE ROW LEVEL SECURITY;

--
-- Name: pipeline_template; Type: TABLE; Schema: institution; Owner: -
--

CREATE TABLE institution.pipeline_template (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    institution_id uuid NOT NULL,
    name text NOT NULL,
    description text,
    product_code text,
    is_default boolean DEFAULT false NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY institution.pipeline_template FORCE ROW LEVEL SECURITY;

--
-- Name: product_config; Type: TABLE; Schema: institution; Owner: -
--

CREATE TABLE institution.product_config (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    institution_id uuid NOT NULL,
    product_id uuid NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    min_rate numeric,
    max_rate numeric,
    min_amount numeric,
    max_amount numeric,
    min_term_months integer,
    max_term_months integer,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    bid_window_minutes integer,
    auto_withdraw_minutes integer,
    conditions jsonb DEFAULT '{}'::jsonb NOT NULL
);

ALTER TABLE ONLY institution.product_config FORCE ROW LEVEL SECURITY;

--
-- Name: webhook; Type: TABLE; Schema: institution; Owner: -
--

CREATE TABLE institution.webhook (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    institution_id uuid NOT NULL,
    label text NOT NULL,
    endpoint_url text NOT NULL,
    secret_hash text NOT NULL,
    event_types jsonb DEFAULT '[]'::jsonb NOT NULL,
    active boolean DEFAULT true NOT NULL,
    retry_max integer DEFAULT 3 NOT NULL,
    timeout_ms integer DEFAULT 30000 NOT NULL,
    last_fired_at timestamp with time zone,
    last_status text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    signing_secret text DEFAULT ''::text NOT NULL,
    failure_count integer DEFAULT 0 NOT NULL
);

ALTER TABLE ONLY institution.webhook FORCE ROW LEVEL SECURITY;

--
-- Name: webhook_delivery; Type: TABLE; Schema: institution; Owner: -
--

CREATE TABLE institution.webhook_delivery (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    webhook_id uuid NOT NULL,
    institution_id uuid NOT NULL,
    event_type text NOT NULL,
    event_id uuid NOT NULL,
    payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    attempts integer DEFAULT 0 NOT NULL,
    last_attempt_at timestamp with time zone,
    response_status integer,
    response_body text,
    next_retry_at timestamp with time zone,
    delivered_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT webhook_delivery_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'delivered'::text, 'failed'::text, 'retrying'::text, 'abandoned'::text])))
);

ALTER TABLE ONLY institution.webhook_delivery FORCE ROW LEVEL SECURITY;

--
-- Name: acceptance_log; Type: TABLE; Schema: integration; Owner: -
--

CREATE TABLE integration.acceptance_log (
    idempotency_key text NOT NULL,
    body_sha256 text NOT NULL,
    request_id uuid,
    bid_id uuid,
    status_code integer NOT NULL,
    response jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT acceptance_log_idempotency_key_check CHECK (((length(idempotency_key) >= 8) AND (length(idempotency_key) <= 200)))
);

--
-- Name: admin_command_log; Type: TABLE; Schema: integration; Owner: -
--

CREATE TABLE integration.admin_command_log (
    command_id uuid NOT NULL,
    op text NOT NULL,
    body_sha256 text NOT NULL,
    maker_sub uuid NOT NULL,
    checker_sub uuid NOT NULL,
    status_code integer NOT NULL,
    response jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: aggregate_sequence; Type: TABLE; Schema: integration; Owner: -
--

CREATE TABLE integration.aggregate_sequence (
    aggregate_id text NOT NULL,
    last_sequence bigint NOT NULL
);

--
-- Name: api_channel_policy; Type: TABLE; Schema: integration; Owner: -
--

CREATE TABLE integration.api_channel_policy (
    channel text NOT NULL,
    state text NOT NULL,
    version bigint NOT NULL,
    reason text,
    changed_by uuid,
    changed_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT api_channel_policy_channel_check CHECK ((channel ~ '^[a-z][a-z0-9_.]{2,59}$'::text)),
    CONSTRAINT api_channel_policy_reason_check CHECK (((reason IS NULL) OR (length(reason) <= 2000))),
    CONSTRAINT api_channel_policy_state_check CHECK ((state = ANY (ARRAY['enabled'::text, 'paused'::text]))),
    CONSTRAINT api_channel_policy_version_check CHECK ((version >= 0))
);

--
-- Name: api_channel_status; Type: TABLE; Schema: integration; Owner: -
--

CREATE TABLE integration.api_channel_status (
    channel text NOT NULL,
    last_ok_at timestamp with time zone,
    last_error_at timestamp with time zone,
    last_error text,
    CONSTRAINT api_channel_status_channel_check CHECK ((channel ~ '^[a-z][a-z0-9_.]{2,59}$'::text)),
    CONSTRAINT api_channel_status_last_error_check CHECK (((last_error IS NULL) OR (length(last_error) <= 300)))
);

--
-- Name: api_traffic_hour; Type: TABLE; Schema: integration; Owner: -
--

CREATE TABLE integration.api_traffic_hour (
    channel text NOT NULL,
    hour timestamp with time zone NOT NULL,
    total bigint DEFAULT 0 NOT NULL,
    ok bigint DEFAULT 0 NOT NULL,
    client_errors bigint DEFAULT 0 NOT NULL,
    server_errors bigint DEFAULT 0 NOT NULL,
    network_errors bigint DEFAULT 0 NOT NULL,
    blocked bigint DEFAULT 0 NOT NULL,
    lat_b0 bigint DEFAULT 0 NOT NULL,
    lat_b1 bigint DEFAULT 0 NOT NULL,
    lat_b2 bigint DEFAULT 0 NOT NULL,
    lat_b3 bigint DEFAULT 0 NOT NULL,
    lat_b4 bigint DEFAULT 0 NOT NULL,
    lat_b5 bigint DEFAULT 0 NOT NULL,
    lat_b6 bigint DEFAULT 0 NOT NULL,
    lat_b7 bigint DEFAULT 0 NOT NULL,
    CONSTRAINT api_traffic_hour_channel_check CHECK ((channel ~ '^[a-z][a-z0-9_.]{2,59}$'::text)),
    CONSTRAINT api_traffic_hour_hour_check CHECK ((hour = date_trunc('hour'::text, hour)))
);

--
-- Name: document_upload; Type: TABLE; Schema: integration; Owner: -
--

CREATE TABLE integration.document_upload (
    upload_id uuid DEFAULT gen_random_uuid() NOT NULL,
    pipeline_id uuid NOT NULL,
    deal_document_id uuid NOT NULL,
    requirement_id uuid NOT NULL,
    institution_id uuid NOT NULL,
    storage_path text NOT NULL,
    file_name text NOT NULL,
    mime_type text NOT NULL,
    size_bytes integer NOT NULL,
    source text NOT NULL,
    document_date date,
    status text DEFAULT 'open'::text NOT NULL,
    sha256 text,
    expires_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone,
    content_sha256 text,
    content_received_at timestamp with time zone,
    CONSTRAINT document_upload_content_sha256_check CHECK (((content_sha256 IS NULL) OR (content_sha256 ~ '^[0-9a-f]{64}$'::text))),
    CONSTRAINT document_upload_source_check CHECK ((source = ANY (ARRAY['vault'::text, 'new'::text]))),
    CONSTRAINT document_upload_status_check CHECK ((status = ANY (ARRAY['open'::text, 'completed'::text])))
);

--
-- Name: emit_error; Type: TABLE; Schema: integration; Owner: -
--

CREATE TABLE integration.emit_error (
    id bigint NOT NULL,
    at timestamp with time zone DEFAULT now() NOT NULL,
    aggregate_id text,
    stage text,
    error text
);

--
-- Name: emit_error_id_seq; Type: SEQUENCE; Schema: integration; Owner: -
--

CREATE SEQUENCE integration.emit_error_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: emit_error_id_seq; Type: SEQUENCE OWNED BY; Schema: integration; Owner: -
--

ALTER SEQUENCE integration.emit_error_id_seq OWNED BY integration.emit_error.id;

--
-- Name: inbox; Type: TABLE; Schema: integration; Owner: -
--

CREATE TABLE integration.inbox (
    event_id text NOT NULL,
    type text NOT NULL,
    source text NOT NULL,
    aggregate_id text NOT NULL,
    sequence bigint NOT NULL,
    outcome text NOT NULL,
    received_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT inbox_outcome_check CHECK ((outcome = ANY (ARRAY['apply'::text, 'stale'::text]))),
    CONSTRAINT inbox_source_check CHECK ((source = ANY (ARRAY['borrower'::text, 'institution'::text])))
);

--
-- Name: inbox_watermark; Type: TABLE; Schema: integration; Owner: -
--

CREATE TABLE integration.inbox_watermark (
    aggregate_id text NOT NULL,
    last_sequence bigint NOT NULL
);

--
-- Name: outbox; Type: TABLE; Schema: integration; Owner: -
--

CREATE TABLE integration.outbox (
    id text NOT NULL,
    type text NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    aggregate_id text NOT NULL,
    sequence bigint NOT NULL,
    envelope jsonb NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    attempts integer DEFAULT 0 NOT NULL,
    next_attempt_at timestamp with time zone DEFAULT now() NOT NULL,
    lease_until timestamp with time zone,
    last_error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    delivered_at timestamp with time zone,
    CONSTRAINT outbox_id_check CHECK ((id ~ '^evt_[0-9A-Za-z]{10,40}$'::text)),
    CONSTRAINT outbox_sequence_check CHECK ((sequence >= 1)),
    CONSTRAINT outbox_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'sending'::text, 'delivered'::text, 'dead'::text])))
);

--
-- Name: request_shadow; Type: TABLE; Schema: integration; Owner: -
--

CREATE TABLE integration.request_shadow (
    request_id uuid NOT NULL,
    anon_borrower_id uuid NOT NULL,
    product_type text NOT NULL,
    amount numeric NOT NULL,
    currency character(3) NOT NULL,
    term_months integer,
    max_rate numeric,
    decision_deadline timestamp with time zone,
    allocation_mode text,
    allocations jsonb DEFAULT '[]'::jsonb NOT NULL,
    phase1 jsonb NOT NULL,
    status text DEFAULT 'open'::text NOT NULL,
    created_at timestamp with time zone NOT NULL,
    last_event_id text NOT NULL,
    last_sequence bigint NOT NULL,
    received_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: request; Type: TABLE; Schema: marketplace; Owner: -
--

CREATE TABLE marketplace.request (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    consumer_id uuid NOT NULL,
    consumer_ref text,
    product_id uuid NOT NULL,
    country character(2) DEFAULT 'MU'::bpchar NOT NULL,
    currency character(3) DEFAULT 'MUR'::bpchar NOT NULL,
    amount numeric(20,6) NOT NULL,
    term_months integer NOT NULL,
    params jsonb DEFAULT '{}'::jsonb NOT NULL,
    status text DEFAULT 'open'::text NOT NULL,
    bid_window_opens_at timestamp with time zone DEFAULT now() NOT NULL,
    bid_window_closes_at timestamp with time zone DEFAULT (now() + '04:00:00'::interval) NOT NULL,
    auto_close_at timestamp with time zone,
    winning_bid_id uuid,
    accepted_at timestamp with time zone,
    cancelled_at timestamp with time zone,
    cancellation_reason text,
    idempotency_key text NOT NULL,
    source text DEFAULT 'app'::text NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    relist_count smallint DEFAULT 0 NOT NULL,
    relisted_from uuid,
    notified_at timestamp with time zone,
    ficium_risk_tier text,
    ficium_score numeric(6,2),
    allocation_mode text,
    CONSTRAINT request_allocation_mode_check CHECK ((allocation_mode = ANY (ARRAY['client_specified'::text, 'institution_decides'::text]))),
    CONSTRAINT request_amount_check CHECK ((amount > (0)::numeric)),
    CONSTRAINT request_check CHECK ((bid_window_closes_at > bid_window_opens_at)),
    CONSTRAINT request_ficium_risk_tier_check CHECK ((ficium_risk_tier = ANY (ARRAY['A'::text, 'B'::text, 'C'::text, 'D'::text]))),
    CONSTRAINT request_source_check CHECK ((source = ANY (ARRAY['app'::text, 'api'::text, 'referral'::text]))),
    CONSTRAINT request_status_check CHECK ((status = ANY (ARRAY['open'::text, 'bidding'::text, 'closed'::text, 'accepted'::text, 'cancelled'::text, 'expired'::text, 'withdrawn'::text]))),
    CONSTRAINT request_term_months_check CHECK ((term_months > 0))
);

ALTER TABLE ONLY marketplace.request FORCE ROW LEVEL SECURITY;

--
-- Name: request_allocation; Type: TABLE; Schema: marketplace; Owner: -
--

CREATE TABLE marketplace.request_allocation (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    request_id uuid NOT NULL,
    product_id uuid NOT NULL,
    amount numeric,
    sort_order integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY marketplace.request_allocation FORCE ROW LEVEL SECURITY;

--
-- Name: v_request_parity; Type: VIEW; Schema: integration; Owner: -
--

CREATE VIEW integration.v_request_parity AS
 WITH x AS (
         SELECT s.request_id,
            r.id AS live_id,
            s.last_sequence,
            (s.amount = r.amount) AS amount_ok,
            (COALESCE(s.term_months, 12) = r.term_months) AS term_ok,
            (
                CASE s.status
                    WHEN 'open'::text THEN 'bidding'::text
                    WHEN 'accepted'::text THEN 'accepted'::text
                    WHEN 'cancelled'::text THEN 'cancelled'::text
                    WHEN 'expired'::text THEN 'expired'::text
                    WHEN 'closed'::text THEN 'closed'::text
                    ELSE 'bidding'::text
                END = r.status) AS status_ok,
            (COALESCE(s.decision_deadline, (s.created_at + '72:00:00'::interval)) = r.bid_window_closes_at) AS window_ok,
            (jsonb_strip_nulls(jsonb_build_object('app_product_type', s.product_type, 'max_rate', s.max_rate, 'loan_purpose', (s.phase1 -> 'loan_purpose'::text), 'collateral_type', (s.phase1 -> 'collateral_type'::text), 'collateral_sub', (s.phase1 -> 'collateral_sub'::text), 'ltv_pct', (s.phase1 -> 'ltv_pct'::text))) = r.params) AS params_ok,
            (jsonb_strip_nulls(jsonb_build_object('ficium_attested', true, 'kyc_verified', (s.phase1 -> 'kyc_verified'::text), 'employment_status', (s.phase1 -> 'employment_status'::text), 'employment_type', (s.phase1 -> 'employment_type'::text), 'years_employed', (s.phase1 -> 'years_employed'::text), 'gross_monthly_income', (s.phase1 -> 'gross_monthly_income'::text), 'income_verified', (s.phase1 -> 'income_verified'::text), 'dsr_current_pct', (s.phase1 -> 'dsr_current_pct'::text), 'dsr_post_pct', (s.phase1 -> 'dsr_post_pct'::text), 'net_worth_band', (s.phase1 -> 'net_worth_band'::text), 'has_existing_loans', (s.phase1 -> 'has_existing_loans'::text), 'existing_monthly_repayment', (s.phase1 -> 'existing_monthly_repayment'::text), 'existing_loan_balance', (s.phase1 -> 'existing_loan_balance'::text), 'loan_breakdown', (s.phase1 -> 'loan_breakdown'::text), 'health_score', (s.phase1 -> 'health_score'::text), 'risk_score', (s.phase1 -> 'risk_score'::text), 'affordability_score', (s.phase1 -> 'affordability_score'::text), 'risk_tier', (s.phase1 -> 'risk_tier'::text), 'age', (s.phase1 -> 'age'::text), 'risk_appetite', (s.phase1 -> 'risk_appetite'::text), 'investment_horizon', (s.phase1 -> 'investment_horizon'::text), 'liquidity_pref', (s.phase1 -> 'liquidity_pref'::text), 'investment_style', (s.phase1 -> 'investment_style'::text), 'target_amount', (s.phase1 -> 'target_amount'::text), 'monthly_contribution', (s.phase1 -> 'monthly_contribution'::text), 'investment_objective', (s.phase1 -> 'investment_objective'::text), 'investment_product_answers', (s.phase1 -> 'investment_product_answers'::text))) = r.metadata) AS metadata_ok,
            (( SELECT count(*) AS count
                   FROM marketplace.request_allocation a
                  WHERE (a.request_id = s.request_id)) = jsonb_array_length(s.allocations)) AS allocations_ok
           FROM (integration.request_shadow s
             LEFT JOIN marketplace.request r ON ((r.id = s.request_id)))
        )
 SELECT request_id,
    (live_id IS NOT NULL) AS in_live,
    last_sequence,
    array_remove(ARRAY[
        CASE
            WHEN (NOT amount_ok) THEN 'amount'::text
            ELSE NULL::text
        END,
        CASE
            WHEN (NOT term_ok) THEN 'term_months'::text
            ELSE NULL::text
        END,
        CASE
            WHEN (NOT status_ok) THEN 'status'::text
            ELSE NULL::text
        END,
        CASE
            WHEN (NOT window_ok) THEN 'bid_window'::text
            ELSE NULL::text
        END,
        CASE
            WHEN (NOT params_ok) THEN 'params'::text
            ELSE NULL::text
        END,
        CASE
            WHEN (NOT metadata_ok) THEN 'metadata'::text
            ELSE NULL::text
        END,
        CASE
            WHEN (NOT allocations_ok) THEN 'allocations'::text
            ELSE NULL::text
        END], NULL::text) AS mismatches,
    ((live_id IS NOT NULL) AND COALESCE((amount_ok AND term_ok AND status_ok AND window_ok AND params_ok AND metadata_ok AND allocations_ok), false)) AS matches
   FROM x;

--
-- Name: v_request_parity_summary; Type: VIEW; Schema: integration; Owner: -
--

CREATE VIEW integration.v_request_parity_summary AS
 SELECT ( SELECT count(*) AS count
           FROM integration.request_shadow) AS shadow_rows,
    ( SELECT count(*) AS count
           FROM marketplace.request) AS live_rows,
    ( SELECT count(*) AS count
           FROM integration.v_request_parity
          WHERE v_request_parity.matches) AS matching,
    ( SELECT count(*) AS count
           FROM integration.v_request_parity
          WHERE (v_request_parity.in_live AND (NOT v_request_parity.matches))) AS mismatching,
    ( SELECT count(*) AS count
           FROM integration.v_request_parity
          WHERE (NOT v_request_parity.in_live)) AS shadow_not_in_live,
    ( SELECT count(*) AS count
           FROM marketplace.request r
          WHERE (NOT (EXISTS ( SELECT 1
                   FROM integration.request_shadow s
                  WHERE (s.request_id = r.id))))) AS live_not_in_shadow;

--
-- Name: acceptance; Type: TABLE; Schema: marketplace; Owner: -
--

CREATE TABLE marketplace.acceptance (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    request_id uuid NOT NULL,
    bid_id uuid NOT NULL,
    accepted_by_consumer uuid NOT NULL,
    acceptance_method text DEFAULT 'app'::text NOT NULL,
    terms_version text,
    ip inet,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    accepted_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT acceptance_acceptance_method_check CHECK ((acceptance_method = ANY (ARRAY['app'::text, 'api'::text, 'agent'::text])))
);

ALTER TABLE ONLY marketplace.acceptance FORCE ROW LEVEL SECURITY;

--
-- Name: bid; Type: TABLE; Schema: marketplace; Owner: -
--

CREATE TABLE marketplace.bid (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    request_id uuid NOT NULL,
    institution_id uuid NOT NULL,
    submitted_by uuid,
    rate numeric(8,4) NOT NULL,
    rate_type text DEFAULT 'fixed'::text NOT NULL,
    rate_valid_days integer,
    amount_offered numeric(20,6) NOT NULL,
    term_months integer NOT NULL,
    conditions jsonb DEFAULT '{}'::jsonb NOT NULL,
    fee_structure jsonb DEFAULT '{}'::jsonb NOT NULL,
    status text DEFAULT 'submitted'::text NOT NULL,
    submitted_via text DEFAULT 'portal'::text NOT NULL,
    submitted_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone,
    withdrawn_at timestamp with time zone,
    withdraw_reason text,
    rejected_at timestamp with time zone,
    rejection_reason text,
    response_time_ms integer,
    idempotency_key text,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT bid_amount_offered_check CHECK ((amount_offered > (0)::numeric)),
    CONSTRAINT bid_rate_check CHECK ((rate >= (0)::numeric)),
    CONSTRAINT bid_rate_type_check CHECK ((rate_type = ANY (ARRAY['fixed'::text, 'variable'::text]))),
    CONSTRAINT bid_status_check CHECK ((status = ANY (ARRAY['draft'::text, 'submitted'::text, 'under_review'::text, 'accepted'::text, 'rejected'::text, 'expired'::text, 'withdrawn'::text]))),
    CONSTRAINT bid_submitted_via_check CHECK ((submitted_via = ANY (ARRAY['portal'::text, 'api'::text, 'webhook'::text, 'core_banking'::text]))),
    CONSTRAINT bid_term_months_check CHECK ((term_months > 0)),
    CONSTRAINT bid_withdraw_reason_len CHECK (((withdraw_reason IS NULL) OR (char_length(withdraw_reason) <= 500)))
);

ALTER TABLE ONLY marketplace.bid FORCE ROW LEVEL SECURITY;

--
-- Name: bid_acceptance; Type: TABLE; Schema: marketplace; Owner: -
--

CREATE TABLE marketplace.bid_acceptance (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    request_id uuid NOT NULL,
    bid_id uuid NOT NULL,
    institution_id uuid NOT NULL,
    full_name text NOT NULL,
    email text NOT NULL,
    phone text,
    address text,
    date_of_birth date,
    document_number text,
    revealed_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: bid_allocation; Type: TABLE; Schema: marketplace; Owner: -
--

CREATE TABLE marketplace.bid_allocation (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    bid_id uuid NOT NULL,
    product_id uuid NOT NULL,
    amount_offered numeric NOT NULL,
    rate numeric,
    term_months integer,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY marketplace.bid_allocation FORCE ROW LEVEL SECURITY;

--
-- Name: bid_benefit; Type: TABLE; Schema: marketplace; Owner: -
--

CREATE TABLE marketplace.bid_benefit (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    bid_id uuid NOT NULL,
    benefit_id uuid NOT NULL,
    title text NOT NULL,
    description text,
    value_display text,
    is_guaranteed boolean NOT NULL,
    cat_code text NOT NULL,
    value_amount numeric(14,4),
    value_unit text
);

--
-- Name: bid_event; Type: TABLE; Schema: marketplace; Owner: -
--

CREATE TABLE marketplace.bid_event (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    bid_id uuid NOT NULL,
    from_status text,
    to_status text NOT NULL,
    actor_id uuid,
    actor_type text DEFAULT 'system'::text NOT NULL,
    reason text,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    occurred_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT bid_event_actor_type_check CHECK ((actor_type = ANY (ARRAY['member'::text, 'consumer'::text, 'system'::text, 'admin'::text])))
);

ALTER TABLE ONLY marketplace.bid_event FORCE ROW LEVEL SECURITY;

--
-- Name: credit_decision; Type: TABLE; Schema: marketplace; Owner: -
--

CREATE TABLE marketplace.credit_decision (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    pipeline_id uuid NOT NULL,
    institution_id uuid NOT NULL,
    stage_instance_id uuid NOT NULL,
    source text NOT NULL,
    outcome text NOT NULL,
    conditions jsonb DEFAULT '[]'::jsonb NOT NULL,
    reason text,
    external_reference text,
    decided_at timestamp with time zone DEFAULT now() NOT NULL,
    recorded_by uuid,
    api_key_id uuid,
    approval_instance_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT credit_decision_check CHECK (((outcome <> 'approved_with_conditions'::text) OR (jsonb_array_length(conditions) > 0))),
    CONSTRAINT credit_decision_check1 CHECK (((outcome <> ALL (ARRAY['declined'::text, 'referred_back'::text])) OR (reason IS NOT NULL))),
    CONSTRAINT credit_decision_check2 CHECK (((source = 'engine'::text) OR (external_reference IS NOT NULL))),
    CONSTRAINT credit_decision_conditions_check CHECK ((jsonb_typeof(conditions) = 'array'::text)),
    CONSTRAINT credit_decision_outcome_check CHECK ((outcome = ANY (ARRAY['approved'::text, 'approved_with_conditions'::text, 'referred_back'::text, 'declined'::text]))),
    CONSTRAINT credit_decision_source_check CHECK ((source = ANY (ARRAY['engine'::text, 'portal'::text, 'api'::text])))
);

--
-- Name: deal_document; Type: TABLE; Schema: marketplace; Owner: -
--

CREATE TABLE marketplace.deal_document (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    pipeline_id uuid NOT NULL,
    requirement_id uuid NOT NULL,
    institution_id uuid NOT NULL,
    status text DEFAULT 'required'::text NOT NULL,
    storage_path text,
    file_name text,
    mime_type text,
    file_size bigint,
    uploaded_by uuid,
    uploaded_at timestamp with time zone,
    reviewed_by uuid,
    reviewed_at timestamp with time zone,
    review_note text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    uploaded_via text DEFAULT 'bank'::text NOT NULL,
    source text,
    document_date date,
    CONSTRAINT ck_deal_document_via CHECK (((uploaded_via = ANY (ARRAY['bank'::text, 'borrower'::text])) AND ((source IS NULL) OR (source = ANY (ARRAY['vault'::text, 'new'::text]))))),
    CONSTRAINT deal_document_status_check CHECK ((status = ANY (ARRAY['required'::text, 'received'::text, 'verified'::text, 'rejected'::text, 'waived'::text])))
);

ALTER TABLE ONLY marketplace.deal_document FORCE ROW LEVEL SECURITY;

--
-- Name: deal_document_event; Type: TABLE; Schema: marketplace; Owner: -
--

CREATE TABLE marketplace.deal_document_event (
    id bigint NOT NULL,
    document_id uuid NOT NULL,
    action text NOT NULL,
    actor_id uuid,
    note text,
    file_name text,
    storage_path text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY marketplace.deal_document_event FORCE ROW LEVEL SECURITY;

--
-- Name: deal_document_event_id_seq; Type: SEQUENCE; Schema: marketplace; Owner: -
--

CREATE SEQUENCE marketplace.deal_document_event_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: deal_document_event_id_seq; Type: SEQUENCE OWNED BY; Schema: marketplace; Owner: -
--

ALTER SEQUENCE marketplace.deal_document_event_id_seq OWNED BY marketplace.deal_document_event.id;

--
-- Name: loan_booking; Type: TABLE; Schema: marketplace; Owner: -
--

CREATE TABLE marketplace.loan_booking (
    pipeline_id uuid NOT NULL,
    institution_id uuid NOT NULL,
    core_reference text NOT NULL,
    booked_on date NOT NULL,
    disbursed_amount numeric(18,2) NOT NULL,
    disbursed_on date,
    notes text,
    source text NOT NULL,
    recorded_by uuid,
    api_key_id uuid,
    corrected_count integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    monthly_amount numeric(18,2),
    term_months integer,
    closed_on date,
    closure_reason text,
    closed_source text,
    closed_by uuid,
    closed_api_key_id uuid,
    CONSTRAINT loan_booking_closed_consistent CHECK ((((closed_on IS NULL) = (closure_reason IS NULL)) AND ((closed_on IS NULL) = (closed_source IS NULL)) AND ((closed_on IS NULL) OR (closed_on >= booked_on)))),
    CONSTRAINT loan_booking_closed_source_check CHECK (((closed_source IS NULL) OR (closed_source = ANY (ARRAY['portal'::text, 'api'::text])))),
    CONSTRAINT loan_booking_closure_reason_check CHECK (((closure_reason IS NULL) OR ((char_length(closure_reason) >= 5) AND (char_length(closure_reason) <= 500)))),
    CONSTRAINT loan_booking_core_reference_check CHECK (((char_length(btrim(core_reference)) >= 1) AND (char_length(btrim(core_reference)) <= 64))),
    CONSTRAINT loan_booking_disbursed_amount_check CHECK ((disbursed_amount > (0)::numeric)),
    CONSTRAINT loan_booking_monthly_amount_check CHECK (((monthly_amount IS NULL) OR (monthly_amount >= (0)::numeric))),
    CONSTRAINT loan_booking_notes_check CHECK (((notes IS NULL) OR (char_length(notes) <= 1000))),
    CONSTRAINT loan_booking_source_check CHECK ((source = ANY (ARRAY['portal'::text, 'api'::text]))),
    CONSTRAINT loan_booking_term_months_check CHECK (((term_months IS NULL) OR ((term_months >= 1) AND (term_months <= 600))))
);

ALTER TABLE ONLY marketplace.loan_booking FORCE ROW LEVEL SECURITY;

--
-- Name: loan_pipeline; Type: TABLE; Schema: marketplace; Owner: -
--

CREATE TABLE marketplace.loan_pipeline (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    request_id uuid NOT NULL,
    bid_id uuid NOT NULL,
    institution_id uuid NOT NULL,
    consumer_id uuid NOT NULL,
    template_id uuid NOT NULL,
    current_stage_id uuid,
    status marketplace.pipeline_status_enum DEFAULT 'active'::marketplace.pipeline_status_enum NOT NULL,
    deal_amount numeric(15,2) NOT NULL,
    deal_rate numeric(6,4) NOT NULL,
    deal_term_months integer NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    closed_reason text,
    closed_at timestamp with time zone,
    closed_by uuid
);

ALTER TABLE ONLY marketplace.loan_pipeline FORCE ROW LEVEL SECURITY;

--
-- Name: my_bids; Type: VIEW; Schema: marketplace; Owner: -
--

CREATE VIEW marketplace.my_bids WITH (security_invoker='on') AS
 SELECT b.id,
    b.request_id,
    b.institution_id,
    b.rate,
    b.rate_type,
    b.amount_offered,
    b.term_months,
    b.conditions,
    b.fee_structure,
    b.status,
    b.submitted_via,
    b.submitted_at,
    b.expires_at,
    b.created_at,
    r.product_id,
    r.amount AS requested_amount,
    r.currency,
    r.term_months AS requested_term_months,
    r.status AS request_status,
    r.bid_window_closes_at,
    r.consumer_ref,
    p.label AS product_label,
    pf.label AS product_family_label
   FROM (((marketplace.bid b
     JOIN marketplace.request r ON ((r.id = b.request_id)))
     JOIN catalog.product p ON ((p.id = r.product_id)))
     JOIN catalog.product_family pf ON ((pf.id = p.family_id)));

--
-- Name: pipeline_stage_instance; Type: TABLE; Schema: marketplace; Owner: -
--

CREATE TABLE marketplace.pipeline_stage_instance (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    pipeline_id uuid NOT NULL,
    stage_def_id uuid NOT NULL,
    "position" smallint NOT NULL,
    status marketplace.stage_status_enum DEFAULT 'pending'::marketplace.stage_status_enum NOT NULL,
    submitted_by uuid,
    submitted_at timestamp with time zone,
    approved_by uuid,
    approved_at timestamp with time zone,
    rejection_reason text,
    notes text,
    documents jsonb DEFAULT '[]'::jsonb,
    started_at timestamp with time zone,
    sla_due_at timestamp with time zone,
    completed_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    approval_instance_id uuid
);

ALTER TABLE ONLY marketplace.pipeline_stage_instance FORCE ROW LEVEL SECURITY;

--
-- Name: request_decline; Type: TABLE; Schema: marketplace; Owner: -
--

CREATE TABLE marketplace.request_decline (
    request_id uuid NOT NULL,
    institution_id uuid NOT NULL,
    reason text,
    declined_by uuid,
    declined_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT request_decline_reason_check CHECK (((reason IS NULL) OR (char_length(reason) <= 500)))
);

--
-- Name: sync_state; Type: TABLE; Schema: marketplace; Owner: -
--

CREATE TABLE marketplace.sync_state (
    id smallint DEFAULT 1 NOT NULL,
    last_updated_at timestamp with time zone DEFAULT '1970-01-01 00:00:00+00'::timestamp with time zone NOT NULL,
    last_run_at timestamp with time zone,
    last_run_pulled integer DEFAULT 0 NOT NULL,
    last_run_synced integer DEFAULT 0 NOT NULL,
    last_run_failed integer DEFAULT 0 NOT NULL,
    last_id uuid DEFAULT '00000000-0000-0000-0000-000000000000'::uuid NOT NULL,
    CONSTRAINT sync_state_id_check CHECK ((id = 1))
);

--
-- Name: COLUMN sync_state.last_updated_at; Type: COMMENT; Schema: marketplace; Owner: -
--

COMMENT ON COLUMN marketplace.sync_state.last_updated_at IS 'Max public.requests.updated_at successfully ingested. The sync pulls rows strictly greater than this, ordered ASC, so a batch limit defers remaining rows to the next run instead of dropping them permanently.';

--
-- Name: COLUMN sync_state.last_id; Type: COMMENT; Schema: marketplace; Owner: -
--

COMMENT ON COLUMN marketplace.sync_state.last_id IS 'Second half of the keyset cursor. Paired with last_updated_at so rows sharing an identical updated_at (bulk updates) are still ordered totally and none is skipped at a batch boundary.';

--
-- Name: schema_migration; Type: TABLE; Schema: ops; Owner: -
--

CREATE TABLE ops.schema_migration (
    filename text NOT NULL,
    sha256 text NOT NULL,
    status text DEFAULT 'applied'::text NOT NULL,
    applied_at timestamp with time zone DEFAULT now() NOT NULL,
    applied_by text DEFAULT CURRENT_USER NOT NULL,
    CONSTRAINT schema_migration_filename_check CHECK ((filename ~ '^[0-9]{3}_[a-z0-9_]+\.sql$'::text)),
    CONSTRAINT schema_migration_sha256_check CHECK ((sha256 ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT schema_migration_status_check CHECK ((status = ANY (ARRAY['applied'::text, 'baseline'::text, 'skipped'::text])))
);

--
-- Name: TABLE schema_migration; Type: COMMENT; Schema: ops; Owner: -
--

COMMENT ON TABLE ops.schema_migration IS 'One row per db/*.sql file. baseline = recorded as already live when the ledger was introduced.';

--
-- Name: admin_audit_log; Type: TABLE; Schema: portal_admin; Owner: -
--

CREATE TABLE portal_admin.admin_audit_log (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    session_id uuid,
    actor_id uuid,
    actor_email text,
    actor_role text,
    actor_ip inet,
    action_category text NOT NULL,
    event_label text NOT NULL,
    resource_type text,
    resource_id uuid,
    resource_label text,
    dual_control_id uuid,
    state_before jsonb,
    state_after jsonb,
    outcome portal_admin.audit_outcome DEFAULT 'logged'::portal_admin.audit_outcome NOT NULL,
    outcome_note text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: admin_dual_control_actions; Type: TABLE; Schema: portal_admin; Owner: -
--

CREATE TABLE portal_admin.admin_dual_control_actions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    action_category text NOT NULL,
    action_label text NOT NULL,
    risk portal_admin.action_risk DEFAULT 'medium'::portal_admin.action_risk NOT NULL,
    maker_id uuid NOT NULL,
    maker_email text NOT NULL,
    maker_role text NOT NULL,
    maker_ip inet NOT NULL,
    resource_type text NOT NULL,
    resource_id uuid,
    resource_label text,
    payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    payload_before jsonb,
    status portal_admin.dual_control_status DEFAULT 'pending'::portal_admin.dual_control_status NOT NULL,
    checker_id uuid,
    checker_email text,
    checker_role text,
    checker_note text,
    checker_ip inet,
    checked_at timestamp with time zone,
    initiated_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone DEFAULT (now() + '08:00:00'::interval) NOT NULL,
    executed_at timestamp with time zone,
    execution_error text,
    CONSTRAINT no_self_approval CHECK (((checker_id IS NULL) OR (checker_id <> maker_id)))
);

--
-- Name: admin_roles; Type: TABLE; Schema: portal_admin; Owner: -
--

CREATE TABLE portal_admin.admin_roles (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    slug text NOT NULL,
    label text NOT NULL,
    description text DEFAULT ''::text NOT NULL,
    permissions text[] DEFAULT '{}'::text[] NOT NULL,
    is_system boolean DEFAULT false NOT NULL,
    created_by uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: admin_sessions; Type: TABLE; Schema: portal_admin; Owner: -
--

CREATE TABLE portal_admin.admin_sessions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    admin_user_id uuid NOT NULL,
    ip_address inet NOT NULL,
    user_agent text DEFAULT ''::text NOT NULL,
    country text,
    city text,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    last_active_at timestamp with time zone DEFAULT now() NOT NULL,
    ended_at timestamp with time zone,
    end_reason text,
    is_active boolean DEFAULT true NOT NULL,
    CONSTRAINT valid_end_reason CHECK (((end_reason = ANY (ARRAY['logout'::text, 'timeout'::text, 'forced'::text, 'expired'::text])) OR (end_reason IS NULL)))
);

--
-- Name: admin_users; Type: TABLE; Schema: portal_admin; Owner: -
--

CREATE TABLE portal_admin.admin_users (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    auth_user_id uuid NOT NULL,
    email text NOT NULL,
    display_name text NOT NULL,
    role_slug text DEFAULT 'support'::text NOT NULL,
    custom_role_id uuid,
    status portal_admin.admin_user_status DEFAULT 'pending_mfa'::portal_admin.admin_user_status NOT NULL,
    mfa_enabled boolean DEFAULT false NOT NULL,
    mfa_verified_at timestamp with time zone,
    last_login_at timestamp with time zone,
    last_login_ip inet,
    failed_login_count integer DEFAULT 0 NOT NULL,
    locked_at timestamp with time zone,
    locked_reason text,
    suspended_at timestamp with time zone,
    suspended_by uuid,
    suspension_reason text,
    password_changed_at timestamp with time zone,
    force_password_reset boolean DEFAULT true NOT NULL,
    created_by uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    group_id uuid,
    CONSTRAINT custom_role_requires_id CHECK (((role_slug <> 'custom'::text) OR (custom_role_id IS NOT NULL))),
    CONSTRAINT valid_role_slug CHECK ((role_slug = ANY (ARRAY['super_admin'::text, 'institution_mgr'::text, 'compliance'::text, 'support'::text, 'auditor'::text, 'custom'::text])))
);

--
-- Name: commission_event; Type: TABLE; Schema: portal_admin; Owner: -
--

CREATE TABLE portal_admin.commission_event (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    pipeline_id uuid NOT NULL,
    request_id uuid NOT NULL,
    institution_id uuid NOT NULL,
    deal_amount numeric(15,2) NOT NULL,
    commission_rate numeric(6,4),
    commission_amt numeric(15,2),
    currency character(3) DEFAULT 'MUR'::bpchar NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    invoice_ref text,
    invoiced_at timestamp with time zone,
    paid_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    rule_id uuid,
    basis text,
    fee_base numeric(18,2),
    calc jsonb,
    clawback_until date,
    clawback_mode text DEFAULT 'none'::text NOT NULL,
    clawback_months smallint DEFAULT 0 NOT NULL,
    CONSTRAINT commission_event_clawback_mode_check CHECK ((clawback_mode = ANY (ARRAY['none'::text, 'full'::text, 'pro_rata'::text]))),
    CONSTRAINT commission_event_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'invoiced'::text, 'paid'::text, 'waived'::text])))
);

--
-- Name: user_groups; Type: TABLE; Schema: portal_admin; Owner: -
--

CREATE TABLE portal_admin.user_groups (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    slug text NOT NULL,
    label text NOT NULL,
    description text DEFAULT ''::text NOT NULL,
    user_type text DEFAULT 'institution'::text NOT NULL,
    module_permissions text[] DEFAULT '{}'::text[] NOT NULL,
    is_system boolean DEFAULT false NOT NULL,
    created_by uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT user_groups_user_type_check CHECK ((user_type = ANY (ARRAY['admin'::text, 'institution'::text])))
);

--
-- Name: _identity_migration_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public._identity_migration_log (
    id bigint NOT NULL,
    run_at timestamp with time zone DEFAULT now(),
    schema_name text NOT NULL,
    table_name text NOT NULL,
    column_name text NOT NULL,
    old_uuid uuid NOT NULL,
    new_uuid uuid NOT NULL,
    email text NOT NULL,
    dry_run boolean NOT NULL
);

--
-- Name: _identity_migration_log_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public._identity_migration_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: _identity_migration_log_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public._identity_migration_log_id_seq OWNED BY public._identity_migration_log.id;

--
-- Name: portal_notifications; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.portal_notifications (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    institution_id uuid NOT NULL,
    kind text NOT NULL,
    title text NOT NULL,
    body text,
    link text,
    metadata jsonb,
    read_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT portal_notifications_kind_check CHECK ((kind = ANY (ARRAY['bid_accepted'::text, 'bid_rejected'::text, 'bid_expired'::text, 'request_closed'::text, 'pipeline_advanced'::text, 'pipeline_approved'::text, 'approval_needed'::text, 'compliance_expiring'::text, 'system'::text, 'deal_booked'::text, 'deal_closed'::text, 'booking_closed'::text])))
);

--
-- Name: execution_default; Type: TABLE ATTACH; Schema: autobid; Owner: -
--

ALTER TABLE ONLY autobid.execution ATTACH PARTITION autobid.execution_default DEFAULT;

--
-- Name: usage_event_default; Type: TABLE ATTACH; Schema: entitlements; Owner: -
--

ALTER TABLE ONLY entitlements.usage_event ATTACH PARTITION entitlements.usage_event_default DEFAULT;

--
-- Name: approval_outbox id; Type: DEFAULT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.approval_outbox ALTER COLUMN id SET DEFAULT nextval('institution.approval_outbox_id_seq'::regclass);

--
-- Name: esign_event id; Type: DEFAULT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.esign_event ALTER COLUMN id SET DEFAULT nextval('institution.esign_event_id_seq'::regclass);

--
-- Name: emit_error id; Type: DEFAULT; Schema: integration; Owner: -
--

ALTER TABLE ONLY integration.emit_error ALTER COLUMN id SET DEFAULT nextval('integration.emit_error_id_seq'::regclass);

--
-- Name: deal_document_event id; Type: DEFAULT; Schema: marketplace; Owner: -
--

ALTER TABLE ONLY marketplace.deal_document_event ALTER COLUMN id SET DEFAULT nextval('marketplace.deal_document_event_id_seq'::regclass);

--
-- Name: _identity_migration_log id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public._identity_migration_log ALTER COLUMN id SET DEFAULT nextval('public._identity_migration_log_id_seq'::regclass);

--
-- Name: event event_pkey; Type: CONSTRAINT; Schema: audit; Owner: -
--

ALTER TABLE ONLY audit.event
    ADD CONSTRAINT event_pkey PRIMARY KEY (id);

--
-- Name: auth_audit_events auth_audit_events_pkey; Type: CONSTRAINT; Schema: auth_portal; Owner: -
--

ALTER TABLE ONLY auth_portal.auth_audit_events
    ADD CONSTRAINT auth_audit_events_pkey PRIMARY KEY (id);

--
-- Name: auth_sessions auth_sessions_pkey; Type: CONSTRAINT; Schema: auth_portal; Owner: -
--

ALTER TABLE ONLY auth_portal.auth_sessions
    ADD CONSTRAINT auth_sessions_pkey PRIMARY KEY (id);

--
-- Name: auth_sessions auth_sessions_refresh_token_hash_key; Type: CONSTRAINT; Schema: auth_portal; Owner: -
--

ALTER TABLE ONLY auth_portal.auth_sessions
    ADD CONSTRAINT auth_sessions_refresh_token_hash_key UNIQUE (refresh_token_hash);

--
-- Name: auth_users auth_users_pkey; Type: CONSTRAINT; Schema: auth_portal; Owner: -
--

ALTER TABLE ONLY auth_portal.auth_users
    ADD CONSTRAINT auth_users_pkey PRIMARY KEY (id);

--
-- Name: email_verification_tokens email_verification_tokens_pkey; Type: CONSTRAINT; Schema: auth_portal; Owner: -
--

ALTER TABLE ONLY auth_portal.email_verification_tokens
    ADD CONSTRAINT email_verification_tokens_pkey PRIMARY KEY (id);

--
-- Name: email_verification_tokens email_verification_tokens_token_hash_key; Type: CONSTRAINT; Schema: auth_portal; Owner: -
--

ALTER TABLE ONLY auth_portal.email_verification_tokens
    ADD CONSTRAINT email_verification_tokens_token_hash_key UNIQUE (token_hash);

--
-- Name: ip_allowlist ip_allowlist_pkey; Type: CONSTRAINT; Schema: auth_portal; Owner: -
--

ALTER TABLE ONLY auth_portal.ip_allowlist
    ADD CONSTRAINT ip_allowlist_pkey PRIMARY KEY (id);

--
-- Name: mfa_backup_codes mfa_backup_codes_pkey; Type: CONSTRAINT; Schema: auth_portal; Owner: -
--

ALTER TABLE ONLY auth_portal.mfa_backup_codes
    ADD CONSTRAINT mfa_backup_codes_pkey PRIMARY KEY (id);

--
-- Name: password_reset_tokens password_reset_tokens_pkey; Type: CONSTRAINT; Schema: auth_portal; Owner: -
--

ALTER TABLE ONLY auth_portal.password_reset_tokens
    ADD CONSTRAINT password_reset_tokens_pkey PRIMARY KEY (id);

--
-- Name: password_reset_tokens password_reset_tokens_token_hash_key; Type: CONSTRAINT; Schema: auth_portal; Owner: -
--

ALTER TABLE ONLY auth_portal.password_reset_tokens
    ADD CONSTRAINT password_reset_tokens_token_hash_key UNIQUE (token_hash);

--
-- Name: auth_users uq_auth_users_email; Type: CONSTRAINT; Schema: auth_portal; Owner: -
--

ALTER TABLE ONLY auth_portal.auth_users
    ADD CONSTRAINT uq_auth_users_email UNIQUE (email);

--
-- Name: execution execution_idempotency_key_occurred_at_key; Type: CONSTRAINT; Schema: autobid; Owner: -
--

ALTER TABLE ONLY autobid.execution
    ADD CONSTRAINT execution_idempotency_key_occurred_at_key UNIQUE (idempotency_key, occurred_at);

--
-- Name: execution_default execution_default_idempotency_key_occurred_at_key; Type: CONSTRAINT; Schema: autobid; Owner: -
--

ALTER TABLE ONLY autobid.execution_default
    ADD CONSTRAINT execution_default_idempotency_key_occurred_at_key UNIQUE (idempotency_key, occurred_at);

--
-- Name: execution execution_pkey; Type: CONSTRAINT; Schema: autobid; Owner: -
--

ALTER TABLE ONLY autobid.execution
    ADD CONSTRAINT execution_pkey PRIMARY KEY (id, occurred_at);

--
-- Name: execution_default execution_default_pkey; Type: CONSTRAINT; Schema: autobid; Owner: -
--

ALTER TABLE ONLY autobid.execution_default
    ADD CONSTRAINT execution_default_pkey PRIMARY KEY (id, occurred_at);

--
-- Name: exposure exposure_pkey; Type: CONSTRAINT; Schema: autobid; Owner: -
--

ALTER TABLE ONLY autobid.exposure
    ADD CONSTRAINT exposure_pkey PRIMARY KEY (institution_id);

--
-- Name: rule rule_institution_id_name_key; Type: CONSTRAINT; Schema: autobid; Owner: -
--

ALTER TABLE ONLY autobid.rule
    ADD CONSTRAINT rule_institution_id_name_key UNIQUE (institution_id, name);

--
-- Name: rule rule_pkey; Type: CONSTRAINT; Schema: autobid; Owner: -
--

ALTER TABLE ONLY autobid.rule
    ADD CONSTRAINT rule_pkey PRIMARY KEY (id);

--
-- Name: rule_version rule_version_pkey; Type: CONSTRAINT; Schema: autobid; Owner: -
--

ALTER TABLE ONLY autobid.rule_version
    ADD CONSTRAINT rule_version_pkey PRIMARY KEY (id);

--
-- Name: rule_version rule_version_rule_id_version_no_key; Type: CONSTRAINT; Schema: autobid; Owner: -
--

ALTER TABLE ONLY autobid.rule_version
    ADD CONSTRAINT rule_version_rule_id_version_no_key UNIQUE (rule_id, version_no);

--
-- Name: commission_adjustment commission_adjustment_pkey; Type: CONSTRAINT; Schema: billing; Owner: -
--

ALTER TABLE ONLY billing.commission_adjustment
    ADD CONSTRAINT commission_adjustment_pkey PRIMARY KEY (id);

--
-- Name: commission_rule commission_rule_command_id_key; Type: CONSTRAINT; Schema: billing; Owner: -
--

ALTER TABLE ONLY billing.commission_rule
    ADD CONSTRAINT commission_rule_command_id_key UNIQUE (command_id);

--
-- Name: commission_rule commission_rule_end_command_id_key; Type: CONSTRAINT; Schema: billing; Owner: -
--

ALTER TABLE ONLY billing.commission_rule
    ADD CONSTRAINT commission_rule_end_command_id_key UNIQUE (end_command_id);

--
-- Name: commission_rule commission_rule_pkey; Type: CONSTRAINT; Schema: billing; Owner: -
--

ALTER TABLE ONLY billing.commission_rule
    ADD CONSTRAINT commission_rule_pkey PRIMARY KEY (id);

--
-- Name: country country_pkey; Type: CONSTRAINT; Schema: catalog; Owner: -
--

ALTER TABLE ONLY catalog.country
    ADD CONSTRAINT country_pkey PRIMARY KEY (code);

--
-- Name: currency currency_pkey; Type: CONSTRAINT; Schema: catalog; Owner: -
--

ALTER TABLE ONLY catalog.currency
    ADD CONSTRAINT currency_pkey PRIMARY KEY (code);

--
-- Name: module module_pkey; Type: CONSTRAINT; Schema: catalog; Owner: -
--

ALTER TABLE ONLY catalog.module
    ADD CONSTRAINT module_pkey PRIMARY KEY (key);

--
-- Name: product product_code_key; Type: CONSTRAINT; Schema: catalog; Owner: -
--

ALTER TABLE ONLY catalog.product
    ADD CONSTRAINT product_code_key UNIQUE (code);

--
-- Name: product_document product_document_pkey; Type: CONSTRAINT; Schema: catalog; Owner: -
--

ALTER TABLE ONLY catalog.product_document
    ADD CONSTRAINT product_document_pkey PRIMARY KEY (id);

--
-- Name: product_document product_document_product_id_doc_key_key; Type: CONSTRAINT; Schema: catalog; Owner: -
--

ALTER TABLE ONLY catalog.product_document
    ADD CONSTRAINT product_document_product_id_doc_key_key UNIQUE (product_id, doc_key);

--
-- Name: product_eligibility product_eligibility_pkey; Type: CONSTRAINT; Schema: catalog; Owner: -
--

ALTER TABLE ONLY catalog.product_eligibility
    ADD CONSTRAINT product_eligibility_pkey PRIMARY KEY (id);

--
-- Name: product_family product_family_code_key; Type: CONSTRAINT; Schema: catalog; Owner: -
--

ALTER TABLE ONLY catalog.product_family
    ADD CONSTRAINT product_family_code_key UNIQUE (code);

--
-- Name: product_family product_family_pkey; Type: CONSTRAINT; Schema: catalog; Owner: -
--

ALTER TABLE ONLY catalog.product_family
    ADD CONSTRAINT product_family_pkey PRIMARY KEY (id);

--
-- Name: product_parameter product_parameter_pkey; Type: CONSTRAINT; Schema: catalog; Owner: -
--

ALTER TABLE ONLY catalog.product_parameter
    ADD CONSTRAINT product_parameter_pkey PRIMARY KEY (id);

--
-- Name: product_parameter product_parameter_product_id_key_key; Type: CONSTRAINT; Schema: catalog; Owner: -
--

ALTER TABLE ONLY catalog.product_parameter
    ADD CONSTRAINT product_parameter_product_id_key_key UNIQUE (product_id, key);

--
-- Name: product product_pkey; Type: CONSTRAINT; Schema: catalog; Owner: -
--

ALTER TABLE ONLY catalog.product
    ADD CONSTRAINT product_pkey PRIMARY KEY (id);

--
-- Name: product_rate_model product_rate_model_pkey; Type: CONSTRAINT; Schema: catalog; Owner: -
--

ALTER TABLE ONLY catalog.product_rate_model
    ADD CONSTRAINT product_rate_model_pkey PRIMARY KEY (id);

--
-- Name: product_rate_model product_rate_model_product_id_key; Type: CONSTRAINT; Schema: catalog; Owner: -
--

ALTER TABLE ONLY catalog.product_rate_model
    ADD CONSTRAINT product_rate_model_product_id_key UNIQUE (product_id);

--
-- Name: product_sla product_sla_pkey; Type: CONSTRAINT; Schema: catalog; Owner: -
--

ALTER TABLE ONLY catalog.product_sla
    ADD CONSTRAINT product_sla_pkey PRIMARY KEY (id);

--
-- Name: product_sla product_sla_product_id_key; Type: CONSTRAINT; Schema: catalog; Owner: -
--

ALTER TABLE ONLY catalog.product_sla
    ADD CONSTRAINT product_sla_product_id_key UNIQUE (product_id);

--
-- Name: regulator regulator_pkey; Type: CONSTRAINT; Schema: catalog; Owner: -
--

ALTER TABLE ONLY catalog.regulator
    ADD CONSTRAINT regulator_pkey PRIMARY KEY (code);

--
-- Name: institution_entitlement institution_entitlement_institution_id_module_code_key; Type: CONSTRAINT; Schema: entitlements; Owner: -
--

ALTER TABLE ONLY entitlements.institution_entitlement
    ADD CONSTRAINT institution_entitlement_institution_id_module_code_key UNIQUE (institution_id, module_code);

--
-- Name: institution_entitlement institution_entitlement_pkey; Type: CONSTRAINT; Schema: entitlements; Owner: -
--

ALTER TABLE ONLY entitlements.institution_entitlement
    ADD CONSTRAINT institution_entitlement_pkey PRIMARY KEY (id);

--
-- Name: module module_pkey; Type: CONSTRAINT; Schema: entitlements; Owner: -
--

ALTER TABLE ONLY entitlements.module
    ADD CONSTRAINT module_pkey PRIMARY KEY (module_code);

--
-- Name: usage_event usage_event_idempotency_key_occurred_at_key; Type: CONSTRAINT; Schema: entitlements; Owner: -
--

ALTER TABLE ONLY entitlements.usage_event
    ADD CONSTRAINT usage_event_idempotency_key_occurred_at_key UNIQUE (idempotency_key, occurred_at);

--
-- Name: usage_event_default usage_event_default_idempotency_key_occurred_at_key; Type: CONSTRAINT; Schema: entitlements; Owner: -
--

ALTER TABLE ONLY entitlements.usage_event_default
    ADD CONSTRAINT usage_event_default_idempotency_key_occurred_at_key UNIQUE (idempotency_key, occurred_at);

--
-- Name: usage_event usage_event_pkey; Type: CONSTRAINT; Schema: entitlements; Owner: -
--

ALTER TABLE ONLY entitlements.usage_event
    ADD CONSTRAINT usage_event_pkey PRIMARY KEY (id, occurred_at);

--
-- Name: usage_event_default usage_event_default_pkey; Type: CONSTRAINT; Schema: entitlements; Owner: -
--

ALTER TABLE ONLY entitlements.usage_event_default
    ADD CONSTRAINT usage_event_default_pkey PRIMARY KEY (id, occurred_at);

--
-- Name: action action_pkey; Type: CONSTRAINT; Schema: governance; Owner: -
--

ALTER TABLE ONLY governance.action
    ADD CONSTRAINT action_pkey PRIMARY KEY (id);

--
-- Name: four_eyes_catalog four_eyes_catalog_pkey; Type: CONSTRAINT; Schema: governance; Owner: -
--

ALTER TABLE ONLY governance.four_eyes_catalog
    ADD CONSTRAINT four_eyes_catalog_pkey PRIMARY KEY (category);

--
-- Name: four_eyes_policy four_eyes_policy_pkey; Type: CONSTRAINT; Schema: governance; Owner: -
--

ALTER TABLE ONLY governance.four_eyes_policy
    ADD CONSTRAINT four_eyes_policy_pkey PRIMARY KEY (institution_id, category);

--
-- Name: access_request access_request_pkey; Type: CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.access_request
    ADD CONSTRAINT access_request_pkey PRIMARY KEY (id);

--
-- Name: access_request access_request_reference_key; Type: CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.access_request
    ADD CONSTRAINT access_request_reference_key UNIQUE (reference);

--
-- Name: approval_action approval_action_pkey; Type: CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.approval_action
    ADD CONSTRAINT approval_action_pkey PRIMARY KEY (id);

--
-- Name: approval_committee approval_committee_institution_id_name_key; Type: CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.approval_committee
    ADD CONSTRAINT approval_committee_institution_id_name_key UNIQUE (institution_id, name);

--
-- Name: approval_committee approval_committee_pkey; Type: CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.approval_committee
    ADD CONSTRAINT approval_committee_pkey PRIMARY KEY (id);

--
-- Name: approval_delegation approval_delegation_pkey; Type: CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.approval_delegation
    ADD CONSTRAINT approval_delegation_pkey PRIMARY KEY (id);

--
-- Name: approval_instance approval_instance_pkey; Type: CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.approval_instance
    ADD CONSTRAINT approval_instance_pkey PRIMARY KEY (id);

--
-- Name: approval_outbox approval_outbox_pkey; Type: CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.approval_outbox
    ADD CONSTRAINT approval_outbox_pkey PRIMARY KEY (id);

--
-- Name: approval_stage_def approval_stage_def_pkey; Type: CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.approval_stage_def
    ADD CONSTRAINT approval_stage_def_pkey PRIMARY KEY (id);

--
-- Name: approval_stage_def approval_stage_def_template_id_seq_key; Type: CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.approval_stage_def
    ADD CONSTRAINT approval_stage_def_template_id_seq_key UNIQUE (template_id, seq);

--
-- Name: approval_stage_instance approval_stage_instance_instance_id_seq_key; Type: CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.approval_stage_instance
    ADD CONSTRAINT approval_stage_instance_instance_id_seq_key UNIQUE (instance_id, seq);

--
-- Name: approval_stage_instance approval_stage_instance_pkey; Type: CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.approval_stage_instance
    ADD CONSTRAINT approval_stage_instance_pkey PRIMARY KEY (id);

--
-- Name: approval_template approval_template_institution_id_name_version_key; Type: CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.approval_template
    ADD CONSTRAINT approval_template_institution_id_name_version_key UNIQUE (institution_id, name, version);

--
-- Name: approval_template approval_template_pkey; Type: CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.approval_template
    ADD CONSTRAINT approval_template_pkey PRIMARY KEY (id);

--
-- Name: benefit_cat benefit_cat_code_key; Type: CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.benefit_cat
    ADD CONSTRAINT benefit_cat_code_key UNIQUE (code);

--
-- Name: benefit_cat benefit_cat_pkey; Type: CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.benefit_cat
    ADD CONSTRAINT benefit_cat_pkey PRIMARY KEY (id);

--
-- Name: benefit benefit_pkey; Type: CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.benefit
    ADD CONSTRAINT benefit_pkey PRIMARY KEY (id);

--
-- Name: benefit ck_benefit_title; Type: CHECK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE institution.benefit
    ADD CONSTRAINT ck_benefit_title CHECK (((char_length(btrim(title)) >= 2) AND (char_length(btrim(title)) <= 80))) NOT VALID;

--
-- Name: benefit ck_benefit_validity; Type: CHECK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE institution.benefit
    ADD CONSTRAINT ck_benefit_validity CHECK (((valid_until IS NULL) OR (valid_from IS NULL) OR (valid_until >= valid_from))) NOT VALID;

--
-- Name: committee_member committee_member_committee_id_member_id_valid_from_key; Type: CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.committee_member
    ADD CONSTRAINT committee_member_committee_id_member_id_valid_from_key UNIQUE (committee_id, member_id, valid_from);

--
-- Name: committee_member committee_member_pkey; Type: CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.committee_member
    ADD CONSTRAINT committee_member_pkey PRIMARY KEY (id);

--
-- Name: compliance_waiver compliance_waiver_pkey; Type: CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.compliance_waiver
    ADD CONSTRAINT compliance_waiver_pkey PRIMARY KEY (institution_id);

--
-- Name: doa_rule doa_rule_institution_id_entity_type_priority_key; Type: CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.doa_rule
    ADD CONSTRAINT doa_rule_institution_id_entity_type_priority_key UNIQUE (institution_id, entity_type, priority);

--
-- Name: doa_rule doa_rule_pkey; Type: CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.doa_rule
    ADD CONSTRAINT doa_rule_pkey PRIMARY KEY (id);

--
-- Name: doc_generation doc_generation_pkey; Type: CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.doc_generation
    ADD CONSTRAINT doc_generation_pkey PRIMARY KEY (id);

--
-- Name: doc doc_institution_id_doc_type_id_key; Type: CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.doc
    ADD CONSTRAINT doc_institution_id_doc_type_id_key UNIQUE (institution_id, doc_type_id);

--
-- Name: doc doc_pkey; Type: CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.doc
    ADD CONSTRAINT doc_pkey PRIMARY KEY (id);

--
-- Name: doc_requirement doc_requirement_pkey; Type: CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.doc_requirement
    ADD CONSTRAINT doc_requirement_pkey PRIMARY KEY (id);

--
-- Name: doc_template doc_template_institution_id_code_key; Type: CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.doc_template
    ADD CONSTRAINT doc_template_institution_id_code_key UNIQUE (institution_id, code);

--
-- Name: doc_template doc_template_pkey; Type: CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.doc_template
    ADD CONSTRAINT doc_template_pkey PRIMARY KEY (id);

--
-- Name: doc_template_version doc_template_version_pkey; Type: CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.doc_template_version
    ADD CONSTRAINT doc_template_version_pkey PRIMARY KEY (id);

--
-- Name: doc_template_version doc_template_version_template_id_version_no_key; Type: CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.doc_template_version
    ADD CONSTRAINT doc_template_version_template_id_version_no_key UNIQUE (template_id, version_no);

--
-- Name: doc_type doc_type_code_key; Type: CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.doc_type
    ADD CONSTRAINT doc_type_code_key UNIQUE (code);

--
-- Name: doc_type doc_type_pkey; Type: CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.doc_type
    ADD CONSTRAINT doc_type_pkey PRIMARY KEY (id);

--
-- Name: esign_envelope esign_envelope_pkey; Type: CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.esign_envelope
    ADD CONSTRAINT esign_envelope_pkey PRIMARY KEY (id);

--
-- Name: esign_event esign_event_pkey; Type: CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.esign_event
    ADD CONSTRAINT esign_event_pkey PRIMARY KEY (id);

--
-- Name: esign_signer esign_signer_envelope_id_sign_order_key; Type: CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.esign_signer
    ADD CONSTRAINT esign_signer_envelope_id_sign_order_key UNIQUE (envelope_id, sign_order);

--
-- Name: esign_signer esign_signer_pkey; Type: CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.esign_signer
    ADD CONSTRAINT esign_signer_pkey PRIMARY KEY (id);

--
-- Name: esign_signer esign_signer_token_sha256_key; Type: CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.esign_signer
    ADD CONSTRAINT esign_signer_token_sha256_key UNIQUE (token_sha256);

--
-- Name: group groups_pkey; Type: CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution."group"
    ADD CONSTRAINT groups_pkey PRIMARY KEY (id);

--
-- Name: group groups_slug_unique_per_institution; Type: CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution."group"
    ADD CONSTRAINT groups_slug_unique_per_institution UNIQUE (institution_id, slug);

--
-- Name: api_key institution_api_keys_key_hmac_key; Type: CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.api_key
    ADD CONSTRAINT institution_api_keys_key_hmac_key UNIQUE (key_hmac);

--
-- Name: api_key institution_api_keys_pkey; Type: CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.api_key
    ADD CONSTRAINT institution_api_keys_pkey PRIMARY KEY (id);

--
-- Name: member institution_members_pkey; Type: CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.member
    ADD CONSTRAINT institution_members_pkey PRIMARY KEY (id);

--
-- Name: product_config institution_product_config_institution_id_product_id_key; Type: CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.product_config
    ADD CONSTRAINT institution_product_config_institution_id_product_id_key UNIQUE (institution_id, product_id);

--
-- Name: product_config institution_product_config_pkey; Type: CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.product_config
    ADD CONSTRAINT institution_product_config_pkey PRIMARY KEY (id);

--
-- Name: institution_sla_config institution_sla_config_pkey; Type: CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.institution_sla_config
    ADD CONSTRAINT institution_sla_config_pkey PRIMARY KEY (id);

--
-- Name: webhook institution_webhooks_pkey; Type: CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.webhook
    ADD CONSTRAINT institution_webhooks_pkey PRIMARY KEY (id);

--
-- Name: institution institutions_pkey; Type: CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.institution
    ADD CONSTRAINT institutions_pkey PRIMARY KEY (id);

--
-- Name: kyb_document kyb_document_pkey; Type: CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.kyb_document
    ADD CONSTRAINT kyb_document_pkey PRIMARY KEY (id);

--
-- Name: onboarding_pref onboarding_pref_pkey; Type: CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.onboarding_pref
    ADD CONSTRAINT onboarding_pref_pkey PRIMARY KEY (institution_id);

--
-- Name: pending_actions pending_actions_pkey; Type: CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.pending_actions
    ADD CONSTRAINT pending_actions_pkey PRIMARY KEY (id);

--
-- Name: pipeline_stage_def pipeline_stage_def_pkey; Type: CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.pipeline_stage_def
    ADD CONSTRAINT pipeline_stage_def_pkey PRIMARY KEY (id);

--
-- Name: pipeline_template pipeline_template_pkey; Type: CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.pipeline_template
    ADD CONSTRAINT pipeline_template_pkey PRIMARY KEY (id);

--
-- Name: pipeline_template uq_pipeline_template_name; Type: CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.pipeline_template
    ADD CONSTRAINT uq_pipeline_template_name UNIQUE (institution_id, name);

--
-- Name: pipeline_stage_def uq_stage_def_position; Type: CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.pipeline_stage_def
    ADD CONSTRAINT uq_stage_def_position UNIQUE (template_id, "position");

--
-- Name: webhook_delivery webhook_delivery_pkey; Type: CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.webhook_delivery
    ADD CONSTRAINT webhook_delivery_pkey PRIMARY KEY (id);

--
-- Name: webhook_delivery webhook_delivery_webhook_id_event_id_key; Type: CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.webhook_delivery
    ADD CONSTRAINT webhook_delivery_webhook_id_event_id_key UNIQUE (webhook_id, event_id);

--
-- Name: acceptance_log acceptance_log_pkey; Type: CONSTRAINT; Schema: integration; Owner: -
--

ALTER TABLE ONLY integration.acceptance_log
    ADD CONSTRAINT acceptance_log_pkey PRIMARY KEY (idempotency_key);

--
-- Name: admin_command_log admin_command_log_pkey; Type: CONSTRAINT; Schema: integration; Owner: -
--

ALTER TABLE ONLY integration.admin_command_log
    ADD CONSTRAINT admin_command_log_pkey PRIMARY KEY (command_id);

--
-- Name: aggregate_sequence aggregate_sequence_pkey; Type: CONSTRAINT; Schema: integration; Owner: -
--

ALTER TABLE ONLY integration.aggregate_sequence
    ADD CONSTRAINT aggregate_sequence_pkey PRIMARY KEY (aggregate_id);

--
-- Name: api_channel_policy api_channel_policy_pkey; Type: CONSTRAINT; Schema: integration; Owner: -
--

ALTER TABLE ONLY integration.api_channel_policy
    ADD CONSTRAINT api_channel_policy_pkey PRIMARY KEY (channel);

--
-- Name: api_channel_status api_channel_status_pkey; Type: CONSTRAINT; Schema: integration; Owner: -
--

ALTER TABLE ONLY integration.api_channel_status
    ADD CONSTRAINT api_channel_status_pkey PRIMARY KEY (channel);

--
-- Name: api_traffic_hour api_traffic_hour_pkey; Type: CONSTRAINT; Schema: integration; Owner: -
--

ALTER TABLE ONLY integration.api_traffic_hour
    ADD CONSTRAINT api_traffic_hour_pkey PRIMARY KEY (channel, hour);

--
-- Name: document_upload document_upload_pkey; Type: CONSTRAINT; Schema: integration; Owner: -
--

ALTER TABLE ONLY integration.document_upload
    ADD CONSTRAINT document_upload_pkey PRIMARY KEY (upload_id);

--
-- Name: emit_error emit_error_pkey; Type: CONSTRAINT; Schema: integration; Owner: -
--

ALTER TABLE ONLY integration.emit_error
    ADD CONSTRAINT emit_error_pkey PRIMARY KEY (id);

--
-- Name: inbox inbox_pkey; Type: CONSTRAINT; Schema: integration; Owner: -
--

ALTER TABLE ONLY integration.inbox
    ADD CONSTRAINT inbox_pkey PRIMARY KEY (event_id);

--
-- Name: inbox_watermark inbox_watermark_pkey; Type: CONSTRAINT; Schema: integration; Owner: -
--

ALTER TABLE ONLY integration.inbox_watermark
    ADD CONSTRAINT inbox_watermark_pkey PRIMARY KEY (aggregate_id);

--
-- Name: outbox outbox_aggregate_id_sequence_key; Type: CONSTRAINT; Schema: integration; Owner: -
--

ALTER TABLE ONLY integration.outbox
    ADD CONSTRAINT outbox_aggregate_id_sequence_key UNIQUE (aggregate_id, sequence);

--
-- Name: outbox outbox_pkey; Type: CONSTRAINT; Schema: integration; Owner: -
--

ALTER TABLE ONLY integration.outbox
    ADD CONSTRAINT outbox_pkey PRIMARY KEY (id);

--
-- Name: request_shadow request_shadow_pkey; Type: CONSTRAINT; Schema: integration; Owner: -
--

ALTER TABLE ONLY integration.request_shadow
    ADD CONSTRAINT request_shadow_pkey PRIMARY KEY (request_id);

--
-- Name: acceptance acceptance_bid_id_key; Type: CONSTRAINT; Schema: marketplace; Owner: -
--

ALTER TABLE ONLY marketplace.acceptance
    ADD CONSTRAINT acceptance_bid_id_key UNIQUE (bid_id);

--
-- Name: acceptance acceptance_pkey; Type: CONSTRAINT; Schema: marketplace; Owner: -
--

ALTER TABLE ONLY marketplace.acceptance
    ADD CONSTRAINT acceptance_pkey PRIMARY KEY (id);

--
-- Name: acceptance acceptance_request_id_key; Type: CONSTRAINT; Schema: marketplace; Owner: -
--

ALTER TABLE ONLY marketplace.acceptance
    ADD CONSTRAINT acceptance_request_id_key UNIQUE (request_id);

--
-- Name: bid_acceptance bid_acceptance_pkey; Type: CONSTRAINT; Schema: marketplace; Owner: -
--

ALTER TABLE ONLY marketplace.bid_acceptance
    ADD CONSTRAINT bid_acceptance_pkey PRIMARY KEY (id);

--
-- Name: bid_allocation bid_allocation_bid_id_product_id_key; Type: CONSTRAINT; Schema: marketplace; Owner: -
--

ALTER TABLE ONLY marketplace.bid_allocation
    ADD CONSTRAINT bid_allocation_bid_id_product_id_key UNIQUE (bid_id, product_id);

--
-- Name: bid_allocation bid_allocation_pkey; Type: CONSTRAINT; Schema: marketplace; Owner: -
--

ALTER TABLE ONLY marketplace.bid_allocation
    ADD CONSTRAINT bid_allocation_pkey PRIMARY KEY (id);

--
-- Name: bid_benefit bid_benefit_bid_id_benefit_id_key; Type: CONSTRAINT; Schema: marketplace; Owner: -
--

ALTER TABLE ONLY marketplace.bid_benefit
    ADD CONSTRAINT bid_benefit_bid_id_benefit_id_key UNIQUE (bid_id, benefit_id);

--
-- Name: bid_benefit bid_benefit_pkey; Type: CONSTRAINT; Schema: marketplace; Owner: -
--

ALTER TABLE ONLY marketplace.bid_benefit
    ADD CONSTRAINT bid_benefit_pkey PRIMARY KEY (id);

--
-- Name: bid_event bid_event_pkey; Type: CONSTRAINT; Schema: marketplace; Owner: -
--

ALTER TABLE ONLY marketplace.bid_event
    ADD CONSTRAINT bid_event_pkey PRIMARY KEY (id);

--
-- Name: bid bid_institution_id_request_id_idempotency_key_key; Type: CONSTRAINT; Schema: marketplace; Owner: -
--

ALTER TABLE ONLY marketplace.bid
    ADD CONSTRAINT bid_institution_id_request_id_idempotency_key_key UNIQUE (institution_id, request_id, idempotency_key);

--
-- Name: bid bid_institution_id_request_id_key; Type: CONSTRAINT; Schema: marketplace; Owner: -
--

ALTER TABLE ONLY marketplace.bid
    ADD CONSTRAINT bid_institution_id_request_id_key UNIQUE (institution_id, request_id);

--
-- Name: bid bid_pkey; Type: CONSTRAINT; Schema: marketplace; Owner: -
--

ALTER TABLE ONLY marketplace.bid
    ADD CONSTRAINT bid_pkey PRIMARY KEY (id);

--
-- Name: credit_decision credit_decision_pkey; Type: CONSTRAINT; Schema: marketplace; Owner: -
--

ALTER TABLE ONLY marketplace.credit_decision
    ADD CONSTRAINT credit_decision_pkey PRIMARY KEY (id);

--
-- Name: deal_document_event deal_document_event_pkey; Type: CONSTRAINT; Schema: marketplace; Owner: -
--

ALTER TABLE ONLY marketplace.deal_document_event
    ADD CONSTRAINT deal_document_event_pkey PRIMARY KEY (id);

--
-- Name: deal_document deal_document_pkey; Type: CONSTRAINT; Schema: marketplace; Owner: -
--

ALTER TABLE ONLY marketplace.deal_document
    ADD CONSTRAINT deal_document_pkey PRIMARY KEY (id);

--
-- Name: loan_booking loan_booking_pkey; Type: CONSTRAINT; Schema: marketplace; Owner: -
--

ALTER TABLE ONLY marketplace.loan_booking
    ADD CONSTRAINT loan_booking_pkey PRIMARY KEY (pipeline_id);

--
-- Name: loan_pipeline loan_pipeline_pkey; Type: CONSTRAINT; Schema: marketplace; Owner: -
--

ALTER TABLE ONLY marketplace.loan_pipeline
    ADD CONSTRAINT loan_pipeline_pkey PRIMARY KEY (id);

--
-- Name: pipeline_stage_instance pipeline_stage_instance_pkey; Type: CONSTRAINT; Schema: marketplace; Owner: -
--

ALTER TABLE ONLY marketplace.pipeline_stage_instance
    ADD CONSTRAINT pipeline_stage_instance_pkey PRIMARY KEY (id);

--
-- Name: request_allocation request_allocation_pkey; Type: CONSTRAINT; Schema: marketplace; Owner: -
--

ALTER TABLE ONLY marketplace.request_allocation
    ADD CONSTRAINT request_allocation_pkey PRIMARY KEY (id);

--
-- Name: request_allocation request_allocation_request_id_product_id_key; Type: CONSTRAINT; Schema: marketplace; Owner: -
--

ALTER TABLE ONLY marketplace.request_allocation
    ADD CONSTRAINT request_allocation_request_id_product_id_key UNIQUE (request_id, product_id);

--
-- Name: request_decline request_decline_pkey; Type: CONSTRAINT; Schema: marketplace; Owner: -
--

ALTER TABLE ONLY marketplace.request_decline
    ADD CONSTRAINT request_decline_pkey PRIMARY KEY (request_id, institution_id);

--
-- Name: request request_idempotency_key_key; Type: CONSTRAINT; Schema: marketplace; Owner: -
--

ALTER TABLE ONLY marketplace.request
    ADD CONSTRAINT request_idempotency_key_key UNIQUE (idempotency_key);

--
-- Name: request_message request_message_pkey; Type: CONSTRAINT; Schema: marketplace; Owner: -
--

ALTER TABLE ONLY marketplace.request_message
    ADD CONSTRAINT request_message_pkey PRIMARY KEY (id);

--
-- Name: request request_pkey; Type: CONSTRAINT; Schema: marketplace; Owner: -
--

ALTER TABLE ONLY marketplace.request
    ADD CONSTRAINT request_pkey PRIMARY KEY (id);

--
-- Name: sync_state sync_state_pkey; Type: CONSTRAINT; Schema: marketplace; Owner: -
--

ALTER TABLE ONLY marketplace.sync_state
    ADD CONSTRAINT sync_state_pkey PRIMARY KEY (id);

--
-- Name: bid_acceptance uq_bid_acceptance_request; Type: CONSTRAINT; Schema: marketplace; Owner: -
--

ALTER TABLE ONLY marketplace.bid_acceptance
    ADD CONSTRAINT uq_bid_acceptance_request UNIQUE (request_id);

--
-- Name: deal_document uq_deal_document; Type: CONSTRAINT; Schema: marketplace; Owner: -
--

ALTER TABLE ONLY marketplace.deal_document
    ADD CONSTRAINT uq_deal_document UNIQUE (pipeline_id, requirement_id);

--
-- Name: loan_booking uq_loan_booking_core_reference; Type: CONSTRAINT; Schema: marketplace; Owner: -
--

ALTER TABLE ONLY marketplace.loan_booking
    ADD CONSTRAINT uq_loan_booking_core_reference UNIQUE (institution_id, core_reference);

--
-- Name: loan_pipeline uq_pipeline_per_bid; Type: CONSTRAINT; Schema: marketplace; Owner: -
--

ALTER TABLE ONLY marketplace.loan_pipeline
    ADD CONSTRAINT uq_pipeline_per_bid UNIQUE (bid_id);

--
-- Name: pipeline_stage_instance uq_stage_instance; Type: CONSTRAINT; Schema: marketplace; Owner: -
--

ALTER TABLE ONLY marketplace.pipeline_stage_instance
    ADD CONSTRAINT uq_stage_instance UNIQUE (pipeline_id, stage_def_id);

--
-- Name: schema_migration schema_migration_pkey; Type: CONSTRAINT; Schema: ops; Owner: -
--

ALTER TABLE ONLY ops.schema_migration
    ADD CONSTRAINT schema_migration_pkey PRIMARY KEY (filename);

--
-- Name: admin_audit_log admin_audit_log_pkey; Type: CONSTRAINT; Schema: portal_admin; Owner: -
--

ALTER TABLE ONLY portal_admin.admin_audit_log
    ADD CONSTRAINT admin_audit_log_pkey PRIMARY KEY (id);

--
-- Name: admin_dual_control_actions admin_dual_control_actions_pkey; Type: CONSTRAINT; Schema: portal_admin; Owner: -
--

ALTER TABLE ONLY portal_admin.admin_dual_control_actions
    ADD CONSTRAINT admin_dual_control_actions_pkey PRIMARY KEY (id);

--
-- Name: admin_roles admin_roles_pkey; Type: CONSTRAINT; Schema: portal_admin; Owner: -
--

ALTER TABLE ONLY portal_admin.admin_roles
    ADD CONSTRAINT admin_roles_pkey PRIMARY KEY (id);

--
-- Name: admin_roles admin_roles_slug_key; Type: CONSTRAINT; Schema: portal_admin; Owner: -
--

ALTER TABLE ONLY portal_admin.admin_roles
    ADD CONSTRAINT admin_roles_slug_key UNIQUE (slug);

--
-- Name: admin_sessions admin_sessions_pkey; Type: CONSTRAINT; Schema: portal_admin; Owner: -
--

ALTER TABLE ONLY portal_admin.admin_sessions
    ADD CONSTRAINT admin_sessions_pkey PRIMARY KEY (id);

--
-- Name: admin_users admin_users_auth_user_id_key; Type: CONSTRAINT; Schema: portal_admin; Owner: -
--

ALTER TABLE ONLY portal_admin.admin_users
    ADD CONSTRAINT admin_users_auth_user_id_key UNIQUE (auth_user_id);

--
-- Name: admin_users admin_users_email_key; Type: CONSTRAINT; Schema: portal_admin; Owner: -
--

ALTER TABLE ONLY portal_admin.admin_users
    ADD CONSTRAINT admin_users_email_key UNIQUE (email);

--
-- Name: admin_users admin_users_pkey; Type: CONSTRAINT; Schema: portal_admin; Owner: -
--

ALTER TABLE ONLY portal_admin.admin_users
    ADD CONSTRAINT admin_users_pkey PRIMARY KEY (id);

--
-- Name: commission_event commission_event_pkey; Type: CONSTRAINT; Schema: portal_admin; Owner: -
--

ALTER TABLE ONLY portal_admin.commission_event
    ADD CONSTRAINT commission_event_pkey PRIMARY KEY (id);

--
-- Name: commission_event uq_commission_per_pipeline; Type: CONSTRAINT; Schema: portal_admin; Owner: -
--

ALTER TABLE ONLY portal_admin.commission_event
    ADD CONSTRAINT uq_commission_per_pipeline UNIQUE (pipeline_id);

--
-- Name: user_groups user_groups_pkey; Type: CONSTRAINT; Schema: portal_admin; Owner: -
--

ALTER TABLE ONLY portal_admin.user_groups
    ADD CONSTRAINT user_groups_pkey PRIMARY KEY (id);

--
-- Name: user_groups user_groups_slug_key; Type: CONSTRAINT; Schema: portal_admin; Owner: -
--

ALTER TABLE ONLY portal_admin.user_groups
    ADD CONSTRAINT user_groups_slug_key UNIQUE (slug);

--
-- Name: _identity_migration_log _identity_migration_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public._identity_migration_log
    ADD CONSTRAINT _identity_migration_log_pkey PRIMARY KEY (id);

--
-- Name: portal_notifications portal_notifications_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_notifications
    ADD CONSTRAINT portal_notifications_pkey PRIMARY KEY (id);

--
-- Name: idx_audit_action; Type: INDEX; Schema: audit; Owner: -
--

CREATE INDEX idx_audit_action ON audit.event USING btree (action, outcome);

--
-- Name: idx_audit_actor; Type: INDEX; Schema: audit; Owner: -
--

CREATE INDEX idx_audit_actor ON audit.event USING btree (actor_id, occurred_at DESC);

--
-- Name: idx_audit_governance; Type: INDEX; Schema: audit; Owner: -
--

CREATE INDEX idx_audit_governance ON audit.event USING btree (governance_action_id) WHERE (governance_action_id IS NOT NULL);

--
-- Name: idx_audit_institution; Type: INDEX; Schema: audit; Owner: -
--

CREATE INDEX idx_audit_institution ON audit.event USING btree (institution_id, occurred_at DESC) WHERE (institution_id IS NOT NULL);

--
-- Name: idx_audit_occurred; Type: INDEX; Schema: audit; Owner: -
--

CREATE INDEX idx_audit_occurred ON audit.event USING btree (occurred_at DESC);

--
-- Name: auth_users_username_lower_unique; Type: INDEX; Schema: auth_portal; Owner: -
--

CREATE UNIQUE INDEX auth_users_username_lower_unique ON auth_portal.auth_users USING btree (lower(username)) WHERE (username IS NOT NULL);

--
-- Name: email_verification_tokens_user_id_fk_idx; Type: INDEX; Schema: auth_portal; Owner: -
--

CREATE INDEX email_verification_tokens_user_id_fk_idx ON auth_portal.email_verification_tokens USING btree (user_id);

--
-- Name: ix_auth_audit_created_at; Type: INDEX; Schema: auth_portal; Owner: -
--

CREATE INDEX ix_auth_audit_created_at ON auth_portal.auth_audit_events USING btree (created_at);

--
-- Name: ix_auth_audit_event_type; Type: INDEX; Schema: auth_portal; Owner: -
--

CREATE INDEX ix_auth_audit_event_type ON auth_portal.auth_audit_events USING btree (event_type);

--
-- Name: ix_auth_audit_institution_id; Type: INDEX; Schema: auth_portal; Owner: -
--

CREATE INDEX ix_auth_audit_institution_id ON auth_portal.auth_audit_events USING btree (institution_id);

--
-- Name: ix_auth_audit_user_id; Type: INDEX; Schema: auth_portal; Owner: -
--

CREATE INDEX ix_auth_audit_user_id ON auth_portal.auth_audit_events USING btree (user_id);

--
-- Name: ix_auth_sessions_expires_at; Type: INDEX; Schema: auth_portal; Owner: -
--

CREATE INDEX ix_auth_sessions_expires_at ON auth_portal.auth_sessions USING btree (expires_at);

--
-- Name: ix_auth_sessions_user_id; Type: INDEX; Schema: auth_portal; Owner: -
--

CREATE INDEX ix_auth_sessions_user_id ON auth_portal.auth_sessions USING btree (user_id);

--
-- Name: ix_auth_users_institution_id; Type: INDEX; Schema: auth_portal; Owner: -
--

CREATE INDEX ix_auth_users_institution_id ON auth_portal.auth_users USING btree (institution_id);

--
-- Name: ix_ip_allowlist_institution_id; Type: INDEX; Schema: auth_portal; Owner: -
--

CREATE INDEX ix_ip_allowlist_institution_id ON auth_portal.ip_allowlist USING btree (institution_id);

--
-- Name: ix_mfa_backup_codes_user_id; Type: INDEX; Schema: auth_portal; Owner: -
--

CREATE INDEX ix_mfa_backup_codes_user_id ON auth_portal.mfa_backup_codes USING btree (user_id);

--
-- Name: ix_pwd_reset_user_id; Type: INDEX; Schema: auth_portal; Owner: -
--

CREATE INDEX ix_pwd_reset_user_id ON auth_portal.password_reset_tokens USING btree (user_id);

--
-- Name: idx_ab_exec_inst_request; Type: INDEX; Schema: autobid; Owner: -
--

CREATE INDEX idx_ab_exec_inst_request ON ONLY autobid.execution USING btree (institution_id, loan_request_id);

--
-- Name: execution_default_institution_id_loan_request_id_idx; Type: INDEX; Schema: autobid; Owner: -
--

CREATE INDEX execution_default_institution_id_loan_request_id_idx ON autobid.execution_default USING btree (institution_id, loan_request_id);

--
-- Name: idx_ab_exec_inst_time; Type: INDEX; Schema: autobid; Owner: -
--

CREATE INDEX idx_ab_exec_inst_time ON ONLY autobid.execution USING btree (institution_id, occurred_at DESC);

--
-- Name: execution_default_institution_id_occurred_at_idx; Type: INDEX; Schema: autobid; Owner: -
--

CREATE INDEX execution_default_institution_id_occurred_at_idx ON autobid.execution_default USING btree (institution_id, occurred_at DESC);

--
-- Name: idx_ab_rule_inst_priority; Type: INDEX; Schema: autobid; Owner: -
--

CREATE INDEX idx_ab_rule_inst_priority ON autobid.rule USING btree (institution_id, priority, created_at) WHERE (status = 'active'::text);

--
-- Name: idx_ab_rule_inst_status; Type: INDEX; Schema: autobid; Owner: -
--

CREATE INDEX idx_ab_rule_inst_status ON autobid.rule USING btree (institution_id, status);

--
-- Name: idx_ab_rv_inst_rule; Type: INDEX; Schema: autobid; Owner: -
--

CREATE INDEX idx_ab_rv_inst_rule ON autobid.rule_version USING btree (institution_id, rule_id, version_no DESC);

--
-- Name: rule_current_version_id_fk_idx; Type: INDEX; Schema: autobid; Owner: -
--

CREATE INDEX rule_current_version_id_fk_idx ON autobid.rule USING btree (current_version_id);

--
-- Name: commission_adjustment_inst_month; Type: INDEX; Schema: billing; Owner: -
--

CREATE INDEX commission_adjustment_inst_month ON billing.commission_adjustment USING btree (institution_id, effective_on);

--
-- Name: commission_adjustment_one_clawback; Type: INDEX; Schema: billing; Owner: -
--

CREATE UNIQUE INDEX commission_adjustment_one_clawback ON billing.commission_adjustment USING btree (commission_id) WHERE (kind = 'clawback'::text);

--
-- Name: commission_adjustment_pipeline; Type: INDEX; Schema: billing; Owner: -
--

CREATE INDEX commission_adjustment_pipeline ON billing.commission_adjustment USING btree (pipeline_id);

--
-- Name: commission_rule_lookup; Type: INDEX; Schema: billing; Owner: -
--

CREATE INDEX commission_rule_lookup ON billing.commission_rule USING btree (institution_id, product_code, effective_from);

--
-- Name: commission_rule_open_scope; Type: INDEX; Schema: billing; Owner: -
--

CREATE UNIQUE INDEX commission_rule_open_scope ON billing.commission_rule USING btree (COALESCE(institution_id, '00000000-0000-0000-0000-000000000000'::uuid), COALESCE(product_code, ''::text)) WHERE (ended_on IS NULL);

--
-- Name: idx_catalog_product_family; Type: INDEX; Schema: catalog; Owner: -
--

CREATE INDEX idx_catalog_product_family ON catalog.product USING btree (family_id, active);

--
-- Name: product_currency_fk_idx; Type: INDEX; Schema: catalog; Owner: -
--

CREATE INDEX product_currency_fk_idx ON catalog.product USING btree (currency);

--
-- Name: product_eligibility_country_fk_idx; Type: INDEX; Schema: catalog; Owner: -
--

CREATE INDEX product_eligibility_country_fk_idx ON catalog.product_eligibility USING btree (country);

--
-- Name: product_eligibility_product_id_fk_idx; Type: INDEX; Schema: catalog; Owner: -
--

CREATE INDEX product_eligibility_product_id_fk_idx ON catalog.product_eligibility USING btree (product_id);

--
-- Name: idx_ie_inst_module_status; Type: INDEX; Schema: entitlements; Owner: -
--

CREATE INDEX idx_ie_inst_module_status ON entitlements.institution_entitlement USING btree (institution_id, module_code, status);

--
-- Name: idx_ue_inst_module_time; Type: INDEX; Schema: entitlements; Owner: -
--

CREATE INDEX idx_ue_inst_module_time ON ONLY entitlements.usage_event USING btree (institution_id, module_code, occurred_at);

--
-- Name: institution_entitlement_module_code_fk_idx; Type: INDEX; Schema: entitlements; Owner: -
--

CREATE INDEX institution_entitlement_module_code_fk_idx ON entitlements.institution_entitlement USING btree (module_code);

--
-- Name: usage_event_default_institution_id_module_code_occurred_at_idx; Type: INDEX; Schema: entitlements; Owner: -
--

CREATE INDEX usage_event_default_institution_id_module_code_occurred_at_idx ON entitlements.usage_event_default USING btree (institution_id, module_code, occurred_at);

--
-- Name: action_pending_expiry_idx; Type: INDEX; Schema: governance; Owner: -
--

CREATE INDEX action_pending_expiry_idx ON governance.action USING btree (expires_at) WHERE (status = 'pending'::text);

--
-- Name: four_eyes_policy_category_fk_idx; Type: INDEX; Schema: governance; Owner: -
--

CREATE INDEX four_eyes_policy_category_fk_idx ON governance.four_eyes_policy USING btree (category);

--
-- Name: four_eyes_policy_via_action_id_fk_idx; Type: INDEX; Schema: governance; Owner: -
--

CREATE INDEX four_eyes_policy_via_action_id_fk_idx ON governance.four_eyes_policy USING btree (via_action_id);

--
-- Name: idx_governance_inst; Type: INDEX; Schema: governance; Owner: -
--

CREATE INDEX idx_governance_inst ON governance.action USING btree (institution_id, status, category) WHERE (institution_id IS NOT NULL);

--
-- Name: idx_governance_maker; Type: INDEX; Schema: governance; Owner: -
--

CREATE INDEX idx_governance_maker ON governance.action USING btree (maker_id, created_at DESC);

--
-- Name: idx_governance_platform; Type: INDEX; Schema: governance; Owner: -
--

CREATE INDEX idx_governance_platform ON governance.action USING btree (scope, status) WHERE (scope = 'platform'::text);

--
-- Name: access_request_institution_id_fk_idx; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX access_request_institution_id_fk_idx ON institution.access_request USING btree (institution_id);

--
-- Name: access_request_one_open_per_email; Type: INDEX; Schema: institution; Owner: -
--

CREATE UNIQUE INDEX access_request_one_open_per_email ON institution.access_request USING btree (lower(contact_email)) WHERE (status = ANY (ARRAY['unverified'::text, 'submitted'::text, 'in_review'::text, 'info_requested'::text, 'approved'::text]));

--
-- Name: access_request_status_idx; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX access_request_status_idx ON institution.access_request USING btree (status, created_at DESC);

--
-- Name: access_request_verify_token; Type: INDEX; Schema: institution; Owner: -
--

CREATE UNIQUE INDEX access_request_verify_token ON institution.access_request USING btree (verify_token_hash) WHERE (verify_token_hash IS NOT NULL);

--
-- Name: api_key_revoked_by_fk_idx; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX api_key_revoked_by_fk_idx ON institution.api_key USING btree (revoked_by);

--
-- Name: approval_instance_doa_rule_id_fk_idx; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX approval_instance_doa_rule_id_fk_idx ON institution.approval_instance USING btree (doa_rule_id);

--
-- Name: approval_instance_template_id_fk_idx; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX approval_instance_template_id_fk_idx ON institution.approval_instance USING btree (template_id);

--
-- Name: approval_stage_def_committee_id_fk_idx; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX approval_stage_def_committee_id_fk_idx ON institution.approval_stage_def USING btree (committee_id);

--
-- Name: approval_stage_def_escalate_to_template_id_fk_idx; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX approval_stage_def_escalate_to_template_id_fk_idx ON institution.approval_stage_def USING btree (escalate_to_template_id);

--
-- Name: approval_stage_instance_stage_def_id_fk_idx; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX approval_stage_instance_stage_def_id_fk_idx ON institution.approval_stage_instance USING btree (stage_def_id);

--
-- Name: benefit_cat_id_fk_idx; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX benefit_cat_id_fk_idx ON institution.benefit USING btree (cat_id);

--
-- Name: benefit_created_by_fk_idx; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX benefit_created_by_fk_idx ON institution.benefit USING btree (created_by);

--
-- Name: benefit_institution_id_idx; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX benefit_institution_id_idx ON institution.benefit USING btree (institution_id);

--
-- Name: benefit_institution_id_product_id_idx; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX benefit_institution_id_product_id_idx ON institution.benefit USING btree (institution_id, product_id) WHERE (is_active = true);

--
-- Name: benefit_product_id_fk_idx; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX benefit_product_id_fk_idx ON institution.benefit USING btree (product_id);

--
-- Name: compliance_waiver_granted_by_fk_idx; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX compliance_waiver_granted_by_fk_idx ON institution.compliance_waiver USING btree (granted_by);

--
-- Name: compliance_waiver_revoked_by_fk_idx; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX compliance_waiver_revoked_by_fk_idx ON institution.compliance_waiver USING btree (revoked_by);

--
-- Name: doa_rule_template_id_fk_idx; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX doa_rule_template_id_fk_idx ON institution.doa_rule USING btree (template_id);

--
-- Name: doc_doc_type_id_fk_idx; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX doc_doc_type_id_fk_idx ON institution.doc USING btree (doc_type_id);

--
-- Name: doc_generation_entity_idx; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX doc_generation_entity_idx ON institution.doc_generation USING btree (entity_type, entity_id);

--
-- Name: doc_generation_generated_by_fk_idx; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX doc_generation_generated_by_fk_idx ON institution.doc_generation USING btree (generated_by);

--
-- Name: doc_generation_institution_idx; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX doc_generation_institution_idx ON institution.doc_generation USING btree (institution_id);

--
-- Name: doc_generation_stage_instance_id_fk_idx; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX doc_generation_stage_instance_id_fk_idx ON institution.doc_generation USING btree (stage_instance_id);

--
-- Name: doc_generation_template_idx; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX doc_generation_template_idx ON institution.doc_generation USING btree (template_id);

--
-- Name: doc_generation_template_version_id_fk_idx; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX doc_generation_template_version_id_fk_idx ON institution.doc_generation USING btree (template_version_id);

--
-- Name: doc_institution_id_idx; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX doc_institution_id_idx ON institution.doc USING btree (institution_id);

--
-- Name: doc_institution_id_status_idx; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX doc_institution_id_status_idx ON institution.doc USING btree (institution_id, status);

--
-- Name: doc_requirement_institution_id_fk_idx; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX doc_requirement_institution_id_fk_idx ON institution.doc_requirement USING btree (institution_id);

--
-- Name: doc_requirement_stage_def_id_fk_idx; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX doc_requirement_stage_def_id_fk_idx ON institution.doc_requirement USING btree (stage_def_id);

--
-- Name: doc_reviewed_by_fk_idx; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX doc_reviewed_by_fk_idx ON institution.doc USING btree (reviewed_by);

--
-- Name: doc_reviewed_by_staff_fk_idx; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX doc_reviewed_by_staff_fk_idx ON institution.doc USING btree (reviewed_by_staff);

--
-- Name: doc_template_created_by_fk_idx; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX doc_template_created_by_fk_idx ON institution.doc_template USING btree (created_by);

--
-- Name: doc_template_institution_idx; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX doc_template_institution_idx ON institution.doc_template USING btree (institution_id);

--
-- Name: doc_template_product_id_fk_idx; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX doc_template_product_id_fk_idx ON institution.doc_template USING btree (product_id);

--
-- Name: doc_template_version_approved_by_fk_idx; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX doc_template_version_approved_by_fk_idx ON institution.doc_template_version USING btree (approved_by);

--
-- Name: doc_template_version_created_by_fk_idx; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX doc_template_version_created_by_fk_idx ON institution.doc_template_version USING btree (created_by);

--
-- Name: doc_template_version_institution_idx; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX doc_template_version_institution_idx ON institution.doc_template_version USING btree (institution_id);

--
-- Name: doc_template_version_template_idx; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX doc_template_version_template_idx ON institution.doc_template_version USING btree (template_id);

--
-- Name: doc_uploaded_by_fk_idx; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX doc_uploaded_by_fk_idx ON institution.doc USING btree (uploaded_by);

--
-- Name: esign_envelope_approval_instance_id_fk_idx; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX esign_envelope_approval_instance_id_fk_idx ON institution.esign_envelope USING btree (approval_instance_id);

--
-- Name: esign_event_signer_id_fk_idx; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX esign_event_signer_id_fk_idx ON institution.esign_event USING btree (signer_id);

--
-- Name: group_created_by_fk_idx; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX group_created_by_fk_idx ON institution."group" USING btree (created_by);

--
-- Name: idx_aa_stage; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX idx_aa_stage ON institution.approval_action USING btree (stage_instance_id);

--
-- Name: idx_ai_inst_status; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX idx_ai_inst_status ON institution.approval_instance USING btree (institution_id, status);

--
-- Name: idx_api_key_inst; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX idx_api_key_inst ON institution.api_key USING btree (institution_id, revoked_at);

--
-- Name: idx_apikeys_active; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX idx_apikeys_active ON institution.api_key USING btree (institution_id, key_hmac) WHERE (active AND (mc_status = 'approved'::text));

--
-- Name: idx_apikeys_pending; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX idx_apikeys_pending ON institution.api_key USING btree (institution_id, created_at) WHERE (mc_status = 'pending_approval'::text);

--
-- Name: idx_asi_due; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX idx_asi_due ON institution.approval_stage_instance USING btree (due_at) WHERE (status = 'active'::text);

--
-- Name: idx_cm_committee; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX idx_cm_committee ON institution.committee_member USING btree (committee_id);

--
-- Name: idx_ee_env; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX idx_ee_env ON institution.esign_event USING btree (envelope_id, id);

--
-- Name: idx_es_env; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX idx_es_env ON institution.esign_signer USING btree (envelope_id);

--
-- Name: idx_groups_inst; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX idx_groups_inst ON institution."group" USING btree (institution_id);

--
-- Name: idx_inst_members_group; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX idx_inst_members_group ON institution.member USING btree (group_id);

--
-- Name: idx_inst_members_institution; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX idx_inst_members_institution ON institution.member USING btree (institution_id, auth_user_id);

--
-- Name: idx_institution_country; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX idx_institution_country ON institution.institution USING btree (country, institution_type);

--
-- Name: idx_institution_stage; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX idx_institution_stage ON institution.institution USING btree (onboarding_stage, approved);

--
-- Name: idx_kyb_doc_inst; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX idx_kyb_doc_inst ON institution.kyb_document USING btree (institution_id, doc_type, status);

--
-- Name: idx_member_institution; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX idx_member_institution ON institution.member USING btree (institution_id);

--
-- Name: idx_members_inst; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX idx_members_inst ON institution.member USING btree (institution_id, active);

--
-- Name: idx_outbox_pending; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX idx_outbox_pending ON institution.approval_outbox USING btree (id) WHERE (processed_at IS NULL);

--
-- Name: idx_pending_actions_expiry; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX idx_pending_actions_expiry ON institution.pending_actions USING btree (institution_id, expires_at) WHERE (action_status = 'pending'::text);

--
-- Name: idx_pending_actions_institution; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX idx_pending_actions_institution ON institution.pending_actions USING btree (institution_id, action_status, action_category);

--
-- Name: idx_pending_inst_status; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX idx_pending_inst_status ON institution.pending_actions USING btree (institution_id, action_status, initiated_at DESC);

--
-- Name: idx_product_config_inst; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX idx_product_config_inst ON institution.product_config USING btree (institution_id, enabled);

--
-- Name: idx_sla_config_inst; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX idx_sla_config_inst ON institution.institution_sla_config USING btree (institution_id, product_id);

--
-- Name: idx_webhook_delivery_inst; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX idx_webhook_delivery_inst ON institution.webhook_delivery USING btree (institution_id, status, next_retry_at) WHERE (status = ANY (ARRAY['pending'::text, 'retrying'::text]));

--
-- Name: idx_webhook_inst; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX idx_webhook_inst ON institution.webhook USING btree (institution_id, active);

--
-- Name: idx_webhooks_inst; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX idx_webhooks_inst ON institution.webhook USING btree (institution_id);

--
-- Name: institution_compliance_reviewed_by_fk_idx; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX institution_compliance_reviewed_by_fk_idx ON institution.institution USING btree (compliance_reviewed_by);

--
-- Name: institution_sla_config_product_id_fk_idx; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX institution_sla_config_product_id_fk_idx ON institution.institution_sla_config USING btree (product_id);

--
-- Name: institution_suspended_by_fk_idx; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX institution_suspended_by_fk_idx ON institution.institution USING btree (suspended_by);

--
-- Name: ix_doc_requirement_template; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX ix_doc_requirement_template ON institution.doc_requirement USING btree (template_id);

--
-- Name: ix_institutions_approved; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX ix_institutions_approved ON institution.institution USING btree (approved);

--
-- Name: ix_members_auth_user_id; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX ix_members_auth_user_id ON institution.member USING btree (auth_user_id);

--
-- Name: kyb_document_reviewed_by_fk_idx; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX kyb_document_reviewed_by_fk_idx ON institution.kyb_document USING btree (reviewed_by);

--
-- Name: kyb_document_uploaded_by_fk_idx; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX kyb_document_uploaded_by_fk_idx ON institution.kyb_document USING btree (uploaded_by);

--
-- Name: member_custom_group_id_fk_idx; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX member_custom_group_id_fk_idx ON institution.member USING btree (custom_group_id);

--
-- Name: onboarding_pref_submitted_by_fk_idx; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX onboarding_pref_submitted_by_fk_idx ON institution.onboarding_pref USING btree (submitted_by);

--
-- Name: pending_actions_checker_id_fk_idx; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX pending_actions_checker_id_fk_idx ON institution.pending_actions USING btree (checker_id);

--
-- Name: pending_actions_maker_id_fk_idx; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX pending_actions_maker_id_fk_idx ON institution.pending_actions USING btree (maker_id);

--
-- Name: product_config_product_id_fk_idx; Type: INDEX; Schema: institution; Owner: -
--

CREATE INDEX product_config_product_id_fk_idx ON institution.product_config USING btree (product_id);

--
-- Name: uq_live_envelope_per_entity; Type: INDEX; Schema: institution; Owner: -
--

CREATE UNIQUE INDEX uq_live_envelope_per_entity ON institution.esign_envelope USING btree (entity_type, entity_id) WHERE (status = ANY (ARRAY['draft'::text, 'sent'::text, 'partially_signed'::text]));

--
-- Name: uq_live_instance_per_entity; Type: INDEX; Schema: institution; Owner: -
--

CREATE UNIQUE INDEX uq_live_instance_per_entity ON institution.approval_instance USING btree (entity_type, entity_id) WHERE (status = 'in_progress'::text);

--
-- Name: uq_one_vote_per_actor; Type: INDEX; Schema: institution; Owner: -
--

CREATE UNIQUE INDEX uq_one_vote_per_actor ON institution.approval_action USING btree (stage_instance_id, COALESCE(acting_as, actor_id)) WHERE (action = ANY (ARRAY['approve'::text, 'reject'::text, 'abstain'::text]));

--
-- Name: uq_pipeline_template_default; Type: INDEX; Schema: institution; Owner: -
--

CREATE UNIQUE INDEX uq_pipeline_template_default ON institution.pipeline_template USING btree (institution_id) WHERE ((is_default = true) AND (is_active = true));

--
-- Name: api_traffic_hour_hour_idx; Type: INDEX; Schema: integration; Owner: -
--

CREATE INDEX api_traffic_hour_hour_idx ON integration.api_traffic_hour USING btree (hour);

--
-- Name: document_upload_pipeline_id_fk_idx; Type: INDEX; Schema: integration; Owner: -
--

CREATE INDEX document_upload_pipeline_id_fk_idx ON integration.document_upload USING btree (pipeline_id);

--
-- Name: ix_document_upload_doc; Type: INDEX; Schema: integration; Owner: -
--

CREATE INDEX ix_document_upload_doc ON integration.document_upload USING btree (deal_document_id);

--
-- Name: outbox_due_idx; Type: INDEX; Schema: integration; Owner: -
--

CREATE INDEX outbox_due_idx ON integration.outbox USING btree (status, next_attempt_at) WHERE (status = ANY (ARRAY['pending'::text, 'sending'::text]));

--
-- Name: bid_acceptance_bid_id_fk_idx; Type: INDEX; Schema: marketplace; Owner: -
--

CREATE INDEX bid_acceptance_bid_id_fk_idx ON marketplace.bid_acceptance USING btree (bid_id);

--
-- Name: bid_allocation_product_id_fk_idx; Type: INDEX; Schema: marketplace; Owner: -
--

CREATE INDEX bid_allocation_product_id_fk_idx ON marketplace.bid_allocation USING btree (product_id);

--
-- Name: bid_benefit_benefit_id_fk_idx; Type: INDEX; Schema: marketplace; Owner: -
--

CREATE INDEX bid_benefit_benefit_id_fk_idx ON marketplace.bid_benefit USING btree (benefit_id);

--
-- Name: bid_benefit_bid_id_idx; Type: INDEX; Schema: marketplace; Owner: -
--

CREATE INDEX bid_benefit_bid_id_idx ON marketplace.bid_benefit USING btree (bid_id);

--
-- Name: bid_event_actor_id_fk_idx; Type: INDEX; Schema: marketplace; Owner: -
--

CREATE INDEX bid_event_actor_id_fk_idx ON marketplace.bid_event USING btree (actor_id);

--
-- Name: bid_submitted_by_fk_idx; Type: INDEX; Schema: marketplace; Owner: -
--

CREATE INDEX bid_submitted_by_fk_idx ON marketplace.bid USING btree (submitted_by);

--
-- Name: credit_decision_approval_instance_id_fk_idx; Type: INDEX; Schema: marketplace; Owner: -
--

CREATE INDEX credit_decision_approval_instance_id_fk_idx ON marketplace.credit_decision USING btree (approval_instance_id);

--
-- Name: credit_decision_stage_instance_id_fk_idx; Type: INDEX; Schema: marketplace; Owner: -
--

CREATE INDEX credit_decision_stage_instance_id_fk_idx ON marketplace.credit_decision USING btree (stage_instance_id);

--
-- Name: deal_document_requirement_id_fk_idx; Type: INDEX; Schema: marketplace; Owner: -
--

CREATE INDEX deal_document_requirement_id_fk_idx ON marketplace.deal_document USING btree (requirement_id);

--
-- Name: idx_bid_event_bid; Type: INDEX; Schema: marketplace; Owner: -
--

CREATE INDEX idx_bid_event_bid ON marketplace.bid_event USING btree (bid_id, occurred_at DESC);

--
-- Name: idx_marketplace_bid_expiry; Type: INDEX; Schema: marketplace; Owner: -
--

CREATE INDEX idx_marketplace_bid_expiry ON marketplace.bid USING btree (status, expires_at) WHERE (status = 'submitted'::text);

--
-- Name: idx_marketplace_bid_institution; Type: INDEX; Schema: marketplace; Owner: -
--

CREATE INDEX idx_marketplace_bid_institution ON marketplace.bid USING btree (institution_id, status, submitted_at DESC);

--
-- Name: idx_marketplace_bid_request; Type: INDEX; Schema: marketplace; Owner: -
--

CREATE INDEX idx_marketplace_bid_request ON marketplace.bid USING btree (request_id, status);

--
-- Name: idx_marketplace_request_consumer; Type: INDEX; Schema: marketplace; Owner: -
--

CREATE INDEX idx_marketplace_request_consumer ON marketplace.request USING btree (consumer_id, status);

--
-- Name: idx_marketplace_request_open_window; Type: INDEX; Schema: marketplace; Owner: -
--

CREATE INDEX idx_marketplace_request_open_window ON marketplace.request USING btree (bid_window_closes_at, created_at DESC) WHERE (status = 'bidding'::text);

--
-- Name: idx_marketplace_request_product; Type: INDEX; Schema: marketplace; Owner: -
--

CREATE INDEX idx_marketplace_request_product ON marketplace.request USING btree (product_id, status);

--
-- Name: idx_marketplace_request_risk_tier; Type: INDEX; Schema: marketplace; Owner: -
--

CREATE INDEX idx_marketplace_request_risk_tier ON marketplace.request USING btree (ficium_risk_tier) WHERE (ficium_risk_tier IS NOT NULL);

--
-- Name: idx_marketplace_request_status; Type: INDEX; Schema: marketplace; Owner: -
--

CREATE INDEX idx_marketplace_request_status ON marketplace.request USING btree (status, bid_window_closes_at);

--
-- Name: ix_credit_decision_pipeline; Type: INDEX; Schema: marketplace; Owner: -
--

CREATE INDEX ix_credit_decision_pipeline ON marketplace.credit_decision USING btree (pipeline_id, created_at DESC);

--
-- Name: ix_deal_document_event_doc; Type: INDEX; Schema: marketplace; Owner: -
--

CREATE INDEX ix_deal_document_event_doc ON marketplace.deal_document_event USING btree (document_id);

--
-- Name: ix_request_message_thread; Type: INDEX; Schema: marketplace; Owner: -
--

CREATE INDEX ix_request_message_thread ON marketplace.request_message USING btree (request_id, institution_id, created_at);

--
-- Name: ix_stage_instance_approval; Type: INDEX; Schema: marketplace; Owner: -
--

CREATE INDEX ix_stage_instance_approval ON marketplace.pipeline_stage_instance USING btree (approval_instance_id) WHERE (approval_instance_id IS NOT NULL);

--
-- Name: loan_pipeline_current_stage_id_fk_idx; Type: INDEX; Schema: marketplace; Owner: -
--

CREATE INDEX loan_pipeline_current_stage_id_fk_idx ON marketplace.loan_pipeline USING btree (current_stage_id);

--
-- Name: loan_pipeline_request_id_fk_idx; Type: INDEX; Schema: marketplace; Owner: -
--

CREATE INDEX loan_pipeline_request_id_fk_idx ON marketplace.loan_pipeline USING btree (request_id);

--
-- Name: loan_pipeline_template_id_fk_idx; Type: INDEX; Schema: marketplace; Owner: -
--

CREATE INDEX loan_pipeline_template_id_fk_idx ON marketplace.loan_pipeline USING btree (template_id);

--
-- Name: pipeline_stage_instance_stage_def_id_fk_idx; Type: INDEX; Schema: marketplace; Owner: -
--

CREATE INDEX pipeline_stage_instance_stage_def_id_fk_idx ON marketplace.pipeline_stage_instance USING btree (stage_def_id);

--
-- Name: request_allocation_product_id_fk_idx; Type: INDEX; Schema: marketplace; Owner: -
--

CREATE INDEX request_allocation_product_id_fk_idx ON marketplace.request_allocation USING btree (product_id);

--
-- Name: request_country_fk_idx; Type: INDEX; Schema: marketplace; Owner: -
--

CREATE INDEX request_country_fk_idx ON marketplace.request USING btree (country);

--
-- Name: request_currency_fk_idx; Type: INDEX; Schema: marketplace; Owner: -
--

CREATE INDEX request_currency_fk_idx ON marketplace.request USING btree (currency);

--
-- Name: request_relisted_from_fk_idx; Type: INDEX; Schema: marketplace; Owner: -
--

CREATE INDEX request_relisted_from_fk_idx ON marketplace.request USING btree (relisted_from);

--
-- Name: request_winning_bid_id_fk_idx; Type: INDEX; Schema: marketplace; Owner: -
--

CREATE INDEX request_winning_bid_id_fk_idx ON marketplace.request USING btree (winning_bid_id);

--
-- Name: admin_audit_log_session_id_fk_idx; Type: INDEX; Schema: portal_admin; Owner: -
--

CREATE INDEX admin_audit_log_session_id_fk_idx ON portal_admin.admin_audit_log USING btree (session_id);

--
-- Name: admin_dual_control_actions_checker_id_fk_idx; Type: INDEX; Schema: portal_admin; Owner: -
--

CREATE INDEX admin_dual_control_actions_checker_id_fk_idx ON portal_admin.admin_dual_control_actions USING btree (checker_id);

--
-- Name: admin_users_custom_role_id_fk_idx; Type: INDEX; Schema: portal_admin; Owner: -
--

CREATE INDEX admin_users_custom_role_id_fk_idx ON portal_admin.admin_users USING btree (custom_role_id);

--
-- Name: admin_users_suspended_by_fk_idx; Type: INDEX; Schema: portal_admin; Owner: -
--

CREATE INDEX admin_users_suspended_by_fk_idx ON portal_admin.admin_users USING btree (suspended_by);

--
-- Name: commission_event_rule_id_fk_idx; Type: INDEX; Schema: portal_admin; Owner: -
--

CREATE INDEX commission_event_rule_id_fk_idx ON portal_admin.commission_event USING btree (rule_id);

--
-- Name: idx_admin_sessions_active; Type: INDEX; Schema: portal_admin; Owner: -
--

CREATE INDEX idx_admin_sessions_active ON portal_admin.admin_sessions USING btree (is_active) WHERE (is_active = true);

--
-- Name: idx_admin_sessions_started; Type: INDEX; Schema: portal_admin; Owner: -
--

CREATE INDEX idx_admin_sessions_started ON portal_admin.admin_sessions USING btree (started_at DESC);

--
-- Name: idx_admin_sessions_user; Type: INDEX; Schema: portal_admin; Owner: -
--

CREATE INDEX idx_admin_sessions_user ON portal_admin.admin_sessions USING btree (admin_user_id);

--
-- Name: idx_admin_users_group; Type: INDEX; Schema: portal_admin; Owner: -
--

CREATE INDEX idx_admin_users_group ON portal_admin.admin_users USING btree (group_id);

--
-- Name: idx_admin_users_status; Type: INDEX; Schema: portal_admin; Owner: -
--

CREATE INDEX idx_admin_users_status ON portal_admin.admin_users USING btree (status);

--
-- Name: idx_audit_actor; Type: INDEX; Schema: portal_admin; Owner: -
--

CREATE INDEX idx_audit_actor ON portal_admin.admin_audit_log USING btree (actor_id);

--
-- Name: idx_audit_category; Type: INDEX; Schema: portal_admin; Owner: -
--

CREATE INDEX idx_audit_category ON portal_admin.admin_audit_log USING btree (action_category);

--
-- Name: idx_audit_created; Type: INDEX; Schema: portal_admin; Owner: -
--

CREATE INDEX idx_audit_created ON portal_admin.admin_audit_log USING btree (created_at DESC);

--
-- Name: idx_audit_dc; Type: INDEX; Schema: portal_admin; Owner: -
--

CREATE INDEX idx_audit_dc ON portal_admin.admin_audit_log USING btree (dual_control_id) WHERE (dual_control_id IS NOT NULL);

--
-- Name: idx_audit_outcome; Type: INDEX; Schema: portal_admin; Owner: -
--

CREATE INDEX idx_audit_outcome ON portal_admin.admin_audit_log USING btree (outcome);

--
-- Name: idx_dc_category; Type: INDEX; Schema: portal_admin; Owner: -
--

CREATE INDEX idx_dc_category ON portal_admin.admin_dual_control_actions USING btree (action_category);

--
-- Name: idx_dc_expires; Type: INDEX; Schema: portal_admin; Owner: -
--

CREATE INDEX idx_dc_expires ON portal_admin.admin_dual_control_actions USING btree (expires_at) WHERE (status = 'pending'::portal_admin.dual_control_status);

--
-- Name: idx_dc_maker; Type: INDEX; Schema: portal_admin; Owner: -
--

CREATE INDEX idx_dc_maker ON portal_admin.admin_dual_control_actions USING btree (maker_id);

--
-- Name: idx_dc_status; Type: INDEX; Schema: portal_admin; Owner: -
--

CREATE INDEX idx_dc_status ON portal_admin.admin_dual_control_actions USING btree (status);

--
-- Name: idx_user_groups_user_type; Type: INDEX; Schema: portal_admin; Owner: -
--

CREATE INDEX idx_user_groups_user_type ON portal_admin.user_groups USING btree (user_type);

--
-- Name: uq_dual_control_pending_resource; Type: INDEX; Schema: portal_admin; Owner: -
--

CREATE UNIQUE INDEX uq_dual_control_pending_resource ON portal_admin.admin_dual_control_actions USING btree (action_category, resource_id) WHERE (status = 'pending'::portal_admin.dual_control_status);

--
-- Name: idx_portal_notifs_institution_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_portal_notifs_institution_created ON public.portal_notifications USING btree (institution_id, created_at DESC);

--
-- Name: idx_portal_notifs_unread; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_portal_notifs_unread ON public.portal_notifications USING btree (institution_id, read_at) WHERE (read_at IS NULL);

--
-- Name: execution_default_idempotency_key_occurred_at_key; Type: INDEX ATTACH; Schema: autobid; Owner: -
--

ALTER INDEX autobid.execution_idempotency_key_occurred_at_key ATTACH PARTITION autobid.execution_default_idempotency_key_occurred_at_key;

--
-- Name: execution_default_institution_id_loan_request_id_idx; Type: INDEX ATTACH; Schema: autobid; Owner: -
--

ALTER INDEX autobid.idx_ab_exec_inst_request ATTACH PARTITION autobid.execution_default_institution_id_loan_request_id_idx;

--
-- Name: execution_default_institution_id_occurred_at_idx; Type: INDEX ATTACH; Schema: autobid; Owner: -
--

ALTER INDEX autobid.idx_ab_exec_inst_time ATTACH PARTITION autobid.execution_default_institution_id_occurred_at_idx;

--
-- Name: execution_default_pkey; Type: INDEX ATTACH; Schema: autobid; Owner: -
--

ALTER INDEX autobid.execution_pkey ATTACH PARTITION autobid.execution_default_pkey;

--
-- Name: usage_event_default_idempotency_key_occurred_at_key; Type: INDEX ATTACH; Schema: entitlements; Owner: -
--

ALTER INDEX entitlements.usage_event_idempotency_key_occurred_at_key ATTACH PARTITION entitlements.usage_event_default_idempotency_key_occurred_at_key;

--
-- Name: usage_event_default_institution_id_module_code_occurred_at_idx; Type: INDEX ATTACH; Schema: entitlements; Owner: -
--

ALTER INDEX entitlements.idx_ue_inst_module_time ATTACH PARTITION entitlements.usage_event_default_institution_id_module_code_occurred_at_idx;

--
-- Name: usage_event_default_pkey; Type: INDEX ATTACH; Schema: entitlements; Owner: -
--

ALTER INDEX entitlements.usage_event_pkey ATTACH PARTITION entitlements.usage_event_default_pkey;

--
-- Name: auth_audit_events no_delete_audit; Type: RULE; Schema: auth_portal; Owner: -
--

CREATE RULE no_delete_audit AS
    ON DELETE TO auth_portal.auth_audit_events DO INSTEAD NOTHING;

--
-- Name: auth_audit_events no_update_audit; Type: RULE; Schema: auth_portal; Owner: -
--

CREATE RULE no_update_audit AS
    ON UPDATE TO auth_portal.auth_audit_events DO INSTEAD NOTHING;

--
-- Name: bid_event marketplace_bid_event_no_delete; Type: RULE; Schema: marketplace; Owner: -
--

CREATE RULE marketplace_bid_event_no_delete AS
    ON DELETE TO marketplace.bid_event DO INSTEAD NOTHING;

--
-- Name: bid_event marketplace_bid_event_no_update; Type: RULE; Schema: marketplace; Owner: -
--

CREATE RULE marketplace_bid_event_no_update AS
    ON UPDATE TO marketplace.bid_event DO INSTEAD NOTHING;

--
-- Name: admin_audit_log audit_log_no_delete; Type: RULE; Schema: portal_admin; Owner: -
--

CREATE RULE audit_log_no_delete AS
    ON DELETE TO portal_admin.admin_audit_log DO INSTEAD NOTHING;

--
-- Name: admin_audit_log audit_log_no_update; Type: RULE; Schema: portal_admin; Owner: -
--

CREATE RULE audit_log_no_update AS
    ON UPDATE TO portal_admin.admin_audit_log DO INSTEAD NOTHING;

--
-- Name: event audit_event_no_delete; Type: TRIGGER; Schema: audit; Owner: -
--

CREATE TRIGGER audit_event_no_delete BEFORE DELETE ON audit.event FOR EACH ROW EXECUTE FUNCTION audit.block_mutation();

--
-- Name: event audit_event_no_update; Type: TRIGGER; Schema: audit; Owner: -
--

CREATE TRIGGER audit_event_no_update BEFORE UPDATE ON audit.event FOR EACH ROW EXECUTE FUNCTION audit.block_mutation();

--
-- Name: auth_users trg_auth_users_updated_at; Type: TRIGGER; Schema: auth_portal; Owner: -
--

CREATE TRIGGER trg_auth_users_updated_at BEFORE UPDATE ON auth_portal.auth_users FOR EACH ROW EXECUTE FUNCTION auth_portal.set_updated_at();

--
-- Name: rule_version trg_rule_version_freeze; Type: TRIGGER; Schema: autobid; Owner: -
--

CREATE TRIGGER trg_rule_version_freeze BEFORE UPDATE ON autobid.rule_version FOR EACH ROW EXECUTE FUNCTION autobid.rule_version_freeze();

--
-- Name: commission_adjustment commission_adjustment_append_only; Type: TRIGGER; Schema: billing; Owner: -
--

CREATE TRIGGER commission_adjustment_append_only BEFORE DELETE OR UPDATE ON billing.commission_adjustment FOR EACH ROW EXECUTE FUNCTION billing.adjustment_is_append_only();

--
-- Name: product_family catalog_product_family_updated_at; Type: TRIGGER; Schema: catalog; Owner: -
--

CREATE TRIGGER catalog_product_family_updated_at BEFORE UPDATE ON catalog.product_family FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

--
-- Name: product catalog_product_updated_at; Type: TRIGGER; Schema: catalog; Owner: -
--

CREATE TRIGGER catalog_product_updated_at BEFORE UPDATE ON catalog.product FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

--
-- Name: action governance_action_updated_at; Type: TRIGGER; Schema: governance; Owner: -
--

CREATE TRIGGER governance_action_updated_at BEFORE UPDATE ON governance.action FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

--
-- Name: action trg_guard_member_action; Type: TRIGGER; Schema: governance; Owner: -
--

CREATE TRIGGER trg_guard_member_action BEFORE INSERT ON governance.action FOR EACH ROW WHEN ((new.category = ANY (ARRAY['user.update'::text, 'user.deactivate'::text, 'user.reset_password'::text]))) EXECUTE FUNCTION governance.guard_member_action();

--
-- Name: doc_template doc_template_updated_at; Type: TRIGGER; Schema: institution; Owner: -
--

CREATE TRIGGER doc_template_updated_at BEFORE UPDATE ON institution.doc_template FOR EACH ROW EXECUTE FUNCTION institution.doc_template_set_updated_at();

--
-- Name: group inst_groups_updated_at; Type: TRIGGER; Schema: institution; Owner: -
--

CREATE TRIGGER inst_groups_updated_at BEFORE UPDATE ON institution."group" FOR EACH ROW EXECUTE FUNCTION portal_admin.set_updated_at();

--
-- Name: member institution_member_updated_at; Type: TRIGGER; Schema: institution; Owner: -
--

CREATE TRIGGER institution_member_updated_at BEFORE UPDATE ON institution.member FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

--
-- Name: product_config institution_product_config_updated_at; Type: TRIGGER; Schema: institution; Owner: -
--

CREATE TRIGGER institution_product_config_updated_at BEFORE UPDATE ON institution.product_config FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

--
-- Name: webhook institution_webhook_updated_at; Type: TRIGGER; Schema: institution; Owner: -
--

CREATE TRIGGER institution_webhook_updated_at BEFORE UPDATE ON institution.webhook FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

--
-- Name: doc_requirement integration_requirement_events; Type: TRIGGER; Schema: institution; Owner: -
--

CREATE CONSTRAINT TRIGGER integration_requirement_events AFTER UPDATE ON institution.doc_requirement DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION integration.trg_requirement_emit();

--
-- Name: member trg_assign_default_member_group; Type: TRIGGER; Schema: institution; Owner: -
--

CREATE TRIGGER trg_assign_default_member_group BEFORE INSERT ON institution.member FOR EACH ROW EXECUTE FUNCTION institution.assign_default_member_group();

--
-- Name: approval_instance trg_facility_approval_record_decision; Type: TRIGGER; Schema: institution; Owner: -
--

CREATE TRIGGER trg_facility_approval_record_decision AFTER UPDATE OF status ON institution.approval_instance FOR EACH ROW WHEN (((new.entity_type = 'facility'::text) AND (old.status = 'in_progress'::text) AND (new.status = ANY (ARRAY['approved'::text, 'rejected'::text])))) EXECUTE FUNCTION marketplace.on_facility_approval_record_decision();

--
-- Name: approval_instance trg_facility_approval_resolved; Type: TRIGGER; Schema: institution; Owner: -
--

CREATE TRIGGER trg_facility_approval_resolved AFTER UPDATE OF status ON institution.approval_instance FOR EACH ROW WHEN (((new.entity_type = 'facility'::text) AND (old.status = 'in_progress'::text) AND (new.status <> 'in_progress'::text))) EXECUTE FUNCTION marketplace.on_facility_approval_resolved();

--
-- Name: institution trg_institutions_updated_at; Type: TRIGGER; Schema: institution; Owner: -
--

CREATE TRIGGER trg_institutions_updated_at BEFORE UPDATE ON institution.institution FOR EACH ROW EXECUTE FUNCTION institution.set_updated_at();

--
-- Name: member trg_member_group_tenant; Type: TRIGGER; Schema: institution; Owner: -
--

CREATE TRIGGER trg_member_group_tenant BEFORE INSERT OR UPDATE OF custom_group_id ON institution.member FOR EACH ROW EXECUTE FUNCTION institution.enforce_member_group_tenant();

--
-- Name: member trg_member_sync_login_state; Type: TRIGGER; Schema: institution; Owner: -
--

CREATE TRIGGER trg_member_sync_login_state AFTER UPDATE OF active, email ON institution.member FOR EACH ROW EXECUTE FUNCTION institution.sync_member_login_state();

--
-- Name: admin_command_log admin_command_log_worm; Type: TRIGGER; Schema: integration; Owner: -
--

CREATE TRIGGER admin_command_log_worm BEFORE DELETE OR UPDATE ON integration.admin_command_log FOR EACH ROW EXECUTE FUNCTION integration._admin_command_log_worm();

--
-- Name: admin_command_log admin_command_log_worm_trunc; Type: TRIGGER; Schema: integration; Owner: -
--

CREATE TRIGGER admin_command_log_worm_trunc BEFORE TRUNCATE ON integration.admin_command_log FOR EACH STATEMENT EXECUTE FUNCTION integration._admin_command_log_worm();

--
-- Name: acceptance_log integration_acceptance_documents; Type: TRIGGER; Schema: integration; Owner: -
--

CREATE CONSTRAINT TRIGGER integration_acceptance_documents AFTER INSERT OR UPDATE ON integration.acceptance_log DEFERRABLE INITIALLY DEFERRED FOR EACH ROW WHEN ((new.status_code = 200)) EXECUTE FUNCTION integration.trg_deal_documents_emit();

--
-- Name: acceptance_log integration_acceptance_progress; Type: TRIGGER; Schema: integration; Owner: -
--

CREATE CONSTRAINT TRIGGER integration_acceptance_progress AFTER INSERT OR UPDATE ON integration.acceptance_log DEFERRABLE INITIALLY DEFERRED FOR EACH ROW WHEN ((new.status_code = 200)) EXECUTE FUNCTION integration.trg_deal_progress_emit();

--
-- Name: bid_event bid_event_no_delete; Type: TRIGGER; Schema: marketplace; Owner: -
--

CREATE TRIGGER bid_event_no_delete BEFORE DELETE ON marketplace.bid_event FOR EACH ROW EXECUTE FUNCTION marketplace.block_bid_event_mutation();

--
-- Name: bid_event bid_event_no_update; Type: TRIGGER; Schema: marketplace; Owner: -
--

CREATE TRIGGER bid_event_no_update BEFORE UPDATE ON marketplace.bid_event FOR EACH ROW EXECUTE FUNCTION marketplace.block_bid_event_mutation();

--
-- Name: bid_allocation integration_bid_allocation_events; Type: TRIGGER; Schema: marketplace; Owner: -
--

CREATE CONSTRAINT TRIGGER integration_bid_allocation_events AFTER INSERT OR DELETE OR UPDATE ON marketplace.bid_allocation DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION integration.trg_bid_child_emit();

--
-- Name: bid_benefit integration_bid_benefit_events; Type: TRIGGER; Schema: marketplace; Owner: -
--

CREATE CONSTRAINT TRIGGER integration_bid_benefit_events AFTER INSERT OR DELETE OR UPDATE ON marketplace.bid_benefit DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION integration.trg_bid_child_emit();

--
-- Name: bid integration_bid_events; Type: TRIGGER; Schema: marketplace; Owner: -
--

CREATE CONSTRAINT TRIGGER integration_bid_events AFTER INSERT OR UPDATE ON marketplace.bid DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION integration.trg_bid_emit();

--
-- Name: deal_document integration_deal_document_events; Type: TRIGGER; Schema: marketplace; Owner: -
--

CREATE CONSTRAINT TRIGGER integration_deal_document_events AFTER INSERT OR UPDATE ON marketplace.deal_document DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION integration.trg_deal_documents_emit();

--
-- Name: loan_pipeline integration_deal_progress_pipeline; Type: TRIGGER; Schema: marketplace; Owner: -
--

CREATE CONSTRAINT TRIGGER integration_deal_progress_pipeline AFTER INSERT OR UPDATE ON marketplace.loan_pipeline DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION integration.trg_deal_progress_emit();

--
-- Name: pipeline_stage_instance integration_deal_progress_stage; Type: TRIGGER; Schema: marketplace; Owner: -
--

CREATE CONSTRAINT TRIGGER integration_deal_progress_stage AFTER INSERT OR UPDATE ON marketplace.pipeline_stage_instance DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION integration.trg_deal_progress_emit();

--
-- Name: bid marketplace_bid_updated_at; Type: TRIGGER; Schema: marketplace; Owner: -
--

CREATE TRIGGER marketplace_bid_updated_at BEFORE UPDATE ON marketplace.bid FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

--
-- Name: request marketplace_request_updated_at; Type: TRIGGER; Schema: marketplace; Owner: -
--

CREATE TRIGGER marketplace_request_updated_at BEFORE UPDATE ON marketplace.request FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

--
-- Name: loan_pipeline pipeline_documents; Type: TRIGGER; Schema: marketplace; Owner: -
--

CREATE CONSTRAINT TRIGGER pipeline_documents AFTER INSERT ON marketplace.loan_pipeline DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION marketplace.trg_pipeline_documents();

--
-- Name: request trg_expire_bids_on_request_end; Type: TRIGGER; Schema: marketplace; Owner: -
--

CREATE TRIGGER trg_expire_bids_on_request_end AFTER UPDATE OF status ON marketplace.request FOR EACH ROW WHEN (((new.status = ANY (ARRAY['expired'::text, 'cancelled'::text, 'withdrawn'::text])) AND (old.status IS DISTINCT FROM new.status))) EXECUTE FUNCTION marketplace.expire_bids_on_request_end();

--
-- Name: bid trg_guard_bid_compliance; Type: TRIGGER; Schema: marketplace; Owner: -
--

CREATE TRIGGER trg_guard_bid_compliance BEFORE INSERT ON marketplace.bid FOR EACH ROW EXECUTE FUNCTION marketplace.guard_bid_compliance();

--
-- Name: bid trg_guard_bid_deal_template; Type: TRIGGER; Schema: marketplace; Owner: -
--

CREATE TRIGGER trg_guard_bid_deal_template BEFORE INSERT ON marketplace.bid FOR EACH ROW EXECUTE FUNCTION marketplace.guard_bid_deal_template();

--
-- Name: bid trg_guard_bid_window; Type: TRIGGER; Schema: marketplace; Owner: -
--

CREATE TRIGGER trg_guard_bid_window BEFORE INSERT ON marketplace.bid FOR EACH ROW EXECUTE FUNCTION marketplace.guard_bid_window();

--
-- Name: bid trg_guard_bid_withdraw; Type: TRIGGER; Schema: marketplace; Owner: -
--

CREATE TRIGGER trg_guard_bid_withdraw BEFORE UPDATE OF status ON marketplace.bid FOR EACH ROW EXECUTE FUNCTION marketplace.guard_bid_withdraw();

--
-- Name: request trg_request_strip_employer; Type: TRIGGER; Schema: marketplace; Owner: -
--

CREATE TRIGGER trg_request_strip_employer BEFORE INSERT OR UPDATE OF metadata ON marketplace.request FOR EACH ROW EXECUTE FUNCTION marketplace.strip_employer();

--
-- Name: bid trg_snapshot_bid_benefits; Type: TRIGGER; Schema: marketplace; Owner: -
--

CREATE TRIGGER trg_snapshot_bid_benefits AFTER INSERT ON marketplace.bid FOR EACH ROW EXECUTE FUNCTION marketplace.snapshot_bid_benefits();

--
-- Name: admin_users admin_users_failed_login_lock; Type: TRIGGER; Schema: portal_admin; Owner: -
--

CREATE TRIGGER admin_users_failed_login_lock BEFORE UPDATE OF failed_login_count ON portal_admin.admin_users FOR EACH ROW EXECUTE FUNCTION portal_admin.check_failed_logins();

--
-- Name: admin_users admin_users_updated_at; Type: TRIGGER; Schema: portal_admin; Owner: -
--

CREATE TRIGGER admin_users_updated_at BEFORE UPDATE ON portal_admin.admin_users FOR EACH ROW EXECUTE FUNCTION portal_admin.set_updated_at();

--
-- Name: user_groups user_groups_updated_at; Type: TRIGGER; Schema: portal_admin; Owner: -
--

CREATE TRIGGER user_groups_updated_at BEFORE UPDATE ON portal_admin.user_groups FOR EACH ROW EXECUTE FUNCTION portal_admin.set_updated_at();

--
-- Name: event event_governance_action_id_fkey; Type: FK CONSTRAINT; Schema: audit; Owner: -
--

ALTER TABLE ONLY audit.event
    ADD CONSTRAINT event_governance_action_id_fkey FOREIGN KEY (governance_action_id) REFERENCES governance.action(id) ON DELETE SET NULL;

--
-- Name: event event_institution_id_fkey; Type: FK CONSTRAINT; Schema: audit; Owner: -
--

ALTER TABLE ONLY audit.event
    ADD CONSTRAINT event_institution_id_fkey FOREIGN KEY (institution_id) REFERENCES institution.institution(id) ON DELETE SET NULL;

--
-- Name: auth_audit_events auth_audit_events_user_id_fkey; Type: FK CONSTRAINT; Schema: auth_portal; Owner: -
--

ALTER TABLE ONLY auth_portal.auth_audit_events
    ADD CONSTRAINT auth_audit_events_user_id_fkey FOREIGN KEY (user_id) REFERENCES auth_portal.auth_users(id) ON DELETE SET NULL;

--
-- Name: auth_sessions auth_sessions_user_id_fkey; Type: FK CONSTRAINT; Schema: auth_portal; Owner: -
--

ALTER TABLE ONLY auth_portal.auth_sessions
    ADD CONSTRAINT auth_sessions_user_id_fkey FOREIGN KEY (user_id) REFERENCES auth_portal.auth_users(id) ON DELETE CASCADE;

--
-- Name: email_verification_tokens email_verification_tokens_user_id_fkey; Type: FK CONSTRAINT; Schema: auth_portal; Owner: -
--

ALTER TABLE ONLY auth_portal.email_verification_tokens
    ADD CONSTRAINT email_verification_tokens_user_id_fkey FOREIGN KEY (user_id) REFERENCES auth_portal.auth_users(id) ON DELETE CASCADE;

--
-- Name: mfa_backup_codes mfa_backup_codes_user_id_fkey; Type: FK CONSTRAINT; Schema: auth_portal; Owner: -
--

ALTER TABLE ONLY auth_portal.mfa_backup_codes
    ADD CONSTRAINT mfa_backup_codes_user_id_fkey FOREIGN KEY (user_id) REFERENCES auth_portal.auth_users(id) ON DELETE CASCADE;

--
-- Name: password_reset_tokens password_reset_tokens_user_id_fkey; Type: FK CONSTRAINT; Schema: auth_portal; Owner: -
--

ALTER TABLE ONLY auth_portal.password_reset_tokens
    ADD CONSTRAINT password_reset_tokens_user_id_fkey FOREIGN KEY (user_id) REFERENCES auth_portal.auth_users(id) ON DELETE CASCADE;

--
-- Name: rule rule_current_version_fk; Type: FK CONSTRAINT; Schema: autobid; Owner: -
--

ALTER TABLE ONLY autobid.rule
    ADD CONSTRAINT rule_current_version_fk FOREIGN KEY (current_version_id) REFERENCES autobid.rule_version(id);

--
-- Name: rule_version rule_version_rule_id_fkey; Type: FK CONSTRAINT; Schema: autobid; Owner: -
--

ALTER TABLE ONLY autobid.rule_version
    ADD CONSTRAINT rule_version_rule_id_fkey FOREIGN KEY (rule_id) REFERENCES autobid.rule(id);

--
-- Name: commission_adjustment commission_adjustment_commission_id_fkey; Type: FK CONSTRAINT; Schema: billing; Owner: -
--

ALTER TABLE ONLY billing.commission_adjustment
    ADD CONSTRAINT commission_adjustment_commission_id_fkey FOREIGN KEY (commission_id) REFERENCES portal_admin.commission_event(id);

--
-- Name: commission_adjustment commission_adjustment_institution_id_fkey; Type: FK CONSTRAINT; Schema: billing; Owner: -
--

ALTER TABLE ONLY billing.commission_adjustment
    ADD CONSTRAINT commission_adjustment_institution_id_fkey FOREIGN KEY (institution_id) REFERENCES institution.institution(id);

--
-- Name: commission_adjustment commission_adjustment_pipeline_id_fkey; Type: FK CONSTRAINT; Schema: billing; Owner: -
--

ALTER TABLE ONLY billing.commission_adjustment
    ADD CONSTRAINT commission_adjustment_pipeline_id_fkey FOREIGN KEY (pipeline_id) REFERENCES marketplace.loan_pipeline(id);

--
-- Name: commission_rule commission_rule_institution_id_fkey; Type: FK CONSTRAINT; Schema: billing; Owner: -
--

ALTER TABLE ONLY billing.commission_rule
    ADD CONSTRAINT commission_rule_institution_id_fkey FOREIGN KEY (institution_id) REFERENCES institution.institution(id);

--
-- Name: product product_currency_fkey; Type: FK CONSTRAINT; Schema: catalog; Owner: -
--

ALTER TABLE ONLY catalog.product
    ADD CONSTRAINT product_currency_fkey FOREIGN KEY (currency) REFERENCES catalog.currency(code);

--
-- Name: product_document product_document_product_id_fkey; Type: FK CONSTRAINT; Schema: catalog; Owner: -
--

ALTER TABLE ONLY catalog.product_document
    ADD CONSTRAINT product_document_product_id_fkey FOREIGN KEY (product_id) REFERENCES catalog.product(id) ON DELETE CASCADE;

--
-- Name: product_eligibility product_eligibility_country_fkey; Type: FK CONSTRAINT; Schema: catalog; Owner: -
--

ALTER TABLE ONLY catalog.product_eligibility
    ADD CONSTRAINT product_eligibility_country_fkey FOREIGN KEY (country) REFERENCES catalog.country(code);

--
-- Name: product_eligibility product_eligibility_product_id_fkey; Type: FK CONSTRAINT; Schema: catalog; Owner: -
--

ALTER TABLE ONLY catalog.product_eligibility
    ADD CONSTRAINT product_eligibility_product_id_fkey FOREIGN KEY (product_id) REFERENCES catalog.product(id) ON DELETE CASCADE;

--
-- Name: product product_family_id_fkey; Type: FK CONSTRAINT; Schema: catalog; Owner: -
--

ALTER TABLE ONLY catalog.product
    ADD CONSTRAINT product_family_id_fkey FOREIGN KEY (family_id) REFERENCES catalog.product_family(id);

--
-- Name: product_parameter product_parameter_product_id_fkey; Type: FK CONSTRAINT; Schema: catalog; Owner: -
--

ALTER TABLE ONLY catalog.product_parameter
    ADD CONSTRAINT product_parameter_product_id_fkey FOREIGN KEY (product_id) REFERENCES catalog.product(id) ON DELETE CASCADE;

--
-- Name: product_rate_model product_rate_model_product_id_fkey; Type: FK CONSTRAINT; Schema: catalog; Owner: -
--

ALTER TABLE ONLY catalog.product_rate_model
    ADD CONSTRAINT product_rate_model_product_id_fkey FOREIGN KEY (product_id) REFERENCES catalog.product(id) ON DELETE CASCADE;

--
-- Name: product_sla product_sla_product_id_fkey; Type: FK CONSTRAINT; Schema: catalog; Owner: -
--

ALTER TABLE ONLY catalog.product_sla
    ADD CONSTRAINT product_sla_product_id_fkey FOREIGN KEY (product_id) REFERENCES catalog.product(id) ON DELETE CASCADE;

--
-- Name: institution_entitlement institution_entitlement_module_code_fkey; Type: FK CONSTRAINT; Schema: entitlements; Owner: -
--

ALTER TABLE ONLY entitlements.institution_entitlement
    ADD CONSTRAINT institution_entitlement_module_code_fkey FOREIGN KEY (module_code) REFERENCES entitlements.module(module_code);

--
-- Name: action action_institution_id_fkey; Type: FK CONSTRAINT; Schema: governance; Owner: -
--

ALTER TABLE ONLY governance.action
    ADD CONSTRAINT action_institution_id_fkey FOREIGN KEY (institution_id) REFERENCES institution.institution(id) ON DELETE CASCADE;

--
-- Name: four_eyes_policy four_eyes_policy_category_fkey; Type: FK CONSTRAINT; Schema: governance; Owner: -
--

ALTER TABLE ONLY governance.four_eyes_policy
    ADD CONSTRAINT four_eyes_policy_category_fkey FOREIGN KEY (category) REFERENCES governance.four_eyes_catalog(category);

--
-- Name: four_eyes_policy four_eyes_policy_institution_id_fkey; Type: FK CONSTRAINT; Schema: governance; Owner: -
--

ALTER TABLE ONLY governance.four_eyes_policy
    ADD CONSTRAINT four_eyes_policy_institution_id_fkey FOREIGN KEY (institution_id) REFERENCES institution.institution(id) ON DELETE CASCADE;

--
-- Name: four_eyes_policy four_eyes_policy_via_action_id_fkey; Type: FK CONSTRAINT; Schema: governance; Owner: -
--

ALTER TABLE ONLY governance.four_eyes_policy
    ADD CONSTRAINT four_eyes_policy_via_action_id_fkey FOREIGN KEY (via_action_id) REFERENCES governance.action(id);

--
-- Name: access_request access_request_institution_id_fkey; Type: FK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.access_request
    ADD CONSTRAINT access_request_institution_id_fkey FOREIGN KEY (institution_id) REFERENCES institution.institution(id) ON DELETE SET NULL;

--
-- Name: api_key api_key_revoked_by_fkey; Type: FK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.api_key
    ADD CONSTRAINT api_key_revoked_by_fkey FOREIGN KEY (revoked_by) REFERENCES institution.member(id) ON DELETE SET NULL;

--
-- Name: approval_action approval_action_stage_instance_id_fkey; Type: FK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.approval_action
    ADD CONSTRAINT approval_action_stage_instance_id_fkey FOREIGN KEY (stage_instance_id) REFERENCES institution.approval_stage_instance(id);

--
-- Name: approval_instance approval_instance_doa_rule_id_fkey; Type: FK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.approval_instance
    ADD CONSTRAINT approval_instance_doa_rule_id_fkey FOREIGN KEY (doa_rule_id) REFERENCES institution.doa_rule(id);

--
-- Name: approval_instance approval_instance_template_id_fkey; Type: FK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.approval_instance
    ADD CONSTRAINT approval_instance_template_id_fkey FOREIGN KEY (template_id) REFERENCES institution.approval_template(id);

--
-- Name: approval_stage_def approval_stage_def_committee_id_fkey; Type: FK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.approval_stage_def
    ADD CONSTRAINT approval_stage_def_committee_id_fkey FOREIGN KEY (committee_id) REFERENCES institution.approval_committee(id);

--
-- Name: approval_stage_def approval_stage_def_escalate_to_template_id_fkey; Type: FK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.approval_stage_def
    ADD CONSTRAINT approval_stage_def_escalate_to_template_id_fkey FOREIGN KEY (escalate_to_template_id) REFERENCES institution.approval_template(id);

--
-- Name: approval_stage_def approval_stage_def_template_id_fkey; Type: FK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.approval_stage_def
    ADD CONSTRAINT approval_stage_def_template_id_fkey FOREIGN KEY (template_id) REFERENCES institution.approval_template(id);

--
-- Name: approval_stage_instance approval_stage_instance_instance_id_fkey; Type: FK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.approval_stage_instance
    ADD CONSTRAINT approval_stage_instance_instance_id_fkey FOREIGN KEY (instance_id) REFERENCES institution.approval_instance(id);

--
-- Name: approval_stage_instance approval_stage_instance_stage_def_id_fkey; Type: FK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.approval_stage_instance
    ADD CONSTRAINT approval_stage_instance_stage_def_id_fkey FOREIGN KEY (stage_def_id) REFERENCES institution.approval_stage_def(id);

--
-- Name: benefit benefit_cat_id_fkey; Type: FK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.benefit
    ADD CONSTRAINT benefit_cat_id_fkey FOREIGN KEY (cat_id) REFERENCES institution.benefit_cat(id);

--
-- Name: benefit benefit_created_by_fkey; Type: FK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.benefit
    ADD CONSTRAINT benefit_created_by_fkey FOREIGN KEY (created_by) REFERENCES institution.member(id) ON DELETE SET NULL;

--
-- Name: benefit benefit_institution_id_fkey; Type: FK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.benefit
    ADD CONSTRAINT benefit_institution_id_fkey FOREIGN KEY (institution_id) REFERENCES institution.institution(id) ON DELETE CASCADE;

--
-- Name: benefit benefit_product_id_fkey; Type: FK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.benefit
    ADD CONSTRAINT benefit_product_id_fkey FOREIGN KEY (product_id) REFERENCES catalog.product(id) ON DELETE SET NULL;

--
-- Name: committee_member committee_member_committee_id_fkey; Type: FK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.committee_member
    ADD CONSTRAINT committee_member_committee_id_fkey FOREIGN KEY (committee_id) REFERENCES institution.approval_committee(id);

--
-- Name: compliance_waiver compliance_waiver_granted_by_fkey; Type: FK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.compliance_waiver
    ADD CONSTRAINT compliance_waiver_granted_by_fkey FOREIGN KEY (granted_by) REFERENCES auth_portal.auth_users(id);

--
-- Name: compliance_waiver compliance_waiver_institution_id_fkey; Type: FK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.compliance_waiver
    ADD CONSTRAINT compliance_waiver_institution_id_fkey FOREIGN KEY (institution_id) REFERENCES institution.institution(id) ON DELETE CASCADE;

--
-- Name: compliance_waiver compliance_waiver_revoked_by_fkey; Type: FK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.compliance_waiver
    ADD CONSTRAINT compliance_waiver_revoked_by_fkey FOREIGN KEY (revoked_by) REFERENCES auth_portal.auth_users(id);

--
-- Name: doa_rule doa_rule_template_id_fkey; Type: FK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.doa_rule
    ADD CONSTRAINT doa_rule_template_id_fkey FOREIGN KEY (template_id) REFERENCES institution.approval_template(id);

--
-- Name: doc doc_doc_type_id_fkey; Type: FK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.doc
    ADD CONSTRAINT doc_doc_type_id_fkey FOREIGN KEY (doc_type_id) REFERENCES institution.doc_type(id);

--
-- Name: doc_generation doc_generation_generated_by_fkey; Type: FK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.doc_generation
    ADD CONSTRAINT doc_generation_generated_by_fkey FOREIGN KEY (generated_by) REFERENCES institution.member(id);

--
-- Name: doc_generation doc_generation_institution_id_fkey; Type: FK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.doc_generation
    ADD CONSTRAINT doc_generation_institution_id_fkey FOREIGN KEY (institution_id) REFERENCES institution.institution(id);

--
-- Name: doc_generation doc_generation_stage_instance_id_fkey; Type: FK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.doc_generation
    ADD CONSTRAINT doc_generation_stage_instance_id_fkey FOREIGN KEY (stage_instance_id) REFERENCES marketplace.pipeline_stage_instance(id);

--
-- Name: doc_generation doc_generation_template_id_fkey; Type: FK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.doc_generation
    ADD CONSTRAINT doc_generation_template_id_fkey FOREIGN KEY (template_id) REFERENCES institution.doc_template(id);

--
-- Name: doc_generation doc_generation_template_version_id_fkey; Type: FK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.doc_generation
    ADD CONSTRAINT doc_generation_template_version_id_fkey FOREIGN KEY (template_version_id) REFERENCES institution.doc_template_version(id);

--
-- Name: doc doc_institution_id_fkey; Type: FK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.doc
    ADD CONSTRAINT doc_institution_id_fkey FOREIGN KEY (institution_id) REFERENCES institution.institution(id) ON DELETE CASCADE;

--
-- Name: doc_requirement doc_requirement_institution_id_fkey; Type: FK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.doc_requirement
    ADD CONSTRAINT doc_requirement_institution_id_fkey FOREIGN KEY (institution_id) REFERENCES institution.institution(id) ON DELETE CASCADE;

--
-- Name: doc_requirement doc_requirement_stage_def_id_fkey; Type: FK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.doc_requirement
    ADD CONSTRAINT doc_requirement_stage_def_id_fkey FOREIGN KEY (stage_def_id) REFERENCES institution.pipeline_stage_def(id) ON DELETE SET NULL;

--
-- Name: doc_requirement doc_requirement_template_id_fkey; Type: FK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.doc_requirement
    ADD CONSTRAINT doc_requirement_template_id_fkey FOREIGN KEY (template_id) REFERENCES institution.pipeline_template(id) ON DELETE CASCADE;

--
-- Name: doc doc_reviewed_by_fkey; Type: FK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.doc
    ADD CONSTRAINT doc_reviewed_by_fkey FOREIGN KEY (reviewed_by) REFERENCES portal_admin.admin_users(id) ON DELETE SET NULL;

--
-- Name: doc doc_reviewed_by_staff_fkey; Type: FK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.doc
    ADD CONSTRAINT doc_reviewed_by_staff_fkey FOREIGN KEY (reviewed_by_staff) REFERENCES auth_portal.auth_users(id);

--
-- Name: doc_template doc_template_created_by_fkey; Type: FK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.doc_template
    ADD CONSTRAINT doc_template_created_by_fkey FOREIGN KEY (created_by) REFERENCES institution.member(id);

--
-- Name: doc_template doc_template_institution_id_fkey; Type: FK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.doc_template
    ADD CONSTRAINT doc_template_institution_id_fkey FOREIGN KEY (institution_id) REFERENCES institution.institution(id);

--
-- Name: doc_template doc_template_product_id_fkey; Type: FK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.doc_template
    ADD CONSTRAINT doc_template_product_id_fkey FOREIGN KEY (product_id) REFERENCES catalog.product(id);

--
-- Name: doc_template_version doc_template_version_approved_by_fkey; Type: FK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.doc_template_version
    ADD CONSTRAINT doc_template_version_approved_by_fkey FOREIGN KEY (approved_by) REFERENCES institution.member(id);

--
-- Name: doc_template_version doc_template_version_created_by_fkey; Type: FK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.doc_template_version
    ADD CONSTRAINT doc_template_version_created_by_fkey FOREIGN KEY (created_by) REFERENCES institution.member(id);

--
-- Name: doc_template_version doc_template_version_institution_id_fkey; Type: FK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.doc_template_version
    ADD CONSTRAINT doc_template_version_institution_id_fkey FOREIGN KEY (institution_id) REFERENCES institution.institution(id);

--
-- Name: doc_template_version doc_template_version_template_id_fkey; Type: FK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.doc_template_version
    ADD CONSTRAINT doc_template_version_template_id_fkey FOREIGN KEY (template_id) REFERENCES institution.doc_template(id);

--
-- Name: doc doc_uploaded_by_fkey; Type: FK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.doc
    ADD CONSTRAINT doc_uploaded_by_fkey FOREIGN KEY (uploaded_by) REFERENCES institution.member(id) ON DELETE SET NULL;

--
-- Name: esign_envelope esign_envelope_approval_instance_id_fkey; Type: FK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.esign_envelope
    ADD CONSTRAINT esign_envelope_approval_instance_id_fkey FOREIGN KEY (approval_instance_id) REFERENCES institution.approval_instance(id);

--
-- Name: esign_event esign_event_envelope_id_fkey; Type: FK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.esign_event
    ADD CONSTRAINT esign_event_envelope_id_fkey FOREIGN KEY (envelope_id) REFERENCES institution.esign_envelope(id);

--
-- Name: esign_event esign_event_signer_id_fkey; Type: FK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.esign_event
    ADD CONSTRAINT esign_event_signer_id_fkey FOREIGN KEY (signer_id) REFERENCES institution.esign_signer(id);

--
-- Name: esign_signer esign_signer_envelope_id_fkey; Type: FK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.esign_signer
    ADD CONSTRAINT esign_signer_envelope_id_fkey FOREIGN KEY (envelope_id) REFERENCES institution.esign_envelope(id);

--
-- Name: group groups_created_by_fkey; Type: FK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution."group"
    ADD CONSTRAINT groups_created_by_fkey FOREIGN KEY (created_by) REFERENCES institution.member(id);

--
-- Name: group groups_institution_id_fkey; Type: FK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution."group"
    ADD CONSTRAINT groups_institution_id_fkey FOREIGN KEY (institution_id) REFERENCES institution.institution(id) ON DELETE CASCADE;

--
-- Name: api_key institution_api_keys_institution_id_fkey; Type: FK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.api_key
    ADD CONSTRAINT institution_api_keys_institution_id_fkey FOREIGN KEY (institution_id) REFERENCES institution.institution(id);

--
-- Name: institution institution_compliance_reviewed_by_fkey; Type: FK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.institution
    ADD CONSTRAINT institution_compliance_reviewed_by_fkey FOREIGN KEY (compliance_reviewed_by) REFERENCES auth_portal.auth_users(id) ON DELETE SET NULL;

--
-- Name: member institution_members_custom_group_id_fkey; Type: FK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.member
    ADD CONSTRAINT institution_members_custom_group_id_fkey FOREIGN KEY (custom_group_id) REFERENCES institution."group"(id) ON DELETE SET NULL;

--
-- Name: member institution_members_group_id_fkey; Type: FK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.member
    ADD CONSTRAINT institution_members_group_id_fkey FOREIGN KEY (group_id) REFERENCES portal_admin.user_groups(id);

--
-- Name: member institution_members_institution_id_fkey; Type: FK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.member
    ADD CONSTRAINT institution_members_institution_id_fkey FOREIGN KEY (institution_id) REFERENCES institution.institution(id) ON DELETE CASCADE;

--
-- Name: product_config institution_product_config_institution_id_fkey; Type: FK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.product_config
    ADD CONSTRAINT institution_product_config_institution_id_fkey FOREIGN KEY (institution_id) REFERENCES institution.institution(id);

--
-- Name: institution_sla_config institution_sla_config_institution_id_fkey; Type: FK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.institution_sla_config
    ADD CONSTRAINT institution_sla_config_institution_id_fkey FOREIGN KEY (institution_id) REFERENCES institution.institution(id);

--
-- Name: institution institution_suspended_by_fkey; Type: FK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.institution
    ADD CONSTRAINT institution_suspended_by_fkey FOREIGN KEY (suspended_by) REFERENCES auth_portal.auth_users(id) ON DELETE SET NULL;

--
-- Name: webhook institution_webhooks_institution_id_fkey; Type: FK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.webhook
    ADD CONSTRAINT institution_webhooks_institution_id_fkey FOREIGN KEY (institution_id) REFERENCES institution.institution(id);

--
-- Name: kyb_document kyb_document_institution_id_fkey; Type: FK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.kyb_document
    ADD CONSTRAINT kyb_document_institution_id_fkey FOREIGN KEY (institution_id) REFERENCES institution.institution(id) ON DELETE CASCADE;

--
-- Name: kyb_document kyb_document_reviewed_by_fkey; Type: FK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.kyb_document
    ADD CONSTRAINT kyb_document_reviewed_by_fkey FOREIGN KEY (reviewed_by) REFERENCES auth_portal.auth_users(id) ON DELETE SET NULL;

--
-- Name: kyb_document kyb_document_uploaded_by_fkey; Type: FK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.kyb_document
    ADD CONSTRAINT kyb_document_uploaded_by_fkey FOREIGN KEY (uploaded_by) REFERENCES institution.member(id);

--
-- Name: onboarding_pref onboarding_pref_institution_id_fkey; Type: FK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.onboarding_pref
    ADD CONSTRAINT onboarding_pref_institution_id_fkey FOREIGN KEY (institution_id) REFERENCES institution.institution(id) ON DELETE CASCADE;

--
-- Name: onboarding_pref onboarding_pref_submitted_by_fkey; Type: FK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.onboarding_pref
    ADD CONSTRAINT onboarding_pref_submitted_by_fkey FOREIGN KEY (submitted_by) REFERENCES auth_portal.auth_users(id) ON DELETE SET NULL;

--
-- Name: pending_actions pending_actions_checker_id_fkey; Type: FK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.pending_actions
    ADD CONSTRAINT pending_actions_checker_id_fkey FOREIGN KEY (checker_id) REFERENCES institution.member(id);

--
-- Name: pending_actions pending_actions_institution_id_fkey; Type: FK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.pending_actions
    ADD CONSTRAINT pending_actions_institution_id_fkey FOREIGN KEY (institution_id) REFERENCES institution.institution(id) ON DELETE CASCADE;

--
-- Name: pending_actions pending_actions_maker_id_fkey; Type: FK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.pending_actions
    ADD CONSTRAINT pending_actions_maker_id_fkey FOREIGN KEY (maker_id) REFERENCES institution.member(id);

--
-- Name: pipeline_stage_def pipeline_stage_def_template_id_fkey; Type: FK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.pipeline_stage_def
    ADD CONSTRAINT pipeline_stage_def_template_id_fkey FOREIGN KEY (template_id) REFERENCES institution.pipeline_template(id) ON DELETE CASCADE;

--
-- Name: pipeline_template pipeline_template_institution_id_fkey; Type: FK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.pipeline_template
    ADD CONSTRAINT pipeline_template_institution_id_fkey FOREIGN KEY (institution_id) REFERENCES institution.institution(id) ON DELETE CASCADE;

--
-- Name: product_config product_config_product_id_catalog_fkey; Type: FK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.product_config
    ADD CONSTRAINT product_config_product_id_catalog_fkey FOREIGN KEY (product_id) REFERENCES catalog.product(id) ON DELETE CASCADE;

--
-- Name: institution_sla_config sla_config_product_id_catalog_fkey; Type: FK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.institution_sla_config
    ADD CONSTRAINT sla_config_product_id_catalog_fkey FOREIGN KEY (product_id) REFERENCES catalog.product(id) ON DELETE CASCADE;

--
-- Name: webhook_delivery webhook_delivery_institution_id_fkey; Type: FK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.webhook_delivery
    ADD CONSTRAINT webhook_delivery_institution_id_fkey FOREIGN KEY (institution_id) REFERENCES institution.institution(id) ON DELETE CASCADE;

--
-- Name: webhook_delivery webhook_delivery_webhook_id_fkey; Type: FK CONSTRAINT; Schema: institution; Owner: -
--

ALTER TABLE ONLY institution.webhook_delivery
    ADD CONSTRAINT webhook_delivery_webhook_id_fkey FOREIGN KEY (webhook_id) REFERENCES institution.webhook(id) ON DELETE CASCADE;

--
-- Name: document_upload document_upload_deal_document_id_fkey; Type: FK CONSTRAINT; Schema: integration; Owner: -
--

ALTER TABLE ONLY integration.document_upload
    ADD CONSTRAINT document_upload_deal_document_id_fkey FOREIGN KEY (deal_document_id) REFERENCES marketplace.deal_document(id);

--
-- Name: document_upload document_upload_pipeline_id_fkey; Type: FK CONSTRAINT; Schema: integration; Owner: -
--

ALTER TABLE ONLY integration.document_upload
    ADD CONSTRAINT document_upload_pipeline_id_fkey FOREIGN KEY (pipeline_id) REFERENCES marketplace.loan_pipeline(id);

--
-- Name: acceptance acceptance_bid_id_fkey; Type: FK CONSTRAINT; Schema: marketplace; Owner: -
--

ALTER TABLE ONLY marketplace.acceptance
    ADD CONSTRAINT acceptance_bid_id_fkey FOREIGN KEY (bid_id) REFERENCES marketplace.bid(id);

--
-- Name: acceptance acceptance_request_id_fkey; Type: FK CONSTRAINT; Schema: marketplace; Owner: -
--

ALTER TABLE ONLY marketplace.acceptance
    ADD CONSTRAINT acceptance_request_id_fkey FOREIGN KEY (request_id) REFERENCES marketplace.request(id);

--
-- Name: bid_acceptance bid_acceptance_bid_id_fkey; Type: FK CONSTRAINT; Schema: marketplace; Owner: -
--

ALTER TABLE ONLY marketplace.bid_acceptance
    ADD CONSTRAINT bid_acceptance_bid_id_fkey FOREIGN KEY (bid_id) REFERENCES marketplace.bid(id);

--
-- Name: bid_acceptance bid_acceptance_request_id_fkey; Type: FK CONSTRAINT; Schema: marketplace; Owner: -
--

ALTER TABLE ONLY marketplace.bid_acceptance
    ADD CONSTRAINT bid_acceptance_request_id_fkey FOREIGN KEY (request_id) REFERENCES marketplace.request(id);

--
-- Name: bid_allocation bid_allocation_bid_id_fkey; Type: FK CONSTRAINT; Schema: marketplace; Owner: -
--

ALTER TABLE ONLY marketplace.bid_allocation
    ADD CONSTRAINT bid_allocation_bid_id_fkey FOREIGN KEY (bid_id) REFERENCES marketplace.bid(id) ON DELETE CASCADE;

--
-- Name: bid_allocation bid_allocation_product_id_fkey; Type: FK CONSTRAINT; Schema: marketplace; Owner: -
--

ALTER TABLE ONLY marketplace.bid_allocation
    ADD CONSTRAINT bid_allocation_product_id_fkey FOREIGN KEY (product_id) REFERENCES catalog.product(id);

--
-- Name: bid_benefit bid_benefit_benefit_id_fkey; Type: FK CONSTRAINT; Schema: marketplace; Owner: -
--

ALTER TABLE ONLY marketplace.bid_benefit
    ADD CONSTRAINT bid_benefit_benefit_id_fkey FOREIGN KEY (benefit_id) REFERENCES institution.benefit(id) ON DELETE RESTRICT;

--
-- Name: bid_benefit bid_benefit_bid_id_fkey; Type: FK CONSTRAINT; Schema: marketplace; Owner: -
--

ALTER TABLE ONLY marketplace.bid_benefit
    ADD CONSTRAINT bid_benefit_bid_id_fkey FOREIGN KEY (bid_id) REFERENCES marketplace.bid(id) ON DELETE CASCADE;

--
-- Name: bid_event bid_event_actor_id_fkey; Type: FK CONSTRAINT; Schema: marketplace; Owner: -
--

ALTER TABLE ONLY marketplace.bid_event
    ADD CONSTRAINT bid_event_actor_id_fkey FOREIGN KEY (actor_id) REFERENCES auth_portal.auth_users(id) ON DELETE SET NULL;

--
-- Name: bid_event bid_event_bid_id_fkey; Type: FK CONSTRAINT; Schema: marketplace; Owner: -
--

ALTER TABLE ONLY marketplace.bid_event
    ADD CONSTRAINT bid_event_bid_id_fkey FOREIGN KEY (bid_id) REFERENCES marketplace.bid(id) ON DELETE CASCADE;

--
-- Name: bid bid_institution_id_fkey; Type: FK CONSTRAINT; Schema: marketplace; Owner: -
--

ALTER TABLE ONLY marketplace.bid
    ADD CONSTRAINT bid_institution_id_fkey FOREIGN KEY (institution_id) REFERENCES institution.institution(id);

--
-- Name: bid bid_request_id_fkey; Type: FK CONSTRAINT; Schema: marketplace; Owner: -
--

ALTER TABLE ONLY marketplace.bid
    ADD CONSTRAINT bid_request_id_fkey FOREIGN KEY (request_id) REFERENCES marketplace.request(id) ON DELETE RESTRICT;

--
-- Name: bid bid_submitted_by_fkey; Type: FK CONSTRAINT; Schema: marketplace; Owner: -
--

ALTER TABLE ONLY marketplace.bid
    ADD CONSTRAINT bid_submitted_by_fkey FOREIGN KEY (submitted_by) REFERENCES institution.member(id) ON DELETE SET NULL;

--
-- Name: credit_decision credit_decision_approval_instance_id_fkey; Type: FK CONSTRAINT; Schema: marketplace; Owner: -
--

ALTER TABLE ONLY marketplace.credit_decision
    ADD CONSTRAINT credit_decision_approval_instance_id_fkey FOREIGN KEY (approval_instance_id) REFERENCES institution.approval_instance(id);

--
-- Name: credit_decision credit_decision_pipeline_id_fkey; Type: FK CONSTRAINT; Schema: marketplace; Owner: -
--

ALTER TABLE ONLY marketplace.credit_decision
    ADD CONSTRAINT credit_decision_pipeline_id_fkey FOREIGN KEY (pipeline_id) REFERENCES marketplace.loan_pipeline(id);

--
-- Name: credit_decision credit_decision_stage_instance_id_fkey; Type: FK CONSTRAINT; Schema: marketplace; Owner: -
--

ALTER TABLE ONLY marketplace.credit_decision
    ADD CONSTRAINT credit_decision_stage_instance_id_fkey FOREIGN KEY (stage_instance_id) REFERENCES marketplace.pipeline_stage_instance(id);

--
-- Name: deal_document_event deal_document_event_document_id_fkey; Type: FK CONSTRAINT; Schema: marketplace; Owner: -
--

ALTER TABLE ONLY marketplace.deal_document_event
    ADD CONSTRAINT deal_document_event_document_id_fkey FOREIGN KEY (document_id) REFERENCES marketplace.deal_document(id) ON DELETE CASCADE;

--
-- Name: deal_document deal_document_pipeline_id_fkey; Type: FK CONSTRAINT; Schema: marketplace; Owner: -
--

ALTER TABLE ONLY marketplace.deal_document
    ADD CONSTRAINT deal_document_pipeline_id_fkey FOREIGN KEY (pipeline_id) REFERENCES marketplace.loan_pipeline(id) ON DELETE CASCADE;

--
-- Name: deal_document deal_document_requirement_id_fkey; Type: FK CONSTRAINT; Schema: marketplace; Owner: -
--

ALTER TABLE ONLY marketplace.deal_document
    ADD CONSTRAINT deal_document_requirement_id_fkey FOREIGN KEY (requirement_id) REFERENCES institution.doc_requirement(id) ON DELETE RESTRICT;

--
-- Name: loan_pipeline fk_current_stage; Type: FK CONSTRAINT; Schema: marketplace; Owner: -
--

ALTER TABLE ONLY marketplace.loan_pipeline
    ADD CONSTRAINT fk_current_stage FOREIGN KEY (current_stage_id) REFERENCES marketplace.pipeline_stage_instance(id) DEFERRABLE INITIALLY DEFERRED;

--
-- Name: loan_booking loan_booking_pipeline_id_fkey; Type: FK CONSTRAINT; Schema: marketplace; Owner: -
--

ALTER TABLE ONLY marketplace.loan_booking
    ADD CONSTRAINT loan_booking_pipeline_id_fkey FOREIGN KEY (pipeline_id) REFERENCES marketplace.loan_pipeline(id) ON DELETE RESTRICT;

--
-- Name: loan_pipeline loan_pipeline_bid_id_fkey; Type: FK CONSTRAINT; Schema: marketplace; Owner: -
--

ALTER TABLE ONLY marketplace.loan_pipeline
    ADD CONSTRAINT loan_pipeline_bid_id_fkey FOREIGN KEY (bid_id) REFERENCES marketplace.bid(id);

--
-- Name: loan_pipeline loan_pipeline_request_id_fkey; Type: FK CONSTRAINT; Schema: marketplace; Owner: -
--

ALTER TABLE ONLY marketplace.loan_pipeline
    ADD CONSTRAINT loan_pipeline_request_id_fkey FOREIGN KEY (request_id) REFERENCES marketplace.request(id);

--
-- Name: loan_pipeline loan_pipeline_template_id_fkey; Type: FK CONSTRAINT; Schema: marketplace; Owner: -
--

ALTER TABLE ONLY marketplace.loan_pipeline
    ADD CONSTRAINT loan_pipeline_template_id_fkey FOREIGN KEY (template_id) REFERENCES institution.pipeline_template(id);

--
-- Name: pipeline_stage_instance pipeline_stage_instance_approval_instance_id_fkey; Type: FK CONSTRAINT; Schema: marketplace; Owner: -
--

ALTER TABLE ONLY marketplace.pipeline_stage_instance
    ADD CONSTRAINT pipeline_stage_instance_approval_instance_id_fkey FOREIGN KEY (approval_instance_id) REFERENCES institution.approval_instance(id);

--
-- Name: pipeline_stage_instance pipeline_stage_instance_pipeline_id_fkey; Type: FK CONSTRAINT; Schema: marketplace; Owner: -
--

ALTER TABLE ONLY marketplace.pipeline_stage_instance
    ADD CONSTRAINT pipeline_stage_instance_pipeline_id_fkey FOREIGN KEY (pipeline_id) REFERENCES marketplace.loan_pipeline(id) ON DELETE CASCADE;

--
-- Name: pipeline_stage_instance pipeline_stage_instance_stage_def_id_fkey; Type: FK CONSTRAINT; Schema: marketplace; Owner: -
--

ALTER TABLE ONLY marketplace.pipeline_stage_instance
    ADD CONSTRAINT pipeline_stage_instance_stage_def_id_fkey FOREIGN KEY (stage_def_id) REFERENCES institution.pipeline_stage_def(id);

--
-- Name: request_allocation request_allocation_product_id_fkey; Type: FK CONSTRAINT; Schema: marketplace; Owner: -
--

ALTER TABLE ONLY marketplace.request_allocation
    ADD CONSTRAINT request_allocation_product_id_fkey FOREIGN KEY (product_id) REFERENCES catalog.product(id);

--
-- Name: request_allocation request_allocation_request_id_fkey; Type: FK CONSTRAINT; Schema: marketplace; Owner: -
--

ALTER TABLE ONLY marketplace.request_allocation
    ADD CONSTRAINT request_allocation_request_id_fkey FOREIGN KEY (request_id) REFERENCES marketplace.request(id) ON DELETE CASCADE;

--
-- Name: request request_country_fkey; Type: FK CONSTRAINT; Schema: marketplace; Owner: -
--

ALTER TABLE ONLY marketplace.request
    ADD CONSTRAINT request_country_fkey FOREIGN KEY (country) REFERENCES catalog.country(code);

--
-- Name: request request_currency_fkey; Type: FK CONSTRAINT; Schema: marketplace; Owner: -
--

ALTER TABLE ONLY marketplace.request
    ADD CONSTRAINT request_currency_fkey FOREIGN KEY (currency) REFERENCES catalog.currency(code);

--
-- Name: request_decline request_decline_request_id_fkey; Type: FK CONSTRAINT; Schema: marketplace; Owner: -
--

ALTER TABLE ONLY marketplace.request_decline
    ADD CONSTRAINT request_decline_request_id_fkey FOREIGN KEY (request_id) REFERENCES marketplace.request(id);

--
-- Name: request_message request_message_request_id_fkey; Type: FK CONSTRAINT; Schema: marketplace; Owner: -
--

ALTER TABLE ONLY marketplace.request_message
    ADD CONSTRAINT request_message_request_id_fkey FOREIGN KEY (request_id) REFERENCES marketplace.request(id);

--
-- Name: request request_product_id_fkey; Type: FK CONSTRAINT; Schema: marketplace; Owner: -
--

ALTER TABLE ONLY marketplace.request
    ADD CONSTRAINT request_product_id_fkey FOREIGN KEY (product_id) REFERENCES catalog.product(id);

--
-- Name: request request_relisted_from_fkey; Type: FK CONSTRAINT; Schema: marketplace; Owner: -
--

ALTER TABLE ONLY marketplace.request
    ADD CONSTRAINT request_relisted_from_fkey FOREIGN KEY (relisted_from) REFERENCES marketplace.request(id);

--
-- Name: request request_winning_bid_fk; Type: FK CONSTRAINT; Schema: marketplace; Owner: -
--

ALTER TABLE ONLY marketplace.request
    ADD CONSTRAINT request_winning_bid_fk FOREIGN KEY (winning_bid_id) REFERENCES marketplace.bid(id) ON DELETE SET NULL;

--
-- Name: admin_audit_log admin_audit_log_dual_control_id_fkey; Type: FK CONSTRAINT; Schema: portal_admin; Owner: -
--

ALTER TABLE ONLY portal_admin.admin_audit_log
    ADD CONSTRAINT admin_audit_log_dual_control_id_fkey FOREIGN KEY (dual_control_id) REFERENCES portal_admin.admin_dual_control_actions(id);

--
-- Name: admin_audit_log admin_audit_log_session_id_fkey; Type: FK CONSTRAINT; Schema: portal_admin; Owner: -
--

ALTER TABLE ONLY portal_admin.admin_audit_log
    ADD CONSTRAINT admin_audit_log_session_id_fkey FOREIGN KEY (session_id) REFERENCES portal_admin.admin_sessions(id);

--
-- Name: admin_dual_control_actions admin_dual_control_actions_checker_id_fkey; Type: FK CONSTRAINT; Schema: portal_admin; Owner: -
--

ALTER TABLE ONLY portal_admin.admin_dual_control_actions
    ADD CONSTRAINT admin_dual_control_actions_checker_id_fkey FOREIGN KEY (checker_id) REFERENCES portal_admin.admin_users(id);

--
-- Name: admin_dual_control_actions admin_dual_control_actions_maker_id_fkey; Type: FK CONSTRAINT; Schema: portal_admin; Owner: -
--

ALTER TABLE ONLY portal_admin.admin_dual_control_actions
    ADD CONSTRAINT admin_dual_control_actions_maker_id_fkey FOREIGN KEY (maker_id) REFERENCES portal_admin.admin_users(id);

--
-- Name: admin_sessions admin_sessions_admin_user_id_fkey; Type: FK CONSTRAINT; Schema: portal_admin; Owner: -
--

ALTER TABLE ONLY portal_admin.admin_sessions
    ADD CONSTRAINT admin_sessions_admin_user_id_fkey FOREIGN KEY (admin_user_id) REFERENCES portal_admin.admin_users(id) ON DELETE CASCADE;

--
-- Name: admin_users admin_users_custom_role_id_fkey; Type: FK CONSTRAINT; Schema: portal_admin; Owner: -
--

ALTER TABLE ONLY portal_admin.admin_users
    ADD CONSTRAINT admin_users_custom_role_id_fkey FOREIGN KEY (custom_role_id) REFERENCES portal_admin.admin_roles(id);

--
-- Name: admin_users admin_users_group_id_fkey; Type: FK CONSTRAINT; Schema: portal_admin; Owner: -
--

ALTER TABLE ONLY portal_admin.admin_users
    ADD CONSTRAINT admin_users_group_id_fkey FOREIGN KEY (group_id) REFERENCES portal_admin.user_groups(id);

--
-- Name: admin_users admin_users_suspended_by_fkey; Type: FK CONSTRAINT; Schema: portal_admin; Owner: -
--

ALTER TABLE ONLY portal_admin.admin_users
    ADD CONSTRAINT admin_users_suspended_by_fkey FOREIGN KEY (suspended_by) REFERENCES portal_admin.admin_users(id);

--
-- Name: commission_event commission_event_pipeline_id_fkey; Type: FK CONSTRAINT; Schema: portal_admin; Owner: -
--

ALTER TABLE ONLY portal_admin.commission_event
    ADD CONSTRAINT commission_event_pipeline_id_fkey FOREIGN KEY (pipeline_id) REFERENCES marketplace.loan_pipeline(id);

--
-- Name: commission_event commission_event_rule_id_fkey; Type: FK CONSTRAINT; Schema: portal_admin; Owner: -
--

ALTER TABLE ONLY portal_admin.commission_event
    ADD CONSTRAINT commission_event_rule_id_fkey FOREIGN KEY (rule_id) REFERENCES billing.commission_rule(id);

--
-- Name: portal_notifications portal_notifications_institution_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_notifications
    ADD CONSTRAINT portal_notifications_institution_id_fkey FOREIGN KEY (institution_id) REFERENCES institution.institution(id) ON DELETE CASCADE;

--
-- Name: event audit_admin_select; Type: POLICY; Schema: audit; Owner: -
--

CREATE POLICY audit_admin_select ON audit.event FOR SELECT TO authenticated USING (portal_admin.is_admin());

--
-- Name: event audit_insert; Type: POLICY; Schema: audit; Owner: -
--

CREATE POLICY audit_insert ON audit.event FOR INSERT TO authenticated WITH CHECK (((( SELECT auth.role() AS role) = 'service_role'::text) OR portal_admin.is_admin() OR (institution_id = ( SELECT ctx.institution_id
   FROM institution.current_member_ctx() ctx(member_id, institution_id, is_admin, member_role, modules)))));

--
-- Name: event audit_inst_select; Type: POLICY; Schema: audit; Owner: -
--

CREATE POLICY audit_inst_select ON audit.event FOR SELECT TO authenticated USING ((institution_id = ( SELECT ctx.institution_id
   FROM institution.current_member_ctx() ctx(member_id, institution_id, is_admin, member_role, modules))));

--
-- Name: event; Type: ROW SECURITY; Schema: audit; Owner: -
--

ALTER TABLE audit.event ENABLE ROW LEVEL SECURITY;

--
-- Name: auth_audit_events; Type: ROW SECURITY; Schema: auth_portal; Owner: -
--

ALTER TABLE auth_portal.auth_audit_events ENABLE ROW LEVEL SECURITY;

--
-- Name: auth_sessions; Type: ROW SECURITY; Schema: auth_portal; Owner: -
--

ALTER TABLE auth_portal.auth_sessions ENABLE ROW LEVEL SECURITY;

--
-- Name: auth_users; Type: ROW SECURITY; Schema: auth_portal; Owner: -
--

ALTER TABLE auth_portal.auth_users ENABLE ROW LEVEL SECURITY;

--
-- Name: email_verification_tokens; Type: ROW SECURITY; Schema: auth_portal; Owner: -
--

ALTER TABLE auth_portal.email_verification_tokens ENABLE ROW LEVEL SECURITY;

--
-- Name: ip_allowlist; Type: ROW SECURITY; Schema: auth_portal; Owner: -
--

ALTER TABLE auth_portal.ip_allowlist ENABLE ROW LEVEL SECURITY;

--
-- Name: mfa_backup_codes; Type: ROW SECURITY; Schema: auth_portal; Owner: -
--

ALTER TABLE auth_portal.mfa_backup_codes ENABLE ROW LEVEL SECURITY;

--
-- Name: password_reset_tokens; Type: ROW SECURITY; Schema: auth_portal; Owner: -
--

ALTER TABLE auth_portal.password_reset_tokens ENABLE ROW LEVEL SECURITY;

--
-- Name: execution ab_exec_tenant_read; Type: POLICY; Schema: autobid; Owner: -
--

CREATE POLICY ab_exec_tenant_read ON autobid.execution FOR SELECT TO authenticated USING ((institution_id = ( SELECT ctx.institution_id
   FROM institution.current_member_ctx_v2() ctx(member_id, institution_id, is_admin, member_role, modules, products))));

--
-- Name: exposure ab_exposure_tenant_read; Type: POLICY; Schema: autobid; Owner: -
--

CREATE POLICY ab_exposure_tenant_read ON autobid.exposure FOR SELECT TO authenticated USING ((institution_id = ( SELECT ctx.institution_id
   FROM institution.current_member_ctx_v2() ctx(member_id, institution_id, is_admin, member_role, modules, products))));

--
-- Name: rule ab_rule_tenant; Type: POLICY; Schema: autobid; Owner: -
--

CREATE POLICY ab_rule_tenant ON autobid.rule TO authenticated USING ((institution_id = ( SELECT ctx.institution_id
   FROM institution.current_member_ctx_v2() ctx(member_id, institution_id, is_admin, member_role, modules, products)))) WITH CHECK ((institution_id = ( SELECT ctx.institution_id
   FROM institution.current_member_ctx_v2() ctx(member_id, institution_id, is_admin, member_role, modules, products))));

--
-- Name: rule_version ab_rv_tenant; Type: POLICY; Schema: autobid; Owner: -
--

CREATE POLICY ab_rv_tenant ON autobid.rule_version TO authenticated USING ((institution_id = ( SELECT ctx.institution_id
   FROM institution.current_member_ctx_v2() ctx(member_id, institution_id, is_admin, member_role, modules, products)))) WITH CHECK ((institution_id = ( SELECT ctx.institution_id
   FROM institution.current_member_ctx_v2() ctx(member_id, institution_id, is_admin, member_role, modules, products))));

--
-- Name: execution; Type: ROW SECURITY; Schema: autobid; Owner: -
--

ALTER TABLE autobid.execution ENABLE ROW LEVEL SECURITY;

--
-- Name: execution_default; Type: ROW SECURITY; Schema: autobid; Owner: -
--

ALTER TABLE autobid.execution_default ENABLE ROW LEVEL SECURITY;

--
-- Name: exposure; Type: ROW SECURITY; Schema: autobid; Owner: -
--

ALTER TABLE autobid.exposure ENABLE ROW LEVEL SECURITY;

--
-- Name: rule; Type: ROW SECURITY; Schema: autobid; Owner: -
--

ALTER TABLE autobid.rule ENABLE ROW LEVEL SECURITY;

--
-- Name: rule_version; Type: ROW SECURITY; Schema: autobid; Owner: -
--

ALTER TABLE autobid.rule_version ENABLE ROW LEVEL SECURITY;

--
-- Name: commission_adjustment; Type: ROW SECURITY; Schema: billing; Owner: -
--

ALTER TABLE billing.commission_adjustment ENABLE ROW LEVEL SECURITY;

--
-- Name: commission_rule; Type: ROW SECURITY; Schema: billing; Owner: -
--

ALTER TABLE billing.commission_rule ENABLE ROW LEVEL SECURITY;

--
-- Name: country catalog_read_all; Type: POLICY; Schema: catalog; Owner: -
--

CREATE POLICY catalog_read_all ON catalog.country FOR SELECT TO authenticated USING (true);

--
-- Name: currency catalog_read_all; Type: POLICY; Schema: catalog; Owner: -
--

CREATE POLICY catalog_read_all ON catalog.currency FOR SELECT TO authenticated USING (true);

--
-- Name: module catalog_read_all; Type: POLICY; Schema: catalog; Owner: -
--

CREATE POLICY catalog_read_all ON catalog.module FOR SELECT TO authenticated USING (true);

--
-- Name: product catalog_read_all; Type: POLICY; Schema: catalog; Owner: -
--

CREATE POLICY catalog_read_all ON catalog.product FOR SELECT TO authenticated USING (true);

--
-- Name: product_document catalog_read_all; Type: POLICY; Schema: catalog; Owner: -
--

CREATE POLICY catalog_read_all ON catalog.product_document FOR SELECT TO authenticated USING (true);

--
-- Name: product_eligibility catalog_read_all; Type: POLICY; Schema: catalog; Owner: -
--

CREATE POLICY catalog_read_all ON catalog.product_eligibility FOR SELECT TO authenticated USING (true);

--
-- Name: product_family catalog_read_all; Type: POLICY; Schema: catalog; Owner: -
--

CREATE POLICY catalog_read_all ON catalog.product_family FOR SELECT TO authenticated USING (true);

--
-- Name: product_parameter catalog_read_all; Type: POLICY; Schema: catalog; Owner: -
--

CREATE POLICY catalog_read_all ON catalog.product_parameter FOR SELECT TO authenticated USING (true);

--
-- Name: product_rate_model catalog_read_all; Type: POLICY; Schema: catalog; Owner: -
--

CREATE POLICY catalog_read_all ON catalog.product_rate_model FOR SELECT TO authenticated USING (true);

--
-- Name: product_sla catalog_read_all; Type: POLICY; Schema: catalog; Owner: -
--

CREATE POLICY catalog_read_all ON catalog.product_sla FOR SELECT TO authenticated USING (true);

--
-- Name: regulator catalog_read_all; Type: POLICY; Schema: catalog; Owner: -
--

CREATE POLICY catalog_read_all ON catalog.regulator FOR SELECT TO authenticated USING (true);

--
-- Name: country; Type: ROW SECURITY; Schema: catalog; Owner: -
--

ALTER TABLE catalog.country ENABLE ROW LEVEL SECURITY;

--
-- Name: currency; Type: ROW SECURITY; Schema: catalog; Owner: -
--

ALTER TABLE catalog.currency ENABLE ROW LEVEL SECURITY;

--
-- Name: module; Type: ROW SECURITY; Schema: catalog; Owner: -
--

ALTER TABLE catalog.module ENABLE ROW LEVEL SECURITY;

--
-- Name: product; Type: ROW SECURITY; Schema: catalog; Owner: -
--

ALTER TABLE catalog.product ENABLE ROW LEVEL SECURITY;

--
-- Name: product_document; Type: ROW SECURITY; Schema: catalog; Owner: -
--

ALTER TABLE catalog.product_document ENABLE ROW LEVEL SECURITY;

--
-- Name: product_eligibility; Type: ROW SECURITY; Schema: catalog; Owner: -
--

ALTER TABLE catalog.product_eligibility ENABLE ROW LEVEL SECURITY;

--
-- Name: product_family; Type: ROW SECURITY; Schema: catalog; Owner: -
--

ALTER TABLE catalog.product_family ENABLE ROW LEVEL SECURITY;

--
-- Name: product_parameter; Type: ROW SECURITY; Schema: catalog; Owner: -
--

ALTER TABLE catalog.product_parameter ENABLE ROW LEVEL SECURITY;

--
-- Name: product_rate_model; Type: ROW SECURITY; Schema: catalog; Owner: -
--

ALTER TABLE catalog.product_rate_model ENABLE ROW LEVEL SECURITY;

--
-- Name: product_sla; Type: ROW SECURITY; Schema: catalog; Owner: -
--

ALTER TABLE catalog.product_sla ENABLE ROW LEVEL SECURITY;

--
-- Name: regulator; Type: ROW SECURITY; Schema: catalog; Owner: -
--

ALTER TABLE catalog.regulator ENABLE ROW LEVEL SECURITY;

--
-- Name: institution_entitlement ie_tenant_read; Type: POLICY; Schema: entitlements; Owner: -
--

CREATE POLICY ie_tenant_read ON entitlements.institution_entitlement FOR SELECT TO authenticated USING ((institution_id = ( SELECT ctx.institution_id
   FROM institution.current_member_ctx_v2() ctx(member_id, institution_id, is_admin, member_role, modules, products))));

--
-- Name: institution_entitlement; Type: ROW SECURITY; Schema: entitlements; Owner: -
--

ALTER TABLE entitlements.institution_entitlement ENABLE ROW LEVEL SECURITY;

--
-- Name: module; Type: ROW SECURITY; Schema: entitlements; Owner: -
--

ALTER TABLE entitlements.module ENABLE ROW LEVEL SECURITY;

--
-- Name: module module_read_all; Type: POLICY; Schema: entitlements; Owner: -
--

CREATE POLICY module_read_all ON entitlements.module FOR SELECT USING (true);

--
-- Name: usage_event ue_tenant_read; Type: POLICY; Schema: entitlements; Owner: -
--

CREATE POLICY ue_tenant_read ON entitlements.usage_event FOR SELECT TO authenticated USING ((institution_id = ( SELECT ctx.institution_id
   FROM institution.current_member_ctx_v2() ctx(member_id, institution_id, is_admin, member_role, modules, products))));

--
-- Name: usage_event; Type: ROW SECURITY; Schema: entitlements; Owner: -
--

ALTER TABLE entitlements.usage_event ENABLE ROW LEVEL SECURITY;

--
-- Name: usage_event_default; Type: ROW SECURITY; Schema: entitlements; Owner: -
--

ALTER TABLE entitlements.usage_event_default ENABLE ROW LEVEL SECURITY;

--
-- Name: action; Type: ROW SECURITY; Schema: governance; Owner: -
--

ALTER TABLE governance.action ENABLE ROW LEVEL SECURITY;

--
-- Name: four_eyes_catalog; Type: ROW SECURITY; Schema: governance; Owner: -
--

ALTER TABLE governance.four_eyes_catalog ENABLE ROW LEVEL SECURITY;

--
-- Name: four_eyes_catalog four_eyes_catalog_read; Type: POLICY; Schema: governance; Owner: -
--

CREATE POLICY four_eyes_catalog_read ON governance.four_eyes_catalog FOR SELECT TO authenticated USING (true);

--
-- Name: four_eyes_policy; Type: ROW SECURITY; Schema: governance; Owner: -
--

ALTER TABLE governance.four_eyes_policy ENABLE ROW LEVEL SECURITY;

--
-- Name: four_eyes_policy four_eyes_policy_read; Type: POLICY; Schema: governance; Owner: -
--

CREATE POLICY four_eyes_policy_read ON governance.four_eyes_policy FOR SELECT TO authenticated USING ((institution_id = ( SELECT ctx.institution_id
   FROM institution.current_member_ctx() ctx(member_id, institution_id, is_admin, member_role, modules))));

--
-- Name: action governance_inst_select; Type: POLICY; Schema: governance; Owner: -
--

CREATE POLICY governance_inst_select ON governance.action FOR SELECT TO authenticated USING (((scope = 'institution'::text) AND (institution_id = ( SELECT ctx.institution_id
   FROM institution.current_member_ctx() ctx(member_id, institution_id, is_admin, member_role, modules)))));

--
-- Name: action governance_inst_update; Type: POLICY; Schema: governance; Owner: -
--

CREATE POLICY governance_inst_update ON governance.action FOR UPDATE TO authenticated USING (((scope = 'institution'::text) AND (institution_id = ( SELECT ctx.institution_id
   FROM institution.current_member_ctx() ctx(member_id, institution_id, is_admin, member_role, modules))) AND ( SELECT ctx.is_admin
   FROM institution.current_member_ctx() ctx(member_id, institution_id, is_admin, member_role, modules))));

--
-- Name: action governance_platform_select; Type: POLICY; Schema: governance; Owner: -
--

CREATE POLICY governance_platform_select ON governance.action FOR SELECT TO authenticated USING (((scope = 'platform'::text) AND portal_admin.is_admin()));

--
-- Name: action governance_platform_update; Type: POLICY; Schema: governance; Owner: -
--

CREATE POLICY governance_platform_update ON governance.action FOR UPDATE TO authenticated USING (((scope = 'platform'::text) AND portal_admin.has_permission('dual_control:approve'::text)));

--
-- Name: approval_action aa_tenant; Type: POLICY; Schema: institution; Owner: -
--

CREATE POLICY aa_tenant ON institution.approval_action TO authenticated USING ((stage_instance_id IN ( SELECT si.id
   FROM (institution.approval_stage_instance si
     JOIN institution.approval_instance i ON ((i.id = si.instance_id)))
  WHERE (i.institution_id = ( SELECT ctx.institution_id
           FROM institution.current_member_ctx_v2() ctx(member_id, institution_id, is_admin, member_role, modules, products))))));

--
-- Name: access_request; Type: ROW SECURITY; Schema: institution; Owner: -
--

ALTER TABLE institution.access_request ENABLE ROW LEVEL SECURITY;

--
-- Name: approval_delegation ad_tenant; Type: POLICY; Schema: institution; Owner: -
--

CREATE POLICY ad_tenant ON institution.approval_delegation TO authenticated USING ((institution_id = ( SELECT ctx.institution_id
   FROM institution.current_member_ctx_v2() ctx(member_id, institution_id, is_admin, member_role, modules, products))));

--
-- Name: approval_instance ai_tenant; Type: POLICY; Schema: institution; Owner: -
--

CREATE POLICY ai_tenant ON institution.approval_instance TO authenticated USING ((institution_id = ( SELECT ctx.institution_id
   FROM institution.current_member_ctx_v2() ctx(member_id, institution_id, is_admin, member_role, modules, products))));

--
-- Name: approval_outbox ao_tenant; Type: POLICY; Schema: institution; Owner: -
--

CREATE POLICY ao_tenant ON institution.approval_outbox TO authenticated USING ((institution_id = ( SELECT ctx.institution_id
   FROM institution.current_member_ctx_v2() ctx(member_id, institution_id, is_admin, member_role, modules, products))));

--
-- Name: approval_committee apc_tenant; Type: POLICY; Schema: institution; Owner: -
--

CREATE POLICY apc_tenant ON institution.approval_committee TO authenticated USING ((institution_id = ( SELECT ctx.institution_id
   FROM institution.current_member_ctx_v2() ctx(member_id, institution_id, is_admin, member_role, modules, products))));

--
-- Name: api_key; Type: ROW SECURITY; Schema: institution; Owner: -
--

ALTER TABLE institution.api_key ENABLE ROW LEVEL SECURITY;

--
-- Name: api_key api_key_delete; Type: POLICY; Schema: institution; Owner: -
--

CREATE POLICY api_key_delete ON institution.api_key FOR DELETE TO authenticated USING ((institution_id = ( SELECT ctx.institution_id
   FROM institution.current_member_ctx() ctx(member_id, institution_id, is_admin, member_role, modules))));

--
-- Name: api_key api_key_insert; Type: POLICY; Schema: institution; Owner: -
--

CREATE POLICY api_key_insert ON institution.api_key FOR INSERT TO authenticated WITH CHECK ((institution_id = ( SELECT ctx.institution_id
   FROM institution.current_member_ctx() ctx(member_id, institution_id, is_admin, member_role, modules))));

--
-- Name: api_key api_key_select; Type: POLICY; Schema: institution; Owner: -
--

CREATE POLICY api_key_select ON institution.api_key FOR SELECT TO authenticated USING ((institution_id = ( SELECT ctx.institution_id
   FROM institution.current_member_ctx() ctx(member_id, institution_id, is_admin, member_role, modules))));

--
-- Name: api_key api_key_update; Type: POLICY; Schema: institution; Owner: -
--

CREATE POLICY api_key_update ON institution.api_key FOR UPDATE TO authenticated USING ((institution_id = ( SELECT ctx.institution_id
   FROM institution.current_member_ctx() ctx(member_id, institution_id, is_admin, member_role, modules))));

--
-- Name: approval_action; Type: ROW SECURITY; Schema: institution; Owner: -
--

ALTER TABLE institution.approval_action ENABLE ROW LEVEL SECURITY;

--
-- Name: approval_committee; Type: ROW SECURITY; Schema: institution; Owner: -
--

ALTER TABLE institution.approval_committee ENABLE ROW LEVEL SECURITY;

--
-- Name: approval_delegation; Type: ROW SECURITY; Schema: institution; Owner: -
--

ALTER TABLE institution.approval_delegation ENABLE ROW LEVEL SECURITY;

--
-- Name: approval_instance; Type: ROW SECURITY; Schema: institution; Owner: -
--

ALTER TABLE institution.approval_instance ENABLE ROW LEVEL SECURITY;

--
-- Name: approval_outbox; Type: ROW SECURITY; Schema: institution; Owner: -
--

ALTER TABLE institution.approval_outbox ENABLE ROW LEVEL SECURITY;

--
-- Name: approval_stage_def; Type: ROW SECURITY; Schema: institution; Owner: -
--

ALTER TABLE institution.approval_stage_def ENABLE ROW LEVEL SECURITY;

--
-- Name: approval_stage_instance; Type: ROW SECURITY; Schema: institution; Owner: -
--

ALTER TABLE institution.approval_stage_instance ENABLE ROW LEVEL SECURITY;

--
-- Name: approval_template; Type: ROW SECURITY; Schema: institution; Owner: -
--

ALTER TABLE institution.approval_template ENABLE ROW LEVEL SECURITY;

--
-- Name: approval_stage_def asd_tenant; Type: POLICY; Schema: institution; Owner: -
--

CREATE POLICY asd_tenant ON institution.approval_stage_def TO authenticated USING ((template_id IN ( SELECT approval_template.id
   FROM institution.approval_template
  WHERE (approval_template.institution_id = ( SELECT ctx.institution_id
           FROM institution.current_member_ctx_v2() ctx(member_id, institution_id, is_admin, member_role, modules, products))))));

--
-- Name: approval_stage_instance asi_tenant; Type: POLICY; Schema: institution; Owner: -
--

CREATE POLICY asi_tenant ON institution.approval_stage_instance TO authenticated USING ((instance_id IN ( SELECT approval_instance.id
   FROM institution.approval_instance
  WHERE (approval_instance.institution_id = ( SELECT ctx.institution_id
           FROM institution.current_member_ctx_v2() ctx(member_id, institution_id, is_admin, member_role, modules, products))))));

--
-- Name: approval_template at_tenant; Type: POLICY; Schema: institution; Owner: -
--

CREATE POLICY at_tenant ON institution.approval_template TO authenticated USING ((institution_id = ( SELECT ctx.institution_id
   FROM institution.current_member_ctx_v2() ctx(member_id, institution_id, is_admin, member_role, modules, products))));

--
-- Name: benefit; Type: ROW SECURITY; Schema: institution; Owner: -
--

ALTER TABLE institution.benefit ENABLE ROW LEVEL SECURITY;

--
-- Name: benefit_cat; Type: ROW SECURITY; Schema: institution; Owner: -
--

ALTER TABLE institution.benefit_cat ENABLE ROW LEVEL SECURITY;

--
-- Name: benefit_cat benefit_cat_select_all; Type: POLICY; Schema: institution; Owner: -
--

CREATE POLICY benefit_cat_select_all ON institution.benefit_cat FOR SELECT USING (true);

--
-- Name: benefit benefit_delete_own; Type: POLICY; Schema: institution; Owner: -
--

CREATE POLICY benefit_delete_own ON institution.benefit FOR DELETE TO authenticated USING ((institution_id = (((( SELECT current_setting('request.jwt.claims'::text, true) AS current_setting))::jsonb ->> 'institution_id'::text))::uuid));

--
-- Name: benefit benefit_select_own; Type: POLICY; Schema: institution; Owner: -
--

CREATE POLICY benefit_select_own ON institution.benefit FOR SELECT TO authenticated USING ((institution_id = (((( SELECT current_setting('request.jwt.claims'::text, true) AS current_setting))::jsonb ->> 'institution_id'::text))::uuid));

--
-- Name: committee_member cm_tenant; Type: POLICY; Schema: institution; Owner: -
--

CREATE POLICY cm_tenant ON institution.committee_member TO authenticated USING ((committee_id IN ( SELECT approval_committee.id
   FROM institution.approval_committee
  WHERE (approval_committee.institution_id = ( SELECT ctx.institution_id
           FROM institution.current_member_ctx_v2() ctx(member_id, institution_id, is_admin, member_role, modules, products))))));

--
-- Name: committee_member; Type: ROW SECURITY; Schema: institution; Owner: -
--

ALTER TABLE institution.committee_member ENABLE ROW LEVEL SECURITY;

--
-- Name: compliance_waiver; Type: ROW SECURITY; Schema: institution; Owner: -
--

ALTER TABLE institution.compliance_waiver ENABLE ROW LEVEL SECURITY;

--
-- Name: doc_generation dg_tenant; Type: POLICY; Schema: institution; Owner: -
--

CREATE POLICY dg_tenant ON institution.doc_generation TO authenticated USING ((institution_id = ( SELECT ctx.institution_id
   FROM institution.current_member_ctx_v2() ctx(member_id, institution_id, is_admin, member_role, modules, products))));

--
-- Name: doa_rule; Type: ROW SECURITY; Schema: institution; Owner: -
--

ALTER TABLE institution.doa_rule ENABLE ROW LEVEL SECURITY;

--
-- Name: doa_rule doa_tenant; Type: POLICY; Schema: institution; Owner: -
--

CREATE POLICY doa_tenant ON institution.doa_rule TO authenticated USING ((institution_id = ( SELECT ctx.institution_id
   FROM institution.current_member_ctx_v2() ctx(member_id, institution_id, is_admin, member_role, modules, products))));

--
-- Name: doc; Type: ROW SECURITY; Schema: institution; Owner: -
--

ALTER TABLE institution.doc ENABLE ROW LEVEL SECURITY;

--
-- Name: doc_generation; Type: ROW SECURITY; Schema: institution; Owner: -
--

ALTER TABLE institution.doc_generation ENABLE ROW LEVEL SECURITY;

--
-- Name: doc doc_insert_own; Type: POLICY; Schema: institution; Owner: -
--

CREATE POLICY doc_insert_own ON institution.doc FOR INSERT TO authenticated WITH CHECK ((institution_id = (((( SELECT current_setting('request.jwt.claims'::text, true) AS current_setting))::jsonb ->> 'institution_id'::text))::uuid));

--
-- Name: doc_requirement; Type: ROW SECURITY; Schema: institution; Owner: -
--

ALTER TABLE institution.doc_requirement ENABLE ROW LEVEL SECURITY;

--
-- Name: doc_requirement doc_requirement_tenant_read; Type: POLICY; Schema: institution; Owner: -
--

CREATE POLICY doc_requirement_tenant_read ON institution.doc_requirement FOR SELECT TO authenticated USING ((institution_id = ( SELECT ctx.institution_id
   FROM institution.current_member_ctx_v2() ctx(member_id, institution_id, is_admin, member_role, modules, products))));

--
-- Name: doc doc_select_own; Type: POLICY; Schema: institution; Owner: -
--

CREATE POLICY doc_select_own ON institution.doc FOR SELECT TO authenticated USING ((institution_id = (((( SELECT current_setting('request.jwt.claims'::text, true) AS current_setting))::jsonb ->> 'institution_id'::text))::uuid));

--
-- Name: doc_template; Type: ROW SECURITY; Schema: institution; Owner: -
--

ALTER TABLE institution.doc_template ENABLE ROW LEVEL SECURITY;

--
-- Name: doc_template_version; Type: ROW SECURITY; Schema: institution; Owner: -
--

ALTER TABLE institution.doc_template_version ENABLE ROW LEVEL SECURITY;

--
-- Name: doc_type; Type: ROW SECURITY; Schema: institution; Owner: -
--

ALTER TABLE institution.doc_type ENABLE ROW LEVEL SECURITY;

--
-- Name: doc_type doc_type_select_all; Type: POLICY; Schema: institution; Owner: -
--

CREATE POLICY doc_type_select_all ON institution.doc_type FOR SELECT USING (true);

--
-- Name: doc_template dt_tenant; Type: POLICY; Schema: institution; Owner: -
--

CREATE POLICY dt_tenant ON institution.doc_template TO authenticated USING ((institution_id = ( SELECT ctx.institution_id
   FROM institution.current_member_ctx_v2() ctx(member_id, institution_id, is_admin, member_role, modules, products))));

--
-- Name: doc_template_version dtv_tenant; Type: POLICY; Schema: institution; Owner: -
--

CREATE POLICY dtv_tenant ON institution.doc_template_version TO authenticated USING ((institution_id = ( SELECT ctx.institution_id
   FROM institution.current_member_ctx_v2() ctx(member_id, institution_id, is_admin, member_role, modules, products))));

--
-- Name: esign_envelope ee_env_tenant; Type: POLICY; Schema: institution; Owner: -
--

CREATE POLICY ee_env_tenant ON institution.esign_envelope TO authenticated USING ((institution_id = ( SELECT ctx.institution_id
   FROM institution.current_member_ctx_v2() ctx(member_id, institution_id, is_admin, member_role, modules, products))));

--
-- Name: esign_event ee_evt_tenant; Type: POLICY; Schema: institution; Owner: -
--

CREATE POLICY ee_evt_tenant ON institution.esign_event TO authenticated USING ((envelope_id IN ( SELECT esign_envelope.id
   FROM institution.esign_envelope
  WHERE (esign_envelope.institution_id = ( SELECT ctx.institution_id
           FROM institution.current_member_ctx_v2() ctx(member_id, institution_id, is_admin, member_role, modules, products))))));

--
-- Name: esign_signer ee_sig_tenant; Type: POLICY; Schema: institution; Owner: -
--

CREATE POLICY ee_sig_tenant ON institution.esign_signer TO authenticated USING ((envelope_id IN ( SELECT esign_envelope.id
   FROM institution.esign_envelope
  WHERE (esign_envelope.institution_id = ( SELECT ctx.institution_id
           FROM institution.current_member_ctx_v2() ctx(member_id, institution_id, is_admin, member_role, modules, products))))));

--
-- Name: esign_envelope; Type: ROW SECURITY; Schema: institution; Owner: -
--

ALTER TABLE institution.esign_envelope ENABLE ROW LEVEL SECURITY;

--
-- Name: esign_event; Type: ROW SECURITY; Schema: institution; Owner: -
--

ALTER TABLE institution.esign_event ENABLE ROW LEVEL SECURITY;

--
-- Name: esign_signer; Type: ROW SECURITY; Schema: institution; Owner: -
--

ALTER TABLE institution.esign_signer ENABLE ROW LEVEL SECURITY;

--
-- Name: group; Type: ROW SECURITY; Schema: institution; Owner: -
--

ALTER TABLE institution."group" ENABLE ROW LEVEL SECURITY;

--
-- Name: group inst_groups_delete; Type: POLICY; Schema: institution; Owner: -
--

CREATE POLICY inst_groups_delete ON institution."group" FOR DELETE TO authenticated USING (((institution_id = ( SELECT ctx.institution_id
   FROM institution.current_member_ctx() ctx(member_id, institution_id, is_admin, member_role, modules))) AND ( SELECT ctx.is_admin
   FROM institution.current_member_ctx() ctx(member_id, institution_id, is_admin, member_role, modules))));

--
-- Name: group inst_groups_insert; Type: POLICY; Schema: institution; Owner: -
--

CREATE POLICY inst_groups_insert ON institution."group" FOR INSERT TO authenticated WITH CHECK ((institution_id = ( SELECT ctx.institution_id
   FROM institution.current_member_ctx() ctx(member_id, institution_id, is_admin, member_role, modules))));

--
-- Name: group inst_groups_select; Type: POLICY; Schema: institution; Owner: -
--

CREATE POLICY inst_groups_select ON institution."group" FOR SELECT TO authenticated USING ((institution_id = ( SELECT ctx.institution_id
   FROM institution.current_member_ctx() ctx(member_id, institution_id, is_admin, member_role, modules))));

--
-- Name: group inst_groups_update; Type: POLICY; Schema: institution; Owner: -
--

CREATE POLICY inst_groups_update ON institution."group" FOR UPDATE TO authenticated USING ((institution_id = ( SELECT ctx.institution_id
   FROM institution.current_member_ctx() ctx(member_id, institution_id, is_admin, member_role, modules))));

--
-- Name: institution; Type: ROW SECURITY; Schema: institution; Owner: -
--

ALTER TABLE institution.institution ENABLE ROW LEVEL SECURITY;

--
-- Name: member institution_members_select_self; Type: POLICY; Schema: institution; Owner: -
--

CREATE POLICY institution_members_select_self ON institution.member FOR SELECT TO authenticated USING ((auth_user_id = ( SELECT auth.uid() AS uid)));

--
-- Name: institution_sla_config; Type: ROW SECURITY; Schema: institution; Owner: -
--

ALTER TABLE institution.institution_sla_config ENABLE ROW LEVEL SECURITY;

--
-- Name: institution institutions_select_own; Type: POLICY; Schema: institution; Owner: -
--

CREATE POLICY institutions_select_own ON institution.institution FOR SELECT TO authenticated USING ((((primary_contact_email)::text = (( SELECT auth.jwt() AS jwt) ->> 'email'::text)) OR (EXISTS ( SELECT 1
   FROM institution.member m
  WHERE ((m.institution_id = institution.id) AND (m.auth_user_id = ( SELECT auth.uid() AS uid)))))));

--
-- Name: kyb_document; Type: ROW SECURITY; Schema: institution; Owner: -
--

ALTER TABLE institution.kyb_document ENABLE ROW LEVEL SECURITY;

--
-- Name: kyb_document kyb_document_select; Type: POLICY; Schema: institution; Owner: -
--

CREATE POLICY kyb_document_select ON institution.kyb_document FOR SELECT TO authenticated USING ((institution_id = ( SELECT ctx.institution_id
   FROM institution.current_member_ctx() ctx(member_id, institution_id, is_admin, member_role, modules))));

--
-- Name: member; Type: ROW SECURITY; Schema: institution; Owner: -
--

ALTER TABLE institution.member ENABLE ROW LEVEL SECURITY;

--
-- Name: onboarding_pref; Type: ROW SECURITY; Schema: institution; Owner: -
--

ALTER TABLE institution.onboarding_pref ENABLE ROW LEVEL SECURITY;

--
-- Name: pending_actions; Type: ROW SECURITY; Schema: institution; Owner: -
--

ALTER TABLE institution.pending_actions ENABLE ROW LEVEL SECURITY;

--
-- Name: pending_actions pending_actions_insert; Type: POLICY; Schema: institution; Owner: -
--

CREATE POLICY pending_actions_insert ON institution.pending_actions FOR INSERT TO authenticated WITH CHECK (((institution_id = ( SELECT ctx.institution_id
   FROM institution.current_member_ctx() ctx(member_id, institution_id, is_admin, member_role, modules))) AND (maker_id = ( SELECT ctx.member_id
   FROM institution.current_member_ctx() ctx(member_id, institution_id, is_admin, member_role, modules)))));

--
-- Name: pending_actions pending_actions_select; Type: POLICY; Schema: institution; Owner: -
--

CREATE POLICY pending_actions_select ON institution.pending_actions FOR SELECT TO authenticated USING ((institution_id = ( SELECT ctx.institution_id
   FROM institution.current_member_ctx() ctx(member_id, institution_id, is_admin, member_role, modules))));

--
-- Name: pending_actions pending_actions_update; Type: POLICY; Schema: institution; Owner: -
--

CREATE POLICY pending_actions_update ON institution.pending_actions FOR UPDATE TO authenticated USING (((institution_id = ( SELECT ctx.institution_id
   FROM institution.current_member_ctx() ctx(member_id, institution_id, is_admin, member_role, modules))) AND ( SELECT ctx.is_admin
   FROM institution.current_member_ctx() ctx(member_id, institution_id, is_admin, member_role, modules))));

--
-- Name: pipeline_stage_def; Type: ROW SECURITY; Schema: institution; Owner: -
--

ALTER TABLE institution.pipeline_stage_def ENABLE ROW LEVEL SECURITY;

--
-- Name: pipeline_stage_def pipeline_stage_def_tenant; Type: POLICY; Schema: institution; Owner: -
--

CREATE POLICY pipeline_stage_def_tenant ON institution.pipeline_stage_def TO authenticated USING ((institution_id = ( SELECT ctx.institution_id
   FROM institution.current_member_ctx_v2() ctx(member_id, institution_id, is_admin, member_role, modules, products)))) WITH CHECK ((institution_id = ( SELECT ctx.institution_id
   FROM institution.current_member_ctx_v2() ctx(member_id, institution_id, is_admin, member_role, modules, products))));

--
-- Name: pipeline_template; Type: ROW SECURITY; Schema: institution; Owner: -
--

ALTER TABLE institution.pipeline_template ENABLE ROW LEVEL SECURITY;

--
-- Name: pipeline_template pipeline_template_tenant; Type: POLICY; Schema: institution; Owner: -
--

CREATE POLICY pipeline_template_tenant ON institution.pipeline_template TO authenticated USING ((institution_id = ( SELECT ctx.institution_id
   FROM institution.current_member_ctx_v2() ctx(member_id, institution_id, is_admin, member_role, modules, products)))) WITH CHECK ((institution_id = ( SELECT ctx.institution_id
   FROM institution.current_member_ctx_v2() ctx(member_id, institution_id, is_admin, member_role, modules, products))));

--
-- Name: product_config; Type: ROW SECURITY; Schema: institution; Owner: -
--

ALTER TABLE institution.product_config ENABLE ROW LEVEL SECURITY;

--
-- Name: institution_sla_config tenant_isolation; Type: POLICY; Schema: institution; Owner: -
--

CREATE POLICY tenant_isolation ON institution.institution_sla_config TO authenticated USING ((institution_id = institution.current_institution_id())) WITH CHECK ((institution_id = institution.current_institution_id()));

--
-- Name: member tenant_isolation; Type: POLICY; Schema: institution; Owner: -
--

CREATE POLICY tenant_isolation ON institution.member TO authenticated USING ((institution_id = institution.current_institution_id())) WITH CHECK ((institution_id = institution.current_institution_id()));

--
-- Name: pending_actions tenant_isolation; Type: POLICY; Schema: institution; Owner: -
--

CREATE POLICY tenant_isolation ON institution.pending_actions TO authenticated USING ((institution_id = institution.current_institution_id())) WITH CHECK ((institution_id = institution.current_institution_id()));

--
-- Name: product_config tenant_isolation; Type: POLICY; Schema: institution; Owner: -
--

CREATE POLICY tenant_isolation ON institution.product_config TO authenticated USING ((institution_id = institution.current_institution_id())) WITH CHECK ((institution_id = institution.current_institution_id()));

--
-- Name: webhook tenant_isolation; Type: POLICY; Schema: institution; Owner: -
--

CREATE POLICY tenant_isolation ON institution.webhook TO authenticated USING ((institution_id = institution.current_institution_id())) WITH CHECK ((institution_id = institution.current_institution_id()));

--
-- Name: webhook; Type: ROW SECURITY; Schema: institution; Owner: -
--

ALTER TABLE institution.webhook ENABLE ROW LEVEL SECURITY;

--
-- Name: webhook_delivery; Type: ROW SECURITY; Schema: institution; Owner: -
--

ALTER TABLE institution.webhook_delivery ENABLE ROW LEVEL SECURITY;

--
-- Name: webhook_delivery webhook_delivery_select; Type: POLICY; Schema: institution; Owner: -
--

CREATE POLICY webhook_delivery_select ON institution.webhook_delivery FOR SELECT TO authenticated USING ((institution_id = ( SELECT ctx.institution_id
   FROM institution.current_member_ctx() ctx(member_id, institution_id, is_admin, member_role, modules))));

--
-- Name: acceptance_log; Type: ROW SECURITY; Schema: integration; Owner: -
--

ALTER TABLE integration.acceptance_log ENABLE ROW LEVEL SECURITY;

--
-- Name: admin_command_log; Type: ROW SECURITY; Schema: integration; Owner: -
--

ALTER TABLE integration.admin_command_log ENABLE ROW LEVEL SECURITY;

--
-- Name: aggregate_sequence; Type: ROW SECURITY; Schema: integration; Owner: -
--

ALTER TABLE integration.aggregate_sequence ENABLE ROW LEVEL SECURITY;

--
-- Name: api_channel_policy; Type: ROW SECURITY; Schema: integration; Owner: -
--

ALTER TABLE integration.api_channel_policy ENABLE ROW LEVEL SECURITY;

--
-- Name: api_channel_status; Type: ROW SECURITY; Schema: integration; Owner: -
--

ALTER TABLE integration.api_channel_status ENABLE ROW LEVEL SECURITY;

--
-- Name: api_traffic_hour; Type: ROW SECURITY; Schema: integration; Owner: -
--

ALTER TABLE integration.api_traffic_hour ENABLE ROW LEVEL SECURITY;

--
-- Name: document_upload; Type: ROW SECURITY; Schema: integration; Owner: -
--

ALTER TABLE integration.document_upload ENABLE ROW LEVEL SECURITY;

--
-- Name: emit_error; Type: ROW SECURITY; Schema: integration; Owner: -
--

ALTER TABLE integration.emit_error ENABLE ROW LEVEL SECURITY;

--
-- Name: inbox; Type: ROW SECURITY; Schema: integration; Owner: -
--

ALTER TABLE integration.inbox ENABLE ROW LEVEL SECURITY;

--
-- Name: inbox_watermark; Type: ROW SECURITY; Schema: integration; Owner: -
--

ALTER TABLE integration.inbox_watermark ENABLE ROW LEVEL SECURITY;

--
-- Name: outbox; Type: ROW SECURITY; Schema: integration; Owner: -
--

ALTER TABLE integration.outbox ENABLE ROW LEVEL SECURITY;

--
-- Name: request_shadow; Type: ROW SECURITY; Schema: integration; Owner: -
--

ALTER TABLE integration.request_shadow ENABLE ROW LEVEL SECURITY;

--
-- Name: acceptance; Type: ROW SECURITY; Schema: marketplace; Owner: -
--

ALTER TABLE marketplace.acceptance ENABLE ROW LEVEL SECURITY;

--
-- Name: bid; Type: ROW SECURITY; Schema: marketplace; Owner: -
--

ALTER TABLE marketplace.bid ENABLE ROW LEVEL SECURITY;

--
-- Name: bid_acceptance; Type: ROW SECURITY; Schema: marketplace; Owner: -
--

ALTER TABLE marketplace.bid_acceptance ENABLE ROW LEVEL SECURITY;

--
-- Name: bid_acceptance bid_acceptance_institution_read; Type: POLICY; Schema: marketplace; Owner: -
--

CREATE POLICY bid_acceptance_institution_read ON marketplace.bid_acceptance FOR SELECT TO authenticated USING ((institution_id IN ( SELECT member.institution_id
   FROM institution.member
  WHERE (member.auth_user_id = ( SELECT auth.uid() AS uid)))));

--
-- Name: bid_allocation; Type: ROW SECURITY; Schema: marketplace; Owner: -
--

ALTER TABLE marketplace.bid_allocation ENABLE ROW LEVEL SECURITY;

--
-- Name: bid_benefit; Type: ROW SECURITY; Schema: marketplace; Owner: -
--

ALTER TABLE marketplace.bid_benefit ENABLE ROW LEVEL SECURITY;

--
-- Name: bid_benefit bid_benefit_insert_trigger; Type: POLICY; Schema: marketplace; Owner: -
--

CREATE POLICY bid_benefit_insert_trigger ON marketplace.bid_benefit FOR INSERT TO postgres WITH CHECK (true);

--
-- Name: bid_benefit bid_benefit_select_own; Type: POLICY; Schema: marketplace; Owner: -
--

CREATE POLICY bid_benefit_select_own ON marketplace.bid_benefit FOR SELECT TO authenticated USING ((EXISTS ( SELECT 1
   FROM marketplace.bid b
  WHERE ((b.id = bid_benefit.bid_id) AND (b.institution_id = (((( SELECT current_setting('request.jwt.claims'::text, true) AS current_setting))::jsonb ->> 'institution_id'::text))::uuid)))));

--
-- Name: bid_event; Type: ROW SECURITY; Schema: marketplace; Owner: -
--

ALTER TABLE marketplace.bid_event ENABLE ROW LEVEL SECURITY;

--
-- Name: credit_decision; Type: ROW SECURITY; Schema: marketplace; Owner: -
--

ALTER TABLE marketplace.credit_decision ENABLE ROW LEVEL SECURITY;

--
-- Name: credit_decision credit_decision_tenant; Type: POLICY; Schema: marketplace; Owner: -
--

CREATE POLICY credit_decision_tenant ON marketplace.credit_decision FOR SELECT TO authenticated USING ((institution_id = ( SELECT current_member_ctx_v2.institution_id
   FROM institution.current_member_ctx_v2() current_member_ctx_v2(member_id, institution_id, is_admin, member_role, modules, products))));

--
-- Name: deal_document; Type: ROW SECURITY; Schema: marketplace; Owner: -
--

ALTER TABLE marketplace.deal_document ENABLE ROW LEVEL SECURITY;

--
-- Name: deal_document_event; Type: ROW SECURITY; Schema: marketplace; Owner: -
--

ALTER TABLE marketplace.deal_document_event ENABLE ROW LEVEL SECURITY;

--
-- Name: deal_document_event deal_document_event_tenant_read; Type: POLICY; Schema: marketplace; Owner: -
--

CREATE POLICY deal_document_event_tenant_read ON marketplace.deal_document_event FOR SELECT TO authenticated USING ((EXISTS ( SELECT 1
   FROM marketplace.deal_document d
  WHERE ((d.id = deal_document_event.document_id) AND (d.institution_id = ( SELECT ctx.institution_id
           FROM institution.current_member_ctx_v2() ctx(member_id, institution_id, is_admin, member_role, modules, products)))))));

--
-- Name: deal_document deal_document_tenant_read; Type: POLICY; Schema: marketplace; Owner: -
--

CREATE POLICY deal_document_tenant_read ON marketplace.deal_document FOR SELECT TO authenticated USING ((institution_id = ( SELECT ctx.institution_id
   FROM institution.current_member_ctx_v2() ctx(member_id, institution_id, is_admin, member_role, modules, products))));

--
-- Name: loan_booking; Type: ROW SECURITY; Schema: marketplace; Owner: -
--

ALTER TABLE marketplace.loan_booking ENABLE ROW LEVEL SECURITY;

--
-- Name: loan_booking loan_booking_tenant_read; Type: POLICY; Schema: marketplace; Owner: -
--

CREATE POLICY loan_booking_tenant_read ON marketplace.loan_booking FOR SELECT TO authenticated USING ((institution_id = ( SELECT ctx.institution_id
   FROM institution.current_member_ctx_v2() ctx(member_id, institution_id, is_admin, member_role, modules, products))));

--
-- Name: loan_pipeline; Type: ROW SECURITY; Schema: marketplace; Owner: -
--

ALTER TABLE marketplace.loan_pipeline ENABLE ROW LEVEL SECURITY;

--
-- Name: loan_pipeline loan_pipeline_tenant; Type: POLICY; Schema: marketplace; Owner: -
--

CREATE POLICY loan_pipeline_tenant ON marketplace.loan_pipeline TO authenticated USING ((institution_id = ( SELECT ctx.institution_id
   FROM institution.current_member_ctx_v2() ctx(member_id, institution_id, is_admin, member_role, modules, products)))) WITH CHECK ((institution_id = ( SELECT ctx.institution_id
   FROM institution.current_member_ctx_v2() ctx(member_id, institution_id, is_admin, member_role, modules, products))));

--
-- Name: acceptance marketplace_acceptance_select; Type: POLICY; Schema: marketplace; Owner: -
--

CREATE POLICY marketplace_acceptance_select ON marketplace.acceptance FOR SELECT TO authenticated USING ((EXISTS ( SELECT 1
   FROM (marketplace.bid b
     JOIN institution.current_member_ctx() ctx(member_id, institution_id, is_admin, member_role, modules) ON ((ctx.institution_id = b.institution_id)))
  WHERE (b.id = acceptance.bid_id))));

--
-- Name: bid_allocation marketplace_bid_allocation_select; Type: POLICY; Schema: marketplace; Owner: -
--

CREATE POLICY marketplace_bid_allocation_select ON marketplace.bid_allocation FOR SELECT TO authenticated USING ((EXISTS ( SELECT 1
   FROM marketplace.bid b
  WHERE (b.id = bid_allocation.bid_id))));

--
-- Name: bid_event marketplace_bid_event_select; Type: POLICY; Schema: marketplace; Owner: -
--

CREATE POLICY marketplace_bid_event_select ON marketplace.bid_event FOR SELECT TO authenticated USING ((EXISTS ( SELECT 1
   FROM (marketplace.bid b
     JOIN institution.current_member_ctx() ctx(member_id, institution_id, is_admin, member_role, modules) ON ((ctx.institution_id = b.institution_id)))
  WHERE (b.id = bid_event.bid_id))));

--
-- Name: bid marketplace_bid_select; Type: POLICY; Schema: marketplace; Owner: -
--

CREATE POLICY marketplace_bid_select ON marketplace.bid FOR SELECT TO authenticated USING (((institution_id = ( SELECT ctx.institution_id
   FROM institution.current_member_ctx_v2() ctx(member_id, institution_id, is_admin, member_role, modules, products))) AND (( SELECT ((ctx.products IS NULL) OR (array_length(ctx.products, 1) IS NULL))
   FROM institution.current_member_ctx_v2() ctx(member_id, institution_id, is_admin, member_role, modules, products)) OR (EXISTS ( SELECT 1
   FROM marketplace.request r,
    institution.current_member_ctx_v2() ctx(member_id, institution_id, is_admin, member_role, modules, products)
  WHERE ((r.id = bid.request_id) AND (r.product_id = ANY (ctx.products))))))));

--
-- Name: request_allocation marketplace_request_allocation_select; Type: POLICY; Schema: marketplace; Owner: -
--

CREATE POLICY marketplace_request_allocation_select ON marketplace.request_allocation FOR SELECT TO authenticated USING ((EXISTS ( SELECT 1
   FROM marketplace.request r
  WHERE (r.id = request_allocation.request_id))));

--
-- Name: request marketplace_request_select; Type: POLICY; Schema: marketplace; Owner: -
--

CREATE POLICY marketplace_request_select ON marketplace.request FOR SELECT TO authenticated USING (((status = ANY (ARRAY['open'::text, 'bidding'::text])) AND (EXISTS ( SELECT 1
   FROM institution.current_member_ctx_v2() ctx(member_id, institution_id, is_admin, member_role, modules, products)
  WHERE ((ctx.institution_id IS NOT NULL) AND ((ctx.products IS NULL) OR (array_length(ctx.products, 1) IS NULL) OR (request.product_id = ANY (ctx.products))))))));

--
-- Name: request marketplace_request_select_own_bids; Type: POLICY; Schema: marketplace; Owner: -
--

CREATE POLICY marketplace_request_select_own_bids ON marketplace.request FOR SELECT TO authenticated USING ((EXISTS ( SELECT 1
   FROM (institution.current_member_ctx_v2() ctx(member_id, institution_id, is_admin, member_role, modules, products)
     JOIN LATERAL institution.get_institution_bid_request_ids(ctx.institution_id) br(request_id) ON ((br.request_id = request.id))))));

--
-- Name: pipeline_stage_instance; Type: ROW SECURITY; Schema: marketplace; Owner: -
--

ALTER TABLE marketplace.pipeline_stage_instance ENABLE ROW LEVEL SECURITY;

--
-- Name: pipeline_stage_instance pipeline_stage_instance_tenant; Type: POLICY; Schema: marketplace; Owner: -
--

CREATE POLICY pipeline_stage_instance_tenant ON marketplace.pipeline_stage_instance TO authenticated USING ((EXISTS ( SELECT 1
   FROM marketplace.loan_pipeline lp,
    institution.current_member_ctx_v2() ctx(member_id, institution_id, is_admin, member_role, modules, products)
  WHERE ((lp.id = pipeline_stage_instance.pipeline_id) AND (lp.institution_id = ctx.institution_id))))) WITH CHECK ((EXISTS ( SELECT 1
   FROM marketplace.loan_pipeline lp,
    institution.current_member_ctx_v2() ctx(member_id, institution_id, is_admin, member_role, modules, products)
  WHERE ((lp.id = pipeline_stage_instance.pipeline_id) AND (lp.institution_id = ctx.institution_id)))));

--
-- Name: request; Type: ROW SECURITY; Schema: marketplace; Owner: -
--

ALTER TABLE marketplace.request ENABLE ROW LEVEL SECURITY;

--
-- Name: request_allocation; Type: ROW SECURITY; Schema: marketplace; Owner: -
--

ALTER TABLE marketplace.request_allocation ENABLE ROW LEVEL SECURITY;

--
-- Name: request_decline; Type: ROW SECURITY; Schema: marketplace; Owner: -
--

ALTER TABLE marketplace.request_decline ENABLE ROW LEVEL SECURITY;

--
-- Name: request_decline request_decline_own; Type: POLICY; Schema: marketplace; Owner: -
--

CREATE POLICY request_decline_own ON marketplace.request_decline FOR SELECT TO authenticated USING ((institution_id = ( SELECT current_member_ctx_v2.institution_id
   FROM institution.current_member_ctx_v2() current_member_ctx_v2(member_id, institution_id, is_admin, member_role, modules, products))));

--
-- Name: request_message; Type: ROW SECURITY; Schema: marketplace; Owner: -
--

ALTER TABLE marketplace.request_message ENABLE ROW LEVEL SECURITY;

--
-- Name: sync_state; Type: ROW SECURITY; Schema: marketplace; Owner: -
--

ALTER TABLE marketplace.sync_state ENABLE ROW LEVEL SECURITY;

--
-- Name: schema_migration; Type: ROW SECURITY; Schema: ops; Owner: -
--

ALTER TABLE ops.schema_migration ENABLE ROW LEVEL SECURITY;

--
-- Name: admin_audit_log; Type: ROW SECURITY; Schema: portal_admin; Owner: -
--

ALTER TABLE portal_admin.admin_audit_log ENABLE ROW LEVEL SECURITY;

--
-- Name: admin_dual_control_actions; Type: ROW SECURITY; Schema: portal_admin; Owner: -
--

ALTER TABLE portal_admin.admin_dual_control_actions ENABLE ROW LEVEL SECURITY;

--
-- Name: admin_roles; Type: ROW SECURITY; Schema: portal_admin; Owner: -
--

ALTER TABLE portal_admin.admin_roles ENABLE ROW LEVEL SECURITY;

--
-- Name: admin_roles admin_roles_select; Type: POLICY; Schema: portal_admin; Owner: -
--

CREATE POLICY admin_roles_select ON portal_admin.admin_roles FOR SELECT TO authenticated USING (portal_admin.is_admin());

--
-- Name: admin_roles admin_roles_write; Type: POLICY; Schema: portal_admin; Owner: -
--

CREATE POLICY admin_roles_write ON portal_admin.admin_roles TO authenticated USING ((( SELECT auth.role() AS role) = 'service_role'::text));

--
-- Name: admin_sessions; Type: ROW SECURITY; Schema: portal_admin; Owner: -
--

ALTER TABLE portal_admin.admin_sessions ENABLE ROW LEVEL SECURITY;

--
-- Name: admin_sessions admin_sessions_select; Type: POLICY; Schema: portal_admin; Owner: -
--

CREATE POLICY admin_sessions_select ON portal_admin.admin_sessions FOR SELECT TO authenticated USING (portal_admin.is_admin());

--
-- Name: admin_sessions admin_sessions_write; Type: POLICY; Schema: portal_admin; Owner: -
--

CREATE POLICY admin_sessions_write ON portal_admin.admin_sessions TO authenticated USING ((portal_admin.is_admin() OR (( SELECT auth.role() AS role) = 'service_role'::text)));

--
-- Name: admin_users; Type: ROW SECURITY; Schema: portal_admin; Owner: -
--

ALTER TABLE portal_admin.admin_users ENABLE ROW LEVEL SECURITY;

--
-- Name: admin_users admin_users_self_select; Type: POLICY; Schema: portal_admin; Owner: -
--

CREATE POLICY admin_users_self_select ON portal_admin.admin_users FOR SELECT TO authenticated USING ((auth_user_id = ( SELECT auth.uid() AS uid)));

--
-- Name: admin_users admin_users_update; Type: POLICY; Schema: portal_admin; Owner: -
--

CREATE POLICY admin_users_update ON portal_admin.admin_users FOR UPDATE TO authenticated USING ((( SELECT auth.role() AS role) = 'service_role'::text));

--
-- Name: admin_audit_log audit_select; Type: POLICY; Schema: portal_admin; Owner: -
--

CREATE POLICY audit_select ON portal_admin.admin_audit_log FOR SELECT TO authenticated USING (portal_admin.has_permission('audit:view'::text));

--
-- Name: commission_event; Type: ROW SECURITY; Schema: portal_admin; Owner: -
--

ALTER TABLE portal_admin.commission_event ENABLE ROW LEVEL SECURITY;

--
-- Name: admin_dual_control_actions dc_select; Type: POLICY; Schema: portal_admin; Owner: -
--

CREATE POLICY dc_select ON portal_admin.admin_dual_control_actions FOR SELECT TO authenticated USING (portal_admin.is_admin());

--
-- Name: admin_dual_control_actions dc_write; Type: POLICY; Schema: portal_admin; Owner: -
--

CREATE POLICY dc_write ON portal_admin.admin_dual_control_actions TO authenticated USING ((( SELECT auth.role() AS role) = 'service_role'::text));

--
-- Name: user_groups groups_select; Type: POLICY; Schema: portal_admin; Owner: -
--

CREATE POLICY groups_select ON portal_admin.user_groups FOR SELECT TO authenticated USING (true);

--
-- Name: user_groups groups_write; Type: POLICY; Schema: portal_admin; Owner: -
--

CREATE POLICY groups_write ON portal_admin.user_groups TO authenticated USING ((( SELECT auth.role() AS role) = 'service_role'::text));

--
-- Name: user_groups; Type: ROW SECURITY; Schema: portal_admin; Owner: -
--

ALTER TABLE portal_admin.user_groups ENABLE ROW LEVEL SECURITY;

--
-- Name: _identity_migration_log; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public._identity_migration_log ENABLE ROW LEVEL SECURITY;

--
-- Name: portal_notifications institution_members_mark_read; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY institution_members_mark_read ON public.portal_notifications FOR UPDATE TO authenticated USING ((institution_id IN ( SELECT m.institution_id
   FROM institution.member m
  WHERE (m.auth_user_id = ( SELECT auth.uid() AS uid))))) WITH CHECK ((institution_id IN ( SELECT m.institution_id
   FROM institution.member m
  WHERE (m.auth_user_id = ( SELECT auth.uid() AS uid)))));

--
-- Name: portal_notifications institution_members_read_own_notifications; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY institution_members_read_own_notifications ON public.portal_notifications FOR SELECT TO authenticated USING ((institution_id IN ( SELECT member.institution_id
   FROM institution.member
  WHERE (member.auth_user_id = ( SELECT auth.uid() AS uid)))));

--
-- Name: portal_notifications; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.portal_notifications ENABLE ROW LEVEL SECURITY;

--
-- Name: portal_notifications service_role_insert_notifications; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY service_role_insert_notifications ON public.portal_notifications FOR INSERT TO service_role WITH CHECK (true);

--
-- Name: SCHEMA audit; Type: ACL; Schema: -; Owner: -
--

GRANT USAGE ON SCHEMA audit TO authenticated;

--
-- Name: SCHEMA auth_portal; Type: ACL; Schema: -; Owner: -
--

GRANT USAGE ON SCHEMA auth_portal TO service_role;

--
-- Name: SCHEMA autobid; Type: ACL; Schema: -; Owner: -
--

GRANT USAGE ON SCHEMA autobid TO authenticated;

--
-- Name: SCHEMA catalog; Type: ACL; Schema: -; Owner: -
--

GRANT USAGE ON SCHEMA catalog TO authenticated;

--
-- Name: SCHEMA entitlements; Type: ACL; Schema: -; Owner: -
--

GRANT USAGE ON SCHEMA entitlements TO authenticated;

--
-- Name: SCHEMA governance; Type: ACL; Schema: -; Owner: -
--

GRANT USAGE ON SCHEMA governance TO authenticated;

--
-- Name: SCHEMA institution; Type: ACL; Schema: -; Owner: -
--

GRANT USAGE ON SCHEMA institution TO authenticated;

--
-- Name: SCHEMA marketplace; Type: ACL; Schema: -; Owner: -
--

GRANT USAGE ON SCHEMA marketplace TO authenticated;

--
-- Name: SCHEMA portal_admin; Type: ACL; Schema: -; Owner: -
--

GRANT USAGE ON SCHEMA portal_admin TO authenticated;
GRANT USAGE ON SCHEMA portal_admin TO service_role;

--
-- Name: SCHEMA public; Type: ACL; Schema: -; Owner: -
--

GRANT USAGE ON SCHEMA public TO postgres;
GRANT USAGE ON SCHEMA public TO anon;
GRANT USAGE ON SCHEMA public TO authenticated;
GRANT USAGE ON SCHEMA public TO service_role;

--
-- Name: FUNCTION log(p_action text, p_actor_id uuid, p_actor_type text, p_institution_id uuid, p_resource_type text, p_resource_id uuid, p_outcome text, p_metadata jsonb); Type: ACL; Schema: audit; Owner: -
--

REVOKE ALL ON FUNCTION audit.log(p_action text, p_actor_id uuid, p_actor_type text, p_institution_id uuid, p_resource_type text, p_resource_id uuid, p_outcome text, p_metadata jsonb) FROM PUBLIC;
GRANT ALL ON FUNCTION audit.log(p_action text, p_actor_id uuid, p_actor_type text, p_institution_id uuid, p_resource_type text, p_resource_id uuid, p_outcome text, p_metadata jsonb) TO authenticated;

--
-- Name: FUNCTION get_member_module_permissions(p_email text, p_institution_id uuid); Type: ACL; Schema: auth_portal; Owner: -
--

REVOKE ALL ON FUNCTION auth_portal.get_member_module_permissions(p_email text, p_institution_id uuid) FROM PUBLIC;
GRANT ALL ON FUNCTION auth_portal.get_member_module_permissions(p_email text, p_institution_id uuid) TO service_role;

--
-- Name: FUNCTION set_updated_at(); Type: ACL; Schema: auth_portal; Owner: -
--

GRANT ALL ON FUNCTION auth_portal.set_updated_at() TO authenticated;
GRANT ALL ON FUNCTION auth_portal.set_updated_at() TO service_role;

--
-- Name: FUNCTION ensure_execution_partition(p_month date); Type: ACL; Schema: autobid; Owner: -
--

REVOKE ALL ON FUNCTION autobid.ensure_execution_partition(p_month date) FROM PUBLIC;
GRANT ALL ON FUNCTION autobid.ensure_execution_partition(p_month date) TO service_role;

--
-- Name: FUNCTION ensure_execution_partitions(); Type: ACL; Schema: autobid; Owner: -
--

REVOKE ALL ON FUNCTION autobid.ensure_execution_partitions() FROM PUBLIC;
GRANT ALL ON FUNCTION autobid.ensure_execution_partitions() TO service_role;

--
-- Name: FUNCTION adjustment_is_append_only(); Type: ACL; Schema: billing; Owner: -
--

REVOKE ALL ON FUNCTION billing.adjustment_is_append_only() FROM PUBLIC;

--
-- Name: FUNCTION compute_fee(p_basis text, p_rate numeric, p_params jsonb, p_min_fee numeric, p_max_fee numeric, p_in jsonb); Type: ACL; Schema: billing; Owner: -
--

REVOKE ALL ON FUNCTION billing.compute_fee(p_basis text, p_rate numeric, p_params jsonb, p_min_fee numeric, p_max_fee numeric, p_in jsonb) FROM PUBLIC;

--
-- Name: FUNCTION rule_shape_error(p_basis text, p_rate numeric, p_params jsonb, p_min_fee numeric, p_max_fee numeric, p_clawback_months integer, p_clawback_mode text); Type: ACL; Schema: billing; Owner: -
--

REVOKE ALL ON FUNCTION billing.rule_shape_error(p_basis text, p_rate numeric, p_params jsonb, p_min_fee numeric, p_max_fee numeric, p_clawback_months integer, p_clawback_mode text) FROM PUBLIC;

--
-- Name: FUNCTION end_commission_rule(p_rule_id uuid, p_ended_on date, p_command_id uuid, p_maker uuid, p_checker uuid); Type: ACL; Schema: billing; Owner: -
--

REVOKE ALL ON FUNCTION billing.end_commission_rule(p_rule_id uuid, p_ended_on date, p_command_id uuid, p_maker uuid, p_checker uuid) FROM PUBLIC;

--
-- Name: FUNCTION json_num(p jsonb); Type: ACL; Schema: billing; Owner: -
--

REVOKE ALL ON FUNCTION billing.json_num(p jsonb) FROM PUBLIC;

--
-- Name: FUNCTION rule_for(p_institution_id uuid, p_product_code text, p_on date); Type: ACL; Schema: billing; Owner: -
--

REVOKE ALL ON FUNCTION billing.rule_for(p_institution_id uuid, p_product_code text, p_on date) FROM PUBLIC;

--
-- Name: FUNCTION set_commission_rule(p_institution_id uuid, p_product_code text, p_rate numeric, p_effective_from date, p_reason text, p_command_id uuid, p_maker uuid, p_checker uuid, p_basis text, p_params jsonb, p_min_fee numeric, p_max_fee numeric, p_clawback_months integer, p_clawback_mode text); Type: ACL; Schema: billing; Owner: -
--

REVOKE ALL ON FUNCTION billing.set_commission_rule(p_institution_id uuid, p_product_code text, p_rate numeric, p_effective_from date, p_reason text, p_command_id uuid, p_maker uuid, p_checker uuid, p_basis text, p_params jsonb, p_min_fee numeric, p_max_fee numeric, p_clawback_months integer, p_clawback_mode text) FROM PUBLIC;

--
-- Name: FUNCTION ensure_usage_partition(p_month date); Type: ACL; Schema: entitlements; Owner: -
--

REVOKE ALL ON FUNCTION entitlements.ensure_usage_partition(p_month date) FROM PUBLIC;
GRANT ALL ON FUNCTION entitlements.ensure_usage_partition(p_month date) TO service_role;

--
-- Name: FUNCTION ensure_usage_partitions(); Type: ACL; Schema: entitlements; Owner: -
--

REVOKE ALL ON FUNCTION entitlements.ensure_usage_partitions() FROM PUBLIC;
GRANT ALL ON FUNCTION entitlements.ensure_usage_partitions() TO service_role;

--
-- Name: FUNCTION _group_label(p_id text); Type: ACL; Schema: governance; Owner: -
--

REVOKE ALL ON FUNCTION governance._group_label(p_id text) FROM PUBLIC;

--
-- Name: FUNCTION _module_label(k text); Type: ACL; Schema: governance; Owner: -
--

REVOKE ALL ON FUNCTION governance._module_label(k text) FROM PUBLIC;

--
-- Name: FUNCTION _modules(a jsonb); Type: ACL; Schema: governance; Owner: -
--

REVOKE ALL ON FUNCTION governance._modules(a jsonb) FROM PUBLIC;

--
-- Name: FUNCTION _money(v text); Type: ACL; Schema: governance; Owner: -
--

REVOKE ALL ON FUNCTION governance._money(v text) FROM PUBLIC;

--
-- Name: FUNCTION _products(a jsonb); Type: ACL; Schema: governance; Owner: -
--

REVOKE ALL ON FUNCTION governance._products(a jsonb) FROM PUBLIC;

--
-- Name: FUNCTION _row(f text, b text, a text); Type: ACL; Schema: governance; Owner: -
--

REVOKE ALL ON FUNCTION governance._row(f text, b text, a text) FROM PUBLIC;

--
-- Name: FUNCTION describe_change(p_category text, p_institution uuid, p_payload jsonb); Type: ACL; Schema: governance; Owner: -
--

REVOKE ALL ON FUNCTION governance.describe_change(p_category text, p_institution uuid, p_payload jsonb) FROM PUBLIC;

--
-- Name: FUNCTION enable_four_eyes(p_category text); Type: ACL; Schema: governance; Owner: -
--

REVOKE ALL ON FUNCTION governance.enable_four_eyes(p_category text) FROM PUBLIC;
GRANT ALL ON FUNCTION governance.enable_four_eyes(p_category text) TO authenticated;

--
-- Name: FUNCTION execute(p_action_id uuid); Type: ACL; Schema: governance; Owner: -
--

REVOKE ALL ON FUNCTION governance.execute(p_action_id uuid) FROM PUBLIC;

--
-- Name: FUNCTION expire_stale_actions(); Type: ACL; Schema: governance; Owner: -
--

REVOKE ALL ON FUNCTION governance.expire_stale_actions() FROM PUBLIC;

--
-- Name: FUNCTION four_eyes_required(p_institution uuid, p_category text); Type: ACL; Schema: governance; Owner: -
--

REVOKE ALL ON FUNCTION governance.four_eyes_required(p_institution uuid, p_category text) FROM PUBLIC;
GRANT ALL ON FUNCTION governance.four_eyes_required(p_institution uuid, p_category text) TO authenticated;

--
-- Name: FUNCTION guard_member_action(); Type: ACL; Schema: governance; Owner: -
--

REVOKE ALL ON FUNCTION governance.guard_member_action() FROM PUBLIC;
GRANT ALL ON FUNCTION governance.guard_member_action() TO authenticated;
GRANT ALL ON FUNCTION governance.guard_member_action() TO service_role;

--
-- Name: FUNCTION submit(p_category text, p_resource_type text, p_resource_id uuid, p_payload jsonb, p_label text, p_risk text); Type: ACL; Schema: governance; Owner: -
--

REVOKE ALL ON FUNCTION governance.submit(p_category text, p_resource_type text, p_resource_id uuid, p_payload jsonb, p_label text, p_risk text) FROM PUBLIC;
GRANT ALL ON FUNCTION governance.submit(p_category text, p_resource_type text, p_resource_id uuid, p_payload jsonb, p_label text, p_risk text) TO authenticated;

--
-- Name: TABLE pending_actions; Type: ACL; Schema: institution; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE institution.pending_actions TO authenticated;

--
-- Name: FUNCTION _call_provision_user(p_action institution.pending_actions); Type: ACL; Schema: institution; Owner: -
--

REVOKE ALL ON FUNCTION institution._call_provision_user(p_action institution.pending_actions) FROM PUBLIC;

--
-- Name: FUNCTION _execute_action(p_action_id uuid); Type: ACL; Schema: institution; Owner: -
--

REVOKE ALL ON FUNCTION institution._execute_action(p_action_id uuid) FROM PUBLIC;
GRANT ALL ON FUNCTION institution._execute_action(p_action_id uuid) TO service_role;

--
-- Name: FUNCTION approvals_advance(p_instance uuid, p_done_seq integer); Type: ACL; Schema: institution; Owner: -
--

REVOKE ALL ON FUNCTION institution.approvals_advance(p_instance uuid, p_done_seq integer) FROM PUBLIC;
GRANT ALL ON FUNCTION institution.approvals_advance(p_instance uuid, p_done_seq integer) TO service_role;

--
-- Name: FUNCTION approvals_cast(p_stage_instance uuid, p_actor uuid, p_action text, p_comment text, p_checklist jsonb, p_acting_as uuid); Type: ACL; Schema: institution; Owner: -
--

REVOKE ALL ON FUNCTION institution.approvals_cast(p_stage_instance uuid, p_actor uuid, p_action text, p_comment text, p_checklist jsonb, p_acting_as uuid) FROM PUBLIC;
GRANT ALL ON FUNCTION institution.approvals_cast(p_stage_instance uuid, p_actor uuid, p_action text, p_comment text, p_checklist jsonb, p_acting_as uuid) TO authenticated;
GRANT ALL ON FUNCTION institution.approvals_cast(p_stage_instance uuid, p_actor uuid, p_action text, p_comment text, p_checklist jsonb, p_acting_as uuid) TO service_role;

--
-- Name: FUNCTION approvals_evaluate_stage(p_stage_instance uuid); Type: ACL; Schema: institution; Owner: -
--

REVOKE ALL ON FUNCTION institution.approvals_evaluate_stage(p_stage_instance uuid) FROM PUBLIC;
GRANT ALL ON FUNCTION institution.approvals_evaluate_stage(p_stage_instance uuid) TO service_role;

--
-- Name: FUNCTION approvals_expire_overdue(); Type: ACL; Schema: institution; Owner: -
--

REVOKE ALL ON FUNCTION institution.approvals_expire_overdue() FROM PUBLIC;
GRANT ALL ON FUNCTION institution.approvals_expire_overdue() TO service_role;

--
-- Name: FUNCTION approvals_is_eligible(p_stage_instance uuid, p_actor uuid); Type: ACL; Schema: institution; Owner: -
--

REVOKE ALL ON FUNCTION institution.approvals_is_eligible(p_stage_instance uuid, p_actor uuid) FROM PUBLIC;
GRANT ALL ON FUNCTION institution.approvals_is_eligible(p_stage_instance uuid, p_actor uuid) TO authenticated;
GRANT ALL ON FUNCTION institution.approvals_is_eligible(p_stage_instance uuid, p_actor uuid) TO service_role;

--
-- Name: FUNCTION approvals_route(p_institution_id uuid, p_entity_type text, p_entity_id uuid, p_entity_maker uuid, p_snapshot jsonb, p_template_id uuid, p_doa_rule_id uuid); Type: ACL; Schema: institution; Owner: -
--

REVOKE ALL ON FUNCTION institution.approvals_route(p_institution_id uuid, p_entity_type text, p_entity_id uuid, p_entity_maker uuid, p_snapshot jsonb, p_template_id uuid, p_doa_rule_id uuid) FROM PUBLIC;
GRANT ALL ON FUNCTION institution.approvals_route(p_institution_id uuid, p_entity_type text, p_entity_id uuid, p_entity_maker uuid, p_snapshot jsonb, p_template_id uuid, p_doa_rule_id uuid) TO authenticated;
GRANT ALL ON FUNCTION institution.approvals_route(p_institution_id uuid, p_entity_type text, p_entity_id uuid, p_entity_maker uuid, p_snapshot jsonb, p_template_id uuid, p_doa_rule_id uuid) TO service_role;

--
-- Name: FUNCTION approvals_seed_defaults(p_institution_id uuid, p_admin uuid); Type: ACL; Schema: institution; Owner: -
--

REVOKE ALL ON FUNCTION institution.approvals_seed_defaults(p_institution_id uuid, p_admin uuid) FROM PUBLIC;
GRANT ALL ON FUNCTION institution.approvals_seed_defaults(p_institution_id uuid, p_admin uuid) TO service_role;

--
-- Name: FUNCTION approvals_withdraw(p_instance uuid, p_actor uuid, p_reason text); Type: ACL; Schema: institution; Owner: -
--

REVOKE ALL ON FUNCTION institution.approvals_withdraw(p_instance uuid, p_actor uuid, p_reason text) FROM PUBLIC;
GRANT ALL ON FUNCTION institution.approvals_withdraw(p_instance uuid, p_actor uuid, p_reason text) TO authenticated;
GRANT ALL ON FUNCTION institution.approvals_withdraw(p_instance uuid, p_actor uuid, p_reason text) TO service_role;

--
-- Name: FUNCTION approve_action(p_action_id uuid, p_note text); Type: ACL; Schema: institution; Owner: -
--

REVOKE ALL ON FUNCTION institution.approve_action(p_action_id uuid, p_note text) FROM PUBLIC;
GRANT ALL ON FUNCTION institution.approve_action(p_action_id uuid, p_note text) TO authenticated;
GRANT ALL ON FUNCTION institution.approve_action(p_action_id uuid, p_note text) TO service_role;

--
-- Name: FUNCTION assign_default_member_group(); Type: ACL; Schema: institution; Owner: -
--

REVOKE ALL ON FUNCTION institution.assign_default_member_group() FROM PUBLIC;
GRANT ALL ON FUNCTION institution.assign_default_member_group() TO service_role;

--
-- Name: FUNCTION current_institution_id(); Type: ACL; Schema: institution; Owner: -
--

GRANT ALL ON FUNCTION institution.current_institution_id() TO authenticated;

--
-- Name: FUNCTION current_member_ctx(); Type: ACL; Schema: institution; Owner: -
--

REVOKE ALL ON FUNCTION institution.current_member_ctx() FROM PUBLIC;
GRANT ALL ON FUNCTION institution.current_member_ctx() TO authenticated;
GRANT ALL ON FUNCTION institution.current_member_ctx() TO service_role;

--
-- Name: FUNCTION current_member_ctx_v2(); Type: ACL; Schema: institution; Owner: -
--

REVOKE ALL ON FUNCTION institution.current_member_ctx_v2() FROM PUBLIC;
GRANT ALL ON FUNCTION institution.current_member_ctx_v2() TO authenticated;
GRANT ALL ON FUNCTION institution.current_member_ctx_v2() TO service_role;

--
-- Name: FUNCTION enforce_member_group_tenant(); Type: ACL; Schema: institution; Owner: -
--

REVOKE ALL ON FUNCTION institution.enforce_member_group_tenant() FROM PUBLIC;
GRANT ALL ON FUNCTION institution.enforce_member_group_tenant() TO service_role;

--
-- Name: FUNCTION esign_append_event(p_envelope uuid, p_signer uuid, p_event text, p_detail jsonb, p_ip inet, p_ua text); Type: ACL; Schema: institution; Owner: -
--

REVOKE ALL ON FUNCTION institution.esign_append_event(p_envelope uuid, p_signer uuid, p_event text, p_detail jsonb, p_ip inet, p_ua text) FROM PUBLIC;
GRANT ALL ON FUNCTION institution.esign_append_event(p_envelope uuid, p_signer uuid, p_event text, p_detail jsonb, p_ip inet, p_ua text) TO authenticated;
GRANT ALL ON FUNCTION institution.esign_append_event(p_envelope uuid, p_signer uuid, p_event text, p_detail jsonb, p_ip inet, p_ua text) TO service_role;

--
-- Name: FUNCTION esign_create_envelope(p_institution_id uuid, p_entity_type text, p_entity_id uuid, p_approval_instance uuid, p_title text, p_document_path text, p_document_sha256 text, p_expires_at timestamp with time zone, p_created_by uuid, p_signers jsonb); Type: ACL; Schema: institution; Owner: -
--

REVOKE ALL ON FUNCTION institution.esign_create_envelope(p_institution_id uuid, p_entity_type text, p_entity_id uuid, p_approval_instance uuid, p_title text, p_document_path text, p_document_sha256 text, p_expires_at timestamp with time zone, p_created_by uuid, p_signers jsonb) FROM PUBLIC;
GRANT ALL ON FUNCTION institution.esign_create_envelope(p_institution_id uuid, p_entity_type text, p_entity_id uuid, p_approval_instance uuid, p_title text, p_document_path text, p_document_sha256 text, p_expires_at timestamp with time zone, p_created_by uuid, p_signers jsonb) TO authenticated;
GRANT ALL ON FUNCTION institution.esign_create_envelope(p_institution_id uuid, p_entity_type text, p_entity_id uuid, p_approval_instance uuid, p_title text, p_document_path text, p_document_sha256 text, p_expires_at timestamp with time zone, p_created_by uuid, p_signers jsonb) TO service_role;

--
-- Name: FUNCTION esign_decline(p_signer uuid, p_reason text, p_ip inet, p_ua text); Type: ACL; Schema: institution; Owner: -
--

REVOKE ALL ON FUNCTION institution.esign_decline(p_signer uuid, p_reason text, p_ip inet, p_ua text) FROM PUBLIC;
GRANT ALL ON FUNCTION institution.esign_decline(p_signer uuid, p_reason text, p_ip inet, p_ua text) TO authenticated;
GRANT ALL ON FUNCTION institution.esign_decline(p_signer uuid, p_reason text, p_ip inet, p_ua text) TO service_role;

--
-- Name: FUNCTION esign_expire_overdue(); Type: ACL; Schema: institution; Owner: -
--

REVOKE ALL ON FUNCTION institution.esign_expire_overdue() FROM PUBLIC;
GRANT ALL ON FUNCTION institution.esign_expire_overdue() TO service_role;

--
-- Name: FUNCTION esign_sign(p_signer uuid, p_ip inet, p_ua text); Type: ACL; Schema: institution; Owner: -
--

REVOKE ALL ON FUNCTION institution.esign_sign(p_signer uuid, p_ip inet, p_ua text) FROM PUBLIC;
GRANT ALL ON FUNCTION institution.esign_sign(p_signer uuid, p_ip inet, p_ua text) TO authenticated;
GRANT ALL ON FUNCTION institution.esign_sign(p_signer uuid, p_ip inet, p_ua text) TO service_role;

--
-- Name: FUNCTION get_institution_bid_request_ids(p_institution_id uuid); Type: ACL; Schema: institution; Owner: -
--

REVOKE ALL ON FUNCTION institution.get_institution_bid_request_ids(p_institution_id uuid) FROM PUBLIC;
GRANT ALL ON FUNCTION institution.get_institution_bid_request_ids(p_institution_id uuid) TO authenticated;
GRANT ALL ON FUNCTION institution.get_institution_bid_request_ids(p_institution_id uuid) TO service_role;

--
-- Name: FUNCTION get_my_institution_id(); Type: ACL; Schema: institution; Owner: -
--

REVOKE ALL ON FUNCTION institution.get_my_institution_id() FROM PUBLIC;
GRANT ALL ON FUNCTION institution.get_my_institution_id() TO service_role;

--
-- Name: FUNCTION get_my_member_id(); Type: ACL; Schema: institution; Owner: -
--

REVOKE ALL ON FUNCTION institution.get_my_member_id() FROM PUBLIC;
GRANT ALL ON FUNCTION institution.get_my_member_id() TO service_role;

--
-- Name: FUNCTION get_my_modules(); Type: ACL; Schema: institution; Owner: -
--

REVOKE ALL ON FUNCTION institution.get_my_modules() FROM PUBLIC;
GRANT ALL ON FUNCTION institution.get_my_modules() TO authenticated;
GRANT ALL ON FUNCTION institution.get_my_modules() TO service_role;

--
-- Name: FUNCTION get_my_products(); Type: ACL; Schema: institution; Owner: -
--

REVOKE ALL ON FUNCTION institution.get_my_products() FROM PUBLIC;
GRANT ALL ON FUNCTION institution.get_my_products() TO authenticated;
GRANT ALL ON FUNCTION institution.get_my_products() TO service_role;

--
-- Name: FUNCTION has_module(p_module text); Type: ACL; Schema: institution; Owner: -
--

REVOKE ALL ON FUNCTION institution.has_module(p_module text) FROM PUBLIC;
GRANT ALL ON FUNCTION institution.has_module(p_module text) TO service_role;

--
-- Name: FUNCTION has_role(p_role text); Type: ACL; Schema: institution; Owner: -
--

REVOKE ALL ON FUNCTION institution.has_role(p_role text) FROM PUBLIC;
GRANT ALL ON FUNCTION institution.has_role(p_role text) TO service_role;

--
-- Name: FUNCTION is_active(); Type: ACL; Schema: institution; Owner: -
--

REVOKE ALL ON FUNCTION institution.is_active() FROM PUBLIC;
GRANT ALL ON FUNCTION institution.is_active() TO service_role;

--
-- Name: FUNCTION is_ficium_admin(); Type: ACL; Schema: institution; Owner: -
--

REVOKE ALL ON FUNCTION institution.is_ficium_admin() FROM PUBLIC;
GRANT ALL ON FUNCTION institution.is_ficium_admin() TO service_role;

--
-- Name: FUNCTION reject_action(p_action_id uuid, p_note text); Type: ACL; Schema: institution; Owner: -
--

REVOKE ALL ON FUNCTION institution.reject_action(p_action_id uuid, p_note text) FROM PUBLIC;
GRANT ALL ON FUNCTION institution.reject_action(p_action_id uuid, p_note text) TO authenticated;
GRANT ALL ON FUNCTION institution.reject_action(p_action_id uuid, p_note text) TO service_role;

--
-- Name: TABLE doc_type; Type: ACL; Schema: institution; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE institution.doc_type TO authenticated;

--
-- Name: FUNCTION required_doc_types(p_institution uuid); Type: ACL; Schema: institution; Owner: -
--

REVOKE ALL ON FUNCTION institution.required_doc_types(p_institution uuid) FROM PUBLIC;
GRANT ALL ON FUNCTION institution.required_doc_types(p_institution uuid) TO authenticated;

--
-- Name: FUNCTION set_updated_at(); Type: ACL; Schema: institution; Owner: -
--

GRANT ALL ON FUNCTION institution.set_updated_at() TO authenticated;
GRANT ALL ON FUNCTION institution.set_updated_at() TO service_role;

--
-- Name: FUNCTION submit_for_approval(p_action_category text, p_resource_type text, p_resource_id uuid, p_payload jsonb); Type: ACL; Schema: institution; Owner: -
--

REVOKE ALL ON FUNCTION institution.submit_for_approval(p_action_category text, p_resource_type text, p_resource_id uuid, p_payload jsonb) FROM PUBLIC;
GRANT ALL ON FUNCTION institution.submit_for_approval(p_action_category text, p_resource_type text, p_resource_id uuid, p_payload jsonb) TO authenticated;
GRANT ALL ON FUNCTION institution.submit_for_approval(p_action_category text, p_resource_type text, p_resource_id uuid, p_payload jsonb) TO service_role;

--
-- Name: FUNCTION sync_member_login_state(); Type: ACL; Schema: institution; Owner: -
--

REVOKE ALL ON FUNCTION institution.sync_member_login_state() FROM PUBLIC;

--
-- Name: FUNCTION _admin_command_log_worm(); Type: ACL; Schema: integration; Owner: -
--

REVOKE ALL ON FUNCTION integration._admin_command_log_worm() FROM PUBLIC;

--
-- Name: FUNCTION app_type_for_allocation(p_request_id uuid, p_product_id uuid); Type: ACL; Schema: integration; Owner: -
--

REVOKE ALL ON FUNCTION integration.app_type_for_allocation(p_request_id uuid, p_product_id uuid) FROM PUBLIC;

--
-- Name: FUNCTION app_type_for_product(p_code text); Type: ACL; Schema: integration; Owner: -
--

REVOKE ALL ON FUNCTION integration.app_type_for_product(p_code text) FROM PUBLIC;

--
-- Name: FUNCTION apply_chat_message(p_env jsonb); Type: ACL; Schema: integration; Owner: -
--

REVOKE ALL ON FUNCTION integration.apply_chat_message(p_env jsonb) FROM PUBLIC;

--
-- Name: FUNCTION apply_request_live(p_request_id uuid); Type: ACL; Schema: integration; Owner: -
--

REVOKE ALL ON FUNCTION integration.apply_request_live(p_request_id uuid) FROM PUBLIC;

--
-- Name: FUNCTION apply_request_published(p_env jsonb); Type: ACL; Schema: integration; Owner: -
--

REVOKE ALL ON FUNCTION integration.apply_request_published(p_env jsonb) FROM PUBLIC;

--
-- Name: FUNCTION apply_request_status_changed(p_env jsonb); Type: ACL; Schema: integration; Owner: -
--

REVOKE ALL ON FUNCTION integration.apply_request_status_changed(p_env jsonb) FROM PUBLIC;

--
-- Name: FUNCTION backfill_bid_events(); Type: ACL; Schema: integration; Owner: -
--

REVOKE ALL ON FUNCTION integration.backfill_bid_events() FROM PUBLIC;

--
-- Name: FUNCTION build_bid_payload(p_bid_id uuid); Type: ACL; Schema: integration; Owner: -
--

REVOKE ALL ON FUNCTION integration.build_bid_payload(p_bid_id uuid) FROM PUBLIC;

--
-- Name: FUNCTION build_deal_documents_payload(p_pipeline_id uuid); Type: ACL; Schema: integration; Owner: -
--

REVOKE ALL ON FUNCTION integration.build_deal_documents_payload(p_pipeline_id uuid) FROM PUBLIC;

--
-- Name: FUNCTION build_deal_progress_payload(p_pipeline_id uuid); Type: ACL; Schema: integration; Owner: -
--

REVOKE ALL ON FUNCTION integration.build_deal_progress_payload(p_pipeline_id uuid) FROM PUBLIC;

--
-- Name: FUNCTION build_market_intelligence(); Type: ACL; Schema: integration; Owner: -
--

REVOKE ALL ON FUNCTION integration.build_market_intelligence() FROM PUBLIC;

--
-- Name: FUNCTION claim_batch(p_limit integer, p_lease_seconds integer); Type: ACL; Schema: integration; Owner: -
--

REVOKE ALL ON FUNCTION integration.claim_batch(p_limit integer, p_lease_seconds integer) FROM PUBLIC;

--
-- Name: FUNCTION defer(p_id text); Type: ACL; Schema: integration; Owner: -
--

REVOKE ALL ON FUNCTION integration.defer(p_id text) FROM PUBLIC;

--
-- Name: FUNCTION enqueue(p_type text, p_aggregate_id text, p_data jsonb, p_source text); Type: ACL; Schema: integration; Owner: -
--

REVOKE ALL ON FUNCTION integration.enqueue(p_type text, p_aggregate_id text, p_data jsonb, p_source text) FROM PUBLIC;

--
-- Name: FUNCTION is_phase2(p_pipeline_id uuid); Type: ACL; Schema: integration; Owner: -
--

REVOKE ALL ON FUNCTION integration.is_phase2(p_pipeline_id uuid) FROM PUBLIC;

--
-- Name: FUNCTION mark_delivered(p_id text); Type: ACL; Schema: integration; Owner: -
--

REVOKE ALL ON FUNCTION integration.mark_delivered(p_id text) FROM PUBLIC;

--
-- Name: FUNCTION mark_failed(p_id text, p_error text); Type: ACL; Schema: integration; Owner: -
--

REVOKE ALL ON FUNCTION integration.mark_failed(p_id text, p_error text) FROM PUBLIC;

--
-- Name: FUNCTION publish_bid(p_bid_id uuid); Type: ACL; Schema: integration; Owner: -
--

REVOKE ALL ON FUNCTION integration.publish_bid(p_bid_id uuid) FROM PUBLIC;

--
-- Name: FUNCTION publish_deal_documents(p_pipeline_id uuid); Type: ACL; Schema: integration; Owner: -
--

REVOKE ALL ON FUNCTION integration.publish_deal_documents(p_pipeline_id uuid) FROM PUBLIC;

--
-- Name: FUNCTION publish_deal_progress(p_pipeline_id uuid); Type: ACL; Schema: integration; Owner: -
--

REVOKE ALL ON FUNCTION integration.publish_deal_progress(p_pipeline_id uuid) FROM PUBLIC;

--
-- Name: FUNCTION publish_market_intelligence(); Type: ACL; Schema: integration; Owner: -
--

REVOKE ALL ON FUNCTION integration.publish_market_intelligence() FROM PUBLIC;

--
-- Name: FUNCTION record_inbox(p_event_id text, p_type text, p_source text, p_aggregate_id text, p_sequence bigint); Type: ACL; Schema: integration; Owner: -
--

REVOKE ALL ON FUNCTION integration.record_inbox(p_event_id text, p_type text, p_source text, p_aggregate_id text, p_sequence bigint) FROM PUBLIC;

--
-- Name: FUNCTION trg_bid_child_emit(); Type: ACL; Schema: integration; Owner: -
--

REVOKE ALL ON FUNCTION integration.trg_bid_child_emit() FROM PUBLIC;

--
-- Name: FUNCTION trg_bid_emit(); Type: ACL; Schema: integration; Owner: -
--

REVOKE ALL ON FUNCTION integration.trg_bid_emit() FROM PUBLIC;

--
-- Name: FUNCTION trg_deal_documents_emit(); Type: ACL; Schema: integration; Owner: -
--

REVOKE ALL ON FUNCTION integration.trg_deal_documents_emit() FROM PUBLIC;

--
-- Name: FUNCTION trg_deal_progress_emit(); Type: ACL; Schema: integration; Owner: -
--

REVOKE ALL ON FUNCTION integration.trg_deal_progress_emit() FROM PUBLIC;

--
-- Name: FUNCTION trg_requirement_emit(); Type: ACL; Schema: integration; Owner: -
--

REVOKE ALL ON FUNCTION integration.trg_requirement_emit() FROM PUBLIC;

--
-- Name: FUNCTION accept_bid(p_request_id uuid, p_bid_id uuid, p_consumer_id uuid, p_phase2 jsonb); Type: ACL; Schema: marketplace; Owner: -
--

REVOKE ALL ON FUNCTION marketplace.accept_bid(p_request_id uuid, p_bid_id uuid, p_consumer_id uuid, p_phase2 jsonb) FROM PUBLIC;
GRANT ALL ON FUNCTION marketplace.accept_bid(p_request_id uuid, p_bid_id uuid, p_consumer_id uuid, p_phase2 jsonb) TO service_role;
GRANT ALL ON FUNCTION marketplace.accept_bid(p_request_id uuid, p_bid_id uuid, p_consumer_id uuid, p_phase2 jsonb) TO authenticated;

--
-- Name: FUNCTION advance_pipeline_stage(p_pipeline_id uuid, p_stage_inst_id uuid, p_actor_id uuid, p_notes text, p_is_approval boolean); Type: ACL; Schema: marketplace; Owner: -
--

REVOKE ALL ON FUNCTION marketplace.advance_pipeline_stage(p_pipeline_id uuid, p_stage_inst_id uuid, p_actor_id uuid, p_notes text, p_is_approval boolean) FROM PUBLIC;
GRANT ALL ON FUNCTION marketplace.advance_pipeline_stage(p_pipeline_id uuid, p_stage_inst_id uuid, p_actor_id uuid, p_notes text, p_is_approval boolean) TO service_role;
GRANT ALL ON FUNCTION marketplace.advance_pipeline_stage(p_pipeline_id uuid, p_stage_inst_id uuid, p_actor_id uuid, p_notes text, p_is_approval boolean) TO authenticated;

--
-- Name: FUNCTION booking_fee_inputs(p_pipeline_id uuid, p_institution_id uuid); Type: ACL; Schema: marketplace; Owner: -
--

REVOKE ALL ON FUNCTION marketplace.booking_fee_inputs(p_pipeline_id uuid, p_institution_id uuid) FROM PUBLIC;

--
-- Name: FUNCTION chat_thread_state(p_request_id uuid, p_institution_id uuid); Type: ACL; Schema: marketplace; Owner: -
--

REVOKE ALL ON FUNCTION marketplace.chat_thread_state(p_request_id uuid, p_institution_id uuid) FROM PUBLIC;

--
-- Name: FUNCTION close_deal(p_pipeline_id uuid, p_institution_id uuid, p_outcome text, p_reason text, p_actor uuid); Type: ACL; Schema: marketplace; Owner: -
--

REVOKE ALL ON FUNCTION marketplace.close_deal(p_pipeline_id uuid, p_institution_id uuid, p_outcome text, p_reason text, p_actor uuid) FROM PUBLIC;

--
-- Name: FUNCTION close_expired_windows(); Type: ACL; Schema: marketplace; Owner: -
--

REVOKE ALL ON FUNCTION marketplace.close_expired_windows() FROM PUBLIC;
GRANT ALL ON FUNCTION marketplace.close_expired_windows() TO service_role;
GRANT ALL ON FUNCTION marketplace.close_expired_windows() TO authenticated;

--
-- Name: FUNCTION create_pipeline_from_acceptance(p_request_id uuid, p_bid_id uuid, p_institution_id uuid, p_consumer_id uuid); Type: ACL; Schema: marketplace; Owner: -
--

REVOKE ALL ON FUNCTION marketplace.create_pipeline_from_acceptance(p_request_id uuid, p_bid_id uuid, p_institution_id uuid, p_consumer_id uuid) FROM PUBLIC;
GRANT ALL ON FUNCTION marketplace.create_pipeline_from_acceptance(p_request_id uuid, p_bid_id uuid, p_institution_id uuid, p_consumer_id uuid) TO service_role;
GRANT ALL ON FUNCTION marketplace.create_pipeline_from_acceptance(p_request_id uuid, p_bid_id uuid, p_institution_id uuid, p_consumer_id uuid) TO authenticated;

--
-- Name: FUNCTION deal_document_act(p_document_id uuid, p_institution_id uuid, p_action text, p_actor uuid, p_note text, p_storage_path text, p_file_name text, p_mime_type text, p_file_size bigint); Type: ACL; Schema: marketplace; Owner: -
--

REVOKE ALL ON FUNCTION marketplace.deal_document_act(p_document_id uuid, p_institution_id uuid, p_action text, p_actor uuid, p_note text, p_storage_path text, p_file_name text, p_mime_type text, p_file_size bigint) FROM PUBLIC;

--
-- Name: FUNCTION deal_template_for(p_institution_id uuid, p_product_id uuid); Type: ACL; Schema: marketplace; Owner: -
--

REVOKE ALL ON FUNCTION marketplace.deal_template_for(p_institution_id uuid, p_product_id uuid) FROM PUBLIC;

--
-- Name: FUNCTION decline_request(p_request_id uuid, p_institution_id uuid, p_actor uuid, p_reason text); Type: ACL; Schema: marketplace; Owner: -
--

REVOKE ALL ON FUNCTION marketplace.decline_request(p_request_id uuid, p_institution_id uuid, p_actor uuid, p_reason text) FROM PUBLIC;

--
-- Name: FUNCTION ensure_deal_documents(p_pipeline_id uuid); Type: ACL; Schema: marketplace; Owner: -
--

REVOKE ALL ON FUNCTION marketplace.ensure_deal_documents(p_pipeline_id uuid) FROM PUBLIC;

--
-- Name: FUNCTION expire_bids_on_request_end(); Type: ACL; Schema: marketplace; Owner: -
--

REVOKE ALL ON FUNCTION marketplace.expire_bids_on_request_end() FROM PUBLIC;

--
-- Name: FUNCTION guard_bid_compliance(); Type: ACL; Schema: marketplace; Owner: -
--

REVOKE ALL ON FUNCTION marketplace.guard_bid_compliance() FROM PUBLIC;
GRANT ALL ON FUNCTION marketplace.guard_bid_compliance() TO authenticated;
GRANT ALL ON FUNCTION marketplace.guard_bid_compliance() TO service_role;

--
-- Name: FUNCTION guard_bid_deal_template(); Type: ACL; Schema: marketplace; Owner: -
--

REVOKE ALL ON FUNCTION marketplace.guard_bid_deal_template() FROM PUBLIC;

--
-- Name: FUNCTION guard_bid_window(); Type: ACL; Schema: marketplace; Owner: -
--

GRANT ALL ON FUNCTION marketplace.guard_bid_window() TO service_role;

--
-- Name: FUNCTION ingest_app_request(p_app_request_id uuid, p_consumer_id uuid, p_app_product_type text, p_amount numeric, p_term_months integer, p_max_rate numeric, p_deadline timestamp with time zone, p_status text, p_created_at timestamp with time zone, p_phase1 jsonb, p_allocation_mode text, p_allocations jsonb); Type: ACL; Schema: marketplace; Owner: -
--

REVOKE ALL ON FUNCTION marketplace.ingest_app_request(p_app_request_id uuid, p_consumer_id uuid, p_app_product_type text, p_amount numeric, p_term_months integer, p_max_rate numeric, p_deadline timestamp with time zone, p_status text, p_created_at timestamp with time zone, p_phase1 jsonb, p_allocation_mode text, p_allocations jsonb) FROM PUBLIC;
GRANT ALL ON FUNCTION marketplace.ingest_app_request(p_app_request_id uuid, p_consumer_id uuid, p_app_product_type text, p_amount numeric, p_term_months integer, p_max_rate numeric, p_deadline timestamp with time zone, p_status text, p_created_at timestamp with time zone, p_phase1 jsonb, p_allocation_mode text, p_allocations jsonb) TO authenticated;
GRANT ALL ON FUNCTION marketplace.ingest_app_request(p_app_request_id uuid, p_consumer_id uuid, p_app_product_type text, p_amount numeric, p_term_months integer, p_max_rate numeric, p_deadline timestamp with time zone, p_status text, p_created_at timestamp with time zone, p_phase1 jsonb, p_allocation_mode text, p_allocations jsonb) TO service_role;

--
-- Name: FUNCTION ingest_request(p_app_request_id uuid, p_consumer_id uuid, p_app_product_type text, p_amount numeric, p_term_months integer, p_max_rate numeric, p_deadline timestamp with time zone, p_status text, p_created_at timestamp with time zone, p_phase1 jsonb, p_allocation_mode text, p_allocations jsonb); Type: ACL; Schema: marketplace; Owner: -
--

REVOKE ALL ON FUNCTION marketplace.ingest_request(p_app_request_id uuid, p_consumer_id uuid, p_app_product_type text, p_amount numeric, p_term_months integer, p_max_rate numeric, p_deadline timestamp with time zone, p_status text, p_created_at timestamp with time zone, p_phase1 jsonb, p_allocation_mode text, p_allocations jsonb) FROM PUBLIC;

--
-- Name: FUNCTION on_facility_approval_record_decision(); Type: ACL; Schema: marketplace; Owner: -
--

REVOKE ALL ON FUNCTION marketplace.on_facility_approval_record_decision() FROM PUBLIC;

--
-- Name: FUNCTION on_facility_approval_resolved(); Type: ACL; Schema: marketplace; Owner: -
--

REVOKE ALL ON FUNCTION marketplace.on_facility_approval_resolved() FROM PUBLIC;

--
-- Name: FUNCTION record_booking_closure(p_pipeline_id uuid, p_institution_id uuid, p_closed_on date, p_reason text, p_source text, p_actor_id uuid, p_api_key_id uuid); Type: ACL; Schema: marketplace; Owner: -
--

REVOKE ALL ON FUNCTION marketplace.record_booking_closure(p_pipeline_id uuid, p_institution_id uuid, p_closed_on date, p_reason text, p_source text, p_actor_id uuid, p_api_key_id uuid) FROM PUBLIC;

--
-- Name: FUNCTION record_credit_decision(p_pipeline_id uuid, p_institution_id uuid, p_outcome text, p_conditions jsonb, p_reason text, p_reference text, p_decided_at timestamp with time zone, p_source text, p_actor uuid, p_api_key uuid); Type: ACL; Schema: marketplace; Owner: -
--

REVOKE ALL ON FUNCTION marketplace.record_credit_decision(p_pipeline_id uuid, p_institution_id uuid, p_outcome text, p_conditions jsonb, p_reason text, p_reference text, p_decided_at timestamp with time zone, p_source text, p_actor uuid, p_api_key uuid) FROM PUBLIC;

--
-- Name: FUNCTION record_loan_booking(p_pipeline_id uuid, p_institution_id uuid, p_core_reference text, p_booked_on date, p_disbursed_amount numeric, p_disbursed_on date, p_notes text, p_source text, p_actor_id uuid, p_api_key_id uuid, p_correction boolean, p_monthly_amount numeric, p_term_months integer); Type: ACL; Schema: marketplace; Owner: -
--

REVOKE ALL ON FUNCTION marketplace.record_loan_booking(p_pipeline_id uuid, p_institution_id uuid, p_core_reference text, p_booked_on date, p_disbursed_amount numeric, p_disbursed_on date, p_notes text, p_source text, p_actor_id uuid, p_api_key_id uuid, p_correction boolean, p_monthly_amount numeric, p_term_months integer) FROM PUBLIC;

--
-- Name: FUNCTION relist_request(p_request_id uuid, p_consumer_id uuid, p_extend_hours integer); Type: ACL; Schema: marketplace; Owner: -
--

REVOKE ALL ON FUNCTION marketplace.relist_request(p_request_id uuid, p_consumer_id uuid, p_extend_hours integer) FROM PUBLIC;
GRANT ALL ON FUNCTION marketplace.relist_request(p_request_id uuid, p_consumer_id uuid, p_extend_hours integer) TO service_role;
GRANT ALL ON FUNCTION marketplace.relist_request(p_request_id uuid, p_consumer_id uuid, p_extend_hours integer) TO authenticated;

--
-- Name: FUNCTION send_institution_message(p_request_id uuid, p_institution_id uuid, p_member_id uuid, p_template_code text, p_params jsonb, p_body text); Type: ACL; Schema: marketplace; Owner: -
--

REVOKE ALL ON FUNCTION marketplace.send_institution_message(p_request_id uuid, p_institution_id uuid, p_member_id uuid, p_template_code text, p_params jsonb, p_body text) FROM PUBLIC;

--
-- Name: FUNCTION stage_documents_outstanding(p_stage_instance_id uuid); Type: ACL; Schema: marketplace; Owner: -
--

REVOKE ALL ON FUNCTION marketplace.stage_documents_outstanding(p_stage_instance_id uuid) FROM PUBLIC;

--
-- Name: FUNCTION strip_employer(); Type: ACL; Schema: marketplace; Owner: -
--

REVOKE ALL ON FUNCTION marketplace.strip_employer() FROM PUBLIC;

--
-- Name: FUNCTION trg_pipeline_documents(); Type: ACL; Schema: marketplace; Owner: -
--

REVOKE ALL ON FUNCTION marketplace.trg_pipeline_documents() FROM PUBLIC;

--
-- Name: FUNCTION _execute_dual_control_action(p_action_id uuid); Type: ACL; Schema: portal_admin; Owner: -
--

REVOKE ALL ON FUNCTION portal_admin._execute_dual_control_action(p_action_id uuid) FROM PUBLIC;
GRANT ALL ON FUNCTION portal_admin._execute_dual_control_action(p_action_id uuid) TO service_role;

--
-- Name: FUNCTION admin_approve_dual_control(p_action_id uuid, p_note text); Type: ACL; Schema: portal_admin; Owner: -
--

REVOKE ALL ON FUNCTION portal_admin.admin_approve_dual_control(p_action_id uuid, p_note text) FROM PUBLIC;
GRANT ALL ON FUNCTION portal_admin.admin_approve_dual_control(p_action_id uuid, p_note text) TO authenticated;
GRANT ALL ON FUNCTION portal_admin.admin_approve_dual_control(p_action_id uuid, p_note text) TO service_role;

--
-- Name: FUNCTION admin_reject_dual_control(p_action_id uuid, p_note text); Type: ACL; Schema: portal_admin; Owner: -
--

REVOKE ALL ON FUNCTION portal_admin.admin_reject_dual_control(p_action_id uuid, p_note text) FROM PUBLIC;
GRANT ALL ON FUNCTION portal_admin.admin_reject_dual_control(p_action_id uuid, p_note text) TO authenticated;
GRANT ALL ON FUNCTION portal_admin.admin_reject_dual_control(p_action_id uuid, p_note text) TO service_role;

--
-- Name: FUNCTION admin_submit_dual_control(action_category text, action_label text, risk text, resource_type text, resource_id uuid, resource_label text, payload jsonb, payload_before jsonb); Type: ACL; Schema: portal_admin; Owner: -
--

REVOKE ALL ON FUNCTION portal_admin.admin_submit_dual_control(action_category text, action_label text, risk text, resource_type text, resource_id uuid, resource_label text, payload jsonb, payload_before jsonb) FROM PUBLIC;
GRANT ALL ON FUNCTION portal_admin.admin_submit_dual_control(action_category text, action_label text, risk text, resource_type text, resource_id uuid, resource_label text, payload jsonb, payload_before jsonb) TO service_role;

--
-- Name: FUNCTION check_failed_logins(); Type: ACL; Schema: portal_admin; Owner: -
--

GRANT ALL ON FUNCTION portal_admin.check_failed_logins() TO authenticated;
GRANT ALL ON FUNCTION portal_admin.check_failed_logins() TO service_role;

--
-- Name: FUNCTION expire_dual_control_actions(); Type: ACL; Schema: portal_admin; Owner: -
--

REVOKE ALL ON FUNCTION portal_admin.expire_dual_control_actions() FROM PUBLIC;
GRANT ALL ON FUNCTION portal_admin.expire_dual_control_actions() TO service_role;

--
-- Name: FUNCTION get_institutions(); Type: ACL; Schema: portal_admin; Owner: -
--

REVOKE ALL ON FUNCTION portal_admin.get_institutions() FROM PUBLIC;
GRANT ALL ON FUNCTION portal_admin.get_institutions() TO service_role;

--
-- Name: FUNCTION get_my_group(); Type: ACL; Schema: portal_admin; Owner: -
--

REVOKE ALL ON FUNCTION portal_admin.get_my_group() FROM PUBLIC;
GRANT ALL ON FUNCTION portal_admin.get_my_group() TO authenticated;
GRANT ALL ON FUNCTION portal_admin.get_my_group() TO service_role;

--
-- Name: FUNCTION get_user_display_name(p_auth_user_id uuid); Type: ACL; Schema: portal_admin; Owner: -
--

REVOKE ALL ON FUNCTION portal_admin.get_user_display_name(p_auth_user_id uuid) FROM PUBLIC;
GRANT ALL ON FUNCTION portal_admin.get_user_display_name(p_auth_user_id uuid) TO authenticated;
GRANT ALL ON FUNCTION portal_admin.get_user_display_name(p_auth_user_id uuid) TO service_role;

--
-- Name: FUNCTION get_user_groups(); Type: ACL; Schema: portal_admin; Owner: -
--

REVOKE ALL ON FUNCTION portal_admin.get_user_groups() FROM PUBLIC;
GRANT ALL ON FUNCTION portal_admin.get_user_groups() TO authenticated;
GRANT ALL ON FUNCTION portal_admin.get_user_groups() TO service_role;

--
-- Name: FUNCTION has_permission(p_key text); Type: ACL; Schema: portal_admin; Owner: -
--

REVOKE ALL ON FUNCTION portal_admin.has_permission(p_key text) FROM PUBLIC;
GRANT ALL ON FUNCTION portal_admin.has_permission(p_key text) TO authenticated;
GRANT ALL ON FUNCTION portal_admin.has_permission(p_key text) TO service_role;

--
-- Name: FUNCTION is_admin(); Type: ACL; Schema: portal_admin; Owner: -
--

REVOKE ALL ON FUNCTION portal_admin.is_admin() FROM PUBLIC;
GRANT ALL ON FUNCTION portal_admin.is_admin() TO authenticated;
GRANT ALL ON FUNCTION portal_admin.is_admin() TO service_role;

--
-- Name: FUNCTION my_permissions(); Type: ACL; Schema: portal_admin; Owner: -
--

REVOKE ALL ON FUNCTION portal_admin.my_permissions() FROM PUBLIC;
GRANT ALL ON FUNCTION portal_admin.my_permissions() TO service_role;

--
-- Name: FUNCTION my_role_slug(); Type: ACL; Schema: portal_admin; Owner: -
--

REVOKE ALL ON FUNCTION portal_admin.my_role_slug() FROM PUBLIC;
GRANT ALL ON FUNCTION portal_admin.my_role_slug() TO service_role;

--
-- Name: FUNCTION set_updated_at(); Type: ACL; Schema: portal_admin; Owner: -
--

GRANT ALL ON FUNCTION portal_admin.set_updated_at() TO authenticated;
GRANT ALL ON FUNCTION portal_admin.set_updated_at() TO service_role;

--
-- Name: FUNCTION update_group_modules(p_group_id uuid, p_module_permissions text[]); Type: ACL; Schema: portal_admin; Owner: -
--

REVOKE ALL ON FUNCTION portal_admin.update_group_modules(p_group_id uuid, p_module_permissions text[]) FROM PUBLIC;
GRANT ALL ON FUNCTION portal_admin.update_group_modules(p_group_id uuid, p_module_permissions text[]) TO service_role;

--
-- Name: FUNCTION detect_portal_user_type(p_auth_user_id uuid); Type: ACL; Schema: public; Owner: -
--

REVOKE ALL ON FUNCTION public.detect_portal_user_type(p_auth_user_id uuid) FROM PUBLIC;
GRANT ALL ON FUNCTION public.detect_portal_user_type(p_auth_user_id uuid) TO service_role;

--
-- Name: FUNCTION get_admin_audit(p_limit integer, p_outcome text, p_category text); Type: ACL; Schema: public; Owner: -
--

REVOKE ALL ON FUNCTION public.get_admin_audit(p_limit integer, p_outcome text, p_category text) FROM PUBLIC;
GRANT ALL ON FUNCTION public.get_admin_audit(p_limit integer, p_outcome text, p_category text) TO service_role;

--
-- Name: FUNCTION get_admin_dual_control(p_status text); Type: ACL; Schema: public; Owner: -
--

REVOKE ALL ON FUNCTION public.get_admin_dual_control(p_status text) FROM PUBLIC;
GRANT ALL ON FUNCTION public.get_admin_dual_control(p_status text) TO service_role;

--
-- Name: FUNCTION get_admin_me(); Type: ACL; Schema: public; Owner: -
--

REVOKE ALL ON FUNCTION public.get_admin_me() FROM PUBLIC;
GRANT ALL ON FUNCTION public.get_admin_me() TO service_role;

--
-- Name: FUNCTION get_admin_metrics(); Type: ACL; Schema: public; Owner: -
--

REVOKE ALL ON FUNCTION public.get_admin_metrics() FROM PUBLIC;
GRANT ALL ON FUNCTION public.get_admin_metrics() TO service_role;

--
-- Name: FUNCTION get_admin_roles(); Type: ACL; Schema: public; Owner: -
--

REVOKE ALL ON FUNCTION public.get_admin_roles() FROM PUBLIC;
GRANT ALL ON FUNCTION public.get_admin_roles() TO service_role;

--
-- Name: FUNCTION get_admin_sessions(p_active_only boolean); Type: ACL; Schema: public; Owner: -
--

REVOKE ALL ON FUNCTION public.get_admin_sessions(p_active_only boolean) FROM PUBLIC;
GRANT ALL ON FUNCTION public.get_admin_sessions(p_active_only boolean) TO service_role;

--
-- Name: FUNCTION get_admin_users(p_status text); Type: ACL; Schema: public; Owner: -
--

REVOKE ALL ON FUNCTION public.get_admin_users(p_status text) FROM PUBLIC;
GRANT ALL ON FUNCTION public.get_admin_users(p_status text) TO service_role;

--
-- Name: FUNCTION rls_auto_enable(); Type: ACL; Schema: public; Owner: -
--

REVOKE ALL ON FUNCTION public.rls_auto_enable() FROM PUBLIC;
GRANT ALL ON FUNCTION public.rls_auto_enable() TO service_role;

--
-- Name: FUNCTION set_updated_at(); Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON FUNCTION public.set_updated_at() TO authenticated;
GRANT ALL ON FUNCTION public.set_updated_at() TO service_role;

--
-- Name: TABLE event; Type: ACL; Schema: audit; Owner: -
--

GRANT SELECT,INSERT ON TABLE audit.event TO authenticated;

--
-- Name: TABLE auth_audit_events; Type: ACL; Schema: auth_portal; Owner: -
--

GRANT ALL ON TABLE auth_portal.auth_audit_events TO service_role;

--
-- Name: TABLE auth_sessions; Type: ACL; Schema: auth_portal; Owner: -
--

GRANT ALL ON TABLE auth_portal.auth_sessions TO service_role;

--
-- Name: TABLE auth_users; Type: ACL; Schema: auth_portal; Owner: -
--

GRANT ALL ON TABLE auth_portal.auth_users TO service_role;

--
-- Name: COLUMN auth_users.id; Type: ACL; Schema: auth_portal; Owner: -
--

GRANT SELECT(id) ON TABLE auth_portal.auth_users TO authenticated;

--
-- Name: COLUMN auth_users.is_active; Type: ACL; Schema: auth_portal; Owner: -
--

GRANT SELECT(is_active) ON TABLE auth_portal.auth_users TO authenticated;

--
-- Name: TABLE email_verification_tokens; Type: ACL; Schema: auth_portal; Owner: -
--

GRANT ALL ON TABLE auth_portal.email_verification_tokens TO service_role;

--
-- Name: TABLE ip_allowlist; Type: ACL; Schema: auth_portal; Owner: -
--

GRANT ALL ON TABLE auth_portal.ip_allowlist TO service_role;

--
-- Name: TABLE mfa_backup_codes; Type: ACL; Schema: auth_portal; Owner: -
--

GRANT ALL ON TABLE auth_portal.mfa_backup_codes TO service_role;

--
-- Name: TABLE password_reset_tokens; Type: ACL; Schema: auth_portal; Owner: -
--

GRANT ALL ON TABLE auth_portal.password_reset_tokens TO service_role;

--
-- Name: TABLE execution; Type: ACL; Schema: autobid; Owner: -
--

GRANT SELECT ON TABLE autobid.execution TO authenticated;

--
-- Name: TABLE exposure; Type: ACL; Schema: autobid; Owner: -
--

GRANT SELECT ON TABLE autobid.exposure TO authenticated;

--
-- Name: TABLE rule; Type: ACL; Schema: autobid; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE autobid.rule TO authenticated;

--
-- Name: TABLE rule_version; Type: ACL; Schema: autobid; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE autobid.rule_version TO authenticated;

--
-- Name: TABLE country; Type: ACL; Schema: catalog; Owner: -
--

GRANT SELECT ON TABLE catalog.country TO authenticated;

--
-- Name: TABLE currency; Type: ACL; Schema: catalog; Owner: -
--

GRANT SELECT ON TABLE catalog.currency TO authenticated;

--
-- Name: TABLE module; Type: ACL; Schema: catalog; Owner: -
--

GRANT SELECT ON TABLE catalog.module TO authenticated;

--
-- Name: TABLE product; Type: ACL; Schema: catalog; Owner: -
--

GRANT SELECT ON TABLE catalog.product TO authenticated;

--
-- Name: TABLE product_document; Type: ACL; Schema: catalog; Owner: -
--

GRANT SELECT ON TABLE catalog.product_document TO authenticated;

--
-- Name: TABLE product_eligibility; Type: ACL; Schema: catalog; Owner: -
--

GRANT SELECT ON TABLE catalog.product_eligibility TO authenticated;

--
-- Name: TABLE product_family; Type: ACL; Schema: catalog; Owner: -
--

GRANT SELECT ON TABLE catalog.product_family TO authenticated;

--
-- Name: TABLE product_parameter; Type: ACL; Schema: catalog; Owner: -
--

GRANT SELECT ON TABLE catalog.product_parameter TO authenticated;

--
-- Name: TABLE product_rate_model; Type: ACL; Schema: catalog; Owner: -
--

GRANT SELECT ON TABLE catalog.product_rate_model TO authenticated;

--
-- Name: TABLE product_sla; Type: ACL; Schema: catalog; Owner: -
--

GRANT SELECT ON TABLE catalog.product_sla TO authenticated;

--
-- Name: TABLE regulator; Type: ACL; Schema: catalog; Owner: -
--

GRANT SELECT ON TABLE catalog.regulator TO authenticated;

--
-- Name: TABLE institution_entitlement; Type: ACL; Schema: entitlements; Owner: -
--

GRANT SELECT ON TABLE entitlements.institution_entitlement TO authenticated;

--
-- Name: TABLE module; Type: ACL; Schema: entitlements; Owner: -
--

GRANT SELECT ON TABLE entitlements.module TO authenticated;

--
-- Name: TABLE usage_event; Type: ACL; Schema: entitlements; Owner: -
--

GRANT SELECT ON TABLE entitlements.usage_event TO authenticated;

--
-- Name: TABLE action; Type: ACL; Schema: governance; Owner: -
--

GRANT SELECT ON TABLE governance.action TO authenticated;

--
-- Name: COLUMN action.execution_status; Type: ACL; Schema: governance; Owner: -
--

GRANT UPDATE(execution_status) ON TABLE governance.action TO authenticated;

--
-- Name: COLUMN action.executed_at; Type: ACL; Schema: governance; Owner: -
--

GRANT UPDATE(executed_at) ON TABLE governance.action TO authenticated;

--
-- Name: COLUMN action.execution_error; Type: ACL; Schema: governance; Owner: -
--

GRANT UPDATE(execution_error) ON TABLE governance.action TO authenticated;

--
-- Name: TABLE four_eyes_catalog; Type: ACL; Schema: governance; Owner: -
--

GRANT SELECT ON TABLE governance.four_eyes_catalog TO authenticated;

--
-- Name: TABLE four_eyes_policy; Type: ACL; Schema: governance; Owner: -
--

GRANT SELECT ON TABLE governance.four_eyes_policy TO authenticated;

--
-- Name: TABLE api_key; Type: ACL; Schema: institution; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE institution.api_key TO authenticated;

--
-- Name: TABLE approval_action; Type: ACL; Schema: institution; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE institution.approval_action TO authenticated;

--
-- Name: TABLE approval_committee; Type: ACL; Schema: institution; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE institution.approval_committee TO authenticated;

--
-- Name: TABLE approval_delegation; Type: ACL; Schema: institution; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE institution.approval_delegation TO authenticated;

--
-- Name: TABLE approval_instance; Type: ACL; Schema: institution; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE institution.approval_instance TO authenticated;

--
-- Name: TABLE approval_outbox; Type: ACL; Schema: institution; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE institution.approval_outbox TO authenticated;

--
-- Name: SEQUENCE approval_outbox_id_seq; Type: ACL; Schema: institution; Owner: -
--

GRANT SELECT,USAGE ON SEQUENCE institution.approval_outbox_id_seq TO authenticated;

--
-- Name: TABLE approval_stage_def; Type: ACL; Schema: institution; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE institution.approval_stage_def TO authenticated;

--
-- Name: TABLE approval_stage_instance; Type: ACL; Schema: institution; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE institution.approval_stage_instance TO authenticated;

--
-- Name: TABLE approval_template; Type: ACL; Schema: institution; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE institution.approval_template TO authenticated;

--
-- Name: TABLE benefit; Type: ACL; Schema: institution; Owner: -
--

GRANT SELECT ON TABLE institution.benefit TO authenticated;

--
-- Name: TABLE benefit_cat; Type: ACL; Schema: institution; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE institution.benefit_cat TO authenticated;

--
-- Name: TABLE committee_member; Type: ACL; Schema: institution; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE institution.committee_member TO authenticated;

--
-- Name: TABLE doc; Type: ACL; Schema: institution; Owner: -
--

GRANT SELECT,INSERT,DELETE ON TABLE institution.doc TO authenticated;

--
-- Name: TABLE institution; Type: ACL; Schema: institution; Owner: -
--

GRANT SELECT,DELETE,UPDATE ON TABLE institution.institution TO authenticated;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE institution.institution TO service_role;

--
-- Name: TABLE compliance; Type: ACL; Schema: institution; Owner: -
--

GRANT SELECT ON TABLE institution.compliance TO authenticated;

--
-- Name: TABLE doa_rule; Type: ACL; Schema: institution; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE institution.doa_rule TO authenticated;

--
-- Name: TABLE doc_generation; Type: ACL; Schema: institution; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE institution.doc_generation TO authenticated;

--
-- Name: TABLE doc_requirement; Type: ACL; Schema: institution; Owner: -
--

GRANT SELECT ON TABLE institution.doc_requirement TO authenticated;

--
-- Name: TABLE doc_template; Type: ACL; Schema: institution; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE institution.doc_template TO authenticated;

--
-- Name: TABLE doc_template_version; Type: ACL; Schema: institution; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE institution.doc_template_version TO authenticated;

--
-- Name: TABLE esign_envelope; Type: ACL; Schema: institution; Owner: -
--

GRANT SELECT,UPDATE ON TABLE institution.esign_envelope TO authenticated;

--
-- Name: TABLE esign_event; Type: ACL; Schema: institution; Owner: -
--

GRANT SELECT ON TABLE institution.esign_event TO authenticated;

--
-- Name: SEQUENCE esign_event_id_seq; Type: ACL; Schema: institution; Owner: -
--

GRANT SELECT,USAGE ON SEQUENCE institution.esign_event_id_seq TO authenticated;

--
-- Name: TABLE esign_signer; Type: ACL; Schema: institution; Owner: -
--

GRANT SELECT ON TABLE institution.esign_signer TO authenticated;

--
-- Name: TABLE "group"; Type: ACL; Schema: institution; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE institution."group" TO authenticated;

--
-- Name: TABLE institution_sla_config; Type: ACL; Schema: institution; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE institution.institution_sla_config TO authenticated;

--
-- Name: TABLE kyb_document; Type: ACL; Schema: institution; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE institution.kyb_document TO authenticated;

--
-- Name: TABLE member; Type: ACL; Schema: institution; Owner: -
--

GRANT SELECT ON TABLE institution.member TO authenticated;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE institution.member TO service_role;

--
-- Name: TABLE pipeline_stage_def; Type: ACL; Schema: institution; Owner: -
--

GRANT SELECT,INSERT,UPDATE ON TABLE institution.pipeline_stage_def TO service_role;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE institution.pipeline_stage_def TO authenticated;

--
-- Name: TABLE pipeline_template; Type: ACL; Schema: institution; Owner: -
--

GRANT SELECT,INSERT,UPDATE ON TABLE institution.pipeline_template TO service_role;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE institution.pipeline_template TO authenticated;

--
-- Name: TABLE product_config; Type: ACL; Schema: institution; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE institution.product_config TO authenticated;

--
-- Name: TABLE webhook; Type: ACL; Schema: institution; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE institution.webhook TO authenticated;

--
-- Name: TABLE webhook_delivery; Type: ACL; Schema: institution; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE institution.webhook_delivery TO authenticated;

--
-- Name: TABLE request; Type: ACL; Schema: marketplace; Owner: -
--

GRANT SELECT ON TABLE marketplace.request TO authenticated;

--
-- Name: TABLE request_allocation; Type: ACL; Schema: marketplace; Owner: -
--

GRANT SELECT ON TABLE marketplace.request_allocation TO authenticated;

--
-- Name: TABLE acceptance; Type: ACL; Schema: marketplace; Owner: -
--

GRANT SELECT ON TABLE marketplace.acceptance TO authenticated;

--
-- Name: TABLE bid; Type: ACL; Schema: marketplace; Owner: -
--

GRANT SELECT ON TABLE marketplace.bid TO authenticated;

--
-- Name: TABLE bid_acceptance; Type: ACL; Schema: marketplace; Owner: -
--

GRANT SELECT ON TABLE marketplace.bid_acceptance TO authenticated;

--
-- Name: TABLE bid_allocation; Type: ACL; Schema: marketplace; Owner: -
--

GRANT SELECT ON TABLE marketplace.bid_allocation TO authenticated;

--
-- Name: TABLE bid_benefit; Type: ACL; Schema: marketplace; Owner: -
--

GRANT SELECT ON TABLE marketplace.bid_benefit TO authenticated;

--
-- Name: TABLE bid_event; Type: ACL; Schema: marketplace; Owner: -
--

GRANT SELECT ON TABLE marketplace.bid_event TO authenticated;

--
-- Name: TABLE credit_decision; Type: ACL; Schema: marketplace; Owner: -
--

GRANT SELECT ON TABLE marketplace.credit_decision TO authenticated;

--
-- Name: TABLE deal_document; Type: ACL; Schema: marketplace; Owner: -
--

GRANT SELECT ON TABLE marketplace.deal_document TO authenticated;

--
-- Name: TABLE deal_document_event; Type: ACL; Schema: marketplace; Owner: -
--

GRANT SELECT ON TABLE marketplace.deal_document_event TO authenticated;

--
-- Name: TABLE loan_booking; Type: ACL; Schema: marketplace; Owner: -
--

GRANT SELECT ON TABLE marketplace.loan_booking TO authenticated;

--
-- Name: TABLE loan_pipeline; Type: ACL; Schema: marketplace; Owner: -
--

GRANT SELECT,INSERT,UPDATE ON TABLE marketplace.loan_pipeline TO service_role;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE marketplace.loan_pipeline TO authenticated;

--
-- Name: TABLE my_bids; Type: ACL; Schema: marketplace; Owner: -
--

GRANT SELECT ON TABLE marketplace.my_bids TO authenticated;

--
-- Name: TABLE pipeline_stage_instance; Type: ACL; Schema: marketplace; Owner: -
--

GRANT SELECT,INSERT,UPDATE ON TABLE marketplace.pipeline_stage_instance TO service_role;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE marketplace.pipeline_stage_instance TO authenticated;

--
-- Name: TABLE request_decline; Type: ACL; Schema: marketplace; Owner: -
--

GRANT SELECT ON TABLE marketplace.request_decline TO authenticated;

--
-- Name: TABLE admin_audit_log; Type: ACL; Schema: portal_admin; Owner: -
--

GRANT SELECT ON TABLE portal_admin.admin_audit_log TO authenticated;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE portal_admin.admin_audit_log TO service_role;

--
-- Name: TABLE admin_dual_control_actions; Type: ACL; Schema: portal_admin; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE portal_admin.admin_dual_control_actions TO authenticated;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE portal_admin.admin_dual_control_actions TO service_role;

--
-- Name: TABLE admin_roles; Type: ACL; Schema: portal_admin; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE portal_admin.admin_roles TO authenticated;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE portal_admin.admin_roles TO service_role;

--
-- Name: TABLE admin_sessions; Type: ACL; Schema: portal_admin; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE portal_admin.admin_sessions TO authenticated;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE portal_admin.admin_sessions TO service_role;

--
-- Name: TABLE admin_users; Type: ACL; Schema: portal_admin; Owner: -
--

GRANT SELECT ON TABLE portal_admin.admin_users TO authenticated;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE portal_admin.admin_users TO service_role;

--
-- Name: TABLE commission_event; Type: ACL; Schema: portal_admin; Owner: -
--

GRANT SELECT,INSERT,UPDATE ON TABLE portal_admin.commission_event TO service_role;

--
-- Name: TABLE user_groups; Type: ACL; Schema: portal_admin; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE portal_admin.user_groups TO authenticated;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE portal_admin.user_groups TO service_role;

--
-- Name: TABLE _identity_migration_log; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public._identity_migration_log TO service_role;

--
-- Name: SEQUENCE _identity_migration_log_id_seq; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON SEQUENCE public._identity_migration_log_id_seq TO authenticated;
GRANT ALL ON SEQUENCE public._identity_migration_log_id_seq TO service_role;

--
-- Name: TABLE portal_notifications; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.portal_notifications TO authenticated;
GRANT ALL ON TABLE public.portal_notifications TO service_role;

--
-- Name: DEFAULT PRIVILEGES FOR TABLES; Type: DEFAULT ACL; Schema: catalog; Owner: -
--

ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA catalog GRANT SELECT ON TABLES TO authenticated;

--
-- Name: DEFAULT PRIVILEGES FOR SEQUENCES; Type: DEFAULT ACL; Schema: public; Owner: -
--

ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON SEQUENCES TO postgres;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON SEQUENCES TO anon;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON SEQUENCES TO authenticated;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON SEQUENCES TO service_role;

--
-- Name: DEFAULT PRIVILEGES FOR FUNCTIONS; Type: DEFAULT ACL; Schema: public; Owner: -
--

ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON FUNCTIONS TO postgres;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON FUNCTIONS TO authenticated;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON FUNCTIONS TO service_role;

--
-- Name: DEFAULT PRIVILEGES FOR TABLES; Type: DEFAULT ACL; Schema: public; Owner: -
--

ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON TABLES TO postgres;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT SELECT,INSERT,DELETE,UPDATE ON TABLES TO authenticated;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON TABLES TO service_role;

--
-- PostgreSQL database dump complete
--
