# Ficium — Platform Documentation

_Set regenerated 1 August 2026 from live code and both live Supabase catalogs._

These five documents describe the platform as a whole and are kept identical
across `ficium`, `ficium-portal` and `ficium-portal-api`. Repo-specific setup
stays in each repository's own `README.md` and `INSTALLATION.md`.

| Document | Read it when |
|---|---|
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | You need the system map, the two-database split, the sync design, auth, or deployment topology |
| [`FUNCTIONAL_SPEC.md`](FUNCTIONAL_SPEC.md) | You need to know what the product does — actors, journeys, modules, business rules |
| [`DATA_DICTIONARY.md`](DATA_DICTIONARY.md) | You need a column, a type, an enum value, or the schema debt list. 131 tables, 1,524 columns |
| [`API_REFERENCE.md`](API_REFERENCE.md) | You need an endpoint. 153 portal API routes plus 12 borrower serverless functions |
| [`SECURITY_MODEL.md`](SECURITY_MODEL.md) | You are answering a due-diligence questionnaire, briefing a penetration tester, or adding a control |

## Regenerating

`DATA_DICTIONARY.md` and `API_REFERENCE.md` are generated, not written.

```bash
python3 tools/build_dd.py    # rebuilds the data dictionary from the schema dumps
python3 tools/build_api.py   # rescans route decorators in ficium-portal-api
```

`build_api.py` reads the repositories directly, so it is accurate the moment it
runs. `build_dd.py` reads `.psv` schema dumps captured from the live catalogs —
refresh those first if the schema has changed.

`ARCHITECTURE.md`, `FUNCTIONAL_SPEC.md` and `SECURITY_MODEL.md` are prose and are
edited by hand. Each carries a "last verified" date at the top; update it when
you confirm the content still matches reality, not just when you change a word.

## What changed in this pass

The previous documentation set was last accurate in **late June 2026** and had
drifted materially. Corrections worth calling out:

- The App DB was documented as three schemas (`public`, `institution`, `admin`).
  It is four (`public`, `finance`, `fico`, `admin`), and `institution` is not one
  of them — that schema lives in the Portal DB.
- Table count was documented as approximate. It is 47 in the App DB and 84 in the
  Portal DB.
- The Finance module, FICO advisor, couple finance, KYC NIC scanning, per-lender
  structured chat, document templates, e-signature, approval chains and auto-bid
  were all undocumented.
- The marketplace sync was documented as a cron pull. It is a trigger kick plus a
  composite keyset cursor, and the reason the cursor is composite is now written
  down.
- `product_type` was documented with 8 values. It has 17.

## Known documentation gaps

Not written yet, listed so they are choices rather than omissions:

- **Runbook** — what to do when the sync stalls, when Railway cold-starts spike,
  when a webhook endpoint starts failing.
- **Disaster recovery** — RPO/RTO targets, restore procedure, and the
  three-environment (UAT/Production/DR) plan that has been discussed but not
  documented.
- **Data retention and erasure** — vault documents carry `retain_until`, but
  there is no stated policy and no erasure procedure for a borrower who asks to
  be forgotten. This will be asked for during regulatory review.
- **Integration guide for institutions** — `docs/API-INTEGRATION-GUIDE.md` in
  `ficium-portal-api` predates the `/v1/` API and webhook signing.
