-- APPLIED to the Portal DB (egwobcajdlragubtkpqp) on 2026-10-03 as migration integration_bid_publisher_triggers_2026_10_03,
-- AFTER ficium#71 (borrower receiver) and ficium-portal-api#62 (dispatcher contract gate) were deployed.
-- Step 4 stage B: this is what starts bid events (shadow mode: the borrower stores them in integration.bid_shadow; nothing
-- user-facing reads it yet).
-- DEFERRED constraint triggers run at COMMIT, so benefits/allocations inserted after the bid row in the same transaction are
-- already there when the payload is built.
--
-- VERIFIED in production:
--   backfill published the 2 visible bids; both bid.placed delivered on attempt 1; both applied on the borrower side;
--   the borrower's bid_shadow payload == the portal's expected payload for both (numeric-aware jsonb equality);
--   0 publishing errors on either side, 0 stuck events.
--   Deferred-trigger probe (forced with SET CONSTRAINTS ALL IMMEDIATE, then rolled back): a rate change plus a new benefit in one
--   transaction produce ONE bid.updated carrying both; nothing persisted.
DROP TRIGGER IF EXISTS integration_bid_events ON marketplace.bid;
CREATE CONSTRAINT TRIGGER integration_bid_events AFTER INSERT OR UPDATE ON marketplace.bid
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION integration.trg_bid_emit();
DROP TRIGGER IF EXISTS integration_bid_benefit_events ON marketplace.bid_benefit;
CREATE CONSTRAINT TRIGGER integration_bid_benefit_events AFTER INSERT OR UPDATE OR DELETE ON marketplace.bid_benefit
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION integration.trg_bid_child_emit();
DROP TRIGGER IF EXISTS integration_bid_allocation_events ON marketplace.bid_allocation;
CREATE CONSTRAINT TRIGGER integration_bid_allocation_events AFTER INSERT OR UPDATE OR DELETE ON marketplace.bid_allocation
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION integration.trg_bid_child_emit();
-- then, once:  SELECT integration.backfill_bid_events();
