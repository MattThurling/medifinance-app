# Access and secrets handover

Everything a maintainer needs beyond a clone of this repo. Nothing here is a
secret itself; it says where each one lives and who can grant it.

## External accounts

| System | What it's for | How access is granted |
|---|---|---|
| **GitHub** `MattThurling/medifinance-app` | Code, CI/CD (GitHub Actions), repo variables listed in DEPLOYMENT.md §7 | Repo admin adds a collaborator. Deploys need no personal cloud credentials: Actions uses Workload Identity Federation (DEPLOYMENT.md §6) |
| **Google Cloud** project | Cloud Run services (`medifinance-dev/prod`, `docuseal-dev`), Cloud SQL (one instance, `medifinance_dev` / `medifinance_prod` / DocuSeal DBs), Artifact Registry, Secret Manager, GCS document buckets, the global load balancer and certs, billing | Project Owner grants IAM roles. The project id is the `GCP_PROJECT_ID` repo variable |
| **Domain registrar** for `medifinance.co.uk` | DNS A records for `app`, `app-dev`, `sign` → LB IP `34.54.188.97`; Mailgun DNS records for `send.medifinance.co.uk` | Managed by hand by the owner; registrar login is theirs |
| **Mailgun** | Prod outbound email from `send.medifinance.co.uk` (EU region) | Account owner invites a user. Sending key is the `mailgun-api-key` secret in Secret Manager |
| **Mailtrap** | Dev inbox (sandbox SMTP) | Owner's account; credentials in Secret Manager for dev |
| **DocuSeal consoles** | `https://sign.medifinance.co.uk` (dev). Templates are built here by hand; API token and webhook secret are Secret Manager secrets | Admin user created on first boot of each instance; owner adds further users in the DocuSeal UI |
| **Xero developer app** | OAuth client for invoice sync (one app per environment) | developer.xero.com account of the owner; `XERO_CLIENT_ID/SECRET` |
| **Vercel** | Hosts the ACME demo site (sibling repo `acme-ltd`, Git-connected, no CLI login needed) | Owner's Vercel account; env holds a real partner API key for dev |
| **HubSpot** | Source system. Exports were taken 2026-08-03; portal id `3378161` is used for deep links | Client's account. Not needed day to day |

## Local files that are deliberately not in git

| Path | Contents | Handover route |
|---|---|---|
| `.env` | Local dev settings; Xero and DocuSeal credentials for a local instance | Recreate from `.env.example`; share real values through a password manager, never chat or email |
| `import/` | HubSpot import CSVs for `import_hubspot` (customer PII) | Regenerate from `hubspot-migration/` or secure file transfer |
| `hubspot-migration/` (ignored files) | `hubspot.db`, `hubspot exports/`, `preview.sqlite3`, contact-level decision CSVs — see its README | Secure file transfer (encrypted archive) from the owner |
| `db.sqlite3`, `db-old.sqlite3`, `db*.bak-*` | Local SQLite with migrated data. `db-old.sqlite3` is the build template for the migration; `db.sqlite3.bak-pre-owner` holds the owner's login row that gets re-inserted after a preview deploy | Secure file transfer if the migration needs rebuilding; otherwise not needed |
| `.claude/settings.local.json` | Personal Claude Code permission allowlist | Not needed; a shared baseline is in `.claude/settings.json` |

## Secrets in Secret Manager (names only)

Per environment unless stated: `django-secret-key-*`, `database-url-*`,
`mailgun-api-key` (prod), Mailtrap SMTP credentials (dev),
`docuseal-api-token-*`, `docuseal-webhook-secret-*`, DocuSeal
`SECRET_KEY_BASE` per env, the `GCS_CREDENTIALS` SA key for DocuSeal's
bucket. Rotate with `gcloud secrets versions add`; the next deploy picks up
the new version (DEPLOYMENT.md "Notes & gotchas").

## Handover checklist

1. GitHub: add the new maintainer as admin; confirm they can see Actions variables.
2. GCP: grant Owner (or at minimum Cloud Run Admin, Cloud SQL Admin, Secret Manager Admin, Storage Admin, Compute Load Balancer Admin, Artifact Registry Admin, Logging Viewer, IAM Workload Identity Pool Admin). Add them to billing.
3. Registrar: either transfer the domain or document the login in the shared password manager.
4. Mailgun, Mailtrap, Vercel, Xero developer: invite or document.
5. DocuSeal: create an admin user for them on each instance.
6. Share `.env` values and the ignored migration files through a password manager and an encrypted archive.
7. Walk them through `docs/DECISIONS.md` "Open items".
