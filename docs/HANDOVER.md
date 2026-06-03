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
| Email | Django SMTP backend → `mail.medi-finance.co.uk:465` (SSL); console backend if no password set |
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
| **User** | email login (no username); `role` = admin / associate / customer; `hubspot_id`. Imported & customer users have unusable passwords (use reset / magic link). |
| **MagicLink** | single-use, 7-day login link for customers. `issue()` / `consume()`. Consumed at `/m/<token>/`. |
| **Organisation** | `name`, structured UK address (`address_line1/2/city/county/postcode`), `hubspot_id`. |
| **Contact** | name/email/phone, `date_of_birth`, structured `home_address_*`, **required** `organisation` FK (PROTECT), optional `user` (OneToOne), `hubspot_id`. |
| **Deal** | `owner` (User, PROTECT), `customer` (Contact, PROTECT), optional `introducer` (Contact) + `equipment_supplier` (Organisation), financials (`funded_amount`, `earnings`, `flat_fee`, `commission`, `document_fee` — all nullable), `selected_quote`, `co_applicants` (M2M Contact), `hubspot_id`. Org is reached via `customer.organisation` (no direct FK — deliberate). |
| **Quote** | belongs to a Deal; `apr`, `term`, `monthly_payment` (auto-calculated in `save()` from deal funded amount + apr + term). |
| **Stage** | append-only event log per deal; `name` = application / info_received; latest = current (`deal.current_stage`). "Application" auto-created on deal creation. |
| **Document** | per-deal request: `name`, `required`, `status` (requested/provided), `file`. `attach(file, by=…)` flips to provided. |

All four core records + User carry a unique, nullable `hubspot_id`. Each model deep-links to its HubSpot record (`hubspot_url`, portal id in `HUBSPOT_PORTAL_ID`).

## Roles & permissions (`accounts/permissions.py`)

- **admin / associate** = staff (internal UI). **customer** = portal only.
- Gating: `StaffRequiredMixin`, `AdminRequiredMixin`, `CustomerRequiredMixin`, `@role_required`.
- guardian: on Deal save a signal grants the owner `view_deal` + `change_deal`; ownership change re-assigns. `ANONYMOUS_USER_NAME=None`.

## Features

- **Staff CRUD** for Organisation / Contact / Deal (list + search + pagination, detail, create/edit/delete).
- **Deal overview** (`deal_detail.html`): header + meta, amount card, xs tables for Quotes / Applicants / Documents with icon actions, vertical **timeline** of stages (both sides, secondary colour, `+` to add). Responsive: amount above quotes, timeline below docs on small screens.
- **Quotes / Stages / Documents** managed inline from the deal (each via `?deal=<pk>` create views).
- **Searchable selects** (HTMX): company/contact FK fields query `/search/{contacts,organisations}/` (≤20 results) instead of dumping every row into a `<select>`. Reusable `_combobox.html`.
- **Customer portal** (magic link → 4-step wizard, plain white layout, no sidebar): Quotes → Company Details → Applicants → Documents → thank-you. Steps indicator (`_portal_steps.html`). Completing Applicants records an "Info Received" stage.
- **Email** (SMTP/SSL via `mail.medi-finance.co.uk:465` as `info@medi-finance.co.uk`): "Email link to customer" + password reset. Console backend until `EMAIL_HOST_PASSWORD` is set.

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
templates/  base, _app_layout (staff sidebar), _portal_layout (plain), dashboard, email/, portal/
static/     src/input.css → dist/output.css (Tailwind), vendor/htmx, js/combobox
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
- Prod email goes through `mail.medi-finance.co.uk:465` (SSL) as `info@medi-finance.co.uk` — set `EMAIL_HOST_PASSWORD` in Secret Manager. Dev uses the same server when the password is set, else the console backend.
- Search uses `icontains`; add a `pg_trgm` GIN index when row counts get large (deferred).
- Customer sessions use Django's default ~2-week length after magic-link consumption.
