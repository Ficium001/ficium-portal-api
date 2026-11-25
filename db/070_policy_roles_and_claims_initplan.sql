-- =============================================================================
-- 070 — Policy roles, the last per-row claims reads, one duplicate.
--
-- From the 2026-11-24 database audit. No change to what any member can see or do:
--   1. institution.group: group_select dropped; inst_groups_select is identical.
--   2. 5 policies (institution.benefit, institution.doc, marketplace.bid_benefit)
--      read current_setting('request.jwt.claims') per row; now through
--      (select ...), evaluated once per query (advisor 0003, as 069 did for auth.*).
--   3. 47 tenant-scoped policies written for role "public" now name
--      authenticated, the role every tenant session uses (tenant_session() does
--      SET LOCAL ROLE authenticated). Reference tables with condition true stay
--      public.
--
-- Self-checking; dry-run on live first (rolled back): 1 dropped, 5 wrapped,
-- 47 retargeted.
-- =============================================================================

DO $m$
DECLARE
  schemas text[] := array['admin', 'audit', 'auth_portal', 'autobid', 'billing', 'catalog', 'entitlements', 'governance', 'identity', 'institution', 'integration', 'marketplace', 'ops', 'portal_admin', 'public'];
  r record;
  nq text;
  nw text;
  n_auth int := 0;
  n_wrap int := 0;
  n_dropped int := 0;
  left_over int;
BEGIN
  PERFORM set_config('search_path', '', true);

  -- 1. Exact duplicate: group_select repeats inst_groups_select word for word.
  IF (SELECT qual FROM pg_catalog.pg_policies WHERE schemaname = 'institution' AND tablename = 'group'
       AND policyname = 'group_select')
     = (SELECT qual FROM pg_catalog.pg_policies WHERE schemaname = 'institution' AND tablename = 'group'
       AND policyname = 'inst_groups_select') THEN
    DROP POLICY group_select ON institution."group";
    n_dropped := n_dropped + 1;
  END IF;

  -- 2. The policies that read the JWT claims directly do it once per query, like 069 did for
  --    auth.uid(): (select current_setting(...)) means the same and is evaluated once.
  FOR r IN
    SELECT schemaname, tablename, policyname, qual, with_check
      FROM pg_catalog.pg_policies WHERE schemaname = ANY (schemas)
  LOOP
    nq := regexp_replace(r.qual, '(?<!SELECT )current_setting\(''request\.jwt\.claims''::text, true\)',
                         '(select current_setting(''request.jwt.claims''::text, true))', 'g');
    nw := regexp_replace(r.with_check, '(?<!SELECT )current_setting\(''request\.jwt\.claims''::text, true\)',
                         '(select current_setting(''request.jwt.claims''::text, true))', 'g');
    IF nq IS DISTINCT FROM r.qual OR nw IS DISTINCT FROM r.with_check THEN
      EXECUTE format('ALTER POLICY %I ON %I.%I%s%s', r.policyname, r.schemaname, r.tablename,
                     CASE WHEN r.qual IS NOT NULL THEN ' USING (' || nq || ')' ELSE '' END,
                     CASE WHEN r.with_check IS NOT NULL THEN ' WITH CHECK (' || nw || ')' ELSE '' END);
      n_wrap := n_wrap + 1;
    END IF;
  END LOOP;

  -- 3. Policies for role "public" are all tenant-scoped here: their condition needs a member's
  --    claims (current_member_ctx*, current_institution_id, the JWT, auth.uid) or rows that are
  --    themselves tenant-scoped. They now name authenticated, the role every tenant session uses.
  --    Reference tables readable by everyone (condition true) stay public.
  FOR r IN
    SELECT schemaname, tablename, policyname, qual, with_check
      FROM pg_catalog.pg_policies
     WHERE schemaname = ANY (schemas) AND roles = '{public}' AND coalesce(qual, '') <> 'true'
  LOOP
    EXECUTE format('ALTER POLICY %I ON %I.%I TO authenticated', r.policyname, r.schemaname, r.tablename);
    n_auth := n_auth + 1;
  END LOOP;

  SELECT count(*) INTO left_over FROM pg_catalog.pg_policies
   WHERE schemaname = ANY (schemas)
     AND (coalesce(qual, '') || coalesce(with_check, '')) ~ '(?<!SELECT )current_setting\(''request\.jwt\.claims''';
  IF left_over > 0 THEN
    RAISE EXCEPTION '% policies still read the JWT claims per row', left_over;
  END IF;

  RAISE NOTICE 'duplicates dropped: %, claims reads wrapped: %, to authenticated: %', n_dropped, n_wrap, n_auth;
END $m$;
