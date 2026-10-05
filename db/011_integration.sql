-- =============================================================================
-- Integration contract v1: outbox, inbox and delivery state.
-- Identical on both databases (App DB and Portal DB). See ficium-integration.
--
-- Access model: nothing here is reachable by `anon` or `authenticated`.
--   * Portal DB: portal-api connects as `postgres` and calls integration.* directly.
--   * App DB:    Vercel functions call the public.integration_* wrappers as
--                `service_role` (defined in the App DB file only).
-- Tables have RLS enabled with no policies as a second lock.
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS integration;
REVOKE ALL ON SCHEMA integration FROM PUBLIC;
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
    EXECUTE 'REVOKE ALL ON SCHEMA integration FROM anon, authenticated';
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS integration.outbox (
  id               text PRIMARY KEY CHECK (id ~ '^evt_[0-9A-Za-z]{10,40}$'),
  type             text        NOT NULL,
  version          integer     NOT NULL DEFAULT 1,
  aggregate_id     text        NOT NULL,
  sequence         bigint      NOT NULL CHECK (sequence >= 1),
  envelope         jsonb       NOT NULL,
  status           text        NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending','sending','delivered','dead')),
  attempts         integer     NOT NULL DEFAULT 0,
  next_attempt_at  timestamptz NOT NULL DEFAULT now(),
  lease_until      timestamptz,
  last_error       text,
  created_at       timestamptz NOT NULL DEFAULT now(),
  delivered_at     timestamptz,
  UNIQUE (aggregate_id, sequence)
);
CREATE INDEX IF NOT EXISTS outbox_due_idx
  ON integration.outbox (status, next_attempt_at) WHERE status IN ('pending','sending');

CREATE TABLE IF NOT EXISTS integration.aggregate_sequence (
  aggregate_id   text PRIMARY KEY,
  last_sequence  bigint NOT NULL
);

