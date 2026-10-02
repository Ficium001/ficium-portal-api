-- APPLIED to the Portal DB (egwobcajdlragubtkpqp) on 2026-10-02 as migration
-- drop_employer_from_marketplace_request_2026_10_02.
--
-- Decision 2026-10-02: the employer name is not released to institutions (in a market the size of Mauritius it can
-- identify a borrower). It was stored in marketplace.request.metadata.employer on 12 of 13 requests.
-- 1. scrub it, WITHOUT rewriting history (updated_at trigger disabled around the UPDATE and re-enabled);
-- 2. guard trigger so no code path (old sync, new events, a future change) can store it again.
--
-- VERIFIED (rolled back): insert with employer -> stripped; update re-adding it -> stripped; the real
-- marketplace.ingest_app_request with an employer in Phase 1 -> stripped; 0 live rows hold it;
-- max(updated_at) and an md5 fingerprint of (id, updated_at) are identical before and after.

ALTER TABLE marketplace.request DISABLE TRIGGER marketplace_request_updated_at;
UPDATE marketplace.request SET metadata = metadata - 'employer' WHERE metadata ? 'employer';
ALTER TABLE marketplace.request ENABLE TRIGGER marketplace_request_updated_at;

CREATE OR REPLACE FUNCTION marketplace.strip_employer()
RETURNS trigger LANGUAGE plpgsql SET search_path = '' AS $f$
BEGIN
  NEW.metadata := NEW.metadata - 'employer';
  RETURN NEW;
END $f$;
REVOKE ALL ON FUNCTION marketplace.strip_employer() FROM PUBLIC, anon, authenticated;

DROP TRIGGER IF EXISTS trg_request_strip_employer ON marketplace.request;
CREATE TRIGGER trg_request_strip_employer
  BEFORE INSERT OR UPDATE OF metadata ON marketplace.request
  FOR EACH ROW EXECUTE FUNCTION marketplace.strip_employer();
