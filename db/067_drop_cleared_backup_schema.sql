-- =============================================================================
-- 067 — Drop the Nov 12 2026 pre-reset backup schema.
--
-- _cleared_20261112 held a copy of test data from before the reset (20 tables,
-- including auth_portal.auth_users with password hashes, institution.member and
-- marketplace bids/requests). RLS was off, it had no primary keys, and it rode
-- along in every dump and backup. Nothing refers to it: no view, function,
-- foreign key or code path (checked 2026-11-24; only its own TOAST tables
-- depend on it). It is not in db/baseline/schema.sql, so the daily baseline
-- proof is unaffected. No real bank or borrower data was ever in it.
--
-- Audit: Ficium database audit, 2026-11-24. Approved by KJ.
-- =============================================================================

-- if exists: a new cell built from db/baseline/ never had this schema.
drop schema if exists _cleared_20261112 cascade;
