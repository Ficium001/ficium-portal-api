-- =============================================================================
-- 028_structured_benefits.sql — benefits with a real value, one snapshot path.
--
-- WHY
--   A benefit's value was free text (value_display). The only live benefit was
--   titled "Free" with no value. Nothing could price a "0.25% rate discount",
--   so a borrower couldn't compare the true cost of competing bids.
--
--   Bug: bids placed through maker-checker got their benefits snapshotted
--   twice — once by trigger trg_snapshot_bid_benefits and again by an explicit
--   INSERT in institution._execute_action('bid.submit') — and the two copies
--   used different validity rules (the explicit one ignored valid_from).
--
-- WHAT
--   1. benefit.value_amount + value_unit ('pct_pa' rate points per year,
--      'pct' percent of a fee, 'mur' amount). Both or neither. Rules per
--      category are enforced by portal-api (which also writes value_display
--      from them, so the wording is consistent).
--   2. bid_benefit carries value_amount / value_unit, so the borrower side can
--      compute effective cost from the snapshot frozen with the bid.
--   3. snapshot_bid_benefits() copies them.
--   4. _execute_action: the duplicate bid_benefit INSERT is removed (the
--      trigger is the single snapshot path); benefit.create / benefit.update
--      (maker-checker for guaranteed benefits) carry value_amount / value_unit.
--   5. Integrity: valid_until >= valid_from; title 2-80 chars (NOT VALID: the
--      existing row is grandfathered, new writes are checked).
-- =============================================================================

BEGIN;

ALTER TABLE institution.benefit
    ADD COLUMN IF NOT EXISTS value_amount numeric(14,4),
    ADD COLUMN IF NOT EXISTS value_unit   text;
ALTER TABLE institution.benefit DROP CONSTRAINT IF EXISTS ck_benefit_value;
ALTER TABLE institution.benefit ADD CONSTRAINT ck_benefit_value CHECK (
    (value_amount IS NULL AND value_unit IS NULL)
    OR (value_amount IS NOT NULL AND value_unit IS NOT NULL
        AND value_amount > 0 AND value_unit IN ('pct_pa', 'pct', 'mur'))
);  -- IS NOT NULLs matter: a NULL unit would otherwise make the CHECK pass
ALTER TABLE institution.benefit DROP CONSTRAINT IF EXISTS ck_benefit_validity;
ALTER TABLE institution.benefit ADD CONSTRAINT ck_benefit_validity
    CHECK (valid_until IS NULL OR valid_from IS NULL OR valid_until >= valid_from) NOT VALID;
ALTER TABLE institution.benefit DROP CONSTRAINT IF EXISTS ck_benefit_title;
ALTER TABLE institution.benefit ADD CONSTRAINT ck_benefit_title
    CHECK (char_length(btrim(title)) BETWEEN 2 AND 80) NOT VALID;

ALTER TABLE marketplace.bid_benefit
    ADD COLUMN IF NOT EXISTS value_amount numeric(14,4),
    ADD COLUMN IF NOT EXISTS value_unit   text;

CREATE OR REPLACE FUNCTION marketplace.snapshot_bid_benefits()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
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
$function$;

-- _execute_action surgery: three targeted edits, each asserted.
DO $$
DECLARE d text; n text;
BEGIN
  d := pg_get_functiondef('institution._execute_action'::regproc);

  -- (a) remove the duplicate snapshot in bid.submit
  n := regexp_replace(d,
        '\n\s*INSERT INTO marketplace\.bid_benefit \(.*?AND \(b\.valid_until IS NULL OR b\.valid_until >= CURRENT_DATE\);',
        E'\n      -- Benefits are snapshotted by trigger trg_snapshot_bid_benefits (db/028).');
  IF n = d THEN RAISE EXCEPTION '028: bid_benefit block not found in _execute_action'; END IF;
  d := n;

  -- (b) benefit.create carries the structured value
  n := regexp_replace(d,
        'title, description, value_display,(\s+)is_guaranteed, conditions, valid_from, valid_until,(\s+)is_active, created_by',
        'title, description, value_display, value_amount, value_unit,\1is_guaranteed, conditions, valid_from, valid_until,\2is_active, created_by');
  IF n = d THEN RAISE EXCEPTION '028: benefit.create column list not found'; END IF;
  d := n;
  n := regexp_replace(d,
        E'NULLIF\\(a\\.payload->>''value_display'', ''''\\),(\\s+)true,',
        E'NULLIF(a.payload->>''value_display'', ''''),\\1NULLIF(a.payload->>''value_amount'', '''')::NUMERIC,\\1NULLIF(a.payload->>''value_unit'', ''''),\\1true,');
  IF n = d THEN RAISE EXCEPTION '028: benefit.create values not found'; END IF;
  d := n;

  -- (c) benefit.update carries it too
  n := regexp_replace(d,
        E'(value_display = NULLIF\\(a\\.payload->>''value_display'',\\s+''''\\),)(\\s+)',
        E'\\1\\2value_amount  = NULLIF(a.payload->>''value_amount'', '''')::NUMERIC,\\2value_unit    = NULLIF(a.payload->>''value_unit'', ''''),\\2');
  IF n = d THEN RAISE EXCEPTION '028: benefit.update not found'; END IF;

  EXECUTE n;
END $$;

COMMIT;
