-- 049_onboarding_and_compliance_rule.sql   (NOT YET APPLIED: ships with Z4)
-- Found 2026-10-21 while moving onboarding behind the API:
--   * institution_onboarding_prefs and update_onboarding_stage() never existed.
--     The portal called both and ignored the errors: every onboarding silently
--     lost its preferences and never left 'registered', yet showed "pending".
--   * The compliance view ignored doc_type.applies_to, so every institution
--     needed BOTH the BoM approval (banks) and the FSC licence (non-banks):
--     nobody could ever become compliant. applies_to also used a different
--     vocabulary (bank/mfi) from institution_type (commercial_bank/micro_credit).
-- This migration: the missing table; ONE rule for which documents an
-- institution needs (institution.required_doc_types), used by the compliance
-- view and the onboarding endpoint; applies_to in the institution_type
-- vocabulary. Regulator mapping confirmed by KJ (2026-10-21).
-- Bid enforcement of can_bid: db/050.

begin;

create table if not exists institution.onboarding_pref (
  institution_id   uuid primary key references institution.institution(id) on delete cascade,
  deployment_model text not null check (deployment_model in ('saas', 'paas', 'on_prem')),
  integration_mode text not null,
  it_contact_name  text not null,
  it_contact_email text not null,
  webhook_url      text,
  expected_volume  text,
  submitted_by     uuid references auth_portal.auth_users(id) on delete set null,
  submitted_at     timestamptz not null default now()
);
alter table institution.onboarding_pref enable row level security;
revoke all on institution.onboarding_pref from public, anon, authenticated;

-- applies_to in the institution_type vocabulary (confirmed by KJ, 2026-10-21):
--   BoM approval -> commercial banks; FSC licence -> non-bank financial
--   institutions (micro-credit, leasing, insurance, fintech). Cooperatives:
--   neither (Registrar of Cooperatives) until told otherwise.
update institution.doc_type set applies_to = '{commercial_bank}'
 where code = 'bom_approval';
update institution.doc_type set applies_to = '{micro_credit,leasing,insurance,fintech}'
 where code = 'fsc_license';

create or replace function institution.required_doc_types(p_institution uuid)
  returns setof institution.doc_type language sql stable security definer
  set search_path = '' as
$$
  select dt.* from institution.doc_type dt
    join institution.institution i on i.id = p_institution
   where dt.is_mandatory
     and (dt.applies_to is null or i.institution_type = any (dt.applies_to))
   order by dt.sort_order
$$;
revoke all on function institution.required_doc_types(uuid) from public, anon;
grant execute on function institution.required_doc_types(uuid) to authenticated;

create or replace view institution.compliance as
select i.id as institution_id,
       (i.approved = true and i.suspended_at is null) as is_approved,
       not exists (select 1 from institution.required_doc_types(i.id) dt
                    where not exists (select 1 from institution.doc d
                                       where d.institution_id = i.id and d.doc_type_id = dt.id
                                         and d.status = 'approved'
                                         and (d.expiry_date is null or d.expiry_date > current_date)))
         as is_compliant,
       array(select dt.code from institution.required_doc_types(i.id) dt
              where not exists (select 1 from institution.doc d
                                 where d.institution_id = i.id and d.doc_type_id = dt.id
                                   and d.status = 'approved'
                                   and (d.expiry_date is null or d.expiry_date > current_date)))
         as missing_docs,
       (i.approved = true and i.suspended_at is null
        and not exists (select 1 from institution.required_doc_types(i.id) dt
                         where not exists (select 1 from institution.doc d
                                            where d.institution_id = i.id and d.doc_type_id = dt.id
                                              and d.status = 'approved'
                                              and (d.expiry_date is null or d.expiry_date > current_date))))
         as can_bid
  from institution.institution i;

do $verify$
declare n_bank int; n_mfi int;
begin
  -- a commercial bank needs BoM approval, not the FSC licence; an MFI the reverse
  select count(*) into n_bank from institution.doc_type dt
   where dt.is_mandatory and (dt.applies_to is null or 'commercial_bank' = any (dt.applies_to))
     and dt.code in ('bom_approval', 'fsc_license');
  select count(*) into n_mfi from institution.doc_type dt
   where dt.is_mandatory and (dt.applies_to is null or 'micro_credit' = any (dt.applies_to))
     and dt.code in ('bom_approval', 'fsc_license');
  if n_bank <> 1 or n_mfi <> 1 then
    raise exception '049: each institution type must need exactly one regulator document (bank %, mfi %)', n_bank, n_mfi;
  end if;
end
$verify$;

commit;
