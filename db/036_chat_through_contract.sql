-- =============================================================================
-- 036_chat_through_contract.sql — contract 1.9.1. Per-lender chat without the
-- borrower database: the portal read and wrote public.request_messages in the
-- App DB directly (and stored the bank staff member's id there as sender_id).
--
-- 1. marketplace.request_message: the institution side's copy of each thread
--    (request_id + institution_id). member_id is the sending staff member,
--    internal to this side: chat.message never carries a person's identity.
-- 2. The rules, from the portal's own data (who won = an accepted bid):
--    a thread closes once another institution's offer is accepted; free text
--    only for the institution whose offer was accepted. Same rules the borrower
--    side enforces (request_messages_enforce), so both agree.
-- 3. marketplace.send_institution_message(): checks, records, emits chat.message
--    in one transaction. Bodies are rendered by the contract renderer before.
-- 4. integration.apply_chat_message(): a borrower's chat.message.
-- 5. Seed: the two per-lender messages that existed in the App DB.
-- =============================================================================
BEGIN;

CREATE TABLE IF NOT EXISTS marketplace.request_message (
    id             uuid PRIMARY KEY,
    request_id     uuid        NOT NULL REFERENCES marketplace.request(id),
    institution_id uuid        NOT NULL,
    sender_side    text        NOT NULL CHECK (sender_side IN ('borrower', 'institution')),
    kind           text        NOT NULL CHECK (kind IN ('structured', 'free')),
    template_code  text,
    params         jsonb       NOT NULL DEFAULT '{}'::jsonb,
    body           text        NOT NULL CHECK (char_length(body) BETWEEN 1 AND 2000),
    member_id      uuid,
    created_at     timestamptz NOT NULL DEFAULT now(),
    CHECK ((kind = 'structured') = (template_code IS NOT NULL)),
    CHECK (sender_side = 'institution' OR member_id IS NULL)
);
CREATE INDEX IF NOT EXISTS ix_request_message_thread ON marketplace.request_message (request_id, institution_id, created_at);
ALTER TABLE marketplace.request_message ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON marketplace.request_message FROM PUBLIC;
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
    EXECUTE 'REVOKE ALL ON marketplace.request_message FROM anon, authenticated';
  END IF;
END $$;

CREATE OR REPLACE FUNCTION marketplace.chat_thread_state(p_request_id uuid, p_institution_id uuid)
RETURNS TABLE (is_open boolean, is_winner boolean) LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, marketplace AS $$
  SELECT NOT EXISTS (SELECT 1 FROM marketplace.bid b WHERE b.request_id = p_request_id AND b.status = 'accepted')
         OR EXISTS (SELECT 1 FROM marketplace.bid b WHERE b.request_id = p_request_id AND b.status = 'accepted'
                      AND b.institution_id = p_institution_id),
         EXISTS (SELECT 1 FROM marketplace.bid b WHERE b.request_id = p_request_id AND b.status = 'accepted'
                   AND b.institution_id = p_institution_id);
$$;

CREATE OR REPLACE FUNCTION marketplace.send_institution_message(
    p_request_id uuid, p_institution_id uuid, p_member_id uuid,
    p_template_code text, p_params jsonb, p_body text)
RETURNS marketplace.request_message LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, marketplace, integration AS $$
DECLARE st record; m marketplace.request_message%ROWTYPE; r marketplace.request%ROWTYPE;
BEGIN
  SELECT * INTO r FROM marketplace.request WHERE id = p_request_id;
  IF NOT FOUND THEN RAISE EXCEPTION 'CHAT_REQUEST_NOT_FOUND'; END IF;
  SELECT * INTO st FROM marketplace.chat_thread_state(p_request_id, p_institution_id);
  IF NOT st.is_open THEN RAISE EXCEPTION 'CHAT_CLOSED'; END IF;
  IF p_template_code IS NULL AND NOT st.is_winner THEN RAISE EXCEPTION 'CHAT_FREE_TEXT_NOT_ALLOWED'; END IF;
  INSERT INTO marketplace.request_message (id, request_id, institution_id, sender_side, kind, template_code, params, body, member_id)
  VALUES (gen_random_uuid(), p_request_id, p_institution_id, 'institution',
          CASE WHEN p_template_code IS NULL THEN 'free' ELSE 'structured' END, p_template_code,
          coalesce(p_params, '{}'::jsonb), p_body, p_member_id)
  RETURNING * INTO m;
  IF r.source = 'app' THEN
    PERFORM integration.enqueue('chat.message', p_request_id::text, jsonb_strip_nulls(jsonb_build_object(
      'message_id', m.id, 'request_id', m.request_id, 'institution_id', m.institution_id, 'sender_side', 'institution',
      'template_code', m.template_code, 'params', m.params, 'body', m.body, 'sent_at', m.created_at,
      'free_text_allowed', st.is_winner)), 'institution');
  END IF;
  RETURN m;
END $$;

CREATE OR REPLACE FUNCTION integration.apply_chat_message(p_env jsonb)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, integration, marketplace AS $$
DECLARE d jsonb := p_env -> 'data';
BEGIN
  IF d ->> 'sender_side' <> 'borrower' THEN RAISE EXCEPTION 'apply_chat_message: borrower messages only'; END IF;
  INSERT INTO marketplace.request_message (id, request_id, institution_id, sender_side, kind, template_code, params, body, created_at)
  VALUES ((d ->> 'message_id')::uuid, (d ->> 'request_id')::uuid, (d ->> 'institution_id')::uuid, 'borrower',
          CASE WHEN d ->> 'template_code' IS NULL THEN 'free' ELSE 'structured' END, d ->> 'template_code',
          coalesce(d -> 'params', '{}'::jsonb), d ->> 'body', (d ->> 'sent_at')::timestamptz)
  ON CONFLICT (id) DO NOTHING;
END $$;

-- 5. Seed (copied from the App DB on 2026-10-18; the one older message has no lender and stays borrower-side).
INSERT INTO marketplace.request_message (id, request_id, institution_id, sender_side, kind, template_code, params, body, created_at)
VALUES
  ('92d09306-abe3-49af-a368-27be64578f95', 'f8a91939-9b4b-444e-8f4b-86dd0d6020c0', 'f192050a-dfdd-4da7-874e-c3db88f11e41',
   'borrower', 'structured', 'q.fees_breakdown', '{}', 'Can you break down the fees on this offer?', '2026-08-03T14:06:31.6826+00:00'),
  ('42f9c8e2-690a-45bd-8e37-33cd7e44f07a', 'f8a91939-9b4b-444e-8f4b-86dd0d6020c0', 'f192050a-dfdd-4da7-874e-c3db88f11e41',
   'institution', 'structured', 'a.docs_list', '{"documents": ["POA", "NIC"]}', 'We will need the following: POA, NIC.',
   '2026-08-03T14:10:58.274513+00:00')
ON CONFLICT (id) DO NOTHING;

REVOKE ALL ON FUNCTION marketplace.chat_thread_state(uuid, uuid), integration.apply_chat_message(jsonb),
  marketplace.send_institution_message(uuid, uuid, uuid, text, jsonb, text) FROM PUBLIC;
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
    EXECUTE 'REVOKE ALL ON FUNCTION marketplace.chat_thread_state(uuid, uuid), integration.apply_chat_message(jsonb),
      marketplace.send_institution_message(uuid, uuid, uuid, text, jsonb, text) FROM anon, authenticated';
  END IF;
END $$;

COMMIT;
