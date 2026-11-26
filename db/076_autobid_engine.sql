-- =============================================================================
-- 076 — Autobid engine: per-bank decision and approval modes, the bank's own
--       decision engine (open-banking style), and the worker's bookkeeping.
--
-- 010 shipped the rules and their governance but nothing ever placed a bid.
-- This adds what the engine needs, and lets each bank fit autobid to its own
-- credit process instead of one fixed flow:
--
--   decision_mode   rules                Ficium evaluates the bank's approved rules
--                   external             Ficium asks the bank's own decision engine
--                                        (signed HTTPS call, bank answers bid/decline)
--                   rules_then_external  rules pre-filter, then the bank's engine
--                                        decides and prices
--   approval_mode   queue_for_approval   every auto-bid waits for a checker in the
--                                        bank's normal bid approval (four-eyes per bid)
--                   pre_approved         the rule's / engine's own approval is the
--                                        four eyes: the bid goes out at once, like a
--                                        bid through the bank API (/v1/bids)
--
-- A. marketplace.bid.submitted_via accepts 'autobid', so every auto-bid is
--    labelled as such (attribution to the rule version lives in autobid.execution).
-- B. autobid.institution_config: the live setting per bank. Written only by the
--    API on the service session, after a second person approves the change
--    (autobid.config_change). Turning autobid OFF is one person, instant.
-- C. autobid.config_change: the four-eyes request for a setting change.
-- D. autobid.claim: one evaluation per (bank, request), ever. The worker claims
--    before it evaluates, so two replicas or a retry can never bid twice.
-- E. autobid.execution: rule columns nullable (an external-engine decision has
--    no rule), and the outcomes the engine reports.
-- F. autobid.queue_bid(): puts an auto-bid in the bank's bid approval queue as a
--    governance.action, maker = the member who submitted the rule (so that person
--    cannot also approve the bid), described like any other bid.submit.
-- G. inst:autobid granted to the institution_admin group.
--
-- Guarded and additive: runs in order on a fresh cell. No rows are touched.
-- =============================================================================

-- ── A. submitted_via ────────────────────────────────────────────────────────
alter table marketplace.bid drop constraint if exists bid_submitted_via_check;
alter table marketplace.bid add constraint bid_submitted_via_check
  check (submitted_via = any (array['portal', 'api', 'webhook', 'core_banking', 'autobid']));

-- ── B. per-bank configuration ───────────────────────────────────────────────
create table if not exists autobid.institution_config (
  institution_id          uuid primary key references institution.institution(id) on delete cascade,
  enabled                 boolean not null default false,
  decision_mode           text not null default 'rules'
                          check (decision_mode in ('rules', 'external', 'rules_then_external')),
  approval_mode           text not null default 'queue_for_approval'
                          check (approval_mode in ('queue_for_approval', 'pre_approved')),
  engine_url              text check (engine_url is null or engine_url ~ '^https://[^\s]{4,2000}$'),
  engine_signing_secret   text,   -- HMAC key Ficium signs engine calls with; shown once
  engine_secret_set_at    timestamptz,
  engine_timeout_ms       int  not null default 5000 check (engine_timeout_ms between 500 and 10000),
  engine_failure_mode     text not null default 'skip'
                          check (engine_failure_mode in ('skip', 'use_rule')),
  max_bids_per_day        int  not null default 20 check (max_bids_per_day between 1 and 10000),
  max_amount_per_day      numeric(16,2) not null default 10000000 check (max_amount_per_day > 0),
  product_codes           text[],          -- null = every product the bank has enabled
  version                 int  not null default 0,
  changed_by              uuid,
  approved_by             uuid,
  updated_at              timestamptz not null default now(),
  check (decision_mode = 'rules' or engine_url is not null or not enabled),
  check (engine_failure_mode = 'skip' or decision_mode = 'rules_then_external')
);

-- ── C. four-eyes change requests ────────────────────────────────────────────
create table if not exists autobid.config_change (
  id               uuid primary key default gen_random_uuid(),
  institution_id   uuid not null references institution.institution(id) on delete cascade,
  proposed         jsonb not null,
  before           jsonb,
  reason           text not null check (char_length(reason) between 3 and 500),
  status           text not null default 'pending'
                   check (status in ('pending', 'approved', 'rejected', 'cancelled', 'expired')),
  proposed_by      uuid not null,
  decided_by       uuid,
  decided_at       timestamptz,
  decision_note    text check (decision_note is null or char_length(decision_note) <= 500),
  expires_at       timestamptz not null default now() + interval '7 days',
  created_at       timestamptz not null default now(),
  check (decided_by is null or decided_by <> proposed_by)
);
create index if not exists idx_ab_config_change_inst
  on autobid.config_change (institution_id, created_at desc);
create unique index if not exists uq_ab_config_change_one_pending
  on autobid.config_change (institution_id) where status = 'pending';

