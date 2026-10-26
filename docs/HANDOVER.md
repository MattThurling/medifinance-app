# Medifinance CRM — Handover

A custom CRM (v1) replacing HubSpot for an asset-finance broker. Staff manage
organisations, contacts and deals; customers complete an application through a
magic-link portal.

## Stack

| Area | Choice |
|---|---|
| Framework | Django 5.2, custom email-login User |
| DB | SQLite (local) · Postgres / Cloud SQL (prod), via `DATABASE_URL` |
| UI | server-rendered templates, Tailwind v4 + DaisyUI v5, Lucide icons |
| Interactivity | HTMX (vendored, no jQuery) — searchable selects |
| Auth perms | role field + django-guardian (object perms on deals) |
| Email | Prod: Mailgun HTTP API via django-anymail (`send.medifinance.co.uk`); dev: Mailtrap sandbox over SMTP; console backend if no credentials set |
| Files | django-storages → GCS in prod, local `media/` in dev |
| Serving | gunicorn + WhiteNoise (static), Cloud Run |

## Run locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
npm install
cp .env.example .env            # SECRET_KEY etc. (email optional — defaults to console)
python manage.py migrate
python manage.py createsuperuser
# two terminals:
python manage.py runserver
npm run watch:css
```

Dev logins (password `medifinance123`): `admin@…`, `associate@…`, `customer@medifinance.test`.

## Data model (`accounts/`, `crm/`)

| Model | Notes |
|---|---|
| **User** | email login (no username); `role` = admin / associate / customer; optional `organisation` (groups associates into a team that shares deals, contacts and organisations — see `crm/access.py`); `hubspot_id`. Imported & customer users have unusable passwords (use reset / magic link). |
| **MagicLink** | single-use, 7-day login link for customers. `issue()` / `consume()`. Consumed at `/m/<token>/`. |
| **Organisation** | `name`, structured UK address (`address_line1/2/city/county/postcode`), optional `owner` (User; associates can't see records owned by staff outside their team — unowned ones are visible), `hubspot_id`. |
| **Contact** | name/email/phone, `date_of_birth`, structured `home_address_*`, `organisations` M2M, optional `owner` (User; same visibility rule as Organisation), optional `user` (OneToOne), `hubspot_id`. |
| **Deal** | `owner` (User, PROTECT), `customer` (Contact, PROTECT), optional `introducer` (Contact) + `equipment_supplier` (Organisation), financials (`funded_amount`, `earnings`, `flat_fee`, `commission`, `document_fee` — all nullable), `selected_quote`, `co_applicants` (M2M Contact), `hubspot_id`. Org is reached via `customer.organisation` (no direct FK — deliberate). |
| **Quote** | belongs to a Deal; `apr`, `term`, `monthly_payment` (auto-calculated in `save()` from deal funded amount + apr + term). |
| **Stage** | append-only event log per deal; `name` = application / info_received; latest = current (`deal.current_stage`). "Application" auto-created on deal creation. |
| **Document** | per-deal request: `name`, `required`, `status` (requested/provided), `file`. `attach(file, by=…)` flips to provided. |

All four core records + User carry a unique, nullable `hubspot_id`. Each model deep-links to its HubSpot record (`hubspot_url`, portal id in `HUBSPOT_PORTAL_ID`).

## Roles & permissions (`accounts/permissions.py`)

- **admin / associate** = staff (internal UI). **customer** = portal only.
- Gating: `StaffRequiredMixin`, `AdminRequiredMixin`, `CustomerRequiredMixin`, `@role_required`.
- guardian: on Deal save a signal grants the owner `view_deal` + `change_deal`; ownership change re-assigns. `ANONYMOUS_USER_NAME=None`.
- **Two-factor auth** (`accounts/two_factor.py`, TOTP via `pyotp`): `TWO_FACTOR_REQUIRED_ROLES` (env, comma-separated roles or `all`) picks who *must* enrol; anyone else may opt in from the sidebar user menu → Security. `TwoFactorMiddleware` (process_view) redirects authenticated users who owe a code to `/accounts/2fa/setup/` or `/accounts/2fa/verify/`; verification is per login session (`two_factor_verified_user` = user pk). Secrets live on `User.totp_*`; codes are single-use (`totp_last_used_step`); 5 wrong codes = logout. Lost device: Django admin → Users → action "Reset two-factor authentication". Required roles can't turn it off themselves.

## Features

- **Staff CRUD** for Organisation / Contact / Deal (list + search + pagination, detail, create/edit/delete).
- **Deal overview** (`deal_detail.html`): header + meta, amount card, xs tables for Quotes / Applicants / Documents with icon actions, vertical **timeline** of stages (both sides, secondary colour, `+` to add). Responsive: amount above quotes, timeline below docs on small screens.
- **Quotes / Stages / Documents** managed inline from the deal (each via `?deal=<pk>` create views).
- **Terms (commercial deals)**: every deal type except Asset Finance (`Deal.is_commercial_finance`) shows ONE "Terms" card in place of Quotes + Proposals. A Terms row is a `Quote` carrying the lender, a `status` (Submitted / Approved / Declined / Withdrawn, shared `LenderStatus` choices), `lender_reference`, `notified_at`, and — once the bank comes back — `interest_rate_percent`, `term` (nullable until then) and `commitment_months` ("rate fixed for"). `TermsForm` requires rate + term only when Approved. Status changes emit the same `proposal_*` stage events as Proposals, via `crm/lender_status.py`, which also holds the shared client "Notify" flow (`quote_notify` / `proposal_notify`). `Deal.accepted_lender_terms` is what the commission-invoice email and Xero prefill read (selected Terms row on commercial deals, selected Proposal otherwise); `Deal.customer_quotes` is what the portal shows (Approved rows only on commercial deals). Migration 0047 folded the migrated HubSpot proposals on commercial deals into Terms rows. Asset Finance deals are unchanged.
- **Searchable selects** (HTMX): company/contact FK fields query `/search/{contacts,organisations}/` (≤20 results) instead of dumping every row into a `<select>`. Reusable `_combobox.html`.
- **Customer portal** (`/my/`, same app shell as staff with a one-item sidebar): the magic link lands on the deal page `/my/deals/<pk>/`, which is the customer's home — a derived "What we still need from you" summary (`crm/portal_checklist.py`: blank key details, quote to choose, requested documents, outstanding signatures), inline-editable (HTMX) cards for each applicant and the company, then quotes, documents, signatures and repayments. `/my/deals/` lists all deals with an "N actions needed" badge. `/` routes a customer to their deal (one deal) or the list.
- **Email** (prod: Mailgun API from `send.medifinance.co.uk`, From `info@medifinance.co.uk`; dev: Mailtrap sandbox over SMTP): "Email link to customer" + password reset. Console backend until `MAILGUN_API_KEY` or `EMAIL_HOST_PASSWORD` is set.

## HubSpot migration

- **Contract**: `docs/HUBSPOT_IMPORT.md` — four CSVs (`users/organisations/contacts/deals`) keyed by `hubspot_id`.
- **Queries**: `docs/HUBSPOT_EXPORT_QUERIES.md` — SQLite SQL to produce those CSVs from the staging tables (owners are text names → synthesised user ids; pick primary company per contact / primary contact per deal; orphan refs filtered out).
- **Importer**: `python manage.py import_hubspot [--dir import/] [--dry-run]` — idempotent upsert by `hubspot_id`, one transaction, warns-and-skips missing FK targets. `import/` is git-ignored (customer PII).

## Deployment — `docs/DEPLOYMENT.md`

Google Cloud Run, **one project / two services** (`medifinance-dev`, `medifinance-prod`), **one Cloud SQL Postgres** instance + two DBs, Artifact Registry, Secret Manager. GitHub Actions deploys via **Workload Identity Federation** (keyless): `develop`→dev, `main`→prod. Migrations run as a Cloud Run Job before each deploy. Multi-stage `Dockerfile` (Node builds CSS, Python runtime). The runbook has the exact `gcloud` commands.

> Not yet pushed to GitHub / no GCP project created — that's step 1–8 of the runbook.

## Repo layout

```
accounts/   User, MagicLink, permissions, emails, templatetags/icons
crm/        models, views, forms, urls, admin, signals,
            management/commands/import_hubspot.py, templates/crm/, templatetags/crm_extras
