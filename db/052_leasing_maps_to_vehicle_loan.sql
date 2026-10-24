-- 052_leasing_maps_to_vehicle_loan.sql
-- The borrower app's "Vehicle Loan" product is app type 'leasing'.
-- catalog.product_id_for_app_type() had no branch for it, so it fell through
-- to ELSE 'personal_loan': every vehicle loan request reached banks labelled
-- Personal Loan (1 live request at time of writing, created before this fix).
-- Same issue makes the borrower-app Offers feed route vehicle offers to the
-- wrong product. Fix: 'leasing' -> 'vehicle_loan'. Every other mapping is
-- unchanged; the check below proves it and rolls back on any difference.
-- Existing mis-filed requests are NOT rewritten here (they may carry bids).

begin;

create temp table _before_052 on commit drop as
  select t, catalog.product_id_for_app_type(t) as pid
  from unnest(array['mortgage','personal_loan','sme_loan','business_loan','business_account','fixed_deposit',
    'savings_account','investment_account','credit_card','education_loan','vehicle_loan','equities','unit_trust',
    'government_bonds','offshore_investment','savings_plan','mixed_portfolio','overdraft','unknown_type']) t;

create or replace function catalog.product_id_for_app_type(p_app_type text)
 returns uuid
 language sql
 stable
 set search_path to 'catalog', 'public'
as $function$
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
$function$;

do $$
declare n int;
begin
  if catalog.product_id_for_app_type('leasing') is distinct from (select id from catalog.product where code = 'vehicle_loan') then
    raise exception '052: leasing does not map to vehicle_loan';
  end if;
  select count(*) into n from _before_052 b where catalog.product_id_for_app_type(b.t) is distinct from b.pid;
  if n <> 0 then raise exception '052: % other mapping(s) changed', n; end if;
end $$;

commit;
