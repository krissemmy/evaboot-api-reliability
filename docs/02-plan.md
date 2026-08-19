# Stage 2 — Implementation plan

## What this is

A black-box reliability and contract probe for Evaboot's public API, driven from a CLI and from CI. It answers three questions without holding any Evaboot credentials:

1. Is the public API surface behaving the way its published contract says it does, right now?
2. Did the published contract change since the last time we looked, and is the change breaking?
3. Does this webhook/job payload match a documented Evaboot response shape?

## Files (10 source files, ~700-900 lines total including tests)

```
evaboot-api-reliability/
├── evaboot_probe/
│   ├── __init__.py
│   ├── cli.py          argparse subcommands, exit codes
│   ├── client.py       httpx wrapper: timeouts, bounded retry/backoff, redaction, attempt counting
│   ├── checks.py       the individual black-box checks
│   ├── schema.py       fetch / normalize / structural-validate / diff + classify
│   ├── webhook.py      validate a payload against spec-derived response schemas
│   └── reporting.py    CheckResult/Report dataclasses + text | json | markdown renderers
├── schemas/
│   └── openapi.baseline.json     committed normalized baseline for `schema diff`
├── tests/
│   ├── test_client_retry.py
│   ├── test_checks.py
│   ├── test_schema_diff.py
│   ├── test_webhook.py
│   └── test_cli.py
├── .github/workflows/reliability.yml
├── docs/01-research-findings.md   (already written)
├── pyproject.toml
├── .gitignore
├── README.md
└── LICENSE  (MIT)
```

Dependencies: **`httpx`** (HTTP) and **`jsonschema`** (OpenAPI 3.1 schemas *are* JSON Schema 2020-12, so webhook validation is a library call, not a hand-rolled validator). Dev: `pytest`, `ruff`. Nothing else. Python 3.12+.

HTTP mocking in tests uses `httpx.MockTransport` — already in `httpx`, no `respx` dependency, and it forces the client to accept an injectable transport, which is the right shape anyway.

## Commands

```
evaboot-probe check   [--format text|json|markdown] [--include-trial] [--timeout 10]
                      [--base-url ...] [--auth-scheme bearer|token] [--max-attempts 3]
evaboot-probe schema fetch [--url ...] [--out schemas/openapi.current.json]
evaboot-probe schema diff  [--baseline schemas/openapi.baseline.json] [--current ...|--fetch]
                           [--fail-on breaking|potentially-breaking|any]
evaboot-probe webhook validate PAYLOAD.json [--spec schemas/openapi.baseline.json]
                                            [--as EmailFinderJobOut|...]
```

### `check` — runs with no credentials

| Check | Assertion | Source |
|---|---|---|
| `openapi.reachable` | `GET /openapi.json` → 200, `application/json`, parses, `openapi` starts with `3.`, `paths` non-empty, `securitySchemes` present; records latency | measured |
| `auth.enforced` | `GET /v1/quota/` with no key → `401` **and** body matches `{success:false, error:{type,message}}` | observed contract |
| `errors.validation_envelope` | `POST /trial/email-validation/` with `{}` → `422` + same envelope | observed contract |
| `errors.non_json_surface` | `GET /v1/nonexistent-probe` → 4xx with `text/html` — recorded as INFO so a future switch to JSON errors shows up as a change | observed contract |
| `ratelimit.headers` | report which of `retry-after`, `x-ratelimit-*`, `ratelimit-*` appear across responses. Currently none → INFO, never FAIL | measured |
| `redirect.trailing_slash` | `GET /v1/quota` → `301` to `/v1/quota/`. INFO; documents that clients must follow redirects | measured |
| `trial.email_validation` (opt-in `--include-trial`) | `POST /trial/email-validation/` `{"email":"noreply@example.com"}` → 200, validates against `EmailValidationTrialOut` | measured |

### `check` — extra checks only when `EVABOOT_API_KEY` is set

| Check | Assertion |
|---|---|
| `auth.scheme_accepted` | try `Bearer`, fall back to `Token`; report which the API accepts (resolves the spec vs help-centre conflict) |
| `quota.readable` | `GET /v1/quota/` → 200, validates against `QuotaOut` from the spec |
| `quota.budget` | report `daily_limit` / `used_today` / `remaining`; WARN when `remaining == 0` or `has_valid_salesnav` is false |

Explicitly **not** checked, and the README says why: `search-builder`, `search-agent`, `extractions/*`, `email-finder/*` bulk, and all `sn/*` endpoints. They spend credits, consume daily budgets a customer needs, run LLM calls, or delete data.

### `schema fetch` / `schema diff`

`fetch` retrieves the spec, structurally validates it, normalizes it (recursive key sort, stable 2-space JSON, trailing newline) and writes it out with a SHA-256. Normalization is what makes the diff signal-bearing instead of noise. No `ETag`/`Last-Modified` is served, so drift detection is content-hash based.

`diff` walks paths → methods → request bodies → responses and classifies:

- **BREAKING** — path removed, method removed, operation's `2xx` response removed, property removed from a response schema, new required request field, security added to a previously open operation.
- **POTENTIALLY BREAKING** — type changed on an existing field, field dropped from a response's `required` list, `anyOf`/nullable widened on a response field, response schema `$ref` swapped.
- **INFO** — path added, method added, optional request field added, new response status code, new response property.
- **Ignored entirely** — `description`, `summary`, `title`, `example(s)`, `operationId`.

Stated limitations: `$ref`s are resolved one document deep with a cycle guard; `oneOf`/`allOf` composition is compared structurally, not semantically; this is a change *classifier*, not an OpenAPI compatibility engine.

### `webhook validate`

Loads the stored spec, builds a `jsonschema` registry over `#/components/schemas/...`, and validates the payload against the documented candidates:

