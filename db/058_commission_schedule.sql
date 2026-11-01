-- 058_commission_schedule.sql
-- Ficium's commission becomes configurable from the portal admin site.
-- Until now every booking wrote portal_admin.commission_event with the column
-- default rate (0.5%), the same for every bank and product.
--
-- billing.commission_rule holds the schedule. A rule has a scope (bank and/or
-- product, either may be "all"), a rate and an effective date. It is never
-- edited: a new rule for the same scope closes the open one on the new start
-- date, so the history of what was charged, when, stays intact. The most
-- specific scope in force on the booking date wins:
--     bank + product  >  bank  >  product  >  platform default
-- Rules are written only by the admin command channel (four-eyes: a maker in
-- the portal admin site, a different approver), through the two functions
-- below. No client role can read or write the schema.
--
-- The platform default is seeded at the previous hardcoded 0.5% so nothing
-- changes until an admin sets a schedule. It can be replaced, never ended,
-- so a booking always finds a rate.
--
-- Basis: 'booked_amount' (the amount on the booking) is the only basis today.
-- A first-year-inflow basis for investments needs a true-up process and is
-- not built.
--
-- Banks without the deals ("pipeline") module get no deal and therefore no
-- booking and no commission event (decision KJ, 2026-11-01).

begin;

create schema if not exists billing;
revoke all on schema billing from public, anon, authenticated;

create table billing.commission_rule (
  id             uuid primary key default gen_random_uuid(),
  institution_id uuid references institution.institution(id),
  product_code   text,
  rate           numeric(6,4) not null check (rate >= 0 and rate <= 0.05),  -- same precision as commission_event
  basis          text not null default 'booked_amount' check (basis in ('booked_amount')),
  effective_from date not null,
  ended_on       date,
  reason         text not null check (char_length(btrim(reason)) >= 10),
  command_id     uuid not null unique,
  set_by         uuid not null,
  approved_by    uuid not null,
  ended_by       uuid,
  end_command_id uuid unique,
  created_at     timestamptz not null default now(),
  check (ended_on is null or ended_on > effective_from)
);
-- one open rule per scope
create unique index commission_rule_open_scope on billing.commission_rule
  (coalesce(institution_id, '00000000-0000-0000-0000-000000000000'::uuid), coalesce(product_code, ''))
  where ended_on is null;
create index commission_rule_lookup on billing.commission_rule (institution_id, product_code, effective_from);
alter table billing.commission_rule enable row level security;
alter table billing.commission_rule force row level security;
-- no policies: only the owner / service role (admin channel, booking) reaches it
revoke all on billing.commission_rule from public, anon, authenticated;

insert into billing.commission_rule (rate, effective_from, reason, command_id, set_by, approved_by)
values (0.005, date '2026-01-01', 'Seeded from the previous hardcoded 0.5% default (db/058)',
        gen_random_uuid(), '00000000-0000-0000-0000-000000000000', '00000000-0000-0000-0000-000000000000');

-- ── resolution ───────────────────────────────────────────────────────────────
create or replace function billing.commission_for(p_institution_id uuid, p_product_code text, p_on date)
  returns table (rule_id uuid, rate numeric) language sql stable set search_path = '' as
$$
  select r.id, r.rate
    from billing.commission_rule r
   where r.effective_from <= p_on
     and (r.ended_on is null or r.ended_on > p_on)
     and (r.institution_id is null or r.institution_id = p_institution_id)
     and (r.product_code   is null or r.product_code   = p_product_code)
   order by (r.institution_id is not null) desc, (r.product_code is not null) desc, r.effective_from desc
   limit 1
$$;

-- ── writes (admin channel only) ──────────────────────────────────────────────
create or replace function billing.set_commission_rule(
  p_institution_id uuid, p_product_code text, p_rate numeric, p_effective_from date,
  p_reason text, p_command_id uuid, p_maker uuid, p_checker uuid)
  returns billing.commission_rule language plpgsql set search_path = '' as
$$
declare
  v_today date := (now() at time zone 'Indian/Mauritius')::date;
  v_open  billing.commission_rule;
  v_new   billing.commission_rule;
begin
  if p_maker = p_checker then
    raise exception using errcode = 'P0001', message = 'COMMISSION_FOUR_EYES';
  end if;
  if p_rate is null or p_rate < 0 or p_rate > 0.05 then
    raise exception using errcode = 'P0001', message = 'COMMISSION_RATE_INVALID';
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

  insert into billing.commission_rule (institution_id, product_code, rate, effective_from, reason,
                                       command_id, set_by, approved_by)
  values (p_institution_id, p_product_code, p_rate, p_effective_from, btrim(p_reason),
          p_command_id, p_maker, p_checker)
  returning * into v_new;
  return v_new;
end
$$;

create or replace function billing.end_commission_rule(
  p_rule_id uuid, p_ended_on date, p_command_id uuid, p_maker uuid, p_checker uuid)
  returns billing.commission_rule language plpgsql set search_path = '' as
$$
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

revoke all on function billing.commission_for(uuid, text, date) from public, anon, authenticated;
revoke all on function billing.set_commission_rule(uuid, text, numeric, date, text, uuid, uuid, uuid) from public, anon, authenticated;
revoke all on function billing.end_commission_rule(uuid, date, uuid, uuid, uuid) from public, anon, authenticated;

-- ── the booking uses the schedule ────────────────────────────────────────────
alter table portal_admin.commission_event
  add column if not exists rule_id uuid references billing.commission_rule(id);

create or replace function marketplace.record_loan_booking(p_pipeline_id uuid, p_institution_id uuid, p_core_reference text, p_booked_on date, p_disbursed_amount numeric, p_disbursed_on date, p_notes text, p_source text, p_actor_id uuid, p_api_key_id uuid, p_correction boolean DEFAULT false)
 RETURNS jsonb
 LANGUAGE plpgsql
 SET search_path TO 'marketplace', 'portal_admin', 'pg_temp'
