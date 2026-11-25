-- =============================================================================
-- 074 — institution.compliance: a bank sees only its own row.
--
-- The view runs with its owner's rights (postgres), so RLS on the tables under
-- it never applied, and `authenticated` could SELECT it: any signed-in member
-- of any bank could read every bank's approval, missing documents, waiver and
-- can_bid. Found by the database audit, 2026-11-24.
--
-- Not security_invoker: bank sessions cannot read compliance_waiver, and the
-- institution policy still keys on Supabase Auth (retired by 073), so the badge
-- would break. Instead the view filters itself:
--   - tenant sessions (role authenticated/anon) see the row whose
--     institution_id is in their verified claims, the same rule as
--     institution.doc (doc_select_own);
--   - postgres / service sessions see every bank: the bid guard
--     (marketplace.guard_bid_compliance, SECURITY DEFINER) and the internal
--     admin endpoints are unchanged.
-- security_barrier stops a caller's function in WHERE from seeing other rows.
-- Write grants on the view are removed: it is not updatable and nothing writes
-- through it.
--
-- Columns and their order are unchanged, so CREATE OR REPLACE keeps the guard's
-- dependency. Guarded, so a fresh cell runs it in order.
-- =============================================================================

create or replace view institution.compliance with (security_barrier = true) as
with base as (
  select i.id as institution_id,
         i.approved = true and i.suspended_at is null as is_approved,
         array(
           select dt.code
             from institution.required_doc_types(i.id) dt(id, code, label, description, is_mandatory, applies_to, sort_order, created_at)
            where not exists (
                    select 1 from institution.doc d
                     where d.institution_id = i.id and d.doc_type_id = dt.id and d.status = 'approved'
                       and (d.expiry_date is null or d.expiry_date > current_date))
         ) as missing_docs,
         (select w.waived_until from institution.compliance_waiver w
           where w.institution_id = i.id and w.revoked_at is null and w.waived_until > now()) as waived_until
    from institution.institution i
   where current_user not in ('authenticated', 'anon')
      or i.id = (select nullif(nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'institution_id', '')::uuid)
)
select institution_id,
       is_approved,
       cardinality(missing_docs) = 0 as is_compliant,
       missing_docs,
       is_approved and (cardinality(missing_docs) = 0 or waived_until is not null) as can_bid,
       waived_until
  from base;

revoke insert, update, delete on institution.compliance from authenticated;
revoke all on institution.compliance from anon;
grant select on institution.compliance to authenticated;
