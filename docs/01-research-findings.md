# Stage 1 — Evaboot public API: research findings

All statements below are tagged:

- **[SPEC]** — read directly from Evaboot's published OpenAPI document (`https://api.evaboot.com/openapi.json`, retrieved 2026-08-19).
- **[OBSERVED]** — measured by sending a small number of safe requests to the live API on 2026-08-19.
- **[SECONDARY]** — from Evaboot's marketing page / help centre / search-indexed doc pages, not from the spec.
- **[ASSUMPTION]** — not documented anywhere; our design choice.

## 1. What is publicly available

| Artifact | Status |
|---|---|
| `https://api.evaboot.com/openapi.json` | **Reachable, unauthenticated, HTTP 200, 51 067 bytes, `content-type: application/json`, OpenAPI `3.1.0`** [OBSERVED] |
| `https://docs.evaboot.com/...` (human docs) | **Not machine-reachable from here.** TCP + TLS complete, then the HTTP request is dropped (curl, headless Chromium: 15 s timeout). Anthropic's fetcher gets `HTTP 402 Payment Required` — the signature of Cloudflare pay-per-crawl for AI agents. [OBSERVED] |
| `https://api.evaboot.com/v1/docs/`, `/redoc`, `/schema/` | 404 [OBSERVED] |

