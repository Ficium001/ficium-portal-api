-- 047_actor_refs_to_ficium_auth.sql
-- Step 1 of moving every portal login to ficium-auth (one identity system),
-- before registration stops creating Supabase Auth users.
-- Four columns referenced Supabase's auth.users. They worked only because
-- existing logins happen to share their id with a Supabase user; a bank
-- registered without Supabase Auth would fail on its first bid event, and
-- Ficium staff created only in ficium-auth could not record a compliance
-- review. They now reference auth_portal.auth_users, the ficium-auth table.
-- All four were empty when this was written (2026-10-21); ON DELETE SET NULL
-- keeps the history rows if a login is ever removed.

begin;

alter table marketplace.bid_event
  drop constraint if exists bid_event_actor_id_fkey,
  add constraint bid_event_actor_id_fkey
    foreign key (actor_id) references auth_portal.auth_users(id) on delete set null;

alter table institution.institution
  drop constraint if exists institution_suspended_by_fkey,
  add constraint institution_suspended_by_fkey
    foreign key (suspended_by) references auth_portal.auth_users(id) on delete set null,
  drop constraint if exists institution_compliance_reviewed_by_fkey,
  add constraint institution_compliance_reviewed_by_fkey
    foreign key (compliance_reviewed_by) references auth_portal.auth_users(id) on delete set null;

alter table institution.kyb_document
  drop constraint if exists kyb_document_reviewed_by_fkey,
  add constraint kyb_document_reviewed_by_fkey
    foreign key (reviewed_by) references auth_portal.auth_users(id) on delete set null;

do $verify$
declare n int;
begin
  select count(*) into n from pg_constraint
   where contype = 'f' and confrelid = 'auth.users'::regclass
     and conrelid in ('marketplace.bid_event'::regclass, 'institution.institution'::regclass,
                      'institution.kyb_document'::regclass);
  if n <> 0 then raise exception '047: % reference(s) to auth.users remain', n; end if;
end
$verify$;

commit;
