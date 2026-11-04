# HubSpot → CRM migration tooling

Out-of-app scripts that turn the raw HubSpot CSV exports into a ready-made
SQLite database for the **dev** environment. Prod is never loaded from this —
it starts empty (see `docs/DECISIONS.md`).

Until 2026-11-04 this folder lived outside git as
`../new hubspot migration/`. The scripts, docs and business-level decision
files are now tracked here. Files carrying customer personal data are
git-ignored (see `.gitignore` in this folder) and must be obtained from the
owner as an encrypted archive — see `docs/ACCESS.md`.

## What is tracked

- `build_hubspot_db.py` — loads the three exports into `hubspot.db` (`raw_*` tables).
- `build_*.py`, `automate_*.py`, `eyeball_org_variants.py`, `field_breakdown.py` — the analysis passes that produced the review CSVs (duplicates, org conflicts, name normalisation, role mailboxes, org merges, deal entity matching).
- `build_preview_db.py` — the main build: copies the app's `db-old.sqlite3` schema template and populates it from `hubspot.db` plus the `*_decisions.csv` files, writing `preview.sqlite3`. Prints counts and enforces the sensitive-fields policy.
- `field_map.md` — **authoritative** field-by-field map and Phase 1/2 decision status.
- `findings-so-far.md` — early data findings.
- `schemas.json` — JSON Schema of the Django models at the time.
- Business-level CSVs with no personal data: `stage_map.csv`, `stage_derivation_audit.csv`, `sensitive_fields.csv`, `sector_derivation_audit.csv`, `automated_deal_funder_matches.csv`, `first_conversion_counts*.csv`, `nameless_org_names.csv`, `recent_unworked_orgs.csv`.

## What you must obtain separately (git-ignored)

| File | Why it's excluded |
|---|---|
| `hubspot exports/` (`all-contacts.csv`, `all-companies.csv`, `my-deals.csv`, `old/`) | Raw exports, full customer PII including sensitive fields |
| `hubspot.db`, `hubspot.db.bak-*` | Same data in SQLite |
| `preview.sqlite3` | Build output with all migrated customer data |
| `automated_created_orgs.csv`, `automated_dropped_contacts.csv`, `contact_org_conflicts.csv`, `deal_only_edges.csv`, `duplicate_contacts*.csv`, `multi_org_contacts.csv`, `name_normalisation*.csv`, `role_mailbox_contacts*.csv` | Contact-level rows (names, emails, phones) |
| `automated_deal_introducer_matches.csv`, `source_inference_audit.csv` | Introducer and deal names include individuals |
| `automated_deal_supplier_matches.csv`, `org_merge_candidates*.csv` | A few free-text supplier names embed an individual's email |
| `user_mapping.csv`, `user_mapping_decisions.csv` | Staff accounts, including a former employee's personal email |
| `field_breakdown_raw_*.csv` | "Most common value" column leaks real emails |

`build_preview_db.py` reads `org_merge_candidates*.csv`, `user_mapping_decisions.csv`
and the contact-level files above, so it cannot run without the archive.

## Rebuilding dev's data

```bash
# 1. In the app: make sure db-old.sqlite3 has every migration applied
cd ..
DATABASE_URL=sqlite:///$PWD/db-old.sqlite3 .venv/bin/python manage.py migrate

# 2. Build
cd hubspot-migration
../.venv/bin/python build_hubspot_db.py      # only if the exports changed
../.venv/bin/python build_preview_db.py      # writes preview.sqlite3

# 3. Deploy locally
cp preview.sqlite3 ../db.sqlite3             # back up db.sqlite3 first
```

Then re-insert the owner's login row (user id 20; migrated users are 1–19)
from the pre-deploy backup. For dev on Cloud Run the equivalent is the
`import_hubspot` management command fed by `import/` CSVs — see
`docs/HUBSPOT_IMPORT.md` and `docs/HUBSPOT_EXPORT_QUERIES.md`.

## Rules

- Sensitive raw values (sort codes, income, CCJ flags, DOBs, addresses) must never appear in logs, sample output, commit messages or new CSVs.
- New CSVs are ignored by default; whitelist one in `.gitignore` only after checking it holds no personal data.
- Keep `field_map.md` updated when a decision changes.
