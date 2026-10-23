-- 051_compliance_waiver.sql
-- KJ (2026-10-23): the document gate (050) must be controllable by Ficium
-- admins. A global off-switch would silently disable the control for every
-- institution with no end date, so instead: a PER-INSTITUTION waiver, granted
-- only by a Ficium admin (portal-api /admin, service session), with a required
-- reason and a hard expiry (max 180 days), revocable, audited by the API.
-- can_bid = approved and not suspended and (documents complete OR an active
-- waiver). Approval/suspension are never waivable. The view keeps its column
-- order (can_bid stays 5th); waived_until is appended.

begin;

create table if not exists institution.compliance_waiver (
  institution_id uuid primary key references institution.institution(id) on delete cascade,
  reason         text not null check (length(btrim(reason)) >= 10),
  waived_until   timestamptz not null,
  granted_by     uuid not null references auth_portal.auth_users(id),
  granted_at     timestamptz not null default now(),
  revoked_at     timestamptz,
  revoked_by     uuid references auth_portal.auth_users(id),
  check (waived_until > granted_at and waived_until <= granted_at + interval '180 days')
);
alter table institution.compliance_waiver enable row level security;
revoke all on institution.compliance_waiver from public, anon, authenticated;

create or replace view institution.compliance as
with base as (
  select i.id as institution_id,
         (i.approved = true and i.suspended_at is null) as is_approved,
         array(select dt.code from institution.required_doc_types(i.id) dt
                where not exists (select 1 from institution.doc d
                                   where d.institution_id = i.id and d.doc_type_id = dt.id
                                     and d.status = 'approved'
                                     and (d.expiry_date is null or d.expiry_date > current_date)))
           as missing_docs,
         (select w.waived_until from institution.compliance_waiver w
           where w.institution_id = i.id and w.revoked_at is null and w.waived_until > now())
           as waived_until
    from institution.institution i
)
select institution_id,
       is_approved,
       cardinality(missing_docs) = 0 as is_compliant,
       missing_docs,
       is_approved and (cardinality(missing_docs) = 0 or waived_until is not null) as can_bid,
       waived_until
  from base;

do $verify$
begin
  if (select count(*) from information_schema.columns
       where table_schema = 'institution' and table_name = 'compliance'
         and column_name in ('can_bid', 'waived_until')) <> 2 then
    raise exception '051: compliance view missing columns';
  end if;
end
$verify$;

commit;
