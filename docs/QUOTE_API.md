# Quote API

`POST /api/quotes/` — given an amount and a term, returns the cheapest available
quote: lender name, monthly payment, APR, flat rate, and total interest.

## Auth

Send your API key in the `Authorization` header:

```
Authorization: Bearer mfk_…
```

Keys are issued by Medifinance staff against your organisation and shown to you
exactly once. Treat them like a password — store in a secrets manager, not in
source. You can hold more than one key at a time so you can rotate cleanly:
issue the new one, switch traffic over, then ask for the old one to be
revoked.

Production and sandbox keys come from separate environments with separate
databases, so a sandbox key is never valid against production (and vice
versa) — call the right base URL for the key you're using.

## Base URL

| Environment | URL                                |
|-------------|------------------------------------|
| Production  | `https://crm.medifinance.co.uk`    |
| Sandbox     | `https://medifinance-dev-3ibfasnqaq-nw.a.run.app` |

## Request

`Content-Type: application/json`

| Field                  | Type    | Required | Notes                                          |
|------------------------|---------|----------|------------------------------------------------|
| `amount`               | number  | yes      | Loan principal in GBP. Must be > 0.            |
| `term_months`          | integer | yes      | Loan term in whole months. Must be > 0.        |
| `commission_percent`   | number  | no       | Optional gross-up on the monthly payment. Defaults to `0`. |

Example:

```json
{
  "amount": 25000,
  "term_months": 60,
  "commission_percent": 1.5
}
```

## Response — `200 OK`

```json
{
  "amount": "25000.00",
  "term_months": 60,
  "commission_percent": "1.50",
  "lender": "BNP Paribas",
  "monthly_payment": "534.69",
  "apr": "10.29",
  "flat_rate": "5.59",
  "total_interest": "7081.40"
}
```

All monetary and percentage values are JSON strings with **2 decimal places**.
Parse them as decimals (not floats) if you're doing further arithmetic — the
values are computed at full precision and rounded only at the end, matching the
broker's pricing spreadsheet.

`lender` is the name of the cheapest available lender for those inputs.

## Errors

All errors are JSON, with an `error` code and human-readable `detail`.

| Status | `error`               | When                                                              |
|--------|-----------------------|-------------------------------------------------------------------|
| 400    | `invalid_json`        | Body isn't valid JSON / not an object.                            |
| 400    | `missing_field`       | `amount` or `term_months` not supplied.                           |
| 400    | `invalid_field`       | Field is the wrong type or out of range (negative, zero, etc.).   |
| 401    | `not_authenticated`   | Missing, malformed, unknown, or revoked bearer token.             |
| 404    | `no_rate_available`   | No active rate band covers this amount + term combination.        |

Example error body:

```json
{
  "error": "no_rate_available",
  "detail": "No active rate band covers this amount and term."
}
```

## curl example

```bash
curl -X POST https://crm.medifinance.co.uk/api/quotes/ \
  -H "Authorization: Bearer mfk_YOUR_KEY_HERE" \
  -H "Content-Type: application/json" \
  -d '{"amount": 25000, "term_months": 60}'
```

## Notes

- The endpoint is **stateless** — nothing is persisted on our side from a quote
  call. Each request is priced against the *current* active rate sheets.
- Quotes are **indicative** and may move when rate sheets are updated. We
  publish updated sheets monthly; expect material price changes to coincide.
- There is no rate limiting today, but every call is logged against your key.
  Sustained abuse will get a key revoked.
- CORS is not enabled — call the API from a server, not a browser.
