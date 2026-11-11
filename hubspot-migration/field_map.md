# HubSpot → Django CRM field map

Phase 1 output. For each Django model in `schemas.json`, this document lists
what HubSpot column feeds each field, the transformation required, and which
fields have no HubSpot source. Column names in quotes are exact HubSpot
export headings.

Rows counts are as-of the current `hubspot.db` build.

---

## Models in scope for migration

| Django model | Source table | Notes |
|---|---|---|
| `Organisation` | `companies_for_migration` (1,310) + funder/introducer/supplier synthesis | Multi-role via FKs, no `type` field |
| `Contact` | `contacts_for_migration` (1,271) | Plus a handful synthesised from deal P1/P2 fields |
| `Deal` | `deals_for_migration` (2,078) | Requires `customer` FK — see Phase 2G decision |
| `Participation` | Derived from `deals_for_migration` supplier fields | 1 per deal with a Supplier |
| `Proposal` | Derived from `deals_for_migration` funder fields | 1 per deal with a Funder |
| `Stage` | Derived from `deals_for_migration.Deal Stage` | 1 per deal representing current stage |
| `User` | Distinct `Deal owner` values (16) + admin seeds | Emails need client input |

## Models NOT migrated (empty on cutover)

- `Quote`, `RateBand` — lender-configured, seeded separately in the new system
- `Document` — no equivalent in HubSpot export
- `ParticipationInvoiceLink`, `MagicLink`, `ApiKey`, `XeroConnection`, `XeroInvoice` — runtime-generated
- `SiteSettings` — singleton, default row

---

## Organisation

| Django field | HubSpot source | Transform / notes |
|---|---|---|
| `name` | `raw_companies."Company name"` | Direct; empty on 22 rows — client decision needed for those |
| `legal_name` | *(none)* | Blank on migration; can be filled by later Companies House lookup |
| `trading_name` | *(none)* | Blank |
| `companies_house_number` | *(none)* | HubSpot has no CH column; `raw_deals."Company Reg No"` exists per-deal (7.7% coverage) — copy back to Org if the deal's org matches |
| `address_line1` | `"Street Address"` | Direct |
| `address_line2` | `"Street Address 2"` | Direct; 7.7% coverage |
| `address_city` | `"City"` | Direct |
| `address_county` | `"State/Region"` | Direct; note HubSpot calls it "State/Region", we call it county |
| `address_postcode` | `"Postal Code"` | Direct |
| `url` | `raw_companies."Website URL"` | Direct; 99.3% coverage. `"Company Domain Name"` and `"Additional Domains"` retained on raw only |
| `email` | *(derived, no direct column)* | Backfilled during migration: (a) contacts marked `org_contact_method` in Phase 2A promote their email to their linked Organisation; (b) if ≥2 kept contacts at the same org share the same email, that address becomes the org's email |
| `phone` | `raw_companies."Phone Number"` | Direct; 47% coverage |
| `hubspot_id` | `"Record ID"` | Direct |
| `created_at`, `updated_at` | `"Create Date"`, `"Last Modified Date"` | Preserved for history |

**Synthesised organisations** — funders, introducers, suppliers that appear only as free-text on deals need resolution (see Phase 2 D/E/F CSVs).

---

## Contact

| Django field | HubSpot source | Transform / notes |
|---|---|---|
| `title` | Derived from name-normalisation review | Populated from `stripped_title` in `name_normalisation.csv` where the client accepted the split |
| `first_name` | `raw_contacts."First Name"` | Direct; fall back to `"First name (2)"` if primary blank. Name-normalisation review may override |
| `last_name` | `raw_contacts."Last Name"` | Direct; fall back to `"Last Name (2)"`. Name-normalisation review may override |
| `email` | `raw_contacts."Email"` | Direct; `"Email (2)"` and `"Work email"` retained in a `notes` field if they differ |
| `phone` | `raw_contacts."Phone Number"` | Direct |
| `date_of_birth` | `raw_contacts."Date of Birth"` **SENSITIVE** | Only 3.1% populated; UK day-first formats normalised. Where `Date of Birth__2` disagrees (11 rows) the primary column wins and a migration note records the conflict (decided 2026-08-04). Falls back to `Date of Birth__2` when primary blank |
| `home_address_line1` | `raw_contacts."Home Address"` | Split single-field address on newline/comma into line1/line2 heuristically |
| `home_address_line2` | (derived from `"Home Address"` split) | |
| `home_address_city` | *(none in contacts)* | HubSpot doesn't store contact city separately — parse from address or leave blank |
| `home_address_county` | *(none)* | Blank |
| `home_address_postcode` | `raw_contacts."Home Postcode"` | Direct |
| `organisations` (M2M) | `contact_organisation_edges` table | 720 contacts with at least one org, 42 with multiple |
| `user` | *(none)* | Null; only set for staff contacts, not migrated |
| `hubspot_id` | `"Record ID"` | Direct |

