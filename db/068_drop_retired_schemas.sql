-- =============================================================================
-- 068 — Drop two retired schemas.
--
--   _retired_workflow_20261006  The old workflow tables (template, stage,
--                               stage_assignment, doc_requirement) and their 3
--                               helper functions, renamed by 022 and due for a
--                               hard drop from 2026-11-06. Superseded by
--                               institution.pipeline_*; nothing outside the
--                               schema refers to them.
--   _rollback                   One table, snapshot, holding the definitions
--                               022–024 replaced on Oct 6 2026. With it gone,
--                               db/rollback/022–024_rollback.sql can no longer
--                               run; those changes were long since built on
--                               (040 onward), so rolling them back is not a
--                               path anyone can take now.
--
-- Neither schema is in db/baseline/schema.sql, so the daily baseline proof is
-- unaffected. Checked 2026-11-24: no view, function, trigger, policy, cron job
-- or foreign key outside these schemas depends on them.
--
-- Audit: Ficium database audit, 2026-11-24. Approved by KJ.
-- =============================================================================

-- if exists: a new cell built from db/baseline/ never had these schemas.
drop schema if exists _retired_workflow_20261006 cascade;

drop schema if exists _rollback cascade;
