# HubSpot SQLite → Import CSVs

SQL recipes to turn the HubSpot SQLite export into the four CSVs that
`docs/HUBSPOT_IMPORT.md` specifies.

These queries assume the staging layer you're cleaning into:

- `stg_companies_populated`
- `stg_contacts_populated`
- `stg_deals_populated`
- `assoc_contact_company_primary` — primary company per contact
- `assoc_deal_contact` — deal ↔ contact join (with `is_primary`)

## Owners are text names, so we synthesise IDs

The HubSpot export has no owners table. Owners appear as plain-text names
in `Deal_owner` / `Contact_owner` / `Company_owner`. We derive `users.csv`
from the distinct owner names found in the data, using a deterministic
synthetic ID:

```
hubspot_id = 'OWN-' || lower(replace(trim(name), ' ', '-'))
```

The exact same formula is used to produce `deals.owner_hubspot_id`, so
the FK resolves at import time.

**You will need to fill in real emails (and confirm roles) in `users.csv`
before importing** — it's emitted with `email` blank and `role` set to
`associate`.

## Step 0 — open a session

```bash
sqlite3 /path/to/hubspot.sqlite
```

Set up CSV output mode (do this once per session):

```sql
.headers on
.mode csv
```

## Step 1 — sanity-check what you'll lose

Run these before exporting so there are no surprises. Rows missing a
required FK get dropped on export (and would be rejected by the importer
anyway).

```sql
-- Contacts with no primary company → will be dropped (Organisation is required)
SELECT COUNT(*) AS contacts_without_primary_company
FROM stg_contacts_populated c
LEFT JOIN assoc_contact_company_primary a
  ON a.source_id = c.Record_ID AND a.is_primary = 1
WHERE a.source_id IS NULL;

-- Deals that will be dropped because none of their associated contacts will be in contacts.csv
-- (either the contact isn't in stg_contacts_populated, or it has no primary company)
WITH valid_contacts AS (
  SELECT c.Record_ID
  FROM stg_contacts_populated c
  JOIN assoc_contact_company_primary acp ON acp.source_id = c.Record_ID AND acp.is_primary = 1
),
deals_with_valid_contact AS (
  SELECT DISTINCT adc.source_id
  FROM assoc_deal_contact adc
  JOIN valid_contacts vc ON vc.Record_ID = adc.target_id
)
SELECT COUNT(*) AS deals_dropped_no_valid_contact
FROM stg_deals_populated d
LEFT JOIN deals_with_valid_contact dv ON dv.source_id = d.Record_ID
WHERE dv.source_id IS NULL
  AND d.Deal_Name IS NOT NULL AND trim(d.Deal_Name) != ''
  AND d.Deal_owner IS NOT NULL AND trim(d.Deal_owner) != '';

-- Deals with no owner name → will be dropped (no synthetic ID can be made)
SELECT COUNT(*) AS deals_without_owner
FROM stg_deals_populated
WHERE Deal_owner IS NULL OR trim(Deal_owner) = '';

-- Distinct owner names (the rows that will be in users.csv)
SELECT COUNT(DISTINCT trim(name)) AS distinct_owners FROM (
  SELECT Deal_owner    AS name FROM stg_deals_populated    WHERE Deal_owner    IS NOT NULL AND trim(Deal_owner)    != ''
  UNION
  SELECT Contact_owner AS name FROM stg_contacts_populated WHERE Contact_owner IS NOT NULL AND trim(Contact_owner) != ''
  UNION
  SELECT Company_owner AS name FROM stg_companies_populated WHERE Company_owner IS NOT NULL AND trim(Company_owner) != ''
);
```

If those counts look off, fix the staging layer before exporting.

## Step 2 — emit `users.csv`

One row per distinct owner name across deals/contacts/companies. Email is
blank; **fill it in before importing**. Role defaults to `associate`;
change to `admin` for whoever needs admin access.

```sql
.output users.csv
SELECT
  'OWN-' || lower(replace(trim(name), ' ', '-'))             AS hubspot_id,
  ''                                                          AS email,
  CASE WHEN instr(name, ' ') = 0 THEN trim(name)
       ELSE trim(substr(name, 1, instr(name, ' ') - 1)) END  AS first_name,
  CASE WHEN instr(name, ' ') = 0 THEN ''
       ELSE trim(substr(name, instr(name, ' ') + 1)) END     AS last_name,
  'associate'                                                 AS role
FROM (
  SELECT DISTINCT trim(Deal_owner)    AS name FROM stg_deals_populated    WHERE Deal_owner    IS NOT NULL AND trim(Deal_owner)    != ''
  UNION
  SELECT DISTINCT trim(Contact_owner)  AS name FROM stg_contacts_populated WHERE Contact_owner  IS NOT NULL AND trim(Contact_owner)  != ''
  UNION
  SELECT DISTINCT trim(Company_owner)  AS name FROM stg_companies_populated WHERE Company_owner  IS NOT NULL AND trim(Company_owner)  != ''
)
ORDER BY name;
.output stdout
```

