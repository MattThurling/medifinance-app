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
| `ACCOUNTS_EMAILS` | *(optional)* comma-separated accounts addresses notified when staff request a commission invoice from a deal. Defaults to `mnthurling@gmail.com`. |
| `DOCUSEAL_ENABLED` | *(optional)* set to `true` after the DocuSeal setup (step 11) — wires `DOCUSEAL_URL` + the `docuseal-api-token-*` / `docuseal-webhook-secret-*` secrets into the app. Unset = the send-for-signature UI stays hidden. |
| `DOCUSEAL_URL_DEV` | *(optional)* overrides the dev DocuSeal URL (e.g. the `*.run.app` address while `sign-dev.medifinance.co.uk` doesn't exist). Defaults to `https://sign-dev.medifinance.co.uk`. |

## 8. First deploy

```bash
git checkout -b develop
git push -u origin develop      # triggers .github/workflows/deploy.yml -> medifinance-dev
```

Watch the run in the repo's **Actions** tab. When green, get the URL:

```bash
gcloud run services describe medifinance-dev --region "$REGION" --format='value(status.url)'
```

> **Note:** since the load-balancer setup (step 10), the `*.run.app` URL no
> longer serves traffic — ingress is restricted to the LB, and the dev site
> lives at `https://app-dev.medifinance.co.uk`.

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

## 10. Custom domains (global load balancer)

`gcloud run domain-mappings` is **not available in europe-west2**, so custom
domains are served by a **global external Application Load Balancer** in front
of both Cloud Run services:

- `app.medifinance.co.uk` → `medifinance-prod`
- `app-dev.medifinance.co.uk` → `medifinance-dev`

One static IP, host-based routing, two Google-managed certificates (one per
hostname so they provision independently), and a port-80 listener that 301s to
HTTPS. Cost: ~£15–18/mo (forwarding rules).

```
A records (registrar)          ┌─ app.medifinance.co.uk ────→ medifinance-prod-backend → NEG → medifinance-prod
app, app-dev → static IP ─→ LB ┤
  :80 → 301 https              └─ app-dev.medifinance.co.uk → medifinance-dev-backend  → NEG → medifinance-dev
```

The resources were created with (repeat `dev` block for `prod`):

```bash
gcloud services enable compute.googleapis.com

# Static IP — this is what the registrar A records point at
gcloud compute addresses create medifinance-lb-ip --global --ip-version=IPV4
gcloud compute addresses describe medifinance-lb-ip --global --format='value(address)'

# Serverless NEG + backend service per Cloud Run service
gcloud compute network-endpoint-groups create medifinance-dev-neg \
  --region="$REGION" --network-endpoint-type=serverless --cloud-run-service=medifinance-dev
gcloud compute backend-services create medifinance-dev-backend --global --load-balancing-scheme=EXTERNAL_MANAGED
gcloud compute backend-services add-backend medifinance-dev-backend --global \
  --network-endpoint-group=medifinance-dev-neg --network-endpoint-group-region="$REGION"

# URL map: default → prod, host rules per domain
gcloud compute url-maps create medifinance-lb --default-service=medifinance-prod-backend
gcloud compute url-maps add-path-matcher medifinance-lb --path-matcher-name=prod \
  --default-service=medifinance-prod-backend --new-hosts=app.medifinance.co.uk
gcloud compute url-maps add-path-matcher medifinance-lb --path-matcher-name=dev \
  --default-service=medifinance-dev-backend --new-hosts=app-dev.medifinance.co.uk

# Google-managed certs (provision only after DNS points at the LB IP)
gcloud compute ssl-certificates create medifinance-app-cert     --domains=app.medifinance.co.uk     --global
gcloud compute ssl-certificates create medifinance-app-dev-cert --domains=app-dev.medifinance.co.uk --global

# HTTPS listener
gcloud compute target-https-proxies create medifinance-https-proxy \
  --url-map=medifinance-lb --ssl-certificates=medifinance-app-cert,medifinance-app-dev-cert
gcloud compute forwarding-rules create medifinance-https-rule --global \
  --load-balancing-scheme=EXTERNAL_MANAGED --address=medifinance-lb-ip \
  --target-https-proxy=medifinance-https-proxy --ports=443

# HTTP → HTTPS redirect (redirect-only URL map, imported from YAML)
cat > /tmp/http-redirect.yaml <<'YAML'
name: medifinance-http-redirect
defaultUrlRedirect:
  httpsRedirect: true
  redirectResponseCode: MOVED_PERMANENTLY_DEFAULT
YAML
gcloud compute url-maps import medifinance-http-redirect --global --source=/tmp/http-redirect.yaml
gcloud compute target-http-proxies create medifinance-http-proxy --url-map=medifinance-http-redirect
gcloud compute forwarding-rules create medifinance-http-rule --global \
  --load-balancing-scheme=EXTERNAL_MANAGED --address=medifinance-lb-ip \
  --target-http-proxy=medifinance-http-proxy --ports=80
```

**DNS** (external registrar): two A records, `app` and `app-dev`, both pointing
at the static IP. Certificates flip from `PROVISIONING` to `ACTIVE` 15–60 min
after DNS propagates:

```bash
gcloud compute ssl-certificates list --format='table(name,managed.status,managed.domainStatus)'
```

**Ingress lockdown**: the workflow deploys with
`--ingress internal-and-cloud-load-balancing`, so the `*.run.app` URLs return
403 and all traffic must come through the LB. `DJANGO_ALLOWED_HOSTS` /
`DJANGO_CSRF_TRUSTED_ORIGINS` are set to exactly the per-env custom domain.
Anything that used a `run.app` URL directly (e.g. the ACME demo site's API
calls) must use the custom domain instead.

## 11. DocuSeal (e-signature)

Self-hosted [DocuSeal](https://www.docuseal.com/) (free tier, official image)
provides e-signing: staff send a deal document for signature from the deal
page, the signer gets an email link to a DocuSeal-hosted signing page, and the
signed PDF lands back on the Document via the `/webhooks/docuseal/` webhook.
Same shape as the app: one Cloud Run service per env, a database on the
existing Cloud SQL instance, files in GCS.

Agreement **templates are built by hand in the DocuSeal UI** — give data
fields the exact names `crm/docuseal.py::build_prefill_values()` emits
("Client Name", "Client Address", "Customer Email", "Organisation",
"Deal Name", "Amount", "Date") and they're prefilled per deal. Dev and prod are separate DocuSeal
databases, so templates must be created on each (ids differ — the app always
lists templates via the API, never hardcodes ids).

### 11a. Database (on the existing instance)

```bash
gcloud sql databases create docuseal_dev  --instance=medifinance-sql
gcloud sql databases create docuseal_prod --instance=medifinance-sql

# Same user/password as the app DBs; Rails accepts the unix-socket URL form.
printf 'postgresql://medifinance:%s@/docuseal_dev?host=/cloudsql/%s' "$DB_PASS" "$SQL_CONN" \
  | gcloud secrets create docuseal-database-url-dev --data-file=-
printf 'postgresql://medifinance:%s@/docuseal_prod?host=/cloudsql/%s' "$DB_PASS" "$SQL_CONN" \
  | gcloud secrets create docuseal-database-url-prod --data-file=-
```

### 11b. Storage (Cloud Run disk is ephemeral)

DocuSeal wants a service-account key JSON *string* in `GCS_CREDENTIALS` (it
can't use ambient ADC), so it gets its own SA + private bucket:

```bash
gcloud storage buckets create "gs://${PROJECT_ID}-docuseal" --location="$REGION" \
  --uniform-bucket-level-access --public-access-prevention
gcloud iam service-accounts create docuseal-storage
gcloud storage buckets add-iam-policy-binding "gs://${PROJECT_ID}-docuseal" \
  --member="serviceAccount:docuseal-storage@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role=roles/storage.objectAdmin
gcloud iam service-accounts keys create /dev/stdout \
  --iam-account="docuseal-storage@${PROJECT_ID}.iam.gserviceaccount.com" \
  | gcloud secrets create docuseal-gcs-credentials --data-file=-
```

(If `GCS_CREDENTIALS` misbehaves, the fallback is GCS's S3 interoperability:
an HMAC key + `S3_ENDPOINT=https://storage.googleapis.com` +
`S3_ATTACHMENTS_BUCKET` + `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`.)

### 11c. Cloud Run service (manual deploy — third-party image, no CI)

```bash
# One SECRET_KEY_BASE per env
openssl rand -hex 64 | gcloud secrets create docuseal-secret-key-base-dev --data-file=-

# Pin a concrete docuseal/docuseal tag rather than :latest when deploying.
gcloud run deploy docuseal-dev \
  --image=docuseal/docuseal:3.1.7 \
  --region="$REGION" --port=3000 --memory=1Gi --cpu=1 \
  --min-instances=0 --max-instances=1 \
  --no-cpu-throttling \
  --allow-unauthenticated \
  --ingress=all \
  --add-cloudsql-instances="$SQL_CONN" \
  --set-secrets="DATABASE_URL=docuseal-database-url-dev:latest,SECRET_KEY_BASE=docuseal-secret-key-base-dev:latest,SMTP_USERNAME=mailtrap-user:latest,SMTP_PASSWORD=mailtrap-password:latest,GCS_CREDENTIALS=docuseal-gcs-credentials:latest" \
  --set-env-vars="HOST=<the service's own domain>,FORCE_SSL=true,GCS_PROJECT=${PROJECT_ID},GCS_BUCKET=${PROJECT_ID}-docuseal,SMTP_ADDRESS=sandbox.smtp.mailtrap.io,SMTP_PORT=2525,SMTP_FROM=info@medi-finance.co.uk"
```

- `--max-instances=1` matters: DocuSeal runs its background jobs in-process;
  one instance avoids duplicate webhook deliveries. Ample for our volume.
- `--no-cpu-throttling` matters too: those in-process jobs (embedded Redis +
  Sidekiq) send the signature-request emails *after* the HTTP response; with
  default throttling the CPU drops to ~zero between requests and the SMTP
  send silently starves. Costs a little more (CPU billed while an instance
  is warm), but the emails are the whole point.
- `HOST` is the domain DocuSeal builds the emailed signing links from — the
  service's run.app host until the LB hostname exists, then
  `sign.medifinance.co.uk`. Without it, emails go out with no usable link.
- The `DATABASE_URL` must use Rails' percent-encoded-socket form —
  `postgres://USER:PASS@%2Fcloudsql%2FPROJECT%3AREGION%3AINSTANCE/docuseal_dev`.
  Django's `?host=/cloudsql/…` style makes Rails' URI parser bail with
  "the scheme postgres does not accept registry part".
- **Prod SMTP**: the medi-finance.co.uk server uses implicit SSL on 465, but
  DocuSeal's env config only speaks STARTTLS — try
  `SMTP_ADDRESS=mail.medi-finance.co.uk,SMTP_PORT=587` (+
  `SMTP_USERNAME=info@medi-finance.co.uk`, `SMTP_PASSWORD=email-host-password`
  secret) first; if 587/STARTTLS isn't accepted, configure SMTP in DocuSeal's
  admin UI (Settings → Email) which exposes more options.
- **Ingress staging**: deploy with `--ingress=all` first and use the printed
  `*.run.app` URL as `HOST` for setup. Once the LB host + cert are ACTIVE
  (11d), redeploy prod with `--ingress=internal-and-cloud-load-balancing` and
  `HOST=sign.medifinance.co.uk`. Dev can stay on `--ingress=all` with the
  run.app URL permanently to skip a cert (set the `DOCUSEAL_URL_DEV` repo
  variable to that URL).

### 11d. Load balancer + DNS (prod; pattern from step 10)

```bash
gcloud compute network-endpoint-groups create docuseal-prod-neg \
  --region="$REGION" --network-endpoint-type=serverless --cloud-run-service=docuseal-prod
gcloud compute backend-services create docuseal-prod-backend --global --load-balancing-scheme=EXTERNAL_MANAGED
gcloud compute backend-services add-backend docuseal-prod-backend --global \
  --network-endpoint-group=docuseal-prod-neg --network-endpoint-group-region="$REGION"
gcloud compute url-maps add-path-matcher medifinance-lb --path-matcher-name=sign \
  --default-service=docuseal-prod-backend --new-hosts=sign.medifinance.co.uk
gcloud compute ssl-certificates create medifinance-sign-cert --domains=sign.medifinance.co.uk --global
# The proxy takes the FULL cert list — listing only the new one would drop the others.
gcloud compute target-https-proxies update medifinance-https-proxy \
  --ssl-certificates=medifinance-app-cert,medifinance-app-dev-cert,medifinance-sign-cert
```

**DNS** (external registrar): an A record `sign` → the `medifinance-lb-ip`
static IP. Cert goes ACTIVE 15–60 min after propagation.

### 11e. DocuSeal console setup (per env)

1. Open the instance URL, create the admin account (`info@medi-finance.co.uk`).
2. Build the agreement templates (field names as above).
3. **Settings → API**: copy the token →
   `gcloud secrets create docuseal-api-token-dev --data-file=-` (and `-prod`).
4. **Settings → Webhooks**: URL `https://app-dev.medifinance.co.uk/webhooks/docuseal/`
   (prod: `app.`), subscribe to `form.viewed`, `form.completed`,
   `form.declined`; **Add Secret** with key `X-Docuseal-Secret` and a generated
   value → `gcloud secrets create docuseal-webhook-secret-dev --data-file=-`.
5. Set the `DOCUSEAL_ENABLED=true` repo variable (and `DOCUSEAL_URL_DEV` if dev
   stays on run.app) and re-run the deploy workflow.

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

- **`DJANGO_ALLOWED_HOSTS`** is exactly the per-env custom domain (see step 10); direct `*.run.app` access is blocked at the ingress level anyway.
- **Migrations** run as a Cloud Run *Job* before each service deploy — never on container start (avoids races across instances).
- **Cost** is dominated by Cloud SQL. `db-f1-micro` is the cheapest; stop/scale via `gcloud sql instances patch`.
- **Secrets** are referenced by name in the workflow, so rotating a value (`gcloud secrets versions add ...`) takes effect on the next deploy without code changes.