medifinance/ settings, urls, wsgi
templates/  base, _app_layout (sidebar; staff + customer), _portal_layout (plain; supplier invoice pages only), dashboard (staff), email/, portal/
static/     src/input.css → dist/output.css (Tailwind), vendor/htmx, vendor/chart.umd.min (Chart.js), js/combobox, js/charts
docs/       DEPLOYMENT, HUBSPOT_IMPORT, HUBSPOT_EXPORT_QUERIES, HANDOVER
```

## Conventions & gotchas

- **CSS**: edit templates → `npm run build:css` (or run `watch:css`). DaisyUI theme `corporate`.
- **Icons**: `{% icon "name" %}` reads SVGs from `node_modules/lucide-static` at render time — that dir is copied into the prod image (see Dockerfile).
- **Templates**: multi-line comments need `{% comment %}` (not `{# … #}`); `{% extends %}` must be the first tag.
- **collectstatic** ignores `static/src` (Tailwind source) — only built `dist/` is served.

## Deferred / decisions for v2

- Document **gating & progress** were built then **removed** for a simpler v1 (the `required` flag remains as metadata). Stage advancement is **not** gated.
- Deal overview's amount card shows a **hardcoded** "Asset Finance / £2,500.00 commission" placeholder — wire to `deal.commission` when ready.
- `owner` select isn't searchable (small staff set); applicants "remove" + staff "add applicant" actions are placeholders (no unlink/add-co-applicant view yet).
- Prod email goes through the Mailgun HTTP API (django-anymail) from the verified `send.medifinance.co.uk` domain, From `Medifinance <info@medifinance.co.uk>` — the `mailgun-api-key` secret in Secret Manager plus the `EMAIL_ENABLED=true` repo variable. Dev sends to the Mailtrap sandbox over SMTP; with no credentials at all the console backend logs the email instead.
- Search uses `icontains`; add a `pg_trgm` GIN index when row counts get large (deferred).
- Customer sessions use Django's default ~2-week length after magic-link consumption.
