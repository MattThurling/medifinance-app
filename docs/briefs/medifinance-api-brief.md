# Brief: Medifinance partner API changes to support guest and AI-agent applications

## Context

Medifinance is a UK FCA-regulated medical finance broker. Partner suppliers like the demo store "Acme" call the Medifinance partner API to start a finance application for an equipment purchase. Medifinance then emails the applicant a secure link to complete it. The backend is Django.

Partners are adding two new channels:

1. **Guest applicants:** people who aren't signed in to the partner's site. They type their name, email and company type.
2. **AI agents:** an MCP server run by the partner, called from Claude or ChatGPT, submitting on the user's behalf after the user approves in the chat. Browser agents filling in the partner's web form are also covered here.

These changes mean more submissions, retries, unverified emails and automated callers. This brief is to check the API copes with that, and to make **minimal, additive, backwards-compatible** changes where it doesn't.

**Start by reading the code and reporting.** Before changing anything, produce a short gap analysis against the checklist below: what's already supported, what's missing, and what you propose. Only make changes for real gaps. Existing partner integrations must keep working unchanged.

## What a partner currently sends (from Acme)

Acme's site posts `{ source: 'store', product_id, first_name, last_name, email, ltd }` to its own `/api/finance`. That then calls the Medifinance partner API. Find the actual partner-facing endpoint, its request schema and its auth. Note any mismatch with the fields above.

## Checklist

### 1. Applicants with no prior record
- A first-time applicant (only name, email and company type) can start an application. No existing customer or account is required.
- Repeat submissions for the same email are linked to the existing lead or customer in the CRM rather than creating duplicate people.

### 2. Channel and attribution
- An optional `channel` field: `web`, `web-guest`, `mcp`, `api`, or similar. Store it on the application and lead for reporting and audit.
- The partner is identified from its API key or credentials, never from a request field. Confirm that every application records which partner referred it.
- Optional: `agent_client` (e.g. `claude`, `chatgpt`), if the partner knows it. Make it free text and informational only.

### 3. Asset details
- The request carries what's being financed: description, amount, and optionally a partner product ID and asset category.
- If the API currently expects Medifinance to look up price or product, decide which side is authoritative and document it. The partner should send the price from its own server-side catalogue. Medifinance should validate sensible bounds (min/max finance amount) and return a clear error outside them.

### 4. Idempotency
- Support an `Idempotency-Key` header, or an equivalent field. If the same key is resubmitted within e.g. 24 hours, return the original result and don't create a second application or send a second email. Agents retry.

### 5. Abuse protection
Anyone can now trigger an email to any address. Add:
- Rate limits per partner key, and per recipient email (e.g. no more than N applications per email per hour), returning `429` with a clear message.
- Wording in the applicant email that makes it safe to ignore if unexpected ("If you didn't request this, you can ignore this email").
- Bounce and complaint handling, if the email provider supports it. A note in the report is fine if it's out of scope.

### 6. Responses agents can use
- Success returns a stable `application_reference`, a `status`, and a human-readable `message` approved by Medifinance, e.g. "We've emailed jack@… a secure link to complete your application."
- Errors are structured: `{ "errors": [{ "field": "email", "code": "invalid", "message": "Enter a valid email address" }] }` with correct HTTP status codes (400/401/409/422/429). AI agents read these messages and correct themselves, so make them specific and plain English.
- Optional and only if trivial: `GET /applications/{reference}/status` for partners, scoped to the partner's own applications.

### 7. Compliance (FCA) boundaries
- **All customer-facing finance wording and quotes come from Medifinance's own templates and email.** The API response's `message` must not include representative APR, payment examples or promotional language, unless it's an approved, compliant financial promotion. Assume partners and AI agents will show the message verbatim.
- Log every submission with partner, channel, timestamp, idempotency key and outcome, so it can be audited later.
- Do not record anything in the API that implies the applicant has been advised or that credit is approved. This step only *starts* an application. Regulated steps happen later, through Medifinance's secure link.
- Flag anything in the current flow you think needs a compliance review rather than deciding it in code.

### 8. Sandbox
- If there's no test mode, add one: a sandbox partner key or a `test` flag that runs validation and returns a fake reference without creating real records or sending real emails. Partners need it to test agent flows safely.

### 9. Docs
- Update the partner API docs (or create `docs/partner-api.md`) with the request and response schemas, the new optional fields, idempotency, rate limits, error format and the sandbox. Include an example request for each channel.

## Constraints

- Only make additive changes. No breaking changes to existing fields, endpoints or auth.
- Include Django migrations for any new model fields. Make new fields nullable or defaulted.
- Add tests for: a guest application, an idempotent retry, rate limiting, validation errors and sandbox mode.
- Don't send real emails in tests.

## Deliverable

1. A gap analysis, before any changes.
2. The changes, with tests and migrations.
3. A short summary of the final request and response contract, so the Acme work can be updated to match.
