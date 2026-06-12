# Deployment — Google Cloud Run

Architecture (chosen for v1):

- **One GCP project**, two Cloud Run services: `medifinance-dev` and `medifinance-prod`
- **One Cloud SQL (Postgres) instance** with two databases: `medifinance_dev`, `medifinance_prod`
- **Artifact Registry** for images, **Secret Manager** for secrets
- **GitHub Actions** deploys via **Workload Identity Federation** (keyless)
- Branch mapping: `develop` → dev, `main` → prod

Static files are served in-container by WhiteNoise. Media (documents, later) will move to Cloud Storage.

---

## 0. Prerequisites

- `gcloud` CLI installed and logged in (`gcloud auth login`)
- A GCP project with billing enabled
- The repo pushed to GitHub
- The `info@medi-finance.co.uk` SMTP password to hand (for real email; optional — without it the app falls back to the console backend)

Set shell variables used throughout:

```bash
export PROJECT_ID="REPLACE_ME"             # your GCP project id
export REGION="europe-west2"               # London
export GITHUB_REPO="OWNER/REPO"            # e.g. medifinance/crm
export AR_REPO="app"
export SQL_INSTANCE="medifinance-sql"
export DB_USER="medifinance"

gcloud config set project "$PROJECT_ID"
```

## 1. Enable APIs

```bash
gcloud services enable \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  sqladmin.googleapis.com \
  secretmanager.googleapis.com \
  iamcredentials.googleapis.com
```

## 2. Artifact Registry (image store)

```bash
gcloud artifacts repositories create "$AR_REPO" \
  --repository-format=docker \
  --location="$REGION" \
  --description="Medifinance container images"
```

## 3. Cloud SQL (Postgres) — one instance, two databases

```bash
# Smallest/cheapest tier (~£8–12/mo). Scale up later with `gcloud sql instances patch`.
# --edition=ENTERPRISE is required: the default ENTERPRISE_PLUS rejects shared-core
# tiers like db-f1-micro (it only allows db-perf-optimized-* machines).
gcloud sql instances create "$SQL_INSTANCE" \
  --database-version=POSTGRES_16 \
  --edition=ENTERPRISE \
  --tier=db-f1-micro \
  --region="$REGION" \
  --storage-auto-increase

# URL-safe password (no characters that need escaping in a DATABASE_URL)
export DB_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"

gcloud sql users create "$DB_USER" --instance="$SQL_INSTANCE" --password="$DB_PASSWORD"
gcloud sql databases create medifinance_dev  --instance="$SQL_INSTANCE"
gcloud sql databases create medifinance_prod --instance="$SQL_INSTANCE"

# Instance connection name -> PROJECT:REGION:INSTANCE  (note it; used in several places)
export SQL_CONN="$(gcloud sql instances describe "$SQL_INSTANCE" --format='value(connectionName)')"
echo "$SQL_CONN"
```

## 4. Secret Manager

```bash
# Django SECRET_KEY (one per env)
python3 -c 'import secrets; print(secrets.token_urlsafe(50))' | gcloud secrets create django-secret-dev  --data-file=-
python3 -c 'import secrets; print(secrets.token_urlsafe(50))' | gcloud secrets create django-secret-prod --data-file=-

# DATABASE_URL (per env). The ?host= points Postgres at the Cloud SQL unix socket.
printf 'postgres://%s:%s@/medifinance_dev?host=/cloudsql/%s'  "$DB_USER" "$DB_PASSWORD" "$SQL_CONN" | gcloud secrets create database-url-dev  --data-file=-
printf 'postgres://%s:%s@/medifinance_prod?host=/cloudsql/%s' "$DB_USER" "$DB_PASSWORD" "$SQL_CONN" | gcloud secrets create database-url-prod --data-file=-
```

### Email (optional — skip if you don't have the SMTP password yet)

```bash
# The mail.medi-finance.co.uk SMTP password for info@medi-finance.co.uk.
# One secret shared across dev + prod (same mailbox).
read -rsp "EMAIL_HOST_PASSWORD: " EMAIL_PW && echo
printf '%s' "$EMAIL_PW" | gcloud secrets create email-host-password --data-file=-
unset EMAIL_PW
```

> Without this secret the app falls back to the console backend (emails print
> to Cloud Run logs) — useful for the very first deploy if you don't have the
> password handy. Re-deploy after creating it to flip to real SMTP.

### Mailtrap Sandbox (dev email)