AS $function$
DECLARE
    v_deal     marketplace.loan_pipeline%ROWTYPE;
    v_prev     marketplace.loan_booking%ROWTYPE;
    v_row      marketplace.loan_booking%ROWTYPE;
    v_ref      text := btrim(p_core_reference);
    v_today    date := (now() AT TIME ZONE 'Indian/Mauritius')::date;
    v_comm     text;
    v_product  text;
    v_rule     uuid;
    v_rate     numeric;
BEGIN
    SELECT * INTO v_deal FROM marketplace.loan_pipeline
     WHERE id = p_pipeline_id AND institution_id = p_institution_id
     FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'BOOKING_DEAL_NOT_FOUND'; END IF;
    IF v_deal.status <> 'completed' THEN RAISE EXCEPTION 'BOOKING_DEAL_NOT_COMPLETE'; END IF;

    IF v_ref IS NULL OR v_ref = '' OR char_length(v_ref) > 64 THEN
        RAISE EXCEPTION 'BOOKING_REFERENCE_INVALID';
    END IF;
    IF p_disbursed_amount IS NULL OR p_disbursed_amount <= 0 THEN
        RAISE EXCEPTION 'BOOKING_AMOUNT_INVALID';
    END IF;
    IF v_deal.deal_amount IS NOT NULL AND p_disbursed_amount > v_deal.deal_amount THEN
        RAISE EXCEPTION 'BOOKING_AMOUNT_EXCEEDS_DEAL';
    END IF;
    IF p_booked_on IS NULL OR p_booked_on > v_today THEN
        RAISE EXCEPTION 'BOOKING_DATE_INVALID';
    END IF;
    IF p_booked_on < v_deal.started_at::date THEN
        RAISE EXCEPTION 'BOOKING_DATE_BEFORE_DEAL';
    END IF;
    IF p_disbursed_on IS NOT NULL AND (p_disbursed_on > v_today OR p_disbursed_on < v_deal.started_at::date) THEN
        RAISE EXCEPTION 'BOOKING_DISBURSED_DATE_INVALID';
    END IF;
    IF p_source NOT IN ('portal', 'api') THEN RAISE EXCEPTION 'BOOKING_SOURCE_INVALID'; END IF;

    SELECT * INTO v_prev FROM marketplace.loan_booking WHERE pipeline_id = p_pipeline_id FOR UPDATE;
    IF FOUND AND NOT p_correction THEN RAISE EXCEPTION 'BOOKING_ALREADY_RECORDED'; END IF;
    IF NOT FOUND AND p_correction THEN RAISE EXCEPTION 'BOOKING_NOT_FOUND'; END IF;

    BEGIN
        IF p_correction THEN
            UPDATE marketplace.loan_booking
               SET core_reference   = v_ref,
                   booked_on        = p_booked_on,
                   disbursed_amount = p_disbursed_amount,
                   disbursed_on     = p_disbursed_on,
                   notes            = p_notes,
                   source           = p_source,
                   recorded_by      = p_actor_id,
                   api_key_id       = p_api_key_id,
                   corrected_count  = corrected_count + 1,
                   updated_at       = now()
             WHERE pipeline_id = p_pipeline_id
            RETURNING * INTO v_row;
        ELSE
            INSERT INTO marketplace.loan_booking
                (pipeline_id, institution_id, core_reference, booked_on, disbursed_amount,
                 disbursed_on, notes, source, recorded_by, api_key_id)
            VALUES
                (p_pipeline_id, p_institution_id, v_ref, p_booked_on, p_disbursed_amount,
                 p_disbursed_on, p_notes, p_source, p_actor_id, p_api_key_id)
            RETURNING * INTO v_row;
        END IF;
    EXCEPTION WHEN unique_violation THEN
        RAISE EXCEPTION 'BOOKING_REFERENCE_IN_USE';
    END;

    -- Rate in force on the booking date for this bank and product (db/058).
    SELECT p.code INTO v_product
      FROM marketplace.request r JOIN catalog.product p ON p.id = r.product_id
     WHERE r.id = v_deal.request_id;
    SELECT c.rule_id, c.rate INTO v_rule, v_rate
      FROM billing.commission_for(p_institution_id, v_product, p_booked_on) c;
    IF v_rule IS NULL THEN RAISE EXCEPTION 'BOOKING_NO_COMMISSION_RULE'; END IF;

    INSERT INTO portal_admin.commission_event (pipeline_id, request_id, institution_id, deal_amount,
                                               commission_rate, rule_id)
    VALUES (p_pipeline_id, v_deal.request_id, p_institution_id, p_disbursed_amount, v_rate, v_rule)
    ON CONFLICT (pipeline_id) DO UPDATE
        SET deal_amount = EXCLUDED.deal_amount, commission_rate = EXCLUDED.commission_rate,
            rule_id = EXCLUDED.rule_id, updated_at = now()
        WHERE portal_admin.commission_event.status = 'pending';
    SELECT status INTO v_comm FROM portal_admin.commission_event WHERE pipeline_id = p_pipeline_id;

    RETURN jsonb_build_object(
        'booking', to_jsonb(v_row),
        'previous', CASE WHEN p_correction THEN to_jsonb(v_prev) END,
        'commission_status', v_comm
    );
END;
$function$;

do $verify$
declare v record;
begin
  select * into v from billing.commission_for(gen_random_uuid(), 'home_loan', current_date);
  if v.rate is distinct from 0.005 then raise exception '058: default rate not resolvable'; end if;
end
$verify$;

commit;
