# Brief: Make the Acme demo store agent-ready (guest checkout, MCP server, in-chat UI)

## Context

Acme (https://acme-amber-beta.vercel.app) is a demo dental equipment store. It shows how a supplier embeds **Medifinance** asset finance. Each product has a "Get Finance" button. It opens a confirm dialog and POSTs to `/api/finance`, which calls Medifinance's partner API. Medifinance then emails the applicant a secure link to complete the application.

The goal is to demo three ways a dentist can get finance, from most to least futuristic:

1. **Inside an AI chat with no browser.** Claude or ChatGPT has an Acme MCP connector installed. The dentist says "I need finance for a dental chair". Product and confirm cards render in the chat, and the application is sent without the dentist leaving the chat.
2. **A browser agent on the site.** An AI agent browses the store and completes the flow reliably.
3. **A human on the site, logged in or not.**

Read the repo first and use its existing stack and conventions. Don't add a framework the repo doesn't already use unless there's no reasonable alternative.

## Current state (verified by inspecting the live site)

- The signed-in customer is hardcoded in an inline script: `const CUSTOMER = { first_name: 'Jack', last_name: 'Jones', email: 'jack@jjdental.com', ltd: true }`. **No logged-out flow exists.**
- Submit sends `POST /api/finance` with `{ source: 'store', product_id, ...CUSTOMER }`.
- Product cards are `<article class="product-card" data-id data-name data-price>`. They contain `.buy-now` and `.get-finance` buttons.
- **Bug:** the closed finance modal (`#finance-modal[hidden]`) still appears in the accessibility tree. An agent reading the page sees "Apply for finance", "Confirm application" and even "Application sent" before anything is clicked. Most likely a CSS `display` rule is overriding `[hidden]`.
- All six finance buttons have the identical accessible name "Get Finance".
- "Buy Now" calls `alert()`. Native dialogs block browser agents.

## Work

### 1. Guest (logged-out) flow on the website

- Replace the hardcoded customer with a demo-switchable one. Read `window.ACME_CUSTOMER` or a demo toggle. If it's null, the user is a guest. Add a visible demo control so both modes can be shown live, e.g. a "Sign out / Sign in as Jack" link in the header, persisted in `localStorage` (wrapped in try/catch), plus `?guest=1` support.
- Signed in: keep the current read-only confirm dialog.
- Guest: the same dialog shows real inputs.
  - First name and last name (`autocomplete="given-name"` / `"family-name"`).
  - Email (`type="email"`, `autocomplete="email"`).
  - Company type as a native `<select>` (Limited company / Sole trader / Partnership). Map it to the `ltd` boolean or a richer field if the Medifinance API supports one.
  - Every input needs a `<label for>`. Use native validation plus inline error text.
- Keep the product and price line ("ErgoDent Signature Surgery Package — £32,750") at the top of the dialog in both modes.
- Success state: show Medifinance's message and the **application reference**, if the API returns one.

### 2. Agent-friendliness fixes

- Fix the hidden modal so it's absent from the accessibility tree when closed (`[hidden] { display: none !important; }`, or scope the rule correctly). Give the dialog `role="dialog"`, `aria-modal="true"` and `aria-labelledby`, and move focus into it on open.
- Give each finance button a unique name, e.g. `aria-label="Get finance for ErgoDent Signature Surgery Package (£32,750)"`. Do the same for Buy Now.
- Replace the `alert()` on Buy Now with an inline message or toast.
- Use semantic, stable markup: real `<button type="button">` elements, and no click handlers on divs.

### 3. Server-side `/api/finance`

- Accept both signed-in and guest payloads. Validate server-side: required fields, email format, and a product_id that exists. Look up the price from the product catalogue on the server. Never trust a client-supplied amount.
- Return structured errors: `{ ok: false, errors: [{ field, message }] }`. Agents read these and correct themselves.
- Add a basic rate limit for guest submissions, per IP and per email. In-memory is fine for the demo, but leave a comment noting that production needs a shared store.
- Pass `channel` through to Medifinance: `web`, `web-guest` or `mcp`. Also pass an idempotency key. See the Medifinance brief for the contract, and check what the API actually supports before relying on it.
- Move the product catalogue into one shared module or JSON file used by the page, `/api/finance` and the MCP server.

### 4. Acme MCP server with in-chat UI (the main deliverable)

Add a remote MCP server to the same Vercel project, e.g. at `/api/mcp` or `/mcp`.

- **Transport:** Streamable HTTP, stateless, public HTTPS. ChatGPT requires remote Streamable HTTP. Use the official MCP SDK for the repo's language (most likely `@modelcontextprotocol/sdk` on Vercel/Node).
- **Auth:** none for the demo, because the guest flow needs no account. Structure the code so OAuth can be added later for "act as the signed-in customer". Note this in the README.
- **Tools.** Descriptions matter: models choose tools from them. Write them for the case "a UK dental or medical practice wants to buy or finance equipment".
  - `search_products(query?, max_price?)`. Read-only (`readOnlyHint: true`). Returns matching products (id, name, description, price) and renders a **product card UI**.
  - `get_product(product_id)`. Read-only.
  - `request_finance(product_id, first_name, last_name, email, company_type)`. A write action: set the annotations so clients ask the user for approval (`readOnlyHint: false`, `destructiveHint: false`, `idempotentHint: false`, `openWorldHint: true`). It calls the same server-side logic as `/api/finance` with `channel: 'mcp'`. It returns the Medifinance message and reference, and renders a **confirmation card**.
- **In-chat UI with MCP Apps:** use the MCP Apps extension (the standard Claude supports, which ChatGPT's Apps SDK builds on) so the tools return interactive cards rather than plain text.
  - Product card: name, price, short description, and a "Get finance" action.
  - Finance card: product and price, guest fields for anything not yet supplied, and a Confirm button that calls `request_finance`.
  - Success card: reference number and "check your inbox".

  **Check the current MCP Apps spec and SDK before implementing** (`modelcontextprotocol/ext-apps` on GitHub, plus OpenAI's Apps SDK docs). Don't guess the resource MIME types or the `_meta` keys. Keep the cards self-contained HTML with inline CSS, so they match the site's look. Every tool must also return a sensible text result, so clients without UI support still work.
- Respect the compliance boundary. The cards and tool text must not invent finance terms, APRs or monthly payments, unless they come from the Medifinance API. Use Medifinance's own wording and its email.

### 5. Discovery (forward-looking, cheap)

- Publish `/.well-known/mcp/server-card.json` describing the server: name, description, endpoint URL, transport and tools. Follow the current MCP Server Card proposal (SEP-1649 / SEP-2127). Check its latest format.
- Add a `Link: </.well-known/mcp/server-card.json>; rel="mcp"` response header on the site's pages (via `vercel.json` headers), and a matching `<link rel="mcp">` in the HTML head.

### 6. Docs and testing

- Add a README section covering: how to run locally, the MCP endpoint URL, how to add it as a custom connector in Claude (Settings → Connectors → Add custom connector) and in ChatGPT (Developer mode), and the demo script below.
- Test the MCP server with MCP Inspector (`npx @modelcontextprotocol/inspector`): list the tools, call each one, and check the UI resources resolve.
- Test the site in a headless browser (Playwright). Cover the signed-in and guest flows, check the closed modal isn't in the accessibility tree, and check validation errors show.
- Don't submit real applications to production Medifinance during tests. Use a sandbox or mock mode if the partner API has one. If not, add a `MEDIFINANCE_MOCK=1` env flag that returns a fake reference.

## Demo script this must support

1. In Claude, with the Acme connector added, in a fresh chat: "I'm fitting out a new surgery and need a dental chair — can I spread the cost?"
2. A product card for the ErgoDent Signature Surgery Package (£32,750) appears.
3. "Yes, finance that one — I'm Jack Jones, jack@jjdental.com, limited company."
4. A confirm card appears, the user approves, and a success card shows the reference. The Medifinance email arrives.
5. The same request made by a browser agent on the website as a guest also completes cleanly.

## Out of scope

Real customer accounts and login, payments, and changes to the Medifinance API itself (that's a separate brief). Flag any API gaps you hit rather than working around them in Acme.
