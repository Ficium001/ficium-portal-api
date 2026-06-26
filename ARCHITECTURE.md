# ficium-portal-api — Architecture

_Last updated: 27 June 2026_

The portable data API for the Ficium Portal. It verifies ficium-auth RS256 tokens and serves institution-scoped data with Postgres row-level security enforced — doing exactly what Supabase's PostgREST does, but without PostgREST, so the same database security layer runs on any Postgres. This is the keystone of the platform's lift-and-shift story (ADR-001).

For the full platform picture, see `ficium-portal/ARCHITECTURE.md`.

---

## 1. Core idea

The Portal's security model lives in the **database**: RLS policies and `SECURITY DEFINER` functions that resolve the caller through `auth.uid()`. This service replicates PostgREST's behaviour:

1. Verify the ficium-auth token (RS256, against cached JWKS).
2. Open a DB transaction and `set_config('request.jwt.claims', <claims>, true)`.
3. Run the existing queries — RLS enforces tenant isolation unchanged.

No business logic is duplicated from the database; the API is a thin, portable shell around the SQL security layer.

---

## 2. Structure

```
app/
  api/      institutions, members, groups, approvals, marketplace, catalog,
            documents, benefits, admin, auth_provision, public
  core/     config.py, db.py (tenant_session / app_service_session / pooler),
            security.py (JWKS/RS256)
  deps.py   claim extraction → request context
  main.py   app + router wiring
db/
  000_auth_shim.sql   auth.uid()/jwt()/role() + authenticated/anon (non-Supabase)
  001_workflow.sql    workflow helpers
  003_expiry_notify.sql   close_expired_windows() + pg_net notification dispatch
  004_accept_bid_reveal.sql   accept_bid() with bid financials in return
  README.md           load order
docs/
  ADR-001-portable-portal-data-layer.md
  ADR-002-identity-migration.md
```

---

## 3. Deployment topology

```
ficium-portal (Vercel SPA)                 ficium (Vercel SPA — consumer app)
     │  Bearer <ficium-auth RS256 JWT>          │  X-Service-Secret
     ▼                                          ▼
┌─────────────────────────────────────────────────────────────┐
│                    ficium-portal-api                         │
│                    (FastAPI · Railway)                       │
│                                                             │
│  security.py  verify RS256 (kid → JWKS, 5min cache)        │
│  deps.py      extract claims                                │
│  tenant_session()  SET request.jwt.claims (per request)    │
│  app_service_session()  direct App DB connection           │
└──────────┬────────────────────────────────────┬────────────┘
           │ psycopg2, transaction pooler :6543  │ APP_DATABASE_URL
           ▼                                    ▼
┌──────────────────────────┐     ┌───────────────────────────────┐
│  Portal DB (Institution) │     │  App DB (Consumer)            │
│  egwobcajdlragubtkpqp   │     │  wixfhjlsjkiwfvqewvmt         │
│  ap-southeast-1          │     │  ap-south-1                   │
│                          │     │                               │
│  institution.*           │     │  public.clients               │
│  marketplace.*           │     │  public.requests              │
│  governance.*            │     │  public.kyc_submissions       │
│  catalog.*               │     │  (Phase 2 PII fetch only)     │
│  bid_notify.*            │     └───────────────────────────────┘
└──────────────────────────┘
           ▲
           │ JWKS (cached 5min)
┌──────────┴────────────────┐
│      ficium-auth           │
│  /.well-known/jwks.json    │
└────────────────────────────┘
```

---

## 4. Two DB connections

| Session | DSN variable | Schema | When used |
|---------|-------------|--------|-----------|
| `tenant_session()` | `DATABASE_URL` | Institution (Portal DB) | All institution data routes |
| `app_service_session()` | `APP_DATABASE_URL` | Consumer (App DB) | Phase 2 PII fetch, marketplace sync |

The App DB connection is a direct service-level credential, not tenant-scoped. It is used only for:
- `POST /marketplace/sync-requests` — read open consumer requests + financial data
- `POST /public/requests/:id/accept-bid` — fetch consumer PII for Phase 2 reveal

This is a known architectural debt: `APP_DATABASE_URL` should be replaced by a service JWT call once `ficium-auth` client-credentials grant is built (ADR-002).

---

## 5. Marketplace lifecycle

```
POST /marketplace/sync-requests
  ← called by pg_net (App DB trigger) or pg_cron (5min sweep)
  → pull open requests from App DB (_ENRICH_SQL)
  → enrich with financial data (income, employment, loans, snapshot)
  → marketplace.ingest_app_request() on Portal DB
  → returns { pulled, synced, failed }

POST /marketplace/bids  (maker)
  → validate bid window + request status
  → governance.submit_for_approval(action='bid.submit', payload={...})
  → returns pending governance.action id

POST /approvals/{id}/approve  (checker)
  → governance.approve_action()
  → _execute_action('bid.submit')
  → INSERT INTO marketplace.bid
  → trg_bid_notify fires (pg_net → bid-notify handler)

POST /public/requests/{id}/accept-bid  (s2s from Vercel)
  → _anon_uuid ownership check
  → fetch Phase 2 PII from App DB
  → marketplace.accept_bid() atomic:
      bid → accepted, others → rejected
      request → accepted + winning_bid_id
      bid_acceptance (PII stored on Portal DB)
      pipeline auto-created from institution template
  → returns institution contact + bid financials

POST /marketplace/close-expired  (GitHub Actions, every 30 min)
  → marketplace.close_expired_windows()
  → request status: 'closed' (has bids) or 'expired' (no bids)
  → fires pg_net → request-expired handler (for expired only)
```

---

## 6. Portability (non-Supabase deployments)

For client-cloud or on-prem Postgres, run `db/000_auth_shim.sql` before the Portal migrations. It recreates:
- `auth.uid()` — reads from `request.jwt.claims` GUC
- `auth.jwt()` — same
- `auth.role()` — returns `'authenticated'` when GUC is set
- `authenticated` and `anon` roles

This makes existing RLS policies work unchanged on any Postgres. Do **not** run the shim on Supabase — the platform owns those objects there.

---

## 7. Connection constraints

- **Transaction pooler only (port 6543):** psycopg2, not asyncpg. Supabase blocks direct port 5432 on free plan.
- **Username:** `postgres.<project-ref>` (pooler tenant prefix, not plain `postgres`).
- **No `SET ROLE`:** pgbouncer transaction mode prohibits session-level role changes. RLS keys on the GUC, not `current_role`.
- **Sync driver:** psycopg2 (not asyncpg) — asyncpg requires a direct connection for session setup.
