-- APPLIED to the Portal DB (egwobcajdlragubtkpqp) on 2026-10-03 as migration integration_bid_publisher_functions_2026_10_03.
-- Step 4 of the integration-contract migration, INSTITUTION side, stage A: functions only, no trigger attached (nothing emits).
-- Stage B (deferred triggers + backfill) is db/019, applied only after the borrower receiver and the dispatcher gate are deployed.
--
-- build_bid_payload = exactly what GET /public/requests/{id}/bids returns today, shaped to contract bid.placed v1.
-- app_type_for_allocation: catalog codes != contract product types and the forward mapping is many-to-one (lossy):
--   sme_loan/business_account/business_loan -> business_loan; personal_loan/leasing/overdraft -> personal_loan;
--   savings_account/investment_account -> savings; mortgage -> home_loan; fixed_deposit -> deposit.
--   So the inverse prefers the request's own types, then the catalog code, then canonical order. education_loan and
--   vehicle_loan have no borrower equivalent: returned as-is so the dispatcher's contract gate blocks the event loudly.
-- publish_bid: visible (submitted/under_review) -> bid.placed, then bid.updated; coalesced into an unsent event; skipped when
--   nothing a borrower sees changed. withdrawn/rejected/expired -> bid.withdrawn (only if ever placed). draft/accepted -> nothing.
--
-- VERIFIED (rolled back): payload == today's readback on every field for both visible live bids (null-aware comparison);
--   publish sequence on a real bid: placed / no-op / coalesced / updated / withdrawn once / placed again; accepted bid -> 0 events;
--   all real payloads valid under contract v1.2.0 (placed, updated, withdrawn); an unmappable code is blocked by the contract.

CREATE TABLE IF NOT EXISTS integration.emit_error (
  id bigserial PRIMARY KEY, at timestamptz NOT NULL DEFAULT now(), aggregate_id text, stage text, error text);
ALTER TABLE integration.emit_error ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON integration.emit_error FROM PUBLIC, anon, authenticated;

CREATE OR REPLACE FUNCTION integration.app_type_for_allocation(p_request_id uuid, p_product_id uuid)
RETURNS text LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, integration AS $$
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

CREATE OR REPLACE FUNCTION integration.build_bid_payload(p_bid_id uuid)
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, integration AS $$
  SELECT jsonb_build_object(
    'bid_id', b.id, 'request_id', b.request_id, 'institution_id', b.institution_id,
    'institution_name', i.name, 'institution_logo_url', i.logo_url,
    'status', b.status, 'rate', b.rate, 'rate_type', b.rate_type, 'rate_valid_days', b.rate_valid_days,
    'amount_offered', b.amount_offered, 'currency', r.currency, 'term_months', b.term_months,
    'conditions', coalesce(b.conditions, '{}'::jsonb), 'fee_structure', coalesce(b.fee_structure, '{}'::jsonb),
    'benefits', coalesce((SELECT jsonb_agg(jsonb_strip_nulls(jsonb_build_object('title', bb.title, 'value_display', bb.value_display,
                 'is_guaranteed', bb.is_guaranteed, 'cat_code', bb.cat_code)) ORDER BY bb.is_guaranteed DESC, bb.id)
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

CREATE OR REPLACE FUNCTION integration.publish_bid(p_bid_id uuid)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, integration AS $$
DECLARE st text; rid uuid; d jsonb; n int; ever_placed boolean; last_type text; last_data jsonb;
BEGIN
  SELECT b.status, b.request_id INTO st, rid FROM marketplace.bid b WHERE b.id = p_bid_id;
  IF NOT FOUND THEN RETURN; END IF;
  ever_placed := EXISTS (SELECT 1 FROM integration.outbox o WHERE o.aggregate_id = p_bid_id::text AND o.type = 'bid.placed');
  SELECT o.type, o.envelope -> 'data' INTO last_type, last_data FROM integration.outbox o
   WHERE o.aggregate_id = p_bid_id::text ORDER BY o.sequence DESC LIMIT 1;
  IF st IN ('submitted','under_review') THEN
    d := integration.build_bid_payload(p_bid_id);
    IF last_type IN ('bid.placed','bid.updated') AND last_data = d THEN RETURN; END IF;
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

CREATE OR REPLACE FUNCTION integration.trg_bid_emit() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, integration AS $$
BEGIN
  BEGIN PERFORM integration.publish_bid(NEW.id);
  EXCEPTION WHEN OTHERS THEN INSERT INTO integration.emit_error (aggregate_id, stage, error) VALUES (NEW.id::text, 'bid ' || TG_OP, left(SQLERRM, 500));
  END;
  RETURN NULL;
END $$;

CREATE OR REPLACE FUNCTION integration.trg_bid_child_emit() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, integration AS $$
DECLARE bid uuid := CASE WHEN TG_OP = 'DELETE' THEN OLD.bid_id ELSE NEW.bid_id END;
BEGIN
  BEGIN PERFORM integration.publish_bid(bid);
  EXCEPTION WHEN OTHERS THEN INSERT INTO integration.emit_error (aggregate_id, stage, error) VALUES (bid::text, TG_TABLE_NAME || ' ' || TG_OP, left(SQLERRM, 500));
  END;
  RETURN NULL;
END $$;

CREATE OR REPLACE FUNCTION integration.backfill_bid_events() RETURNS int
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, integration AS $$
DECLARE r record; n int := 0;
BEGIN
  FOR r IN SELECT b.id FROM marketplace.bid b WHERE b.status IN ('submitted','under_review') ORDER BY b.submitted_at LOOP
    IF NOT EXISTS (SELECT 1 FROM integration.outbox o WHERE o.aggregate_id = r.id::text) THEN
      PERFORM integration.publish_bid(r.id); n := n + 1;
    END IF;
  END LOOP;
  RETURN n;
END $$;

REVOKE ALL ON FUNCTION integration.app_type_for_allocation(uuid, uuid), integration.build_bid_payload(uuid), integration.publish_bid(uuid),
  integration.trg_bid_emit(), integration.trg_bid_child_emit(), integration.backfill_bid_events() FROM PUBLIC, anon, authenticated;
