-- 061_bid_withdraw_guard.sql
-- A bank can withdraw an offer it no longer stands behind, through governance
-- (bid.withdraw, four-eyes on by default, switchable in Setup > Controls).
--
-- The executor already existed (institution._execute_action, WHEN 'bid.withdraw')
-- but nothing could reach it, and it had no guard: approving a withdrawal would
-- have flipped an ACCEPTED bid to 'withdrawn' with a live deal behind it. The
-- guard lives on the table, so it holds for every path (executor, sweep, admin).
--
-- Only an unresolved bid can be withdrawn: draft, submitted, under_review.
-- A bid accepted while its withdrawal waited for a checker makes the approval
-- fail with BID_NOT_WITHDRAWABLE; the acceptance stands.
-- Withdrawing fires integration_bid_events, so publish_bid() sends bid.withdrawn
-- to the borrower (contract unchanged). accept_bid already refuses a withdrawn bid.

begin;

create or replace function marketplace.guard_bid_withdraw()
returns trigger
language plpgsql
set search_path = pg_catalog, marketplace
as $$
begin
  if new.status = 'withdrawn' and old.status is distinct from 'withdrawn' then
    if old.status not in ('draft', 'submitted', 'under_review') then
      raise exception 'BID_NOT_WITHDRAWABLE: bid % is %', old.id, old.status
        using errcode = 'P0001';
    end if;
    new.withdrawn_at := coalesce(new.withdrawn_at, now());
  end if;
  return new;
end $$;

drop trigger if exists trg_guard_bid_withdraw on marketplace.bid;
create trigger trg_guard_bid_withdraw
  before update of status on marketplace.bid
  for each row execute function marketplace.guard_bid_withdraw();

alter table marketplace.bid
  drop constraint if exists bid_withdraw_reason_len,
  add constraint bid_withdraw_reason_len check (withdraw_reason is null or char_length(withdraw_reason) <= 500);

commit;