CREATE TABLE IF NOT EXISTS integration.inbox (
  event_id      text PRIMARY KEY,
  type          text        NOT NULL,
  source        text        NOT NULL CHECK (source IN ('borrower','institution')),
  aggregate_id  text        NOT NULL,
  sequence      bigint      NOT NULL,
  outcome       text        NOT NULL CHECK (outcome IN ('apply','stale')),
  received_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS integration.inbox_watermark (
  aggregate_id   text PRIMARY KEY,
  last_sequence  bigint NOT NULL
);

ALTER TABLE integration.outbox             ENABLE ROW LEVEL SECURITY;
ALTER TABLE integration.aggregate_sequence ENABLE ROW LEVEL SECURITY;
ALTER TABLE integration.inbox              ENABLE ROW LEVEL SECURITY;
ALTER TABLE integration.inbox_watermark    ENABLE ROW LEVEL SECURITY;

-- ── enqueue: write one event in the caller's transaction ────────────────────
CREATE OR REPLACE FUNCTION integration.enqueue(
  p_type text, p_aggregate_id text, p_data jsonb, p_source text
) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, integration AS $$
DECLARE
  v_seq bigint;
  v_id  text := 'evt_' || replace(gen_random_uuid()::text, '-', '');
  v_env jsonb;
BEGIN
  IF p_source NOT IN ('borrower','institution') THEN
    RAISE EXCEPTION 'integration.enqueue: bad source %', p_source;
  END IF;
  INSERT INTO integration.aggregate_sequence AS s (aggregate_id, last_sequence)
  VALUES (p_aggregate_id, 1)
  ON CONFLICT (aggregate_id) DO UPDATE SET last_sequence = s.last_sequence + 1
  RETURNING last_sequence INTO v_seq;

  v_env := jsonb_build_object(
    'id', v_id, 'type', p_type, 'version', 1, 'source', p_source,
    'occurred_at', to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'),
    'aggregate_id', p_aggregate_id, 'sequence', v_seq, 'data', coalesce(p_data, '{}'::jsonb));

  INSERT INTO integration.outbox (id, type, aggregate_id, sequence, envelope)
  VALUES (v_id, p_type, p_aggregate_id, v_seq, v_env);
  RETURN v_env;
END $$;

-- ── claim_batch: lease due rows to one dispatcher; safe with many replicas ──
CREATE OR REPLACE FUNCTION integration.claim_batch(p_limit integer DEFAULT 20, p_lease_seconds integer DEFAULT 60)
RETURNS TABLE (id text, envelope jsonb, attempts integer)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, integration AS $$
BEGIN
  RETURN QUERY
  UPDATE integration.outbox o
     SET status = 'sending',
         lease_until = now() + make_interval(secs => p_lease_seconds),
         attempts = o.attempts + 1
   WHERE o.id IN (
     SELECT x.id FROM integration.outbox x
      WHERE (x.status = 'pending' AND x.next_attempt_at <= now())
         OR (x.status = 'sending' AND x.lease_until < now())
      ORDER BY x.created_at
      LIMIT greatest(1, least(p_limit, 100))
      FOR UPDATE SKIP LOCKED)
  RETURNING o.id, o.envelope, o.attempts;
END $$;

CREATE OR REPLACE FUNCTION integration.mark_delivered(p_id text)
RETURNS void LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, integration AS $$
  UPDATE integration.outbox
     SET status = 'delivered', delivered_at = now(), lease_until = NULL, last_error = NULL
   WHERE id = p_id;
$$;

-- Backoff: 30 s, 2 min, 10 min, 1 h, then hourly; dead after 24 h (contract v1).
CREATE OR REPLACE FUNCTION integration.mark_failed(p_id text, p_error text)
RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, integration AS $$
DECLARE
  v_attempts integer; v_created timestamptz; v_delay interval; v_status text;
BEGIN
  SELECT o.attempts, o.created_at INTO v_attempts, v_created FROM integration.outbox o WHERE o.id = p_id;
  IF NOT FOUND THEN RETURN 'missing'; END IF;
  v_delay := CASE v_attempts WHEN 1 THEN interval '30 seconds' WHEN 2 THEN interval '2 minutes'
                             WHEN 3 THEN interval '10 minutes' ELSE interval '1 hour' END;
  v_delay := v_delay * (1 + random() * 0.1);
  v_status := CASE WHEN now() + v_delay > v_created + interval '24 hours' THEN 'dead' ELSE 'pending' END;
  UPDATE integration.outbox
     SET status = v_status, next_attempt_at = now() + v_delay, lease_until = NULL,
         last_error = left(p_error, 1000)
   WHERE id = p_id;
  RETURN v_status;
END $$;

-- ── record_inbox: dedupe + per-aggregate ordering, in the receiver's txn ────
-- Returns 'duplicate' (already seen), 'stale' (older than last applied) or 'apply'.
CREATE OR REPLACE FUNCTION integration.record_inbox(
  p_event_id text, p_type text, p_source text, p_aggregate_id text, p_sequence bigint
) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, integration AS $$
DECLARE v_last bigint; v_outcome text;
BEGIN
  PERFORM 1 FROM integration.inbox i WHERE i.event_id = p_event_id;
  IF FOUND THEN RETURN 'duplicate'; END IF;

  SELECT w.last_sequence INTO v_last FROM integration.inbox_watermark w
   WHERE w.aggregate_id = p_aggregate_id FOR UPDATE;
  IF v_last IS NOT NULL AND p_sequence <= v_last THEN
    v_outcome := 'stale';
  ELSE
    v_outcome := 'apply';
    INSERT INTO integration.inbox_watermark (aggregate_id, last_sequence)
    VALUES (p_aggregate_id, p_sequence)
    ON CONFLICT (aggregate_id) DO UPDATE SET last_sequence = excluded.last_sequence;
  END IF;

  INSERT INTO integration.inbox (event_id, type, source, aggregate_id, sequence, outcome)
  VALUES (p_event_id, p_type, p_source, p_aggregate_id, p_sequence, v_outcome)
  ON CONFLICT (event_id) DO NOTHING;
  IF NOT FOUND THEN RETURN 'duplicate'; END IF;
  RETURN v_outcome;
END $$;

-- Lock everything down, then grant nothing to client roles.
REVOKE ALL ON ALL TABLES    IN SCHEMA integration FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA integration FROM PUBLIC;
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
    EXECUTE 'REVOKE ALL ON ALL TABLES    IN SCHEMA integration FROM anon, authenticated';
    EXECUTE 'REVOKE ALL ON ALL FUNCTIONS IN SCHEMA integration FROM anon, authenticated';
  END IF;
END $$;
