# Medifinance CRM

Django CRM, ported from HubSpot. SQLite for dev, Tailwind v4 + DaisyUI v5 for UI.

## Stack
- Django 5.2 (custom email-based User model)
- django-guardian for object-level permissions
- Tailwind v4 + DaisyUI v5 (built via `@tailwindcss/cli`)
- SQLite

## Models
- `accounts.User` — email login, role of `admin` / `associate` / `customer`; optional `organisation` (a `crm.Organisation`) that groups associates into a team
- `crm.Organisation`
- `crm.Contact` — must belong to an Organisation
- `crm.Deal` — has an `owner` (User) and a `customer` (Contact). Organisation is reached via `deal.customer.organisation`.

All four models (and User) carry a nullable, unique `hubspot_id` for the HubSpot port.

## First-time setup

```bash
# 1. Python deps
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Node deps (Tailwind + DaisyUI)
npm install

# 3. Env
cp .env.example .env  # then edit DJANGO_SECRET_KEY

# 4. DB
python manage.py migrate

# 5. First admin
python manage.py createsuperuser
```

## Running

Two processes — Django and the Tailwind watcher.

```bash
# terminal 1
source .venv/bin/activate
python manage.py runserver

# terminal 2
npm run watch:css
```

Then visit:
- <http://127.0.0.1:8000/> — dashboard (requires login)
- <http://127.0.0.1:8000/accounts/login/> — sign in
- <http://127.0.0.1:8000/admin/> — Django admin

## One-shot CSS build (for prod or CI)

```bash
npm run build:css
```

## E-signing locally (optional)

Documents are sent for e-signature via a self-hosted [DocuSeal](https://www.docuseal.com/)
instance. Without one configured the feature simply stays hidden. To work on it:

```bash
docker run -d --name docuseal -p 3000:3000 -v ~/.docuseal:/data docuseal/docuseal
```

1. Open <http://localhost:3000>, create the admin account and build a test
   template (field names matching `crm/docuseal.py::build_prefill_values`).
2. In `.env`: `DOCUSEAL_URL=http://localhost:3000`, `DOCUSEAL_API_TOKEN=` (from
   Settings → API) and `DOCUSEAL_WEBHOOK_SECRET=dev-secret`.
3. In the DocuSeal console, add a webhook to
   `http://host.docker.internal:8000/webhooks/docuseal/` with a secret header
   `X-Docuseal-Secret: dev-secret` (Docker Desktop reaches the host's
   `runserver` via `host.docker.internal`).
4. No SMTP needed: after sending a document for signature from a deal page,
   open the submission in the DocuSeal UI and copy the signing link.

## Roles

| Role      | What it gets                                              |
|-----------|-----------------------------------------------------------|
| Admin     | Full Django admin, all CRM data                           |
| Associate | Internal CRM UI; own deals (plus colleagues' when linked to the same Organisation). Contacts and organisations: everything except records owned by staff outside their team — unowned ones are visible, as is anything attached to a deal they can see |
| Customer  | The `/my/` portal for deals they are on: the primary applicant edits people and company, picks the quote and adds co-applicants (who are emailed their own portal link at once); a co-applicant edits only themselves, uploads and signs their own requests |

Use the helpers in `accounts/permissions.py`:

```python
from accounts.permissions import staff_required, AdminRequiredMixin

@staff_required
def some_internal_view(request): ...

class SomeAdminView(AdminRequiredMixin, TemplateView): ...
```

Row visibility is decided in one place, `crm/access.py` (`visible_deals`, `visible_contacts`, `visible_organisations`): admins see everything; associates see deals owned by anyone in their `organisation` (or only their own when they have none); customers see deals they are on, as the primary applicant (`Deal.customer`) or as a co-applicant with a portal login (`portal_deals`; `deal_role` and `may_sign` decide what each may do in the portal). Contacts and organisations are softer for associates: they see records owned by their team or by nobody, plus anything attached to a visible deal (customer, co-applicants, organisation, introducer, suppliers, lenders); only records owned by staff outside their team are hidden. The organisation combobox search stays open to all staff so any lender/supplier/introducer can be picked. django-guardian row perms are also assigned on `Deal` save (see `crm/signals.py`) but nothing reads them yet.

## Partner API

Suppliers and other introducers create finance applications with
`POST /api/deals/` (bearer-key auth, see `accounts.models.ApiKey`) and can
poll `GET /api/applications/<reference>/status/`. The contract — including the
optional `channel` / `company_type` / `product_id` fields for guest and
AI-agent submissions, `Idempotency-Key` replay, the structured `errors`
format, rate limits and sandbox keys — is documented for partners in
[`docs/partner-api.md`](docs/partner-api.md). The quote endpoint is in
[`docs/QUOTE_API.md`](docs/QUOTE_API.md). Every submission is logged to
`crm.PartnerApiRequest` (read-only in admin).

## Browser extension API

The **`chrome-extension`** repo (a separate folder/repo) fills partner/bank
application forms from a deal. It talks to two read-only, staff-only JSON
endpoints served here:

- `GET /api/deals/` — recent deals + the signed-in user
- `GET /api/deals/<id>/` — the flat fill payload for one deal

Auth is the staff member's existing session cookie (the extension is granted
host access to the CRM). See `crm/api.py`.

> **Contract:** the JSON shape returned by `_deal_fill_payload()` is consumed by
> the extension's `partners.js` field maps. The two repos are independent but
> coupled by this payload — if you change its shape, update the extension's
> field maps in lockstep.
