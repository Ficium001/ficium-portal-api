-- =============================================================================
-- 069 — Foreign-key indexes and once-per-query auth calls in policies.
--
-- From the 2026-11-24 database audit. Same meaning, faster plans:
--   1. An index for every foreign key that had none: 74 on the live Portal DB.
--      Without one, a delete or key change on the parent scans the whole child.
--   2. Policies call auth.uid(), auth.jwt() and auth.role() through
--      (select ...): 19 policies. Evaluated once per query instead of once per
--      row (Supabase advisor 0003 auth_rls_initplan); conditions unchanged.
--
-- One self-checking block: finds what is missing, fixes it, and fails (rolling
-- back) if anything is left over. On an already-fixed cell it does nothing.
-- Dry-run on live first (rolled back): 74 indexes, 19 policies.
-- =============================================================================

DO $m$
DECLARE
  schemas text[] := array['admin', 'audit', 'auth_portal', 'autobid', 'billing', 'catalog', 'entitlements', 'governance', 'identity', 'institution', 'integration', 'marketplace', 'ops', 'portal_admin', 'public'];
  r record;
  nq text;
  nw text;
  n_idx int := 0;
  n_pol int := 0;
  left_over int;
BEGIN
  -- Expressions and names come out fully qualified, whatever the caller's search_path.
  PERFORM set_config('search_path', '', true);

  -- 1. An index for every foreign key that has none (named <table>_<columns>_fk_idx).
  FOR r IN
    SELECT n.nspname, t.relname, array_agg(a.attname::text ORDER BY k.ord) AS cols
      FROM pg_catalog.pg_constraint c
      JOIN pg_catalog.pg_class t ON t.oid = c.conrelid
      JOIN pg_catalog.pg_namespace n ON n.oid = t.relnamespace
      CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord)
      JOIN pg_catalog.pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
     WHERE c.contype = 'f' AND n.nspname = ANY (schemas) AND NOT t.relispartition
       AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_index i
                        WHERE i.indrelid = c.conrelid
                          AND (i.indkey::int2[])[0:array_length(c.conkey, 1) - 1] = c.conkey)
     GROUP BY c.oid, n.nspname, t.relname
  LOOP
    EXECUTE format('CREATE INDEX IF NOT EXISTS %I ON %I.%I (%s)',
                   left(r.relname || '_' || array_to_string(r.cols, '_') || '_fk_idx', 63),
                   r.nspname, r.relname,
                   (SELECT string_agg(quote_ident(x), ', ') FROM unnest(r.cols) AS x));
    n_idx := n_idx + 1;
  END LOOP;

  SELECT count(*) INTO left_over
    FROM pg_catalog.pg_constraint c
    JOIN pg_catalog.pg_class t ON t.oid = c.conrelid
    JOIN pg_catalog.pg_namespace n ON n.oid = t.relnamespace
   WHERE c.contype = 'f' AND n.nspname = ANY (schemas) AND NOT t.relispartition
     AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_index i
                      WHERE i.indrelid = c.conrelid
                        AND (i.indkey::int2[])[0:array_length(c.conkey, 1) - 1] = c.conkey);
  IF left_over > 0 THEN
    RAISE EXCEPTION '% foreign keys still have no index (an index name clashed?)', left_over;
  END IF;

  -- 2. auth.uid() / auth.jwt() / auth.role() evaluated once per query, not once per row:
  --    (select auth.uid()) is Supabase's documented form and means exactly the same.
  FOR r IN
    SELECT schemaname, tablename, policyname, qual, with_check
      FROM pg_catalog.pg_policies WHERE schemaname = ANY (schemas)
  LOOP
    nq := regexp_replace(r.qual, '(?<!SELECT )auth\.(uid|jwt|role)\(\)', '(select auth.\1())', 'g');
    nw := regexp_replace(r.with_check, '(?<!SELECT )auth\.(uid|jwt|role)\(\)', '(select auth.\1())', 'g');
    IF nq IS DISTINCT FROM r.qual OR nw IS DISTINCT FROM r.with_check THEN
      EXECUTE format('ALTER POLICY %I ON %I.%I%s%s', r.policyname, r.schemaname, r.tablename,
                     CASE WHEN r.qual IS NOT NULL THEN ' USING (' || nq || ')' ELSE '' END,
                     CASE WHEN r.with_check IS NOT NULL THEN ' WITH CHECK (' || nw || ')' ELSE '' END);
      n_pol := n_pol + 1;
    END IF;
  END LOOP;

  SELECT count(*) INTO left_over FROM pg_catalog.pg_policies
   WHERE schemaname = ANY (schemas)
     AND (coalesce(qual, '') ~ '(?<!SELECT )auth\.(uid|jwt|role)\(\)'
       OR coalesce(with_check, '') ~ '(?<!SELECT )auth\.(uid|jwt|role)\(\)');
  IF left_over > 0 THEN
    RAISE EXCEPTION '% policies still call auth.*() per row', left_over;
  END IF;

  RAISE NOTICE 'foreign-key indexes added: %, policies rewritten: %', n_idx, n_pol;
END $m$;
