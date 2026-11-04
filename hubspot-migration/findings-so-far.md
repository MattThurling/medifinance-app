# HubSpot migration — findings so far

Brief handover for Claude Code. See `CLAUDE.md` and `decisions/` in the repo
for full context.

## The data

Three HubSpot CSVs loaded into SQLite (`hubspot.db`):

- **Contacts**: 1,899 rows, 350 columns
- **Companies**: 7,139 rows, 156 columns
- **Deals**: 2,417 rows, 177 columns

Business is a medical finance broker. Contacts are mostly end customers
(dental/medical practices), plus introducers and funder contacts. Companies
are a mix of practices, introducer firms, funders/lenders, and equipment
suppliers. Deals are finance applications.

Target schema: typed Organisations and Contacts who belong to Organisations.

## Key findings

**Contact-side org data is inconsistent.** Two company fields
(`Company Name`, `Associated Company`) disagree in **383 rows (20%)**.
792 have Company Name but no Associated Company link. 414 have no company
info at all. ~11% have no email; 33% have no company.

**Company table is 87% orphans.** 6,246 of 7,139 companies have no
contacts and no deals — likely old lead-list imports. Only 578 companies
are referenced by any deal, 613 by any contact.

**Deals often have no company or contact link.** Only ~60% of deals link
to a company; ~half link to a contact. Deal records themselves carry a lot
of the entity data (Client name, P1/P2 borrower names, free-text Funder /
Introducer / Supplier fields with commission % baked in).

**Real multi-role companies exist.** 21 companies simultaneously appear
as introducer + supplier + borrower. The target schema needs to accommodate
this rather than forcing a single type.

**Funder/Introducer/Supplier are mostly free-text, not links.**
Only 25 company records match the free-text `Funder` strings on deals.
Fuzzy matching will find many more (e.g. "BNP" vs "BNP Paribas Leasing").

**Sensitive data in Contacts.** DOB, sort codes, account numbers, income,
CCJ status. Needs deliberate handling for the new schema and must never
appear in logs, commit messages, or artefacts.

**Junk fields to note.** `Lead Status` is 99% empty (15 rows populated).
`Sector` literally has value "Sector" in every row. Many contact columns
are HubSpot marketing exhaust (session counts, Klout, IP geolocation).

## Stages completed

- **00** — raw CSVs → `raw_contacts` / `raw_companies` / `raw_deals`
- **01** — association tables built and symmetry-checked (all three
  directions agree, so exports are internally consistent)
- **02** — `stg_company_signals` produced with non-exclusive role flags
  per company

## Suggested next stages (pick one)

1. **Fuzzy-match Funder/Introducer/Supplier free-text to Company records.**
   Concrete, unlocks the question of whether funders should be Org records.
2. **Loop back to contacts** — resolve the 383 org-name disagreements,
   decide about 115 role-inbox emails (info@, enquiries@), check the 112
   same-name pairs for duplicates.
3. **Investigate the 288 "active but unrecognised" companies** — have
   contacts or deals but no role signal. Likely edge cases.

## Deliberate open questions

Not answered yet — the user is the domain expert on these:

- When Company Name and Associated Company disagree, which wins? (User
  wants to see cases before deciding.)
- Should the new schema enforce that every Contact has an Organisation?
  (Data doesn't support this cleanly; recommendation is no.)
- What to do with role-inbox emails (info@, enquiries@) — are they
  Contacts, or organisation-level contact methods?
- Multi-role companies: allow multi-type, or split into multiple records?
- Are the 6,246 orphan companies worth migrating at all?
