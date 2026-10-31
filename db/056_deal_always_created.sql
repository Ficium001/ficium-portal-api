-- 056_deal_always_created.sql
-- An accepted bid must always have a deal (loan_pipeline). Until now
-- accept_bid() wrapped create_pipeline_from_acceptance() in
-- "exception when others then raise warning", so a missing or empty
-- template let the acceptance commit, released the borrower's identity to
-- the bank, and left the bank with no deal to work. Nothing retried it and
-- the nightly reconcile had no rule for it.
--
-- Now:
--   1. marketplace.deal_template_for(): ONE resolver for "which template
--      would this deal use". Only templates with at least one active stage
--      count; an empty template is the same as none.
--   2. create_pipeline_from_acceptance(): uses the resolver, raises if a
--      licensed institution has no usable template, and activates the
--      lowest-positioned ACTIVE stage (it used to assume position 1, which
--      left a deal with no current stage if stage 1 was deactivated).
--   3. Guard on marketplace.bid (BEFORE INSERT, like 050): a bank licensed
--      for deals cannot place a bid it could not turn into a deal. Covers
--      every path: four-eyes approval, autobid, the /v1 API.
--   4. accept_bid(): no more swallowing. If the deal cannot be created, the
--      whole acceptance rolls back (bid, request, identity release). The
--      borrower app already tells the borrower "nothing was changed".
--      A repeat acceptance now returns the existing deal id (it returned
--      null) and creates the deal if an older acceptance lost it.
--
-- Unchanged on purpose: an institution NOT licensed for the "pipeline"
-- module still gets no deal (returns null). That is a commercial decision
-- for KJ, not a bug fix - see the PR.

begin;

-- 1 ── resolver ───────────────────────────────────────────────────────────────
create or replace function marketplace.deal_template_for(p_institution_id uuid, p_product_id uuid)
  returns uuid language sql stable security definer set search_path = '' as
$$
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
revoke all on function marketplace.deal_template_for(uuid, uuid) from public, anon, authenticated;

-- 2 ── deal creation ─────────────────────────────────────────────────────────
create or replace function marketplace.create_pipeline_from_acceptance(
  p_request_id uuid, p_bid_id uuid, p_institution_id uuid, p_consumer_id uuid)
  returns uuid language plpgsql security definer set search_path = '' as
$$
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

-- 3 ── no bid you can't turn into a deal ─────────────────────────────────────
create or replace function marketplace.guard_bid_deal_template()
  returns trigger language plpgsql security definer set search_path = '' as
$$
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

drop trigger if exists trg_guard_bid_deal_template on marketplace.bid;
create trigger trg_guard_bid_deal_template
  before insert on marketplace.bid
  for each row execute function marketplace.guard_bid_deal_template();

-- 4 ── accept_bid: atomic with the deal ──────────────────────────────────────
create or replace function marketplace.accept_bid(
  p_request_id uuid, p_bid_id uuid, p_consumer_id uuid, p_phase2 jsonb)
  returns jsonb language plpgsql security definer
  set search_path to 'marketplace', 'institution', 'public' as
$function$
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
$function$;

-- verify ───────────────────────────────────────────────────────────────────
do $verify$
begin
  if not exists (select 1 from pg_trigger where tgname = 'trg_guard_bid_deal_template'
                  and tgrelid = 'marketplace.bid'::regclass) then
    raise exception '056: bid guard not installed';
  end if;
  if position('exception when others' in lower(pg_get_functiondef('marketplace.accept_bid'::regproc))) > 0 then
    raise exception '056: accept_bid still swallows deal creation errors';
  end if;
end
$verify$;

commit;
