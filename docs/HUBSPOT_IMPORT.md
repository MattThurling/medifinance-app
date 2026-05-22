# HubSpot → Medifinance CRM Import

This is the import contract. Hand it to whoever (or whatever) is producing
the export from the HubSpot SQLite DB — they only need to match the column
specs below.

The Medifinance side has a single command that ingests these files:

```bash
python manage.py import_hubspot --dry-run   # validate, no writes
python manage.py import_hubspot             # commit
```

## How it works

- **Four CSV files**, one per model: `users.csv`, `organisations.csv`,
  `contacts.csv`, `deals.csv`.
- Place them in an `import/` folder at the project root.
- **Foreign keys cross files via `hubspot_id`**, not row position or DB
  primary keys. A deal's `owner_hubspot_id` must match a row in `users.csv`
  by `hubspot_id`. Same idea everywhere.
- **Re-running is safe.** Each row is upserted by its `hubspot_id`, so
  correcting a CSV and re-running just updates the affected rows.
- **Import order is fixed**: users → organisations → contacts → deals.
  Each step depends on the previous one being present.

## Two HubSpot shape mismatches to resolve in the export

Both of these need a decision made on the export side, not at import time:

1. **Contact ↔ Company is M2M in HubSpot. We have one Organisation per
   Contact.** Pick the primary company per contact when emitting
   `contacts.csv`.
2. **A Deal can have multiple Contacts in HubSpot. We have one `customer`
   per Deal.** Pick the primary contact per deal when emitting `deals.csv`.

If a contact has no company (or a deal has no primary contact), exclude
that row — the importer will reject it.

## File specs

All files are UTF-8, comma-delimited, with a header row. Empty cells are
treated as "not provided" for optional columns.

### `users.csv`

The HubSpot owners who own deals (Medifinance staff). Customer-facing
HubSpot Contacts do **not** belong here — they're imported into
`contacts.csv` and don't get user accounts in v1.

| Column        | Required | Notes |
|---------------|----------|-------|
| `hubspot_id`  | yes      | HubSpot owner ID. Must be unique. |
| `email`       | yes      | Used as the login. Must be unique. |
| `first_name`  | no       | |
| `last_name`   | no       | |
| `role`        | no       | One of `admin`, `associate`, `customer`. Defaults to `associate`. Can be changed later via Django admin. |

Users are imported with **no usable password**. They sign in by using the
password-reset flow (or an admin sets one via Django admin).

### `organisations.csv`

HubSpot Companies.

| Column        | Required | Notes |
|---------------|----------|-------|
| `hubspot_id`  | yes      | HubSpot company ID. Must be unique. |
| `name`        | yes      | |

### `contacts.csv`

HubSpot Contacts. Every contact **must** reference an Organisation.

| Column                    | Required | Notes |
|---------------------------|----------|-------|
| `hubspot_id`              | yes      | HubSpot contact ID (vid). Must be unique. |
| `organisation_hubspot_id` | yes      | Must match a row in `organisations.csv` by `hubspot_id`. |
| `first_name`              | no       | |
| `last_name`               | no       | |
| `email`                   | no       | |
| `phone`                   | no       | |

### `deals.csv`

HubSpot Deals. Each deal must have an owner (a user) and a customer (a
contact).

| Column                | Required | Notes |
|-----------------------|----------|-------|
| `hubspot_id`          | yes      | HubSpot deal ID. Must be unique. |
| `name`                | yes      | |
| `owner_hubspot_id`    | yes      | Must match a row in `users.csv` by `hubspot_id`. |
| `customer_hubspot_id` | yes      | Must match a row in `contacts.csv` by `hubspot_id`. |

## Example

```csv
# users.csv
hubspot_id,email,first_name,last_name,role
12345,jo@medifinance.co.uk,Jo,Smith,admin
12346,alex@medifinance.co.uk,Alex,Patel,associate
```

```csv
# organisations.csv
hubspot_id,name
9001,Acme Health Ltd
9002,Bramble Practice
```

```csv
# contacts.csv
hubspot_id,organisation_hubspot_id,first_name,last_name,email,phone
501,9001,Pat,Customer,pat@acme.test,07700900001
502,9002,Sam,Patient,sam@bramble.test,
```

```csv
# deals.csv
hubspot_id,name,owner_hubspot_id,customer_hubspot_id
800,Acme renewal,12345,501
801,Bramble onboarding,12346,502
```

## Producing the CSVs from your HubSpot SQLite DB

If the SQLite DB has the typical HubSpot table shape, something like this
gets you `organisations.csv` (adjust column names to match your schema):

```sql
.headers on
.mode csv
.output organisations.csv
SELECT
  hs_object_id AS hubspot_id,
  name
FROM companies
WHERE name IS NOT NULL AND name != '';
.output stdout
```

Repeat per table. Send me the output of `.schema` from your SQLite DB
and I'll write the exact queries.
