-- 059_expire_bids_on_request_end.sql
-- Reconcile R7 (nightly red since at least Nov 1): two bids stayed 'submitted'
-- on requests that had ended. A request can end two ways: the portal's own
-- close_expired_windows() (which already counts bids and picks closed/expired),
-- or the App's status reaching the portal through the request sync (the App's
-- hourly decision-deadline sweep). The second path changed the request and
-- never touched its bids, so banks kept seeing live offers on dead requests and
-- the borrower side never got bid.withdrawn.
--
-- Fix at the data layer, so it holds whichever path ends the request: when a
-- request enters a terminal non-accepted status, its unresolved bids expire.
-- Expiring a bid fires integration_bid_events, so publish_bid() sends
-- bid.withdrawn for any bid the borrower ever saw (contract unchanged).
-- 'closed' is NOT terminal: the window shut with offers and the borrower is
-- still choosing, so those bids must stay live.

begin;

create or replace function marketplace.expire_bids_on_request_end()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, marketplace
as $$
begin
  update marketplace.bid
     set status = 'expired'
   where request_id = new.id
     and status in ('draft', 'submitted', 'under_review');
  return null;
end
$$;

drop trigger if exists trg_expire_bids_on_request_end on marketplace.request;
create trigger trg_expire_bids_on_request_end
  after update of status on marketplace.request
  for each row
  when (new.status in ('expired', 'cancelled', 'withdrawn')
        and old.status is distinct from new.status)
  execute function marketplace.expire_bids_on_request_end();

-- 053 rule: no SECURITY DEFINER function executable by anon (or directly at all).
revoke execute on function marketplace.expire_bids_on_request_end() from public, anon, authenticated;

-- Backfill the rows already stranded (2 at time of writing).
update marketplace.bid b
   set status = 'expired'
  from marketplace.request r
 where r.id = b.request_id
   and r.status in ('expired', 'cancelled', 'withdrawn')
   and b.status in ('draft', 'submitted', 'under_review');

do $verify$
begin
  if exists (select 1 from marketplace.bid b join marketplace.request r on r.id = b.request_id
              where r.status in ('expired', 'cancelled', 'withdrawn')
                and b.status in ('draft', 'submitted', 'under_review')) then
    raise exception '059: unresolved bids remain on ended requests';
  end if;
  if has_function_privilege('anon', 'marketplace.expire_bids_on_request_end()', 'execute') then
    raise exception '059: anon can execute expire_bids_on_request_end';
  end if;
end
$verify$;

commit;