Consequence: **the OpenAPI document is the only reliable machine-readable source of truth**, and it is the right thing to build on. The docs site is rendered from that same spec (its URL structure — `/schema/account`, `/schema/email-verifier/email_validation_retrieve` — mirrors the spec's tags and operation ids), so we lose almost nothing. The tool must not depend on `docs.evaboot.com`.

`api.evaboot.com` answers with `server: uvicorn` and no Cloudflare headers, so the schema URL our tool uses is not behind the bot gate.

## 2. Base URL, transport, auth

- Base URL: `https://api.evaboot.com/v1` [SECONDARY: evaboot.com/api] — consistent with every path in the spec being `/v1/...` [SPEC].
- `servers: []` in the spec — **the spec does not declare its own base URL** [SPEC]. Our tool has to supply it.
- Security scheme: one scheme, `PublicBearer`, `type: http`, `scheme: bearer` → `Authorization: Bearer <key>` [SPEC].
- **Documentation conflict:** the help-centre article says `Authorization: Token your_api_token_here` [SECONDARY], the spec says bearer [SPEC]. One of the two is stale. Design response: default to `Bearer`, allow `--auth-scheme token`, and when a key is present report which scheme the API actually accepts. That turns a doc inconsistency into a check instead of a guess.
- Every `/v1/*` operation is secured. The only two unsecured operations are `POST /trial/email-finder/` and `POST /trial/email-validation/` [SPEC].

## 3. Endpoint surface (43 operations, 45 schemas) [SPEC]

Grouped by tag:

- `extractions` — `POST /v1/extractions/url/`, `POST /v1/extractions/profiles/`, `POST /v1/extractions/single/`, `GET /v1/extractions/`, `GET /v1/extractions/{extraction_id}/`
- `email-finder` — `POST /v1/email-finder/`, `POST /v1/email-finder/single/`, `GET /v1/email-finder/`, `GET /v1/email-finder/{job_id}/`
- `email-validation` — same four shapes under `/v1/email-validation/`
- `quota` — `GET /v1/quota/`  ← this is what the docs site calls "Account"
- `search-builder` — `POST /v1/search-builder/`
- `search-agent` — `POST /v1/search-agent/`, `GET /v1/search-agent/jobs/{job_id}`
- `sales-navigator` — 21 operations under `/v1/sn/*` (lead lists, account lists, saved searches, alerts, search-to-list jobs), including 3 `DELETE`s
- `trial` — `POST /trial/email-finder/`, `POST /trial/email-validation/` (no auth)

**There is no public mock or sandbox API.** The `trial/*` endpoints are the closest thing: real work, no credentials.

## 4. Async model — this is the interesting part [SPEC]

The spec documents **only `200` and `202`**. No 4xx, no 5xx, no 429, anywhere.

Bulk endpoints return `202 + job_id`; the client polls the detail endpoint:

| Operation | 200 | 202 |
|---|---|---|
| `POST /v1/extractions/url/`, `.../profiles/` | — | `ExtractionCreateOut` (`extraction_id`) |
| `GET /v1/extractions/{id}/` | `ExtractionDetailOut` | `ExtractionInProgressOut` |
| `POST /v1/email-finder/` | — | `EmailFinderJobAcceptedOut` (`job_id`) |
| `GET /v1/email-finder/{job_id}/` | `EmailFinderJobOut` | `EmailFinderJobInProgressOut` |
| `POST /v1/email-validation/` | — | `EmailValidationJobAcceptedOut` (`job_id`) |
| `GET /v1/email-validation/{job_id}/` | `EmailValidationJobOut` | `EmailValidationJobInProgressOut` |

Two contract traps worth naming in the README, both straight from the spec text:

1. **The same GET returns two different schemas depending on status.** `202` carries a reduced object (no `prospects`). A worker that treats any `2xx` as "job finished" reads an empty result set as a completed empty result.
2. **The id field is renamed across the boundary.** `POST` returns `extraction_id`; every `GET` exposes the same value as `search_id` (the spec's own description says so).

## 5. Webhooks [SPEC + SECONDARY]

- `webhook_url` is an **optional request field** on exactly four inputs: `UrlExtractionCreateIn`, `ProfileExtractionCreateIn`, `EmailFinderJobIn`, `EmailValidationJobIn` [SPEC].
- **The spec contains no webhook payload schema, no signature header, no retry/delivery documentation.** [SPEC]
- The docs site states the webhook "will receive the same data as the GET detail endpoint" [SECONDARY — search-index snippet; the page itself is not fetchable].

Design response: **do not write a webhook schema by hand.** Validate a supplied payload against the *documented GET-detail response schemas pulled from the spec* (`EmailFinderJobOut`, `EmailValidationJobOut`, `ExtractionDetailOut`, plus the three `...InProgressOut` variants), and report which documented shape it satisfies. Zero invented fields; the one assumption (payload ≈ GET detail body) is stated in the output and the README.

## 6. Rate limiting — the prompt's assumption does not match reality

Measured on both a `401` and a `200` response: **no `Retry-After`, no `X-RateLimit-*`, no `RateLimit-*`, no rate-limit headers of any kind.** [OBSERVED]

What the API actually does [SPEC, from operation descriptions]:

- Limits are **daily quotas**, per user and per Sales Navigator seat, not per-second HTTP rate limits. `LinkedInAccount.daily_export_limit` defaults to **2500** profiles / 24 h.
- `POST /v1/search-builder/` is capped by `SEARCH_BUILDER_DAILY_LIMIT`; `POST /v1/search-agent/` by a **20/day** budget that is charged *before* the work runs (so failed turns still cost); `POST /v1/sn/lead-lists/from-search` by `SN_SEARCH_TO_LIST_DAILY_LIMIT`.
- Poll endpoints (`GET /v1/search-agent/jobs/{id}`, `GET /v1/sn/jobs/{id}`) are **deliberately not metered**, so polling every few seconds is the intended pattern.
- The quota state is readable at `GET /v1/quota/`: `daily_limit`, `used_today`, `remaining`, `credits`, `has_valid_salesnav`, plus per-seat `salesnavs[]`.

**Design change:** replace "rate limit headers present: yes" with two honest signals — (a) a header probe that reports which rate-limit header families are present (currently: none), and (b) a quota-budget check via `GET /v1/quota/` when a key is supplied. Header absence is reported as INFO, not FAIL; if headers ever appear, that is itself a contract change worth surfacing.

## 7. Error behaviour (measured, undocumented) [OBSERVED]

| Request | Status | Content-Type | Body |
|---|---|---|---|
| `GET /v1/quota/` no auth | `401` | `application/json` | `{"success": false, "error": {"type": "AuthenticationFailed", "message": "Unauthorized"}}` |
| `GET /v1/quota/` bogus bearer | `401` | `application/json` | identical (no key echoed, no distinction from missing key) |
| `POST /trial/email-validation/` body `{}` | `422` | `application/json` | `{"success": false, "error": {"type": "ValidationError", "message": "email: Field required"}}` |
| `POST /trial/email-validation/` `{"email":"noreply@example.com"}` | `200` | `application/json` | `{"email": "noreply@example.com", "status": "invalid"}` |
| `GET /v1/search-builder/` (POST-only path) | `405` | `text/html` + `allow: POST` | `Method not allowed` |
| `GET /v1/quota` (no trailing slash) | `301` → `/v1/quota/` | `text/html` | empty |
| `GET /v1/nonexistent` | `404` | `text/html` | dashboard "We couldn't find that" page |

Three findings that matter for a client library:

1. **Application errors are a consistent JSON envelope** (`{success:false, error:{type,message}}`) — worth asserting as a contract, even though the spec omits it.
2. **Framework-level errors are HTML** (`404`, `405`, `301`). A client that does `resp.json()` on any non-2xx will raise a JSON decode error instead of surfacing the real status.
3. **Missing trailing slashes 301-redirect.** `httpx` does not follow redirects by default, and a redirected `POST` is a silent no-op class of bug. Worth a check.

`GET /openapi.json` returns **no `ETag`, no `Last-Modified`, no `Cache-Control`** [OBSERVED] → schema drift detection must be content-hash based; conditional GET is not available.

## 8. What in the original prompt has to change

| Prompt assumption | Reality | Change |
|---|---|---|
| "rate-limit headers present: yes" | No rate-limit headers exist | Probe and report header families (expect none); add quota-budget check via `GET /v1/quota/` |
| "Account endpoint 200" as a default check | `GET /v1/quota/` needs a key | Unauthenticated run asserts `401` + JSON error envelope; the `200` path runs only with `EVABOOT_API_KEY` |
| "Search Builder 200" as a default check | Auth + daily-limited + an LLM call per request | Excluded entirely. Not worth burning a customer's daily budget for a health check |
| "public mock API as default target" | None exists | Default target = the unauthenticated public surface: `openapi.json`, the `401` envelope, the `422` envelope, the redirect behaviour. `trial/*` behind an explicit `--include-trial` flag |
| "429 / Retry-After handling" | Not documented; no 429 ever observed | Still implemented (generic RFC 9110 semantics) and **tested against mock transports**, with the README stating clearly that Evaboot's 429 behaviour is unverified |
| "webhook payload validation" | No webhook schema published | Validate against spec-derived GET-detail schemas; state the one assumption |
| "response schema changed" detection | Feasible — spec is rich and stable-looking | Keep, scoped and with limitations documented |

## 9. Safety boundaries observed during research

Total requests sent to Evaboot: 14 GETs (mostly 404 path probes) and 2 POSTs to `trial/email-validation/` — one with `{}`, one with `noreply@example.com` (RFC 2606 reserved domain, no MX, no real mailbox touched). No emails sent, no jobs created, no credits spent, no `DELETE` or `sn/*` endpoint touched.