**Synthesised contacts** — some deals have no linked contact but populate P1 fields (`P1 First name`, `P1 Last name`, `P1 Date of Birth`, `P1 Home Address`, `P1 Home Postcode`, `P1 Title`). See Phase 2G decision CSV.

**Sensitive fields NOT migrated** (decided 2026-08-04, Phase 2K):
- `Value of Home`, `Value of Home_new`, `Income`, `Employers Address`
- Any `NI Number`, `Sort Code`, `Account Number`, `CCJ` fields (if present)
- These stay in `raw_contacts` only — the client confirmed the drops (no
  encrypted-field schema extension). Full list with per-field decisions in
  `sensitive_fields.csv`.

**Duplicate contact columns present** — HubSpot exported `Email (2)`, `First name (2)`, `Last Name (2)`, `Home Address (2)`, `Home Postcode (2)`, `Mobile Number (2)`. Where the "(2)" differs, capture as a note on the contact for manual merge later.

---

## Deal

| Django field | HubSpot source | Transform / notes |
|---|---|---|
| `name` | `raw_deals."Deal Name"` | Direct |
| `type` | `raw_deals."Pipeline"` | Simplified: Asset Finance → `asset_finance`, MF Commercial Finance → `commercial_finance`, Medifinance Ltd Business Loans → `business_loans`, Structured Finance → `structured_finance`, KMC Associates → `kmc_associates`, Short term Finance → `short_term_finance` (enum extended in migration 0029). Patient Finance / Personal Loans pipelines are out of scope |
| `owner` | `raw_deals."Deal owner"` → `User.id` | 16 distinct names; resolved via user-mapping table (needs client input for emails) |
| `customer` | `raw_deals."Associated Contact IDs"` (first) → `Contact.id` | Nullable FK. 1,061 of 2,078 in-scope deals have a contact link; 1,017 have none (77% of those have no P1/email/phone at all). Schema was updated to allow null customer rather than force a placeholder |
| `organisation` | `raw_deals."Associated Company IDs (Primary)"` → `Organisation.id` | Nullable; fall back to `"Company"` free-text on deal if no association |
| `introducer` | `raw_deals."Introducer"` → fuzzy-match → `Organisation.id` | Phase 2E decision |
| `source` | `raw_deals."Source"` | 12-value `Deal.Source` enum (migration 0034), HubSpot pick-list labels mapped 1:1 via `SOURCE_CODES` (86% populated on in-scope deals). Blanks are inferred to `introducer` when the deal's Introducer resolved to a real external org — not MediFinance's own org (HubSpot 3212912855) and not junk like "N/A" — 24 deals, listed in `source_inference_audit.csv`; the remaining 276 stay NULL. Added 2026-09-01 |
| `earnings` | *(derived)* | Not a HubSpot field; leave blank on migration or compute from commission fields — client to confirm formula |
| `flat_fee` | *(none)* | Blank |
| `source_identifier` | *(none)* | Free-text detail about the source (e.g. a particular exhibition). Blank on migration. Added 2026-11-11 |
| ~~`commission`~~ | `raw_deals."Salesman Commission"` (£) | **Not migrated since 2026-11-11.** `Deal.commission` was removed: commission is `Quote.commission_percent`, and the deal's £ figure is calculated from the selected quote. **To do in the migration redo:** create a quote per deal and port this value to quote level (HubSpot also has `"Sales Comm (%)"`) |
| ~~`document_fee`~~ | `raw_deals."Doc Fee"` (£) | **Not migrated since 2026-11-11.** Moved to `Quote.document_fee`. **To do in the migration redo:** port onto the deal's quote |
| `mf_invoice_number` | `raw_deals."Invoice No"` | Direct; `MF####` refs on 1,057 deals. This is **Medifinance's own commission invoice**, not the supplier's. Presence drives the `mf_invoiced` stage |
| `first_payment_date` | *(none)* | Not tracked in HubSpot; blank on migration, staff to populate |
| `term_end_date` | `raw_deals."End date"`, else `"Close Date" + "Term"` | Projected end of the finance term — drives the Maturing deals view. Explicit End date (~27% of deals) wins; else Close Date + parsed Term months (same arithmetic as the settlement stage check); else NULL |