## Step 3 — emit `organisations.csv`

```sql
.output organisations.csv
SELECT
  Record_ID    AS hubspot_id,
  Company_name AS name
FROM stg_companies_populated
WHERE Record_ID    IS NOT NULL AND trim(Record_ID)    != ''
  AND Company_name IS NOT NULL AND trim(Company_name) != ''
ORDER BY Company_name;
.output stdout
```

## Step 4 — emit `contacts.csv`

Joined to `assoc_contact_company_primary` — contacts with no primary
company are dropped (per the import contract).

```sql
.output contacts.csv
SELECT
  c.Record_ID                                              AS hubspot_id,
  a.target_id                                              AS organisation_hubspot_id,
  c.First_Name                                             AS first_name,
  c.Last_Name                                              AS last_name,
  c.Email                                                  AS email,
  COALESCE(NULLIF(trim(c.Phone_Number), ''),
           NULLIF(trim(c.Mobile_Phone_Number), ''))        AS phone
FROM stg_contacts_populated c
JOIN assoc_contact_company_primary a
  ON a.source_id = c.Record_ID AND a.is_primary = 1
WHERE c.Record_ID IS NOT NULL AND trim(c.Record_ID) != '';
.output stdout
```

## Step 5 — emit `deals.csv`

`owner_hubspot_id` uses the same synthetic-ID formula as `users.csv`.

Two things that make this query more involved than the others:

1. `assoc_deal_contact` is M2M and (as of the current cleaning phase) has no
   `is_primary = 1` rows yet — every row is `0`. We pick one contact per
   deal deterministically with `MIN(target_id)`. When the cleaning step
   adds primaries, swap the subquery for `WHERE a.is_primary = 1`.
2. `assoc_deal_contact` references some contact IDs that don't exist in
   `stg_contacts_populated` (or won't make it into `contacts.csv`). The
   subquery joins through `stg_contacts_populated` and
   `assoc_contact_company_primary` so only contacts that will actually be
   in `contacts.csv` are candidates. A deal whose every associated contact
   is invalid is dropped from `deals.csv` entirely (no orphan FKs).

```sql
.output deals.csv
SELECT
  d.Record_ID                                              AS hubspot_id,
  d.Deal_Name                                              AS name,
  'OWN-' || lower(replace(trim(d.Deal_owner), ' ', '-'))   AS owner_hubspot_id,
  a.target_id                                              AS customer_hubspot_id
FROM stg_deals_populated d
JOIN (
  SELECT adc.source_id, MIN(adc.target_id) AS target_id
  FROM assoc_deal_contact adc
  JOIN stg_contacts_populated c
    ON c.Record_ID = adc.target_id
  JOIN assoc_contact_company_primary acp
    ON acp.source_id = c.Record_ID AND acp.is_primary = 1
  WHERE c.Record_ID IS NOT NULL AND trim(c.Record_ID) != ''
  GROUP BY adc.source_id
) a ON a.source_id = d.Record_ID
WHERE d.Record_ID  IS NOT NULL AND trim(d.Record_ID)  != ''
  AND d.Deal_Name  IS NOT NULL AND trim(d.Deal_Name)  != ''
  AND d.Deal_owner IS NOT NULL AND trim(d.Deal_owner) != '';
.output stdout
```

## Step 6 — verify the CSVs

```bash
.quit
```

Back in your shell:

```bash
wc -l users.csv organisations.csv contacts.csv deals.csv
head users.csv
```

Move them into the Django app's `import/` directory:

```bash
mv users.csv organisations.csv contacts.csv deals.csv /Users/matt/Desktop/MEDIFINANCE/medifinance-app/import/
```

Then from `medifinance-app/`:

```bash
python manage.py import_hubspot --dry-run
# fix anything reported, then:
python manage.py import_hubspot
```

## Notes

- `users.csv` will have **empty `email` cells**. Fill them in before
  importing — the importer requires `email` per row and it must be unique.
- Re-running the importer is idempotent (keyed on `hubspot_id`), so you
  can re-export, re-edit emails, and re-run without duplicates.
- If the same person appears under multiple name spellings in the data
  (e.g. `Jo Smith` vs `Jo Smith `), the trim() handles whitespace but not
  case differences. Spot-check the `users.csv` for near-duplicates before
  importing.
