-- 050_no_compliance_no_bids.sql   (NOT YET APPLIED: ships with Z4, after 049)
-- Bank-grade rule (KJ, 2026-10-21): an institution can only place a bid when
-- it is approved, not suspended, and holds every required document approved
-- and in date (institution.compliance.can_bid, made correct by 049).
-- Until now can_bid was only DISPLAYED in Settings; any approved bank could
-- bid with zero documents. Enforced as ONE guard on marketplace.bid, so every
-- path is covered: four-eyes approval, autobid, the bank-facing API, and any
-- path added later. Existing bids are untouched (INSERT only).
-- NOTE: MCB currently has none of its documents, so MCB cannot bid until they
-- are uploaded and approved.

begin;

create or replace function marketplace.guard_bid_compliance()
  returns trigger language plpgsql security definer set search_path = '' as
$$
declare c record;
begin
  select can_bid, is_approved, missing_docs into c
    from institution.compliance where institution_id = new.institution_id;
  if not found or not c.can_bid then
    raise exception using
      errcode = 'P0001',
      message = 'This institution cannot bid yet: '
                || case when not coalesce(c.is_approved, false)
                        then 'it is not approved or is suspended'
                        else 'required documents missing or not approved ('
                             || array_to_string(c.missing_docs, ', ') || ')' end;
  end if;
  return new;
end
$$;

drop trigger if exists trg_guard_bid_compliance on marketplace.bid;
create trigger trg_guard_bid_compliance
  before insert on marketplace.bid
  for each row execute function marketplace.guard_bid_compliance();

do $verify$
begin
  if not exists (select 1 from pg_trigger
                  where tgname = 'trg_guard_bid_compliance'
                    and tgrelid = 'marketplace.bid'::regclass) then
    raise exception '050: guard not installed';
  end if;
end
$verify$;

commit;
