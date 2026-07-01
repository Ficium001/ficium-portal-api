-- =============================================================================
-- Migration: 005_api_keys_risk_tier.sql
-- Applied: 2026-07-01
-- Portal DB: egwobcajdlragubtkpqp
-- =============================================================================
-- 1. Add risk assessment columns to marketplace.request
-- 2. Grant INSERT/UPDATE/DELETE on institution.api_key to authenticated + RLS
-- 3. SECURITY DEFINER helper to resolve auth_user_id → display name
-- =============================================================================

-- ── 1. Risk tier columns ─────────────────────────────────────────────────────

ALTER TABLE marketplace.request
  ADD COLUMN IF NOT EXISTS ficium_risk_tier text CHECK (ficium_risk_tier IN ('A','B','C','D')),
  ADD COLUMN IF NOT EXISTS ficium_score     numeric(6,2);

CREATE INDEX IF NOT EXISTS idx_marketplace_request_risk_tier
  ON marketplace.request (ficium_risk_tier)
  WHERE ficium_risk_tier IS NOT NULL;

-- ── 2. institution.api_key — write grants + RLS policies ─────────────────────

GRANT INSERT, UPDATE, DELETE ON institution.api_key TO authenticated;

-- INSERT: can only create keys for own institution
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE schemaname = 'institution' AND tablename = 'api_key' AND policyname = 'api_key_insert'
  ) THEN
    EXECUTE $$
      CREATE POLICY api_key_insert ON institution.api_key
        FOR INSERT TO authenticated
        WITH CHECK (
          institution_id = (
            SELECT ctx.institution_id FROM institution.current_member_ctx() ctx
          )
        )
    $$;
  END IF;
END $$;

-- UPDATE: own institution's keys only
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE schemaname = 'institution' AND tablename = 'api_key' AND policyname = 'api_key_update'
  ) THEN
    EXECUTE $$
      CREATE POLICY api_key_update ON institution.api_key
        FOR UPDATE TO authenticated
        USING (
          institution_id = (
            SELECT ctx.institution_id FROM institution.current_member_ctx() ctx
          )
        )
    $$;
  END IF;
END $$;

-- DELETE: own institution's keys only
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE schemaname = 'institution' AND tablename = 'api_key' AND policyname = 'api_key_delete'
  ) THEN
    EXECUTE $$
      CREATE POLICY api_key_delete ON institution.api_key
        FOR DELETE TO authenticated
        USING (
          institution_id = (
            SELECT ctx.institution_id FROM institution.current_member_ctx() ctx
          )
        )
    $$;
  END IF;
END $$;

-- ── 3. SECURITY DEFINER helper — safe display name lookup ────────────────────
-- Lets authenticated role resolve a user UUID → display name
-- without a full SELECT grant on admin."user"

CREATE OR REPLACE FUNCTION admin.get_user_display_name(p_auth_user_id uuid)
RETURNS text
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = admin, public
AS $$
  SELECT COALESCE(display_name, email)
  FROM admin."user"
  WHERE auth_user_id = p_auth_user_id
  LIMIT 1;
$$;

GRANT EXECUTE ON FUNCTION admin.get_user_display_name(uuid) TO authenticated;
