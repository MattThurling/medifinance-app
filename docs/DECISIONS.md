# Decisions, environment state and gotchas

A dated log of things that are not derivable from the code: why a choice was
made, what state each environment is in, and operational traps. Newest
information wins within a section. Add to it in the same commit as the change
it describes.

Consolidated 2026-11-04 from notes that previously lived only on the lead
developer's machine.

---

## Environments (as of 2026-11-04)

| | dev | prod |
|---|---|---|
| URL | `app-dev.medifinance.co.uk` | `app.medifinance.co.uk` |
| Deploys from | `develop` | `main` |
| Cloud Run service | `medifinance-dev` | `medifinance-prod` |
| Database | Cloud SQL `medifinance_dev` | Cloud SQL `medifinance_prod` |
| Documents bucket | `medifinance-medifinance-docs` (`GS_BUCKET_NAME`) | `medifinance-medifinance-docs-prod` (`GS_BUCKET_NAME_PROD`) |
| Data | HubSpot-migrated demo data (~2.1k deals) | Empty apart from bootstrap rows |
| Email | Mailtrap sandbox over SMTP | Mailgun HTTP API from `send.medifinance.co.uk`, From `info@medifinance.co.uk`, EU region |
| DocuSeal | On (`sign.medifinance.co.uk` → `docuseal-dev`) | Off (`DOCUSEAL_ENABLED_PROD` unset; no prod DocuSeal instance yet) |
| Forced 2FA | Per `TWO_FACTOR_REQUIRED_ROLES` repo var | Nobody forced (owner's choice) |
| Xero | Connected via the app | Not connected |
| Partner API keys | ACME demo key (real, non-sandbox) | None issued |

**Prod reset 2026-10-26.** `medifinance_prod` was dropped and recreated (prod
data was disposable; no go-live yet), `main` fast-forwarded to `develop`
(`648bb2d`) and deployed (revision 00005). Bootstrap: superuser for the owner,
Site row → `app.medifinance.co.uk`, Organisation "BNP" (pk 1) + 21 rate bands
via `load_bnp_rates`. Mailgun verified end-to-end with a password-reset send
the same day. The old `email-host-password` secret was deleted.

**Release 2026-11-12.** `main` fast-forwarded to `develop` (`57f1a0b`) and
deployed (revision 00006), running migrations 0048–0050. An on-demand Cloud SQL
backup was taken first (ID `1794474799215`, "pre-release 57f1a0b") because 0050
drops `Deal.commission` and `Deal.document_fee`. The backup covers the whole
instance, so restoring it would also roll back the dev database.

**Backups turned on 2026-11-12.** Until this date the instance had *no*
automated backups, no point-in-time recovery and no deletion protection
(the create command in DEPLOYMENT.md never set them). Now: nightly instance
snapshots at 02:00 UTC (14 kept), PITR with 7 days of WAL, deletion
protection, and a nightly `pg_dump` of `medifinance_prod` alone to
`gs://medifinance-sql-backups/prod/` at 03:00 UTC via Cloud Scheduler →
Workflow `export-prod-db` (source `infra/export-prod-db.workflow.yaml`,
35-day retention). The export exists because an instance restore rewinds
dev and DocuSeal too, and because instance backups die with the instance.
Same day, the prod documents bucket got object versioning (noncurrent
versions kept 90 days) and 30-day soft delete; the dev bucket was left at
defaults. Full runbook in DEPLOYMENT.md §9b. Gotcha: a freshly granted IAM role took
about a minute to propagate; the first workflow run failed with
"IAM permission denied" and the retry succeeded. Restore was rehearsed the same
day on dev data, both from an export (import 18 s) and via a point-in-time
clone (26 min); write-up in DEPLOYMENT.md §9b. Still to do: consider an
off-Google copy (encrypted, second provider) for account-loss scenarios.

**Blast-radius trim 2026-11-12.** The Cloud Run runtime SA (Compute default
SA) had project-wide `roles/editor` from project creation — enough to drop
databases and empty buckets. Removed; it keeps only `cloudsql.client`,
`secretmanager.secretAccessor` and bucket-level `storage.objectAdmin` on the
two documents buckets. Verified on dev afterwards: `migrate-dev` ran, a
throwaway job wrote/read/deleted an object in the dev bucket and counted
users over Cloud SQL, and a fresh `medifinance-dev` revision served the
login page. `deployer` only ever had `cloudsql.client`, so CI cannot touch
databases. The DB-reset recipe in DEPLOYMENT.md §9 now refuses any `ENV`
other than `dev` and takes an on-demand backup first.

**gcloud needs `--project=medifinance`** (or `gcloud config set project
medifinance`) on a machine whose default project is something else; the
project id is `medifinance`, region `europe-west2`.

**Rules for prod from here on:** treat it as empty-but-real. Back up before
destructive ops. **Never run `import_hubspot` on prod** — go-live starts from
a clean database; dev is the migrated/demo environment.

**DB-reset recipe** is DEPLOYMENT.md §9. Dropping a database needs idle
backends terminated first: `gcloud sql databases delete` refuses while the
warm Cloud Run instance holds a connection (`conn_max_age=600`); connecting
through cloud-sql-proxy with psycopg and terminating backends worked.

**gcloud `--args` splits on commas.** For `manage.py shell -c` Cloud Run jobs
use the `^@^` delimiter form.

---

## Custom domains and the load balancer

Custom domains go through a global external Application Load Balancer
(europe-west2 has no Cloud Run domain mappings). Static IP **34.54.188.97**.
DNS A records live at an **external registrar and are managed by hand by the
owner**. Architecture and commands: DEPLOYMENT.md step 10.

Live since 2026-08-19:

- `app.medifinance.co.uk` → `medifinance-prod` (cert `medifinance-app-cert-2`)
- `app-dev.medifinance.co.uk` → `medifinance-dev` (cert `medifinance-app-dev-cert-2`)
- `sign.medifinance.co.uk` → **`docuseal-dev`** (cert `medifinance-sign-cert`; NEG `docuseal-dev-neg`, backend `docuseal-dev-backend` — these are not in DEPLOYMENT step 10). Repoint to `docuseal-prod` once that is provisioned.

Gotchas:

- The original app certs sat `FAILED_NOT_VISIBLE` for ~2 weeks before DNS existed and stayed stuck in retry backoff even after DNS was correct. **Recreating the certs fixed it in minutes** (hence the `-2` names).
- After a cert goes `ACTIVE` the edge still needs a few minutes; `curl` returning `SSL_ERROR_SYSCALL` right after a proxy cert swap is propagation, not breakage.
- When updating the HTTPS proxy's cert list, pass the **full** list — listing only the new cert drops the others.
- 2026-10-26: `deploy.yml` hard-codes ingress `internal-and-cloud-load-balancing` for every service and sets `ALLOWED_HOSTS`/CSRF to exactly the per-env domain. All four `*.run.app` hosts verified 404; the three custom domains serve. Invoker stays `allUsers` on all services — required by the LB's serverless NEG; ingress is the access control.
- `DOCUSEAL_URL_DEV` GitHub variable is `https://sign.medifinance.co.uk`. The workflow's fallback if the variable is removed is `sign-dev.medifinance.co.uk`, which has no DNS.

---

## E-signature: self-hosted DocuSeal

**Decided 2026-08-10:** custom-document e-signing uses **self-hosted DocuSeal**
(free/open-source tier, official Docker image), not a SaaS API.

- Free tier covers the templates/submissions API and webhooks. **Pro (~$240/yr)** would be needed for embedded signing or HTML/PDF/DOCX template APIs — deliberately not used. Documents are DocuSeal templates built in its UI with stable field names; Django prefills per-deal `values` via `POST /submissions`. Signers get an email link to the DocuSeal-hosted page.
- Infra: `docuseal-{dev,prod}` Cloud Run services, databases on the existing Cloud SQL instance, dedicated GCS bucket via `GCS_CREDENTIALS` SA-key JSON, `sign.medifinance.co.uk` on the existing LB. `--max-instances=1` is required (in-process background jobs). Other deployment gotchas (HOST env, `--no-cpu-throttling` for in-process email jobs, Rails DB-URL form, `{{submitter.link}}` in custom messages) are in DEPLOYMENT.md §11 — read it before touching prod.
- Django side: `SignatureRequest` is FK'd **directly to `Deal`** (re-keyed 2026-08-20; originally hung off `Document`). The signed PDF lives on `SignatureRequest.signed_file`; the deal page has a dedicated Signatures card. Wrapper `crm/docuseal.py` mirrors `crm/xero.py`. Webhook at `/webhooks/docuseal/` authenticated by the `X-Docuseal-Secret` header. The feature is dark when `DOCUSEAL_URL` is unset.
- **Prefill field names** (renamed 2026-08-20): templates must use `Client Name` / `Client Address` plus `Customer Email`, `Organisation`, `Deal Name`, `Amount`, `Date`. Templates built before then used "Customer Name" and must be renamed by hand in the DocuSeal UI on each instance.
- Status: dev live end-to-end since 2026-08-10 (send → sign → signed PDF + audit log in GCS, verified on deal 2109). Outstanding: the webhook in the DocuSeal console was never configured (the test completion was hand-delivered); **prod DocuSeal is not provisioned**.

---

## HubSpot migration

Tooling lives in `hubspot-migration/` (moved into the repo 2026-11-04; it was
previously an untracked sibling folder). See its README for what is tracked
and what must be obtained from the owner. `field_map.md` there is the
authoritative field-level status document.

Decisions and state:

- **Phase 2 client decisions (2A–2K) all resolved by 2026-08-04**, including the sensitive-fields policy (2K): drop every sensitive field with no schema counterpart (sort codes, account numbers, income, CCJs, "Upload Accounts" refs…); migrate DOB and home address. DOB conflicts with the duplicate export column keep the primary value and record a migration note. Enforced by `check_sensitive_policy()` in the build script.
- **Ownership migrated 2026-08-05:** `Contact.owner` / `Organisation.owner` (crm migration 0025). The build resolves all three HubSpot owner columns and dedupes by email ("Darren Renton PF" is an alias row sharing an email → one account). Expected rebuild numbers: 664 owned contacts, 501 owned orgs, 19 users, zero owner warnings.
- **`Deal.type`** (migration 0027) has `asset_finance` and `commercial_finance`. The HubSpot pipeline→type mapping in the build script is **postponed** at the owner's request. Agreed so far: "MF Commercial Finance" → Commercial Finance; "Medifinance Ltd Business Loans" → Business Loans (not yet a model choice). Undecided for Asset Finance ×955, Structured Finance ×191, KMC Associates ×31, Short term Finance ×2.
- **`term_end_date`** (migration 0028) is populated by the build: HubSpot "End date" wins (655 deals), else Close Date + parsed Term (433), else NULL (1,020).
- **Schema template** is `db-old.sqlite3` in the app root (git-ignored). Apply new app migrations to it via `DATABASE_URL` before a rebuild or the INSERT crashes.
- **Deploying `preview.sqlite3` over `db.sqlite3`** wipes the owner's own login (user id 20; migrated users are ids 1–19) — copy that row back from the pre-deploy backup. The ACME demo API key survives via the template.
- **Migrated users have unusable passwords** (`'!'`). They sign in through password reset or magic links.
- Historical `created_at` and Stage events are backdated (2017→) by the migration, so time series are meaningful, but most historical deals jump `application` → outcome; intermediate `proposal_*` stages exist on only ~100 deals.
- Still open: 5 active users carry `@tobeconfirmed.email` placeholders in `user_mapping_decisions.csv` pending real addresses from the client; 2 org-merge warnings for cluster 33 are skipped actions worth an eyeball.

---

## Reporting and stats

Added 2026-09-01: `crm/stats.py` (`StatsParams` + `DealStats`), `/reports/`,
dashboard headline tiles, vendored Chart.js in `static/vendor/`, templates
`_stat_tile.html` / `_chart.html` / `_stats_filter_bar.html`.

- **Commission is the money metric.** Since 2026-11-11 it is calculated, not stored: see "Commission and fees live on the quote" below. (`Deal.earnings` was never populated and was dropped in migration 0039.)
- Anchor money to the first `deal_live` Stage event and counts to `created_at`.
- Keep the `owner` / `type` / `source` param vocabulary identical to `DealListView` so report rows can deep-link.
- DaisyUI v5 theme colours are `oklch(...)`, which Chart.js can't parse — `static/js/charts.js` round-trips them through a canvas `fillStyle` to get hex.

---

## ACME demo site and the partner API

The ACME demo store (a sibling repo, `acme-ltd`, deployed on Vercel at
`https://acme-amber-beta.vercel.app`) is a fake dental-equipment supplier that
posts to the partner API on **dev** with a real (non-sandbox) key. Vercel is
Git-connected: pushing `main` deploys. The static site lives in `public/`
because Vercel needs that once `buildCommand` is set.

**2026-10-13 rework** (briefs in `docs/briefs/`): additive partner-API changes
(`channel` / `company_type` / `product_id` / `asset_category` / `agent_client`
fields, `Idempotency-Key` replay, structured `errors[]`, 422 amount bounds,
per-email 429, `ApiKey.is_sandbox`, `PartnerApiRequest` audit log,
`GET /api/applications/<ref>/status/`, partner-flavoured magic-link email).
Contract: `docs/partner-api.md`. ACME side: guest flow, MCP server at
`/api/mcp` (Streamable HTTP), MCP Apps card, server card at
`/.well-known/mcp/server-card.json`.

**Audience decision 2026-10-13:** the demo is for suppliers/partners. Lead
with a browser agent on the site, then the staff-in-Claude connector, then
customer-in-chat as the closer. A consumer connector is not the pitch.

Open: confirm the MCP Apps card renders in a real host (only automated tests
have exercised it). Flagged, not planned: an embedded "Ask Acme" chat; a
Medifinance-owned MCP server for suppliers' staff.

---

## Commission and fees live on the quote (2026-11-11)

Migration `0050_move_fees_to_quote_add_source_identifier`.

- `Deal.commission` and `Deal.document_fee` (both £) were **removed**. Commission
  is entered only as `Quote.commission_percent` (a Terms row on commercial deals
  is the same model). `Quote` gained `document_fee`, `document_fee_split` and
  `option_to_purchase_fee` (all £).
- The three fee fields are **Asset Finance only**: they are on `QuoteForm` and
  not on `TermsForm`. "Asset Finance" here means `not deal.is_commercial_finance`,
  so untyped deals count.
- **Commission £ is calculated**: selected quote's `commission_percent` ×
  its `finance_amount` (funded − deposit − balloon), rounded half-up to the
  penny. `Deal.commission_amount` / `Quote.commission_amount` in Python and
  `crm.stats.commission_amount_expr()` in SQL (deal list column, reports,
  dashboard) must stay in step; `crm/tests/test_quote_fees.py` checks they agree.
  The commission-invoice email, Xero prefill and DocuSeal "Commission" prefill
  all read `Deal.commission_amount`. No selected quote, no percentage or no
  participations means no commission.
- **The existing deal-level values were dropped on purpose** (owner's decision:
  the site is not live and the HubSpot migration will be redone). The migration
  has no data step, and `hubspot-migration/build_preview_db.py` no longer writes
  "Salesman Commission" / "Doc Fee". **Follow-up for the migration redo:** port
  them to quote level (`hubspot-migration/field_map.md` has the note). Until
  then the ~400 migrated deals with a HubSpot commission show "—" and report /
  dashboard commission totals for historic months are zero.
- Same migration adds `Deal.source_identifier` ("Identifier"): optional free
  text about the source, e.g. a particular exhibition. Source and Identifier
  are both on the deal create/edit form (Source was on the form class before
  but the template never rendered it).

---

## Customer-provided organisation details and staff review (2026-11-12)

Partner-API deals are created with no organisation, and until now the portal
gave the customer no way to supply one (and so no way to add co-applicants).
The lead applicant can now create their company and edit all of its
customer-facing details; staff review what they enter. Everything goes
through `crm/org_review.py`.

Decisions (agreed with Matt):

- **Hybrid application.** A customer-created organisation is live and linked
  to the deal immediately, but `is_unverified` until a staff member clicks
  Verify. On any other organisation, contact details (address, phone, email,
  website) save immediately and are queued as an *applied*
  `OrganisationChange`; name, trading name and Companies House number are
  held as a *proposal* and only saved on approval. Reason: holding everything
  would leave the customer stuck (no company card, no co-applicants), while
  letting a customer rename a shared, verified record unreviewed is unsafe —
  a rename shows up everywhere that organisation is used.
- **Any staff member who can see the organisation may review** (admin or
  associate, via `visible_organisations`). No admin-only gate.
- **Customers type, they never search** — a search would expose other
  customers' companies. The review panel lists likely duplicates (same
  normalised Companies House number, or name similarity >= 0.85 using the
  HubSpot migration's normaliser) and offers "Use this instead".
- **Signature requests are blocked while the deal's organisation is
  unverified** (create and re-send; `organisation_blocks_signing` in
  `crm/views/signature.py`). The organisation name is prefilled into the
  DocuSeal template, so an unchecked customer-typed name must not reach an
  agreement. Emailing an existing pending request is not blocked — its values
  were fixed when it was created.

Rules worth knowing before changing this:

- "Free edit" is for the **submitter of an unverified organisation only**
  (`may_edit_freely`). Staff can attach an unverified organisation to a second
  deal (the picker is unscoped); that deal's customer takes the hybrid path
  and cannot rename it.
- A held proposal is shown in the portal **only to the customer who made it**.
  One pending row per (organisation, submitter, applied); later edits merge.
- The Companies House number is **never required** from a customer, whatever
  the deal's company type: partnerships that aren't limited have none (Matt,
  2026-11-12). It is only hidden for sole traders, and format-checked when
  given. Staff confirm it when verifying.
- `legal_name`, `sector` and `owner` are never on a customer form. Staff fill
  in the registered name from Companies House when verifying.
- The portal company edit **never calls `form.save()`** — a ModelForm would
  write the held fields. The view passes `cleaned_data` to
  `record_customer_edit`, which re-reads the row under a lock and decides the
  mode from the database, so a verification that lands mid-edit is honoured.
- Recording a proposal bumps the organisation's `updated_at` even though no
  field changed, so the portal's optimistic lock catches a stale second tab.
- Rejecting an applied contact-detail change reverts each field **only if it
  still holds the customer's value**; a later staff edit wins. The customer is
  not notified of a rejection (v1) — the representative follows up.
- There is no "reject" for an unverified organisation: verify it, edit then
  verify, or swap it for an existing record.
- **Swap** (`swap_to_existing`): only for an unverified organisation, only
  onto a record `likely_duplicates` offers at POST time (so a forged POST
  can't move a deal onto an arbitrary organisation), and refused if anything
  other than deals, notes and stage events points at the duplicate
  (participations, proposals, quotes, rate bands, users, API keys, introduced
  deals). Deals, notes and stages are repointed first — a plain delete would
  cascade the audit notes — contacts join the target, the customer's
  differing contact details become a pending proposal on the target, then the
  duplicate is deleted. After a swap the customer can see and edit the
  existing record's contact details in their portal; the confirm dialog says
  so.
- `DealForm.clean_organisation` stops a stale staff deal form (rendered with
  no organisation) blanking one the customer created in the meantime.
- Partner-API deals are unowned, so the organisation created from them is
  unowned too and therefore visible to every associate, even though the deal
  itself is admin-only until someone takes ownership. The review panel only
  links to deals the viewer can see.
- Migration `0051_organisation_review` has no data step. Apply it to
  `db-old.sqlite3` too if the HubSpot preview DB is rebuilt.

Not done: a Companies House API lookup (staff follow the deep link and check
by hand), customer notification on rejection, and a general organisation
merge tool (the swap covers only the customer-created-duplicate case).

## Email

- Prod: Mailgun HTTP API via django-anymail, `send.medifinance.co.uk`, EU region, From `Medifinance <info@medifinance.co.uk>`. Needs the `mailgun-api-key` secret and `EMAIL_ENABLED=true` repo variable.
- Dev: Mailtrap sandbox over SMTP (STARTTLS on 2525).
- Local: Mailpit on localhost (no auth; `EMAIL_HOST_USER` empty skips SMTP AUTH, `EMAIL_HOST_PASSWORD` non-empty only to select the SMTP backend).
- With no credentials at all the console backend logs the email.

### Deal-owner notifications (2026-11-12)

`crm/notifications.py` emails a deal's owner when something happens on their deal:

| Event | Trigger | Recipients |
|---|---|---|
| E-signature signed | DocuSeal webhook, on the delivery that flips the request to Completed (duplicate deliveries send nothing) | owner |
| Quote / Terms selected | portal `MyDealQuoteSelectView` and staff `QuoteSelectView`, only when the selection actually changes | owner |
| Document uploaded | `DocumentUploadView` (customer or staff) | owner |
| Supplier invoice uploaded | `SubmitParticipationInvoiceView` | owner **and** `NOTIFY_EMAILS` (the staff list already got this email; the owner was added), de-duplicated |

Rules, all in `owner_recipients`:

- The owner is **not** emailed about their own action (selecting a quote or uploading a document on their own deal). A colleague doing it on their deal does notify them.
- A deal with no owner, or an inactive one, falls back to `NOTIFY_EMAILS` so it is never silent. Partner-API deals start unowned, so until someone takes ownership their notifications go to that list.
- Sends are synchronous and best-effort: a failure is logged (`crm.notifications`) and never breaks the upload / selection / webhook. The webhook sends after its row-locking transaction, so a slow mailserver can't hold the lock or make DocuSeal retry.
- There is no per-user opt-out and no batching: several documents uploaded one after another are several emails. On dev the Mailtrap sandbox throttles back-to-back sends, so a burst can make each request wait on the retry in `accounts.emails._send_with_retry`.
- The first three share one template (`templates/email/deal_owner_notification.*`); a new owner notification is one small function in `crm/notifications.py`.

---

## Open items (2026-11-04)

- Prod DocuSeal instance, then repoint `sign.medifinance.co.uk` and set `DOCUSEAL_ENABLED_PROD`.
- DocuSeal console webhook on dev was never configured.
- Pipeline → `Deal.type` mapping in the migration build.
- Five migrated staff users still have placeholder emails.
- **HubSpot migration redo:** port "Salesman Commission" and "Doc Fee" onto a quote per deal (see "Commission and fees live on the quote"). Until then migrated deals show no commission and report commission totals are zero.
- `pg_trgm` index for search when row counts grow.
