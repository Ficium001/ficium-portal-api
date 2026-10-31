-- 057_revoke_deal_guard_execute.sql
-- 056 created marketplace.guard_bid_deal_template() as SECURITY DEFINER and
-- left Postgres' default EXECUTE grant to PUBLIC in place. The 053 rule
-- (no definer function executable by anon) caught it in CI. A trigger
-- function can't be called directly, so this is hygiene, not an exposure,
-- but the rule holds without exceptions. Fix forward (056 is immutable).

revoke execute on function marketplace.guard_bid_deal_template() from public, anon, authenticated;

do $verify$
begin
  if has_function_privilege('anon', 'marketplace.guard_bid_deal_template()', 'execute') then
    raise exception '057: anon can still execute guard_bid_deal_template';
  end if;
end
$verify$;
