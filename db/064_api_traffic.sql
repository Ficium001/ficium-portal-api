-- =============================================================================
-- 064 — API traffic: per-channel state and hourly rollups (portal admin site).
--
-- Every API call into or out of portal-api belongs to a CHANNEL (app/core/api_traffic.py lists them).
-- The portal admin site (ficium-admin, "API traffic") can pause a channel and shows how each one is doing.
--
-- 1. integration.api_channel_policy — the state the admin site last set per channel. Written only by
--    the admin command channel (api_channel.pause / api_channel.resume). `version` comes from the
--    admin database and only ever goes up: a replayed or out-of-order command is a no-op.
-- 2. integration.api_traffic_hour — one row per channel per hour, counts and a latency histogram.
--    The API aggregates in memory and upserts every ~10 s, so writes depend on the number of
--    channels, never on the number of requests. Pruned after 35 days.
-- 3. integration.api_channel_status — when a channel last worked and last failed, and the failure's
--    short, scrubbed text (a status or an exception class: never a body, URL, token or email).
--
-- Only the API's service session touches these; no tenant role may read or write them.
-- =============================================================================

CREATE TABLE IF NOT EXISTS integration.api_channel_policy (
  channel     text        PRIMARY KEY CHECK (channel ~ '^[a-z][a-z0-9_.]{2,59}$'),
  state       text        NOT NULL CHECK (state IN ('enabled', 'paused')),
  version     bigint      NOT NULL CHECK (version >= 0),
  reason      text        CHECK (reason IS NULL OR length(reason) <= 2000),
  changed_by  uuid,                          -- ficium-auth id of the admin who made the change
  changed_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS integration.api_traffic_hour (
  channel        text        NOT NULL CHECK (channel ~ '^[a-z][a-z0-9_.]{2,59}$'),
  hour           timestamptz NOT NULL CHECK (hour = date_trunc('hour', hour)),
  total          bigint      NOT NULL DEFAULT 0,   -- calls attempted, including blocked ones
  ok             bigint      NOT NULL DEFAULT 0,
  client_errors  bigint      NOT NULL DEFAULT 0,   -- 4xx: the other side asked for something we refuse
  server_errors  bigint      NOT NULL DEFAULT 0,   -- 5xx
  network_errors bigint      NOT NULL DEFAULT 0,   -- no answer: timeout, refused, reset
  blocked        bigint      NOT NULL DEFAULT 0,   -- stopped by a pause, never attempted
  -- latency of calls that were attempted, by upper bound in ms: 50, 100, 250, 500, 1000, 2500, 5000, above
  lat_b0 bigint NOT NULL DEFAULT 0, lat_b1 bigint NOT NULL DEFAULT 0, lat_b2 bigint NOT NULL DEFAULT 0,
  lat_b3 bigint NOT NULL DEFAULT 0, lat_b4 bigint NOT NULL DEFAULT 0, lat_b5 bigint NOT NULL DEFAULT 0,
  lat_b6 bigint NOT NULL DEFAULT 0, lat_b7 bigint NOT NULL DEFAULT 0,
  PRIMARY KEY (channel, hour)
);
CREATE INDEX IF NOT EXISTS api_traffic_hour_hour_idx ON integration.api_traffic_hour (hour);

CREATE TABLE IF NOT EXISTS integration.api_channel_status (
  channel       text        PRIMARY KEY CHECK (channel ~ '^[a-z][a-z0-9_.]{2,59}$'),
  last_ok_at    timestamptz,
  last_error_at timestamptz,
  last_error    text        CHECK (last_error IS NULL OR length(last_error) <= 300)
);

ALTER TABLE integration.api_channel_policy ENABLE ROW LEVEL SECURITY;
ALTER TABLE integration.api_traffic_hour   ENABLE ROW LEVEL SECURITY;
ALTER TABLE integration.api_channel_status ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON integration.api_channel_policy, integration.api_traffic_hour, integration.api_channel_status
  FROM PUBLIC, anon, authenticated;

-- A peer that is PAUSED (HTTP 503 channel_paused) is not a failed delivery. Putting the event back
-- without using an attempt keeps a long pause from pushing events past mark_failed's 24-hour limit
-- and dead-lettering them. Order is kept: the row keeps its created_at and is claimed again in turn.
CREATE OR REPLACE FUNCTION integration.defer(p_id text)
RETURNS void LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, integration AS $$
  UPDATE integration.outbox
     SET status = 'pending', attempts = greatest(attempts - 1, 0), lease_until = NULL,
         next_attempt_at = now() + interval '1 minute' * (1 + random() * 0.2),
         last_error = 'held: the receiving side has paused this channel'
   WHERE id = p_id AND status = 'sending';
$$;
REVOKE ALL ON FUNCTION integration.defer(text) FROM PUBLIC, anon, authenticated;
