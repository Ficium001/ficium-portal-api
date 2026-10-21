-- 046_accept_bid_guarded.sql
-- Found by the integration suite on its FIRST real run (2026-10-21):
-- marketplace.accept_bid() never checked whether the request was already
-- accepted, never locked it, and accepted any bid status. A second acceptance
-- naming a DIFFERENT bid (retry with another bid, double-tap, two concurrent
-- calls) flipped the winner to bank B and rejected bank A's accepted bid, while
-- bid_acceptance (ON CONFLICT DO NOTHING) kept bank A: the borrower's identity
-- was released to the bank that no longer won. It also accepted withdrawn /
-- rejected / expired bids and requests no longer open for bidding.
-- Now: request row locked FOR UPDATE; same bid again = idempotent no-op (the
-- contract call is retried); different bid = refused; only 'bidding' requests
-- and live bids ('submitted', 'under_review') can be accepted. Everything else
-- is unchanged. Self-verifying; the checks roll back.

begin;

create or replace function marketplace.accept_bid(
  p_request_id uuid, p_bid_id uuid, p_consumer_id uuid, p_phase2 jsonb)
  returns jsonb language plpgsql security definer
  set search_path = marketplace, institution, public as
$$
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
      v_repeat := true;            -- retried acceptance of the same bid
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

    begin
      v_pipeline_id := marketplace.create_pipeline_from_acceptance(
        p_request_id, p_bid_id, v_inst_id, p_consumer_id);
    exception when others then
      raise warning 'Pipeline auto-create failed for bid %: %', p_bid_id, sqlerrm;
    end;
  end if;

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

do $verify$
declare
  inst_a uuid := gen_random_uuid(); inst_b uuid := gen_random_uuid();
  req uuid := gen_random_uuid(); consumer uuid := gen_random_uuid();
  bid_a uuid; bid_b uuid; prod uuid := (select id from catalog.product limit 1);
  bad text := ''; win uuid; pii_inst uuid; st_a text;
begin
  if prod is null then raise notice '046: no catalog product, skipped'; return; end if;
  begin
    insert into institution.institution (id, name, legal_name, institution_type, country, approved, onboarding_stage, primary_contact_email)
    values (inst_a, 'ZZ A', 'ZZ A', 'bank', 'MU', true, 'approved', 'a@zz.mu'),
           (inst_b, 'ZZ B', 'ZZ B', 'bank', 'MU', true, 'approved', 'b@zz.mu');
    insert into marketplace.request (id, consumer_id, product_id, country, currency, amount, term_months,
           status, bid_window_opens_at, bid_window_closes_at, source, idempotency_key)
    values (req, consumer, prod, 'MU', 'MUR', 500000, 36, 'bidding', now(), now() + interval '1 day', 'app', 'zz046-' || req);
    insert into marketplace.bid (request_id, institution_id, rate, rate_type, amount_offered, term_months, submitted_via, idempotency_key)
    values (req, inst_a, 7.0, 'fixed', 500000, 36, 'portal', 'zz046a-' || req) returning id into bid_a;
    insert into marketplace.bid (request_id, institution_id, rate, rate_type, amount_offered, term_months, submitted_via, idempotency_key)
    values (req, inst_b, 6.9, 'fixed', 500000, 36, 'portal', 'zz046b-' || req) returning id into bid_b;

    perform marketplace.accept_bid(req, bid_a, consumer, '{"full_name":"ZZ","email":"zz@zz.mu"}');
    -- same bid again: harmless
    if not (marketplace.accept_bid(req, bid_a, consumer, '{"full_name":"ZZ"}') ->> 'repeat')::boolean then
      bad := bad || 'repeat not idempotent; ';
    end if;
    -- different bid: refused
    begin
      perform marketplace.accept_bid(req, bid_b, consumer, '{"full_name":"ZZ"}');
      bad := bad || 'second, different acceptance allowed; ';
    exception when raise_exception then null; end;

    select winning_bid_id into win from marketplace.request where id = req;
    select institution_id into pii_inst from marketplace.bid_acceptance where request_id = req;
    select status into st_a from marketplace.bid where id = bid_a;
    if win <> bid_a or pii_inst <> inst_a or st_a <> 'accepted' then
      bad := bad || 'winner/PII/bid state inconsistent; ';
    end if;
    raise exception using errcode = 'P0002', message = 'rollback';
  exception
    when sqlstate 'P0002' then null;
    when others then bad := bad || 'ERROR: ' || sqlerrm || '; ';
  end;
  if bad <> '' then raise exception '046 verification failed: %', bad; end if;
end
$verify$;

commit;
