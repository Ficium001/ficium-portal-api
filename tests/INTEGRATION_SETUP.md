# Integration Test Setup

The test suite in `tests/test_integration.py` uses `SET ROLE` inside a
transaction to impersonate authenticated users, which requires a **direct /
session-pooler connection** (port 5432), not the transaction pooler (6543).
This means it needs the actual Supabase database password.

## 1 — Get the passwords

In Supabase Dashboard, for each project:
> **Project Settings → Database → Connection String → Session pooler**
> (or URI tab — copy the URI that includes `aws-0-*.pooler.supabase.com:5432`)

Or just the password under **Project Settings → Database → Database password**.

## 2 — Add GitHub Actions secrets

Go to **GitHub → ficium-portal-api → Settings → Secrets and variables → Actions → New repository secret**.

Add these two secrets:

| Secret name | Value |
|---|---|
| `PORTAL_DB_DSN` | `postgresql://postgres.egwobcajdlragubtkpqp:<PASSWORD>@aws-0-ap-southeast-1.pooler.supabase.com:5432/postgres` |
| `APP_DB_DSN` | `postgresql://postgres.wixfhjlsjkiwfvqewvmt:<PASSWORD>@aws-0-ap-south-1.pooler.supabase.com:5432/postgres` |

Replace `<PASSWORD>` with the DB password from each project's dashboard.

## 3 — Run locally

```bash
export PORTAL_DB_DSN="postgresql://postgres.egwobcajdlragubtkpqp:<PW>@aws-0-ap-southeast-1.pooler.supabase.com:5432/postgres"
export APP_DB_DSN="postgresql://postgres.wixfhjlsjkiwfvqewvmt:<PW>@aws-0-ap-south-1.pooler.supabase.com:5432/postgres"
pip install pytest psycopg2-binary
pytest tests/test_integration.py -v
```

Or use the Makefile shortcut — only requires the passwords:

```bash
make test-integration PORTAL_PW=<portal-password> APP_PW=<app-password>
```

## 4 — CI schedule

The workflow runs automatically on:
- Every push / PR to `main`
- Daily at 06:00 UTC (regression sweep)
- Manual trigger via GitHub Actions UI

## What each test proves

| Test | Guards against |
|---|---|
| `test_connection_role_does_not_bypass_rls_in_authenticated` | RLS being silently disabled — the most critical regression |
| `test_member_context_resolves` | The keystone auth function breaking (would break all RLS policies) |
| `test_member_sees_only_own_tenant` | Cross-tenant data leakage (proven with two live institutions) |
| `test_catalog_readable_under_authenticated` | The silent deny-all bug that would empty every dropdown |
| `test_institution_can_submit_own_bid` | Bid submission being broken (was the case before migration 13) |
| `test_cross_tenant_bid_blocked` | An institution bidding as a different bank |
| `test_submit_then_approve_by_different_member` | The institution maker-checker being broken (was the case before migration 12c) |
| `test_maker_cannot_approve_own_action` | Four-eyes control regression |
| `test_product_resolver_covers_all_app_types` | Catalog sync failing silently for unknown product types |
| `test_ingest_is_idempotent` | Duplicate marketplace requests from retried syncs |
| `test_sync_requests_actually_ingests_via_real_endpoint` | The real `sync-requests` endpoint silently failing every row (invalid SQL, cascading transaction abort) while still returning HTTP 200 — the Jun 28 - Jul 17 outage |
| `test_client_sees_only_own_rows` | Cross-client PII leakage |
| `test_kyc_settings_locked_to_admin` | The KYC config world-read/write vulnerability (was live before fix) |
| `test_kyc_settings_visible_to_admin` | Admin being locked out of their own config |
| `test_client_cannot_create_request_as_another` | Identity spoofing on request creation |