The dev service sends all email to the [Mailtrap](https://mailtrap.io) Sandbox
— messages are caught in the shared inbox, never delivered. The workflow wires
the host/port (`sandbox.smtp.mailtrap.io:2525`, STARTTLS) automatically; only
the inbox's SMTP credentials (Mailtrap → your inbox → SMTP settings) live in
Secret Manager:

```bash
printf '%s' "MAILTRAP_SMTP_USERNAME" | gcloud secrets create mailtrap-user --data-file=-
printf '%s' "MAILTRAP_SMTP_PASSWORD" | gcloud secrets create mailtrap-password --data-file=-
```

These secrets are **required** for dev deploys — the Cloud Run deploy fails if
they don't exist.

## 4b. Cloud Storage bucket (uploaded documents)

Documents (bank statements, ID, etc.) are uploaded by customers and **must** be
stored off the container — Cloud Run's filesystem is ephemeral. The bucket is
**private**; files are served only through the app's permission-checked download
view.

```bash
export DOCS_BUCKET="${PROJECT_ID}-medifinance-docs"   # bucket names are globally unique

gcloud storage buckets create "gs://${DOCS_BUCKET}" \
  --location="$REGION" \
  --uniform-bucket-level-access \
  --public-access-prevention
```

(IAM for the runtime SA is granted in the next step.)

## 5. Service accounts & IAM

```bash
# Deployer SA (used by GitHub Actions)
gcloud iam service-accounts create deployer --display-name="GitHub Actions deployer"
export DEPLOY_SA="deployer@${PROJECT_ID}.iam.gserviceaccount.com"

for ROLE in \
  roles/run.admin \
  roles/cloudsql.client \
  roles/artifactregistry.writer \
  roles/iam.serviceAccountUser \
  roles/secretmanager.secretAccessor ; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${DEPLOY_SA}" --role="$ROLE" --condition=None
done

# Runtime SA = the Compute default SA that Cloud Run services run as.
# It needs to reach Cloud SQL and read the mounted secrets.
export PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
export RUNTIME_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:${RUNTIME_SA}" --role="roles/cloudsql.client" --condition=None
gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:${RUNTIME_SA}" --role="roles/secretmanager.secretAccessor" --condition=None

# Read/write the documents bucket (scoped to just that bucket)
gcloud storage buckets add-iam-policy-binding "gs://${DOCS_BUCKET}" \
  --member="serviceAccount:${RUNTIME_SA}" --role="roles/storage.objectAdmin"
```

> Tightening later: `secretAccessor` is granted project-wide here for simplicity. For least privilege, grant it per-secret instead.

## 6. Workload Identity Federation (keyless GitHub → GCP)

```bash
gcloud iam workload-identity-pools create github \
  --location=global --display-name="GitHub Actions"

gcloud iam workload-identity-pools providers create-oidc github \
  --location=global \
  --workload-identity-pool=github \
  --display-name="GitHub OIDC" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" \
  --attribute-condition="assertion.repository == '${GITHUB_REPO}'" \
  --issuer-uri="https://token.actions.githubusercontent.com"

# Let only this repo impersonate the deployer SA
export POOL_ID="$(gcloud iam workload-identity-pools describe github --location=global --format='value(name)')"
gcloud iam service-accounts add-iam-policy-binding "$DEPLOY_SA" \
  --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/${POOL_ID}/attribute.repository/${GITHUB_REPO}"

# Print the provider resource name — you need it as a GitHub variable (next step)
gcloud iam workload-identity-pools providers describe github \
  --location=global --workload-identity-pool=github --format='value(name)'
```

## 7. GitHub repo variables

In the repo: **Settings → Secrets and variables → Actions → Variables** (not Secrets — WIF is keyless). Add:

| Variable | Value |
|---|---|
| `GCP_PROJECT_ID` | your project id |
| `WIF_PROVIDER` | the provider resource name printed in step 6 (`projects/NUMBER/locations/global/workloadIdentityPools/github/providers/github`) |
| `DEPLOY_SA_EMAIL` | `deployer@PROJECT_ID.iam.gserviceaccount.com` |
| `CLOUDSQL_CONNECTION` | the `$SQL_CONN` value (`PROJECT:REGION:INSTANCE`) |
| `EMAIL_ENABLED` | *(optional)* set to `true` after creating the `email-host-password` secret to wire real SMTP. Unset = console backend. |
| `GS_BUCKET_NAME` | the documents bucket (`$DOCS_BUCKET`) — required for uploaded documents to persist |
| `NOTIFY_EMAILS` | *(optional)* comma-separated staff addresses notified about new API-created deals. Defaults to `mnthurling@gmail.com`. |

## 8. First deploy

```bash
git checkout -b develop
git push -u origin develop      # triggers .github/workflows/deploy.yml -> medifinance-dev
```

Watch the run in the repo's **Actions** tab. When green, get the URL:

```bash
gcloud run services describe medifinance-dev --region "$REGION" --format='value(status.url)'
```

That `https://medifinance-dev-….run.app` URL **is your test domain** — Cloud Run
assigns it automatically with managed HTTPS. No DNS, no domain purchase, no
verification. It's already covered by `DJANGO_ALLOWED_HOSTS=.run.app`, so it
works on first deploy.

Promote to production by merging into `main`:

```bash
git checkout main && git merge develop && git push      # -> medifinance-prod
```

## 9. Create the first admin user

The migrate job runs automatically, but you still need a superuser. Easiest is a one-off Cloud Run job (replace `dev`/`prod` and pick a strong password):

```bash
IMAGE="$(gcloud run services describe medifinance-dev --region "$REGION" --format='value(spec.template.spec.containers[0].image)')"

gcloud run jobs deploy createsuperuser-dev \
  --image "$IMAGE" --region "$REGION" \
  --set-cloudsql-instances "$SQL_CONN" \
  --set-secrets "DJANGO_SECRET_KEY=django-secret-dev:latest,DATABASE_URL=database-url-dev:latest" \
  --set-env-vars "DJANGO_DEBUG=0,DJANGO_SUPERUSER_EMAIL=you@medifinance.co.uk,DJANGO_SUPERUSER_PASSWORD=CHANGE_ME" \
  --command python --args manage.py,createsuperuser,--noinput
gcloud run jobs execute createsuperuser-dev --region "$REGION" --wait
```

Then log in and change the password. (The custom User model uses email as the login, which `--noinput` reads from `DJANGO_SUPERUSER_EMAIL`.)

## 10. Custom domain (later)

```bash
gcloud run domain-mappings create --service medifinance-prod --domain crm.medifinance.co.uk --region "$REGION"
```

Then add the domain to the prod service's env: append it to `DJANGO_ALLOWED_HOSTS` (e.g. `.run.app,crm.medifinance.co.uk`) and `DJANGO_CSRF_TRUSTED_ORIGINS` (`https://*.run.app,https://crm.medifinance.co.uk`) in `.github/workflows/deploy.yml`.

## Email

The app sends via the **medi-finance.co.uk SMTP server** over SSL on port 465
as `info@medi-finance.co.uk`. The host / port / user are baked into
`settings.py` as defaults; only the password is configured at deploy time.

**Until you set the password**, the app uses Django's **console email backend**:
"Email link to customer" prints the email (with the magic link) to the Cloud
Run logs —

```bash
gcloud run services logs read medifinance-dev --region "$REGION" --limit 50
```

— and the **"Generate link"** button on a deal shows the magic-link URL directly
in the UI, so you can test the full portal flow without sending anything.

### Switching on real email

1. Create the secret (one-time, shared by both envs since the mailbox is the same):
   ```bash
   read -rsp "EMAIL_HOST_PASSWORD: " EMAIL_PW && echo
   printf '%s' "$EMAIL_PW" | gcloud secrets create email-host-password --data-file=-
   unset EMAIL_PW
   ```
2. Grant the runtime SA read access (only needed once, and only if you skipped
   the project-wide `secretmanager.secretAccessor` binding in step 5):
   ```bash
   gcloud secrets add-iam-policy-binding email-host-password \
     --member="serviceAccount:${RUNTIME_SA}" --role=roles/secretmanager.secretAccessor
   ```
3. Set the GitHub **variable** `EMAIL_ENABLED = true`.
4. Push to redeploy. The app picks up `EMAIL_HOST_PASSWORD` from Secret Manager
   and switches to SMTP automatically; From is `Medifinance <info@medi-finance.co.uk>`.

Rotating the password later: `gcloud secrets versions add email-host-password --data-file=-` and re-deploy.

## Notes & gotchas

- **`DJANGO_ALLOWED_HOSTS=.run.app`** (leading dot) matches any Cloud Run URL, so the first deploy works before you know the exact hostname. `CSRF_TRUSTED_ORIGINS` uses `https://*.run.app`.
- **Migrations** run as a Cloud Run *Job* before each service deploy — never on container start (avoids races across instances).
- **Cost** is dominated by Cloud SQL. `db-f1-micro` is the cheapest; stop/scale via `gcloud sql instances patch`.
- **Secrets** are referenced by name in the workflow, so rotating a value (`gcloud secrets versions add ...`) takes effect on the next deploy without code changes.