`deposit` / `balloon` / `repayment_profile` moved from Deal to Quote (migration
0022) and are no longer migration targets.
| `selected_quote` | *(none)* | Null; quotes only exist in the new system |
| `selected_proposal` | *(derived)* | Set to the migrated Proposal id if a single Funder exists |
| `co_applicants` (M2M) | Synthesised from P2 fields | If `P2 First Name`/`P2 Last name` populated: create Contact and link |
| `hubspot_id` | `"Record ID"` | Direct |
| `created_via_api_key` | *(none)* | Null |
| `created_at`, `updated_at` | `"Create Date"`, `"Last Modified Date"` | Preserved |

---

## Participation

One row per deal that has a Supplier or an amount. **Only Asset Finance deals
get a supplier-linked participation** — on every other pipeline suppliers don't
apply, so participations are supplier-less (orphan) by assumption and the
amount still counts toward `Deal.funded_amount`.

| Django field | HubSpot source | Transform / notes |
|---|---|---|
| `deal` | Parent deal | FK |
| `organisation` | Asset Finance only: `raw_deals."Supplier"` → fuzzy-match → `Organisation.id` | Phase 2F decision. NULL everywhere else, and on Asset Finance when there's no Supplier or no org matched. Explanatory `description` set when: an Asset Finance deal lacked a supplier, an Asset Finance supplier failed to match (raw text quoted), or a non-Asset-Finance deal listed a supplier (raw text kept for reference) |
| `amount` | `raw_deals."Funded Amount"` or `"Amount"` | HubSpot doesn't split by supplier; the whole deal amount goes to the single Supplier (or to the orphan participation when there is none). No participation at all if the deal has neither supplier nor amount |
| `description` | *(none)* | Blank; or extract from `"Notes"` |
| `invoice_number` | *(none)* | Blank — `"Invoice No"` turned out to be MF's own commission invoice (`MF####` refs), migrated to `Deal.mf_invoice_number` instead |
| `invoice_contact` | *(none)* | Null |
| `invoice` | *(none)* | Null; uploaded via ParticipationInvoiceLink later |

**Note:** HubSpot only supports one Supplier per deal. Real-world deals with multiple suppliers were previously worked around via `Supplier2`/`Supplier3` columns — both are 100% empty in the export, so no data lost.

---

## Proposal

One row per Funder per deal.

| Django field | HubSpot source | Transform / notes |
|---|---|---|
| `deal` | Parent deal | FK |
| `lender` | `raw_deals."Funder"` → fuzzy-match → `Organisation.id` | Phase 2D decision |
| `contact` | `raw_deals."Funder Contact"` → fuzzy-match to Contact | 5% coverage; nullable |
| `proposal_number` | `raw_deals."Funder Ref:"` | Direct |
| `status` | Derived from `"Deal Stage"` | See stage-mapping table below |

---

## Stage

Each deal gets a **reconstructed chain of Stage events** (`application` first,
current stage last), not a single flattened stage. The enum was redesigned
(2026-08-03) to: `application`, `info_received`, `proposal_submitted`,
`proposal_approved`, `proposal_declined`, `documents_out`,
`supplier_invoice_requested`, `supplier_invoice_received`, `mf_invoiced`,
`deal_live`, `dormant`, `lost`, `settled`.

Derivation is rule-based (see `derive_stage_events` in `build_preview_db.py`),
first match wins:

