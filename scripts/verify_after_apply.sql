-- =============================================================================
-- scripts/verify_after_apply.sql
-- Run after identity_backfill.py --apply to confirm correctness.
-- =============================================================================

-- 1. Every auth_user_id must now exist in auth_portal.auth_users
SELECT
    t.table_schema,
    t.table_name,
    count(*) FILTER (WHERE ap.id IS NULL) AS unresolved,
    count(*)                               AS total
FROM information_schema.columns c
JOIN information_schema.tables  t
     ON t.table_schema = c.table_schema AND t.table_name = c.table_name
CROSS JOIN LATERAL (
    SELECT DISTINCT m.auth_user_id
    FROM   information_schema.columns c2
    JOIN   LATERAL (
        SELECT auth_user_id
        FROM   ONLY information_schema.columns  -- placeholder; see note below
    ) m ON true
) x(auth_user_id)
LEFT JOIN auth_portal.auth_users ap ON ap.id = x.auth_user_id
WHERE c.column_name = 'auth_user_id'
  AND c.table_schema NOT IN ('pg_catalog','information_schema','auth')
GROUP BY t.table_schema, t.table_name;

-- 2. Migration log summary
SELECT
    schema_name,
    table_name,
    count(*) AS rows_migrated,
    max(run_at) AS last_run
FROM public._identity_migration_log
WHERE dry_run = false
GROUP BY schema_name, table_name
ORDER BY last_run DESC;

-- 3. Spot-check: institution_members should resolve through current_member_ctx
-- Replace the UUID below with a known institution member's ficium-auth UUID
-- SELECT * FROM institution.current_member_ctx();
-- (run after SET request.jwt.claims for a real user)