`EmailFinderJobOut`, `EmailFinderJobInProgressOut`, `EmailValidationJobOut`, `EmailValidationJobInProgressOut`, `ExtractionDetailOut`, `ExtractionInProgressOut`.

Auto-detection uses documented discriminators only (`job_type`, presence of `search_id` vs `id`, presence of `prospects`). Output: which shape it matched, missing required fields, type errors, and unexpected top-level keys as INFO. The single assumption — that a webhook body equals the GET-detail body — is printed in the output header and stated in the README, because Evaboot publishes no webhook schema.

## Failure handling (`client.py`)

- Timeouts: explicit `httpx.Timeout(connect=5, read=timeout, write=5, pool=5)`, no unbounded waits.
- Retried: `429`, `500`, `502`, `503`, `504`, connect errors, read timeouts, `RemoteProtocolError`.
- Not retried: every other 4xx (`401/403/404/405/422` are deterministic — retrying them is just latency), `3xx` (surfaced to the caller as data), `501`.
- Backoff: `min(cap, base * 2**attempt)` with full jitter, `base=0.5s`, `cap=8s`, `max_attempts=3` (2 retries), plus a cumulative sleep budget so a CI job can't stall.
- `Retry-After` honoured (delta-seconds and HTTP-date), clamped to 30 s — beyond that we give up rather than hold the runner.
- Every response carries `attempts` and `total_wait_s` into the report, so retries are visible instead of hidden.
- Retrying `POST` is safe here only because the only `POST` we send is a non-mutating trial probe; that reasoning is a comment in the code, not folklore.
- Secrets: key read from `EVABOOT_API_KEY` only, never a flag or file; a redaction filter strips any `Authorization` value and any occurrence of the key from errors and reports.

## Report format and exit codes

`text` (aligned, human), `json` (stable keys, CI-consumable), `markdown` (for the Actions job summary).

- `0` — everything passed (WARN/INFO do not fail)
- `1` — a check failed, or `schema diff` found changes at or above `--fail-on` (default: `breaking`), or webhook validation failed
- `2` — usage or configuration error (bad flag, unreadable file, missing baseline, malformed payload file)
- `3` — the target was unreachable after retries (infrastructure, distinct from "contract violated")

The `3` is an addition to the prompt's scheme: "Evaboot is down / DNS is broken" and "Evaboot changed its contract" should not be the same exit code for whoever is looking at a red CI job.

## CI (`.github/workflows/reliability.yml`)

Triggers: `push`, `pull_request`, `workflow_dispatch`, `schedule` weekly (`17 6 * * 1`), with a `concurrency` group so runs can't overlap.

- **job `test`** — matrix 3.12 / 3.13, `ruff check`, `ruff format --check`, `pytest`. Fully offline. This is the required check.
- **job `probe`** — `schema fetch`, `schema diff` against the committed baseline, `check --format json`, upload report + fetched spec as artifacts, append the markdown report to `$GITHUB_STEP_SUMMARY`. `--include-trial` only on `workflow_dispatch` and `schedule`, never on push/PR, so PR traffic never touches Evaboot's trial endpoint.
- **authenticated step** — one extra `check` run gated on `if: secrets.EVABOOT_API_KEY != ''`, `continue-on-error: true` for forks. The default public CI passes with no secrets configured.

Weekly (not daily/hourly) is the deliberate choice: we are watching for *contract drift* on someone else's API, not measuring their uptime SLO, and we have no business generating recurring load against a bootstrapped company's production API.

## What I am not building

No Terraform, AWS, Kubernetes, database, queue, frontend, auth system, or LLM anything. No metrics backend, no historical latency store, no alerting integration, no SLO math, no async/concurrency layer (sequential requests are fine at this request count), no plugin system, no config file format beyond CLI flags and one env var, no full OpenAPI compatibility engine, no Evaboot client SDK.

## Credential requirements

Works with **no credentials at all**: `openapi.json` reachability + structural validation, the `401`/`422` envelope contracts, HTML-error and redirect behaviour, rate-limit header probe, the whole `schema fetch`/`schema diff` path, all webhook validation, all tests, and both CI jobs.

Requires `EVABOOT_API_KEY`: auth-scheme detection, `GET /v1/quota/` 200-path schema validation, quota budget reporting. All optional, all skipped cleanly with a `SKIP` status when the variable is absent.

## Estimate

Roughly 2-3 hours: client + retry and its tests first (that is where the real engineering is), then schema fetch/diff, then checks and reporting, then webhook validation, then CI and README.

## Revisions applied before implementation

Three scope changes were made to this plan and are reflected in the shipped code:

1. **Every `/trial/*` request is opt-in.** The original plan ran the `{}` -> 422
   validation-envelope probe during a normal `check`, which contradicted the CI
   section. Both trial probes now sit behind `--include-trial`; without it the
   default run is GET-only and sends no POST request to Evaboot at all.
2. **Undocumented error bodies cannot fail a run.** `GET /v1/quota/` returning
   2xx without credentials fails; the expected 401 passes; drift in the exact
   JSON error envelope and in HTML-vs-JSON framework errors is reported at
   INFO/WARN. The OpenAPI document declares no 401/404/405/422 schemas, so
   asserting on them as hard contracts would make an internal Evaboot refactor
   look like a breakage here.
3. **Narrower diff scope.** v1 detects paths, methods, security, required request
   fields, 2xx codes, response schema `$ref`s, response properties and field
   type signatures. Deeper `anyOf`/`oneOf`/`allOf` composition analysis is listed
   under README future improvements instead. Nullable widening does show up,
   because comparing type signatures catches `integer` -> `anyOf(integer,null)`
   for free.
