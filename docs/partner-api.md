# Partner API — starting a finance application

`POST /api/deals/` starts a Medifinance finance application for a customer
your organisation has introduced. Medifinance records the application, then
emails the applicant a secure, single-use link to complete it. **This call
only starts an application.** Nothing is agreed, approved or charged at this
stage; every regulated step happens later, through Medifinance's own secure
link and templates.

The endpoint supports three kinds of caller on the same contract:

| Channel     | Who is submitting                                                | `channel` |
|-------------|------------------------------------------------------------------|-----------|
| Signed-in   | Your website, for a customer with an account on your site        | `web`     |
| Guest       | Your website, for a visitor who typed name, email, company type  | `web-guest` |
| AI agent    | Your MCP server (Claude, ChatGPT…) or a browser agent on your site | `mcp`   |
| Server      | A back-office integration with no end-user interaction           | `api`     |

For the quote endpoint see [`QUOTE_API.md`](QUOTE_API.md).

## Auth

```
Authorization: Bearer mfk_…
```

Keys are issued by Medifinance staff against your organisation and shown to
you once. **Your organisation is identified from the key**, never from the
request body — every application records which partner introduced it. Keep
the key server-side; never call the API from a browser or an agent directly.

Ask Medifinance for a **sandbox key** as well (see [Sandbox](#sandbox)).

## Base URL

| Environment | URL                                   |
|-------------|---------------------------------------|
| Production  | `https://app.medifinance.co.uk`       |
| Development | `https://app-dev.medifinance.co.uk`   |

## Request

`POST /api/deals/` · `Content-Type: application/json`

| Field            | Type    | Required | Notes |
|------------------|---------|----------|-------|
| `name` *or* `description` | string | yes | What is being financed, ≤255 chars. `description` is a synonym for `name`; send either. Becomes the deal name in the CRM. |
| `amount`         | number  | yes      | Price in GBP from **your** server-side catalogue. Must be within the configured bounds (default £1,000–£10,000,000) or the API returns 422. |
| `first_name`     | string  | yes      | ≤150 chars |
| `last_name`      | string  | yes      | ≤150 chars |
| `email`          | string  | yes      | Must be a valid email address. This is where the secure link is sent. |
| `ltd`            | boolean | yes, unless `company_type` is sent | v1 field: is the business a limited company? |
| `company_type`   | string  | no       | `limited_company`, `sole_trader`, `partnership` or `other`. If sent without `ltd`, `ltd` is derived from it. Prefer this over `ltd` for new integrations. |
| `channel`        | string  | no       | `web`, `web-guest`, `mcp` or `api`. Stored on the application for reporting and audit. |
| `agent_client`   | string  | no       | ≤64 chars, free text, informational: the AI client that submitted, e.g. `claude`, `chatgpt`. |
| `product_id`     | string  | no       | ≤100 chars: your own product/SKU id for the asset. |
| `asset_category` | string  | no       | ≤100 chars: your own category, e.g. `imaging`, `surgery`. |

Optional header:

| Header            | Notes |
|-------------------|-------|
| `Idempotency-Key` | ≤255 chars, unique per application attempt (a UUID is ideal). See [Idempotency](#idempotency). Strongly recommended for agents and anything that retries. |

**Who is authoritative for the price?** You are. Medifinance does not look up
your products; send the amount from your server-side catalogue, never from the
browser or from the agent's conversation. Medifinance validates the bounds and
nothing else about the price.

## Responses

### `201 Created` — a new application was started

```json
{
  "id": 42,
  "name": "ErgoDent Signature Surgery Package — Acme Ltd",
  "amount": "32750.00",
  "deduplicated": false,
  "application_reference": "MF-42",
  "status": "awaiting_customer",
  "message": "We've emailed jack@jjdental.com a secure link to complete the finance application. Nothing is agreed or charged at this stage.",
  "sandbox": false
}
```

### `200 OK` — a recent application for this applicant already exists

A repeat for the same applicant email from the same partner within 24 hours
returns the existing application instead of starting another. No second
email is sent.

```json
{
  "id": 42,
  "name": "ErgoDent Signature Surgery Package — Acme Ltd",
  "amount": "32750.00",
  "deduplicated": true,
  "application_reference": "MF-42",
  "status": "awaiting_customer",
  "message": "We already have a recent application for jack@jjdental.com, so we haven't started another one. The secure link to complete it was emailed earlier — please check your inbox (and spam folder).",
  "sandbox": false
}
```

| Field | Notes |
|-------|-------|
| `application_reference` | Stable public reference (`MF-<id>`). Show it to the customer and use it for `status` lookups. |
| `status` | Always `awaiting_customer` from this endpoint: the applicant must open their link. (`sandbox` for sandbox keys.) |
| `message` | Customer-facing wording **approved by Medifinance**. Show it verbatim. It never contains rates, payment examples or promotional language, and you must not add any. |
| `id`, `name`, `amount`, `deduplicated` | The v1 fields, unchanged. `amount` is a 2-dp string. |
| `sandbox` | `true` only for sandbox keys. |

### Errors

Every error is JSON with a flat `error`/`detail` (the v1 shape) **and** a
structured `errors` list. Agents should read `errors`: each entry names the
offending field (or `null` for request-level problems), a short `code`, and a
plain-English `message` that says how to fix it.

```json
{
  "error": "invalid_field",
  "detail": "`email` must be a valid email address.",
  "errors": [
    { "field": "email", "code": "invalid", "message": "`email` must be a valid email address." }
  ]
}
```

| Status | `error` | `errors[].code` | When |
|--------|---------|-----------------|------|
| 400 | `invalid_json` | `invalid_json` | Body isn't a JSON object. |
| 400 | `missing_field` | `required` | A required field is absent. |
| 400 | `invalid_field` | `invalid` | Wrong type, blank, too long, bad email, or a value not in the allowed list (the message lists the allowed values). |
| 401 | `not_authenticated` | — | Missing, malformed, unknown or revoked key. |
| 409 | `idempotency_key_reused` | `reused` | The `Idempotency-Key` was already used with a different body. |
| 422 | `amount_out_of_range` | `out_of_range` | `amount` is outside the configured bounds. |
| 429 | `rate_limited` | `rate_limited` | Your key has started too many new applications (default 5/hour, 25/day). |
| 429 | `email_rate_limited` | `rate_limited` | Too many new applications for one applicant email across all partners (default 3/hour). `field` is `email`. |
| 503 | `api_disabled` | `api_disabled` | Medifinance has paused the partner API. |

Only `429` and `503` are worth retrying automatically, after a delay. A `409`
means a bug in your idempotency-key handling, not a transient fault.

## Idempotency

Agents and serverless functions retry. To make a retry safe, send an
`Idempotency-Key` header that is unique to the *attempt* — a fresh UUID each
time a user confirms, reused only for retries of that same confirmation.

- Same key + same body within 24 hours → the **original successful response**
  is returned verbatim (same status, same `application_reference`). No second
  application, no second email.
- Same key + different body → `409 idempotency_key_reused`.
- A previous attempt that failed (4xx/5xx) is not replayed; the retry is
  processed afresh.
- Keys are scoped to your API key.

Without the header you still get the 24-hour per-applicant dedup described
above, which covers most accidental double-submits.

## Rate limits

| Limit | Default | Scope |
|-------|---------|-------|
| New applications per key | 5/hour, 25/day | Your key. Dedup'd repeats and idempotent replays don't count. |
| New applications per applicant email | 3/hour | Across all partners. Stops one inbox being flooded with links. |

Both return `429` with a `message` you can show. Guest and agent channels
should add their own limits upstream (per IP, per session) so a single abusive
visitor can't exhaust your key's allowance.

## Sandbox

Medifinance can issue your organisation a **sandbox key** (same `mfk_` format,
flagged sandbox on our side). With a sandbox key, `POST /api/deals/`:

- runs the full validation, returning the same 400/422/409 errors as production;
- returns `201` with `"sandbox": true`, `"status": "sandbox"` and a fake
  `application_reference` of the form `MF-SANDBOX-XXXXXXXX`;
- creates **no** application, contact or quote, and sends **no** email;
- honours `Idempotency-Key` replay, so you can test your retry logic;
- is still logged, so Medifinance can help you debug.

`GET /api/applications/MF-SANDBOX-…/status/` with the sandbox key returns
`"status": "sandbox"`. Use the sandbox key for all automated tests and agent
development; never point tests at a production key.

## Application status

`GET /api/applications/{application_reference}/status/`

Returns a coarse status for an application **your organisation introduced**.
Anything else is `404`.

```json
{
  "application_reference": "MF-42",
  "status": "awaiting_customer",
  "stage": "application",
  "name": "ErgoDent Signature Surgery Package — Acme Ltd",
  "amount": "32750.00",
  "channel": "web-guest",
  "created_at": "2026-10-10T11:02:03+00:00",
  "sandbox": false
}
```

| `status` | Meaning |
|----------|---------|
| `awaiting_customer` | The applicant hasn't completed their secure link yet. |
| `in_progress` | Medifinance is working the application. |
| `closed` | The application is lost, dormant or settled. |
| `sandbox` | A sandbox reference. |

`stage` is Medifinance's internal pipeline stage, for information only. The
status endpoint **never** indicates whether credit has been approved or on
what terms; Medifinance tells the customer that directly.

## Compliance boundary

Medifinance is FCA-regulated; you and any AI agent acting for you are not
giving regulated advice when you call this API.

- Show `message` verbatim. Don't paraphrase it, and don't add representative
  APRs, monthly payment examples, "0% finance", "instant approval" or similar.
  If you need a figure for a quote, it must come from Medifinance's own
  quote endpoint and be presented under Medifinance's wording.
- Never tell the customer they have been approved, pre-approved or advised
  because this call succeeded. It only starts an application.
- The applicant's email receives Medifinance's own link email, which names
  your organisation and explains that it can be ignored if unexpected.
- Every submission is logged by Medifinance with partner, channel, timestamp,
  idempotency key and outcome, for audit.

## Example requests

Signed-in customer on your site:

```bash
curl -X POST https://app.medifinance.co.uk/api/deals/ \
  -H "Authorization: Bearer mfk_YOUR_KEY" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: 4b0a6e58-3f7c-4b7e-9d1f-0a6c2f8d9e11" \
  -d '{
    "description": "ErgoDent Signature Surgery Package — Acme Ltd",
    "amount": 32750,
    "first_name": "Jack", "last_name": "Jones",
    "email": "jack@jjdental.com",
    "company_type": "limited_company",
    "channel": "web",
    "product_id": "chair-package", "asset_category": "surgery"
  }'
```

Guest on your site (no account; your server validated the form and looked up the price):

```json
{
  "description": "ScanRite IO Intraoral Scanner — Acme Ltd",
  "amount": 18950,
  "first_name": "Priya", "last_name": "Patel",
  "email": "priya@example-dental.co.uk",
  "company_type": "sole_trader",
  "channel": "web-guest",
  "product_id": "intraoral-scanner"
}
```

AI agent via your MCP server (the user approved the action in chat):

```json
{
  "description": "ErgoDent Signature Surgery Package — Acme Ltd",
  "amount": 32750,
  "first_name": "Jack", "last_name": "Jones",
  "email": "jack@jjdental.com",
  "company_type": "limited_company",
  "channel": "mcp",
  "agent_client": "claude",
  "product_id": "chair-package"
}
```

Server-to-server (v1 payload, still valid):

```json
{
  "name": "Dental chair refit",
  "amount": 25000,
  "first_name": "Jane", "last_name": "Smith",
  "email": "jane@example.com",
  "ltd": true
}
```

## Notes

- All amounts in responses are 2-dp strings; parse as decimals.
- CORS is not enabled: call from a server, never a browser.
- Bounce and complaint handling for the applicant email is not exposed via
  the API. Medifinance monitors delivery on its side.
