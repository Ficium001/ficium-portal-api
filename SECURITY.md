# ficium-portal-api — Security Model

Authoritative reference for how this service enforces security. Read alongside
`ARCHITECTURE.md`, `ADR-001` (portable data layer) and `ADR-002` (identity).

---

## 1. Authentication

Two independent paths, unified into one claims dict by `deps.py`:

| Path | Credential | Verified by | Used by |
|---|---|---|---|
| **Portal JWT** | `Authorization: Bearer <RS256 JWT>` | `core/security.verify_token()` — JWKS lookup, `algorithms=["RS256"]` pinned, `aud` + `iss` + `kid` validated | Human operators in the browser |
| **API key** | `Authorization: Bearer fic_live_<hex>` | `core/api_keys.verify_api_key()` — SHA-256 hash lookup, checks active/approved/revoked/expired | Machine-to-machine (institution LOS, scripts) |

- JWTs are **only ever verified**, never issued, here — `ficium-auth` is the sole issuer.
- Algorithm is pinned to RS256; there is no HS256 acceptance path, so algorithm-confusion attacks do not apply.
- API keys: the raw key is never stored (only its SHA-256 hash + a 12-char display prefix). Keys require maker-checker approval (`mc_status = 'approved'`) before they authenticate.

## 2. Tenant Isolation (RLS)

The core control. Every tenant-scoped table has Row Level Security, and the API
enforces it via `core/db.tenant_session()`:

1. `set_config('request.jwt.claims', …)` publishes the verified claims so
   `auth.uid()` / `auth.jwt()` resolve inside policies.
2. `SET LOCAL ROLE authenticated` — **critical**: the pooler connects as
   `postgres` (BYPASSRLS). Without this role switch, every RLS policy is inert.

Both are `SET LOCAL` (transaction-scoped), safe under pgbouncer transaction-mode
pooling.

**Policy pattern:** tenant tables filter on
`institution_id = (SELECT ctx.institution_id FROM institution.current_member_ctx_v2() ctx)`.
Child tables without their own `institution_id` scope via an `EXISTS` join to
their parent (e.g. `pipeline_stage_instance` → `loan_pipeline`).

**RLS is ENABLED and FORCED** on all tenant tables. `FORCE` ensures isolation
even if a query ever runs as the table owner.

> **Do not** add a tenant-scoped table without: (a) an `institution_id` column
> (or a parent link to one), (b) `ENABLE` + `FORCE ROW LEVEL SECURITY`, (c) a
> tenant policy, (d) `GRANT`s to `authenticated`. Migration `006_pipeline_rls.sql`
> is the reference example.

### `service_session()` — the RLS bypass

Some admin/cross-project operations use `service_session()` (runs as `postgres`,
RLS bypassed). **Every** such handler MUST filter by `institution_id` explicitly,
or (for `/admin/*`) sit behind the admin guard. The admin router has a
router-level `require_admin_dep` dependency so this can't be forgotten.

## 3. Double-Blind Marketplace

PII never enters the marketplace during bidding:

- `marketplace.request` holds only an opaque `consumer_id` (UUID) and
  `consumer_ref` — **no name, email, phone, or national ID**. Consumer PII lives
  in the separate consumer-app database.
- The `/v1/marketplace/requests` listing exposes only loan attributes
  (product type, amount, term, purpose, Ficium risk tier/score) — never the
  consumer identifier.
- PII is revealed to the winning institution **only on bid acceptance**, via the
  dedicated reveal flow (`004_accept_bid_reveal.sql`).

## 4. Rate Limiting

`core/ratelimit.py` (slowapi). Bucketed per **institution_id** (from the JWT) or
API key, falling back to client IP. 600/min global default; 60/min on
`POST /v1/marketplace/bids`. Per-tenant bucketing means one tenant can't exhaust
another's budget or bypass limits by rotating IPs.

> In-memory storage → per-process counters. If portal-api scales past one
> instance, set `storage_uri="redis://…"` on the `Limiter`.

## 5. Outbound Request Safety (SSRF)

Institution-registered webhook URLs are validated by `core/ssrf.py`:

- HTTPS required; blocked hostnames (`localhost`, `*metadata*`); literal
  private/loopback/link-local/reserved IPs rejected; hostnames that **resolve**
  to any non-public address rejected (all A/AAAA records checked).
- Enforced at registration (create + update) **and** re-checked at dispatch time
  (DNS-rebind defence).

## 6. Injection Safety

All SQL uses parametrized values. Dynamic `WHERE`/`SET` clauses are built only
from **fixed column allowlists** — user input never reaches the SQL string
itself. Do not interpolate user-supplied identifiers into `text()`.

## 7. Secrets

No secrets in the repo — all read from environment. `.env.example` documents
required vars. Service-role keys and signing material live in Railway/Supabase
config, never committed.

---

## Reporting

Security issues → the founder directly, not a public issue. Do not open a public
GitHub issue for a suspected vulnerability.
