-- =============================================================================
-- 008_esign.sql — E-signature: envelopes, signers, hash-chained event log
-- Gate: an offer_letter envelope can only be created when its
-- approval_instance is 'approved' — governance before dispatch.
-- ============================================================
BEGIN;

CREATE TABLE IF NOT EXISTS institution.esign_envelope (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  institution_id   uuid NOT NULL,
  entity_type      text NOT NULL CHECK (entity_type IN ('offer_letter','investment_mandate','custom')),
  entity_id        uuid NOT NULL,
  approval_instance_id uuid REFERENCES institution.approval_instance(id),
  title            text NOT NULL,
  document_path    text NOT NULL,           -- Supabase Storage path (unsigned source)
  document_sha256  text NOT NULL,           -- hash of source document
  sealed_path      text,                    -- final sealed PDF
  sealed_sha256    text,
  status           text NOT NULL DEFAULT 'draft' CHECK (status IN
                   ('draft','sent','partially_signed','completed','declined','expired','voided')),
  expires_at       timestamptz NOT NULL,
  created_by       uuid NOT NULL,
  created_at       timestamptz NOT NULL DEFAULT now(),
  completed_at     timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_live_envelope_per_entity
  ON institution.esign_envelope (entity_type, entity_id)
  WHERE status IN ('draft','sent','partially_signed');

CREATE TABLE IF NOT EXISTS institution.esign_signer (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  envelope_id    uuid NOT NULL REFERENCES institution.esign_envelope(id),
  party          text NOT NULL CHECK (party IN ('borrower','institution')),
  sign_order     int  NOT NULL CHECK (sign_order >= 1),
  display_name   text NOT NULL,
  email          text NOT NULL,
  user_ref       uuid,                      -- consumer_id (App DB) or portal user id
  token_sha256   text NOT NULL UNIQUE,      -- fic_sign_<64 hex> — ONLY the hash is stored,
                                             -- mirroring the fic_live_ API key pattern; the raw
                                             -- token exists only in the outbound email link
  otp_hash       text,                      -- sha256(otp + pepper), single active OTP
  otp_expires_at timestamptz,
  otp_attempts   int NOT NULL DEFAULT 0,
  otp_verified_at timestamptz,
  status         text NOT NULL DEFAULT 'pending' CHECK (status IN
                 ('pending','viewed','signed','declined')),
  signed_at      timestamptz,
  signed_ip      inet,
  signed_ua      text,
  decline_reason text,
  UNIQUE (envelope_id, sign_order)
);
CREATE INDEX IF NOT EXISTS idx_es_env ON institution.esign_signer(envelope_id);

-- hash-chained, append-only event log → tamper-evident audit certificate
CREATE TABLE IF NOT EXISTS institution.esign_event (
  id           bigserial PRIMARY KEY,
  envelope_id  uuid NOT NULL REFERENCES institution.esign_envelope(id),
  signer_id    uuid REFERENCES institution.esign_signer(id),
  event        text NOT NULL CHECK (event IN
               ('created','sent','viewed','otp_sent','otp_verified','otp_failed',
                'signed','declined','countersigned','sealed','voided','expired')),
  detail       jsonb NOT NULL DEFAULT '{}',
  ip           inet,
  user_agent   text,
  prev_hash    text NOT NULL,
  hash         text NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ee_env ON institution.esign_event(envelope_id, id);

-- RLS
DO $rls$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['esign_envelope','esign_signer','esign_event'] LOOP
    EXECUTE format('ALTER TABLE institution.%I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE institution.%I FORCE ROW LEVEL SECURITY', t);
  END LOOP;
END $rls$;

DROP POLICY IF EXISTS ee_env_tenant ON institution.esign_envelope;
CREATE POLICY ee_env_tenant ON institution.esign_envelope
  USING (institution_id = (SELECT ctx.institution_id FROM institution.current_member_ctx_v2() ctx));
DROP POLICY IF EXISTS ee_sig_tenant ON institution.esign_signer;
CREATE POLICY ee_sig_tenant ON institution.esign_signer
  USING (envelope_id IN (SELECT id FROM institution.esign_envelope
                         WHERE institution_id = (SELECT ctx.institution_id FROM institution.current_member_ctx_v2() ctx)));
DROP POLICY IF EXISTS ee_evt_tenant ON institution.esign_event;
CREATE POLICY ee_evt_tenant ON institution.esign_event
  USING (envelope_id IN (SELECT id FROM institution.esign_envelope
                         WHERE institution_id = (SELECT ctx.institution_id FROM institution.current_member_ctx_v2() ctx)));

REVOKE UPDATE, DELETE ON institution.esign_event FROM PUBLIC;
REVOKE UPDATE, DELETE ON institution.esign_event FROM authenticated;

-- ---------- event append with hash chain ----------
CREATE OR REPLACE FUNCTION institution.esign_append_event(
  p_envelope uuid, p_signer uuid, p_event text,
  p_detail jsonb DEFAULT '{}', p_ip inet DEFAULT NULL, p_ua text DEFAULT NULL
) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path = institution, public AS $$
DECLARE v_prev text; v_hash text;
BEGIN
  SELECT hash INTO v_prev FROM esign_event
  WHERE envelope_id = p_envelope ORDER BY id DESC LIMIT 1;
  v_prev := COALESCE(v_prev, 'GENESIS');
  v_hash := encode(sha256(convert_to(
              v_prev || p_event || COALESCE(p_signer::text,'') ||
              COALESCE(p_detail::text,'{}') || now()::text, 'UTF8')), 'hex');
  INSERT INTO esign_event (envelope_id, signer_id, event, detail, ip, user_agent, prev_hash, hash)
  VALUES (p_envelope, p_signer, p_event, p_detail, p_ip, p_ua, v_prev, v_hash);
  RETURN v_hash;
END $$;

-- ---------- create envelope (approval-gated) ----------
CREATE OR REPLACE FUNCTION institution.esign_create_envelope(
  p_institution_id uuid, p_entity_type text, p_entity_id uuid,
  p_approval_instance uuid, p_title text,
  p_document_path text, p_document_sha256 text,
  p_expires_at timestamptz, p_created_by uuid,
  p_signers jsonb  -- [{party, sign_order, display_name, email, user_ref, token_sha256}]
) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = institution, public AS $$
DECLARE v_env uuid; s jsonb;
BEGIN
  IF p_entity_type IN ('offer_letter','investment_mandate') THEN
    PERFORM 1 FROM approval_instance
     WHERE id = p_approval_instance AND institution_id = p_institution_id
       AND status = 'approved';
    IF NOT FOUND THEN RAISE EXCEPTION 'ESIGN_APPROVAL_NOT_GRANTED'; END IF;
  END IF;

  INSERT INTO esign_envelope
    (institution_id, entity_type, entity_id, approval_instance_id, title,
     document_path, document_sha256, expires_at, created_by)
  VALUES (p_institution_id, p_entity_type, p_entity_id, p_approval_instance, p_title,
          p_document_path, p_document_sha256, p_expires_at, p_created_by)
  RETURNING id INTO v_env;

  FOR s IN SELECT * FROM jsonb_array_elements(p_signers) LOOP
    INSERT INTO esign_signer
      (envelope_id, party, sign_order, display_name, email, user_ref, token_sha256)
    VALUES (v_env, s->>'party', (s->>'sign_order')::int, s->>'display_name',
            s->>'email', NULLIF(s->>'user_ref','')::uuid, s->>'token_sha256');
  END LOOP;

  PERFORM institution.esign_append_event(v_env, NULL, 'created',
    jsonb_build_object('document_sha256', p_document_sha256));
  RETURN v_env;
END $$;

-- ---------- record signature (order + OTP enforced) ----------
CREATE OR REPLACE FUNCTION institution.esign_sign(
  p_signer uuid, p_ip inet, p_ua text
) RETURNS text  -- envelope status after signing
LANGUAGE plpgsql SECURITY DEFINER SET search_path = institution, public AS $$
DECLARE v record; v_pending int; v_env_status text;
BEGIN
  SELECT sg.*, e.id AS env_id, e.status AS env_status, e.expires_at
    INTO v
  FROM esign_signer sg JOIN esign_envelope e ON e.id = sg.envelope_id
  WHERE sg.id = p_signer FOR UPDATE OF sg, e;

  IF NOT FOUND THEN RAISE EXCEPTION 'ESIGN_SIGNER_NOT_FOUND'; END IF;
  IF v.env_status NOT IN ('sent','partially_signed') THEN
    RAISE EXCEPTION 'ESIGN_ENVELOPE_NOT_SIGNABLE'; END IF;
  IF v.expires_at < now() THEN RAISE EXCEPTION 'ESIGN_ENVELOPE_EXPIRED'; END IF;
  IF v.status = 'signed' THEN RAISE EXCEPTION 'ESIGN_ALREADY_SIGNED'; END IF;
  IF v.otp_verified_at IS NULL OR v.otp_verified_at < now() - interval '10 minutes' THEN
    RAISE EXCEPTION 'ESIGN_OTP_NOT_VERIFIED';
  END IF;
  -- sequential signing: all lower orders must be signed
  PERFORM 1 FROM esign_signer
   WHERE envelope_id = v.env_id AND sign_order < v.sign_order AND status <> 'signed';
  IF FOUND THEN RAISE EXCEPTION 'ESIGN_OUT_OF_ORDER'; END IF;

  UPDATE esign_signer
     SET status = 'signed', signed_at = now(), signed_ip = p_ip, signed_ua = p_ua
   WHERE id = p_signer;

  PERFORM institution.esign_append_event(v.env_id, p_signer,
    CASE WHEN v.party = 'institution' THEN 'countersigned' ELSE 'signed' END,
    jsonb_build_object('sign_order', v.sign_order), p_ip, p_ua);

  SELECT count(*) INTO v_pending FROM esign_signer
   WHERE envelope_id = v.env_id AND status <> 'signed';

  v_env_status := CASE WHEN v_pending = 0 THEN 'completed' ELSE 'partially_signed' END;
  UPDATE esign_envelope
     SET status = v_env_status,
         completed_at = CASE WHEN v_pending = 0 THEN now() END
   WHERE id = v.env_id;

  IF v_pending = 0 THEN
    INSERT INTO institution.approval_outbox (institution_id, event, payload)
    SELECT institution_id, 'esign.completed',
           jsonb_build_object('envelope_id', id, 'entity_type', entity_type, 'entity_id', entity_id)
    FROM esign_envelope WHERE id = v.env_id;
  END IF;
  RETURN v_env_status;
END $$;

-- ---------- decline / void / expire ----------
CREATE OR REPLACE FUNCTION institution.esign_decline(
  p_signer uuid, p_reason text, p_ip inet, p_ua text
) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = institution, public AS $$
DECLARE v record;
BEGIN
  SELECT sg.*, e.id AS env_id INTO v
  FROM esign_signer sg JOIN esign_envelope e ON e.id = sg.envelope_id
  WHERE sg.id = p_signer FOR UPDATE OF sg, e;
  IF NOT FOUND THEN RAISE EXCEPTION 'ESIGN_SIGNER_NOT_FOUND'; END IF;
  IF p_reason IS NULL OR length(trim(p_reason)) = 0 THEN
    RAISE EXCEPTION 'ESIGN_REASON_REQUIRED'; END IF;
  UPDATE esign_signer SET status = 'declined', decline_reason = p_reason WHERE id = p_signer;
  UPDATE esign_envelope SET status = 'declined' WHERE id = v.env_id;
  PERFORM institution.esign_append_event(v.env_id, p_signer, 'declined',
    jsonb_build_object('reason', p_reason), p_ip, p_ua);
  INSERT INTO institution.approval_outbox (institution_id, event, payload)
  SELECT institution_id, 'esign.declined',
         jsonb_build_object('envelope_id', id, 'entity_type', entity_type,
                            'entity_id', entity_id, 'reason', p_reason)
  FROM esign_envelope WHERE id = v.env_id;
END $$;

CREATE OR REPLACE FUNCTION institution.esign_expire_overdue()
RETURNS int
LANGUAGE plpgsql SECURITY DEFINER SET search_path = institution, public AS $$
DECLARE r record; n int := 0;
BEGIN
  FOR r IN SELECT id, institution_id FROM esign_envelope
           WHERE status IN ('sent','partially_signed') AND expires_at < now()
           FOR UPDATE SKIP LOCKED
  LOOP
    n := n + 1;
    UPDATE esign_envelope SET status = 'expired' WHERE id = r.id;
    PERFORM institution.esign_append_event(r.id, NULL, 'expired', '{}');
    INSERT INTO approval_outbox (institution_id, event, payload)
    VALUES (r.institution_id, 'esign.expired', jsonb_build_object('envelope_id', r.id));
  END LOOP;
  RETURN n;
END $$;

-- pg_cron: SELECT cron.schedule('esign-expiry', '*/15 * * * *',
--   $$SELECT institution.esign_expire_overdue()$$);

COMMIT;