1. Dead label (Lost / Dead / NTU Dead / Other Provider) → `lost`
2. Paid Off → `settled`, dated when marked paid off
3. Live label (Deal live / Live / Closed won) → `settled` if Close Date + Term
   has elapsed, else `deal_live`
4. Any other label with an MF invoice number (stale label; the invoice is
   better evidence) → `settled` if term elapsed, else `mf_invoiced`
5. Active label with activity in the last 12 months → natural mapping
6. Everything else → `dormant` (catch-all for legacy unknowns)

The final event's note records the original HubSpot pipeline/stage (plus
Closed Lost Reason where present), so provenance is never lost. Event dates
come from Create Date / Close Date / Close + Term, clamped monotonic and
never in the future. Outcome audit: `stage_derivation_audit.csv`
(supersedes `stage_map.csv`).

---

## User

16 distinct `Deal owner` values in `raw_deals`. Each becomes a Django User.

| Django field | Source | Transform / notes |
|---|---|---|
| `email` | *(none — needs client-provided mapping)* | 16-row CSV: HubSpot owner name → email |
| `first_name`, `last_name` | Split from HubSpot name | Direct |
| `role` | *(default `associate`)* | Client to flag admins |
| `is_active` | *(default true)* | Client to flag leavers |
| `hubspot_id` | HubSpot owner ID (if surfaced) | Direct if present |

---

## HubSpot columns not migrated

Bucketed reasons, not exhaustive per column:

- **Empty in export** — 122 contact / 101 company / 89 deal columns are 0%
  populated. Silent drop.
- **Marketing telemetry** — Sessions, Pageviews, Form Submissions, Traffic
  Source, `Latest/First Referrer *`, IP geolocation, Klout. Not part of the
  new CRM's remit.
- **HubSpot internal mechanics** — `Merged * IDs`, `Record source detail *`,
  workflow enrolments, sequence enrolments, dozens of `Number of *` counters,
  every `Associated *` field for object types the new system doesn't have
  (Ticket, Contract, Cart, Marketing event, Subscription…).
- **Deprecated/junk single-value fields** — `Sector = "Sector"`, `Current
  Customer = "Yes"` on all rows including cold prospects, `Domain to which
  registration email was sent`, etc. 24 contact / 9 company / 6 deal
  single-value columns.
- **Free-text notes on activities** — `Notes`, `Associated Note`,
  `Associated Email`, `Associated Task` names. Consider capturing top-level
  `Notes` as a Deal-level `note` field if the schema is extended — client
  decision.

---

## Sensitive-data handling

Per `findings-so-far.md`, contacts carry DOB, sort codes, account numbers,
income, CCJ status. **DECIDED 2026-08-04** — see `sensitive_fields.csv`
(`final_action` column); enforced by `check_sensitive_policy()` in
`build_preview_db.py`, which refuses to build while any row is undecided.

- **`date_of_birth`** — migrated. UK day-first formats normalised by
  `norm_date` (`23/04/1965`, `23/04/65`, `23041965`, `9th Dec 1943`, …).
  Where the duplicate export column `Date of Birth__2` disagrees (11 rows),
  the primary column wins and a `hubspot_migration_comment` note on the
  contact records both values for later verification. Note:
  `Contact.date_of_birth` is a plain DateField — no field-level encryption;
  client accepted this.
- **`Home Address` / `Home Postcode`** — migrated into the existing
  `home_address_*` fields.
- **Sort code, account number, income, CCJ, employers address, value of
  home, Upload Accounts attachment refs, etc.** — dropped; they remain in
  `raw_contacts` only. These values must never appear in migration logs,
  sample outputs, or git commits.

---

## Open questions this document resolves

Cross-referencing `findings-so-far.md`:

