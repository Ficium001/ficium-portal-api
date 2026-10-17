-- =============================================================================
-- 030_benefit_values_to_borrower.sql — send a benefit's number, not just its wording.
--
-- WHY
--   db/028 gave benefits a number (value_amount + value_unit) so a borrower can
--   compare the real cost of competing bids, but bid events still carried only
--   value_display. Contract v1.3.0 adds value_amount / value_unit to bid
--   benefits, in CONTRACT units, which differ from the portal's:
--
--     portal (db/028)            contract v1.3.0
--     pct_pa  0.25  (points)  -> rate_pa       0.0025  (decimal fraction)
--     pct     50    (percent) -> fee_fraction  0.5
--     mur     5000            -> amount        5000    (only when the bid is in MUR)
--
--   The contract rejects rate_pa above 0.1, so an unconverted 0.25 can never
--   reach the borrower as a 25-point discount.
--
-- WHAT
--   1. integration.benefit_value(amount, unit, currency) -> jsonb, the ONE
--      conversion. '{}' when there's no value or it can't be expressed (e.g. a
--      MUR amount on a non-MUR bid): the benefit then goes as wording only.
--   2. build_bid_payload merges it into each benefit (bid.placed / bid.updated).
--   The synchronous read (app/api/public.py) calls the same function.
-- =============================================================================

BEGIN;

CREATE OR REPLACE FUNCTION integration.benefit_value(p_amount numeric, p_unit text, p_currency text)
RETURNS jsonb LANGUAGE sql IMMUTABLE SET search_path = pg_catalog AS $$
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

CREATE OR REPLACE FUNCTION integration.build_bid_payload(p_bid_id uuid)
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, integration AS $$
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

COMMIT;