-- ── D. one evaluation per (bank, request) ───────────────────────────────────
create table if not exists autobid.claim (
  institution_id   uuid not null,
  request_id       uuid not null,
  claimed_at       timestamptz not null default now(),
  primary key (institution_id, request_id)
);

-- ── E. execution log ────────────────────────────────────────────────────────
alter table autobid.execution alter column rule_id drop not null;
alter table autobid.execution alter column rule_version_id drop not null;
alter table autobid.execution drop constraint if exists execution_outcome_check;
alter table autobid.execution add constraint execution_outcome_check
  check (outcome = any (array[
    'bid_placed', 'queued_for_approval', 'declined_by_engine', 'no_match',
    'suppressed_guardrail', 'suppressed_exposure', 'skipped_compliance',
    'skipped_product', 'engine_unavailable', 'error']));

-- ── RLS + grants ────────────────────────────────────────────────────────────
alter table autobid.institution_config enable row level security;
alter table autobid.institution_config force row level security;
alter table autobid.config_change enable row level security;
alter table autobid.config_change force row level security;
alter table autobid.claim enable row level security;
alter table autobid.claim force row level security;

do $p$
begin
  if not exists (select 1 from pg_policies where schemaname = 'autobid'
                  and tablename = 'institution_config' and policyname = 'ab_config_tenant_read') then
    create policy ab_config_tenant_read on autobid.institution_config for select to authenticated
      using (institution_id = (select ctx.institution_id from institution.current_member_ctx_v2() ctx));
  end if;
  if not exists (select 1 from pg_policies where schemaname = 'autobid'
                  and tablename = 'config_change' and policyname = 'ab_config_change_tenant_read') then
    create policy ab_config_change_tenant_read on autobid.config_change for select to authenticated
      using (institution_id = (select ctx.institution_id from institution.current_member_ctx_v2() ctx));
  end if;
end $p$;

-- Tenants read; every write goes through portal-api on the service session, which
-- checks the four-eyes rules. The engine secret column is never granted.
revoke all on autobid.institution_config, autobid.config_change, autobid.claim
  from public, anon, authenticated;
grant select (institution_id, enabled, decision_mode, approval_mode, engine_url,
              engine_secret_set_at, engine_timeout_ms, engine_failure_mode,
              max_bids_per_day, max_amount_per_day, product_codes, version,
              changed_by, approved_by, updated_at)
  on autobid.institution_config to authenticated;
grant select on autobid.config_change to authenticated;
grant all on autobid.institution_config, autobid.config_change, autobid.claim to service_role;

-- ── F. queue an auto-bid for the bank's bid approval ────────────────────────
create or replace function autobid.queue_bid(
  p_institution_id uuid, p_maker_member_id uuid, p_request_id uuid, p_payload jsonb, p_label text)
returns uuid
language plpgsql security definer
set search_path = pg_catalog
as $function$
declare
  v_role text;
  v_d    jsonb;
  v_id   uuid;
begin
  select coalesce(m.member_role, 'maker') into v_role
    from institution.member m
   where m.id = p_maker_member_id and m.institution_id = p_institution_id;
  if not found then
    raise exception 'autobid.queue_bid: maker % is not a member of institution %',
      p_maker_member_id, p_institution_id;
  end if;

  v_d := governance.describe_change('bid.submit', p_institution_id, p_payload);

  insert into governance.action
    (scope, category, label, risk, institution_id, maker_id, maker_role,
     resource_type, resource_id, payload, payload_before, resource_label, change)
  values
    ('institution', 'bid.submit', left(coalesce(p_label, 'Auto-bid'), 200), 'medium',
     p_institution_id, p_maker_member_id, 'autobid:' || v_role,
     'request', p_request_id, p_payload || jsonb_build_object('submitted_via', 'autobid'),
     nullif(v_d -> 'before', 'null'::jsonb), v_d ->> 'label',
     jsonb_build_object('rows', v_d -> 'rows', 'described', v_d -> 'described'))
  returning id into v_id;
  return v_id;
end
$function$;

revoke all on function autobid.queue_bid(uuid, uuid, uuid, jsonb, text) from public, anon, authenticated;
grant execute on function autobid.queue_bid(uuid, uuid, uuid, jsonb, text) to service_role;

-- ── G. module permission for bank admins ────────────────────────────────────
update portal_admin.user_groups
   set module_permissions = array_append(module_permissions, 'inst:autobid')
 where slug = 'institution_admin'
   and not ('inst:autobid' = any (module_permissions));

do $verify$
begin
  if to_regclass('autobid.institution_config') is null
     or to_regclass('autobid.config_change') is null
     or to_regclass('autobid.claim') is null then
    raise exception '076: tables missing';
  end if;
  if not exists (select 1 from pg_constraint where conname = 'bid_submitted_via_check'
                  and pg_get_constraintdef(oid) like '%autobid%') then
    raise exception '076: submitted_via does not accept autobid';
  end if;
end
$verify$;