| Open question | Resolved by |
|---|---|
| Should every Contact have an Organisation? | No — schema doesn't require it. 33% of contacts have no org and that's OK. |
| Multi-role companies: allow multi-type or split? | Multi-role — schema has no type field; role is expressed via FK direction. |
| Company Name vs Associated Company disagreement | Not a real 383-row conflict — the raw_contacts `Associated Company` column contains the literal string `"Associated Company"` on every row (100% junk). True conflicts are 280 disagreements + 320 typed-with-no-linkage — Phase 2C CSV |
| Role-inbox emails (info@, enquiries@) | Client decision — Phase 2A CSV |
| Fuzzy-match Funder/Introducer/Supplier | Client decision — Phase 2 D/E/F CSVs |
| Orphan companies worth migrating? | Answered: 5,829 dropped already; 2 recent WIP kept for Phase 2H review |

---

## What Phase 2 must produce (given the gaps above)

Only the CSVs that this field map actually depends on:

- **2A** `role_mailbox_contacts.csv` — decides `Contact.email` handling for info@/proposals@ entries
- **2B** `duplicate_contacts.csv` — resolves the `(2)` column disagreements and same-email/same-name pairs
- **2C** ~~`contact_org_conflicts.csv`~~ **AUTOMATED** — `automate_contact_orgs.py` applies deterministic rules: keep any contact with a deal or a HubSpot org edge, drop the rest (367 contacts); fuzzy-match typed Company Name against Organisations at threshold 0.85, create new Orgs for the 141 misses. Two audit CSVs (`automated_created_orgs.csv`, `automated_dropped_contacts.csv`) surface anything the client wants to double-check, but no per-row decision required.
- **2D/2E/2F** ~~`funder_matches.csv` / `introducer_matches.csv` / `supplier_matches.csv`~~ **AUTOMATED** — `automate_deal_orgs.py` fuzzy-matches Funder / Introducer / Supplier free-text against `raw_companies` (threshold 0.85), treats N/A-style values as junk, creates new Orgs for unmatched. Three audit CSVs (`automated_deal_{funder,introducer,supplier}_matches.csv`) for quick eyeball to catch spelling variants of the same org (e.g. "BNP" vs "BNP Paribas Leasing"). No per-row decisions required.
- **2G** ~~`deals_missing_customer.csv`~~ **RESOLVED BY SCHEMA CHANGE + AUTOMATED SYNTHESIS** — `Deal.customer` is now nullable. Of 1,017 missing-customer deals, 21 with P1 First + Last + email get a synthesised Contact via `automate_deal_customer_synthesis.py` (12 unique Contacts, dedupe by email). Remaining 996 migrate with `customer = null`.
- **2H** `recent_unworked_orgs.csv` — keep or drop the 2 recent zero-signal companies
- **Org merge review** ~~`org_merge_candidates.csv`~~ **DECIDED & APPLIED** — `org_merge_candidates_decisions.csv` reviewed in full (2026-08-03): 84 merges, 27 splits (mashed multi-org names fan out participations/proposals per real org, amounts divided equally with an explanatory note), 9 drops, 22 new canonical orgs for misspelling clusters, 4 orphan companies resurrected from `raw_companies` as canonical records. Applied by `apply_org_merge_decisions()` in `build_preview_db.py`
- **2I** ~~`user_mapping.csv`~~ **DECIDED & APPLIED** (2026-08-04) — `user_mapping_decisions.csv` (16 Deal owner names → email + role + active flag) now read by `load_users()` in `build_preview_db.py`; unmapped owners get a warning + placeholder. Four active users still have `@tobeconfirmed.email` addresses pending real ones from the client
- **2J** ~~`stage_map.csv`~~ **AUTOMATED** — rule-based stage derivation in `build_preview_db.py` (see Stage section); outcomes in `stage_derivation_audit.csv`, no per-row decisions required
- **Deal source** **DECIDED & APPLIED** (2026-09-01) — `Deal.source` added (migration 0034); populated from HubSpot `"Source"` with the introducer inference above; audit in `source_inference_audit.csv`
- **2K** ~~`sensitive_fields.csv`~~ **DECIDED & APPLIED** (2026-08-04) — all 13 no-schema-field rows dropped (incl. Upload Accounts attachment refs); DOB + Home Address migrated to existing fields. DOB vs `Date of Birth__2` conflicts: primary column wins, migration note on the contact records both. Enforced by `check_sensitive_policy()` in `build_preview_db.py`

That's the complete set. Nothing else is blocked on client input — **all Phase 2
client decisions are now resolved and applied.**
