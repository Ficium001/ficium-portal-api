-- APPLIED to the Portal DB (egwobcajdlragubtkpqp) on 2026-10-06 as migration 022_schema_cleanup_step1.
-- Rollback: db/rollback/022_rollback.sql (also snapshotted in _rollback.snapshot, migration='022').
--
-- Inspection before the change (2026-10-06):
--   workflow   : 4 tables, 0 rows; no FKs/views/functions/policies outside the schema reference it;
--                no code references (portal-api, portal, auth, infra); not used via the Data API.
--   bid_notify : AFTER INSERT trigger on marketplace.bid -> dispatch() -> POST to the borrower app.
--                Vault secrets portal_api_url / app_service_secret were never created, so every call
--                exited early ("vault keys not configured"). Bids are already published through
--                integration_bid_events (the contract path). Retiring it also removes a direct
--                Portal->borrower-app call that bypassed the integration contract.
--   app        : one function, current_institution_id(), used by tenant_isolation on 5 institution
--                tables. Policies bind by OID, so moving it is behaviour-preserving.
-- NOT changed (live in code): admin (members.py, autobid.py, api_keys.py + 7 DB functions + policies),
--                identity (members.py login history; trigger on auth.users).
--
-- Verified after apply (rolled-back tests): own-institution user sees own 4 members; other/no
-- institution sees 0; no live function references the removed names; bid triggers intact.

CREATE SCHEMA IF NOT EXISTS _rollback;
REVOKE ALL ON SCHEMA _rollback FROM PUBLIC, anon, authenticated;
CREATE TABLE IF NOT EXISTS _rollback.snapshot (
  id bigserial PRIMARY KEY, migration text NOT NULL, object text NOT NULL,
  kind text NOT NULL, definition text, captured_at timestamptz NOT NULL DEFAULT now());
REVOKE ALL ON _rollback.snapshot FROM PUBLIC, anon, authenticated;
ALTER TABLE _rollback.snapshot ENABLE ROW LEVEL SECURITY;
-- (snapshot INSERT of trigger/function/ACL/policy definitions — see _rollback.snapshot)

REVOKE USAGE ON SCHEMA workflow FROM authenticated;
ALTER SCHEMA workflow RENAME TO _retired_workflow_20261006;

DROP TRIGGER trg_bid_notify ON marketplace.bid;
ALTER SCHEMA bid_notify RENAME TO _retired_bid_notify_20261006;

ALTER FUNCTION app.current_institution_id() SET SCHEMA institution;
DROP SCHEMA app;
