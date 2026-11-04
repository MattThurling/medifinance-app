# Medifinance CRM — working notes for Claude Code

Django CRM for an FCA-regulated UK asset-finance broker. Staff manage
organisations, contacts and deals; customers complete applications through a
magic-link portal; partners (equipment suppliers) start applications through
the partner API. Replaces HubSpot.

## Read these first

| Doc | What it holds |
|---|---|
| `docs/HANDOVER.md` | Stack, data model, roles, features, conventions, deferred decisions |
| `docs/DECISIONS.md` | Dated log of environment state, infra gotchas and why things are the way they are. **Keep it current** — it is the source of truth that used to live in Claude's local memory |
| `docs/DEPLOYMENT.md` | Cloud Run / Cloud SQL / load balancer / DocuSeal / Mailgun runbook with exact `gcloud` commands |
| `docs/ACCESS.md` | Every external account and local secret a maintainer needs, and how to hand them over |
| `docs/partner-api.md`, `docs/QUOTE_API.md` | Public API contracts |
| `docs/HUBSPOT_IMPORT.md`, `hubspot-migration/README.md` | The HubSpot migration: importer contract and the out-of-app build tooling |

## Commands

```bash
source .venv/bin/activate
python manage.py runserver            # http://127.0.0.1:8000
npm run watch:css                     # Tailwind; or `npm run build:css` once
python manage.py test                 # Django test runner (accounts/tests, crm/tests)
python manage.py check
python manage.py makemigrations crm --name <descriptive_name>
```

Dev logins (password `medifinance123`): `admin@`, `associate@`, `customer@medifinance.test`.
Local email goes to Mailpit (`brew services start mailpit`, inbox at http://localhost:8025).

## Environments and branches

- `develop` → auto-deploys to **dev** (`app-dev.medifinance.co.uk`) via GitHub Actions. Dev holds the HubSpot-migrated demo data and is what the ACME demo site posts to.
- `main` → auto-deploys to **prod** (`app.medifinance.co.uk`). Prod is an **empty, real** environment awaiting go-live. Never run `import_hubspot` on prod. Back up before any destructive operation there.
- Release = fast-forward `main` to `develop`. Migrations run as a Cloud Run Job before each deploy.
- All Cloud Run services are load-balancer-only ingress; the `*.run.app` hosts return 404 by design.

## Conventions

- **Commit messages**: one imperative sentence describing the user-visible change, no type prefix (see `git log`).
- **Migrations** get a `--name`. Apply new migrations to `db-old.sqlite3` too if you rebuild the HubSpot preview DB (see `hubspot-migration/README.md`).
- **Customer data is PII.** `import/`, `media/`, `db*.sqlite3*` and the HubSpot exports are git-ignored. Never paste real customer names, emails, DOBs or financial details into commits, docs, logs or test fixtures. Use `crm/tests/factories.py`.
- **CSS**: edit templates, then rebuild with Tailwind. DaisyUI theme `corporate`; theme colours are `oklch()` (Chart.js can't parse them — `static/js/charts.js` converts).
- **Templates**: `{% extends %}` first; multi-line comments use `{% comment %}`; icons via `{% icon "name" %}` (lucide-static).
- **Feature flags by env var**: DocuSeal, Xero, Mailgun and forced 2FA are all dark until their env vars are set. Don't assume a feature is on in a given environment — check `docs/DECISIONS.md`.
- **Money metric is `Deal.commission`**, not `Deal.earnings` (never populated in real data).

## When you learn something non-obvious

Write it into `docs/DECISIONS.md` (dated) or the relevant doc in the same
change, not only into Claude's local memory. Another developer must be able to
clone this repo and have everything.
