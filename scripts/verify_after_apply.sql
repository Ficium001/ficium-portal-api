-- =============================================================================
-- ADR-002 — Post-migration verification
-- Run these in the target environment immediately after `--apply` succeeds.
-- =============================================================================

-- 1. Confirm no column anywhere still references auth.users(id).
--    Expect: zero rows.
SELECT tc.table_schema, tc.table_name, kcu.column_name
FROM information_schema.table_constraints tc
JOIN information_schema.key_column_usage kcu
  ON kcu.constraint_name = tc.constraint_name AND kcu.constraint_schema = tc.constraint_schema
JOIN information_schema.constraint_column_usage ccu
  ON ccu.constraint_name = tc.constraint_name AND ccu.constraint_schema = tc.constraint_schema
WHERE tc.constraint_type = 'FOREIGN KEY'
  AND ccu.table_schema = 'auth' AND ccu.table_name = 'users' AND ccu.column_name = 'id';

-- 2. mcbadmin specifically — the case that surfaced this issue.
--    Expect: auth_user_id equals the ficium-auth id, joins succeed.
SELECT im.id, im.auth_user_id, au.id AS ficium_auth_id, au.email, im.is_primary_admin
FROM institution.institution_members im
JOIN auth_portal.auth_users au ON au.id = im.auth_user_id
WHERE au.email = 'mcbadmin@mcb.mu';   -- adjust to the real login email

-- 3. RLS-dependent resolution now works end-to-end. Run with the SAME claims
--    shape ficium-portal-api injects (see app/core/db.py tenant_session):
SELECT set_config(
  'request.jwt.claims',
  json_build_object('sub', (SELECT auth_user_id::text FROM institution.institution_members im
                             JOIN auth_portal.auth_users au ON au.id = im.auth_user_id
                             WHERE au.email = 'mcbadmin@mcb.mu'),
                     'role', 'authenticated')::text,
  true
);

-- Must return mcbadmin's member_id / institution_id / is_inst_admin = true:
SELECT * FROM institution.current_member_ctx();

-- Must return the institution_admin group with its full module_permissions
-- (the array seen in the original diagnostic CSV):
SELECT portal_admin.get_my_group();

-- 4. Spot-check the audit trail exists and is complete for this run:
SELECT table_name, column_name, count(*) AS rows_migrated
FROM public._identity_migration_log
GROUP BY table_name, column_name
ORDER BY table_name;
