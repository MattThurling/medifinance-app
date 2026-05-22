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
- Your Mailgun (EU) API key to hand

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
gcloud sql instances create "$SQL_INSTANCE" \
  --database-version=POSTGRES_16 \
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

# Mailgun API key (paste your real EU key; same key is fine for both envs)
printf 'YOUR_MAILGUN_EU_API_KEY' | gcloud secrets create mailgun-key-dev  --data-file=-
printf 'YOUR_MAILGUN_EU_API_KEY' | gcloud secrets create mailgun-key-prod --data-file=-
```

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

## 8. First deploy

```bash
git checkout -b develop
git push -u origin develop      # triggers .github/workflows/deploy.yml -> medifinance-dev
```

Watch the run in the repo's **Actions** tab. When green, get the URL:

```bash
gcloud run services describe medifinance-dev --region "$REGION" --format='value(status.url)'
```

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
  --add-cloudsql-instances "$SQL_CONN" \
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

Then add the domain to the prod service's env: append it to `DJANGO_ALLOWED_HOSTS` (e.g. `.run.app,crm.medifinance.co.uk`) and `DJANGO_CSRF_TRUSTED_ORIGINS` (`https://*.run.app,https://crm.medifinance.co.uk`) in `.github/workflows/deploy.yml`, and also point `MAILGUN_SENDER_DOMAIN` / `DEFAULT_FROM_EMAIL` at your verified Mailgun domain.

## Notes & gotchas

- **`DJANGO_ALLOWED_HOSTS=.run.app`** (leading dot) matches any Cloud Run URL, so the first deploy works before you know the exact hostname. `CSRF_TRUSTED_ORIGINS` uses `https://*.run.app`.
- **Migrations** run as a Cloud Run *Job* before each service deploy — never on container start (avoids races across instances).
- **Cost** is dominated by Cloud SQL. `db-f1-micro` is the cheapest; stop/scale via `gcloud sql instances patch`.
- **Secrets** are referenced by name in the workflow, so rotating a value (`gcloud secrets versions add ...`) takes effect on the next deploy without code changes.
