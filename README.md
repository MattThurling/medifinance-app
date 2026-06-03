# Medifinance CRM

Django CRM, ported from HubSpot. SQLite for dev, Tailwind v4 + DaisyUI v5 for UI.

## Stack
- Django 5.2 (custom email-based User model)
- django-guardian for object-level permissions
- Tailwind v4 + DaisyUI v5 (built via `@tailwindcss/cli`)
- SQLite

## Models
- `accounts.User` — email login, role of `admin` / `associate` / `customer`
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

## Roles

| Role      | What it gets                                              |
|-----------|-----------------------------------------------------------|
| Admin     | Full Django admin, all CRM data                           |
| Associate | Internal CRM UI                                           |
| Customer  | Login only (portal UI is intentionally not built in v1)   |

Use the helpers in `accounts/permissions.py`:

```python
from accounts.permissions import staff_required, AdminRequiredMixin

@staff_required
def some_internal_view(request): ...

class SomeAdminView(AdminRequiredMixin, TemplateView): ...
```

Per-row permissions are wired through django-guardian. On `Deal` save, the owner gets `view_deal` and `change_deal` on that row (see `crm/signals.py`).

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
