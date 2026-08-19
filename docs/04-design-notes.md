# Design notes

Why this tool is shaped the way it is, how it behaves when the API misbehaves,
what it cannot tell you, and what would come next in a production setting.

## Architecture

See the diagram in the [README](../README.md). Six modules, no framework: `client.py` (timeouts, bounded retries, redaction), `checks.py` (the checks), `schema.py` (fetch, normalize, validate, diff), `webhook.py` (payload validation), `reporting.py` (result models and the text/JSON/markdown renderers), `cli.py` (argparse and exit codes). Runtime dependencies are `httpx` and `jsonschema`.

## Failure handling

Evaboot's OpenAPI document declares only 200 and 202 responses. There is no documented 429 behaviour, no documented 5xx behaviour, and no rate-limit headers were observed on any response. **The retry policy here is therefore generic RFC 9110 behaviour, not Evaboot-specific behaviour**, and it is verified against mock transports rather than by provoking their production API.

- **Timeouts** are explicit: 5s connect, 15s read. Nothing waits indefinitely. The read timeout is 15s rather than something tighter because `POST /trial/email-validation/` does real verification work and was measured at 8.7s.
- **Retried:** 429, 500, 502, 503, 504, connect errors, read/write timeouts, protocol errors.
- **Not retried:** every other 4xx. 401, 403, 404, 405 and 422 are deterministic — retrying only adds latency to a job that is already failing. 3xx is returned as data, since the redirect *is* the finding.
- **`Retry-After`** is honoured when present, in both delta-seconds and HTTP-date form, as long as it fits the run's 20s cumulative sleep budget. A longer value is reported rather than awaited, because a probe that sleeps for ten minutes has become the outage.
- **Backoff** is full-jitter exponential: `min(8s, 0.5s * 2^attempt) * random()`, 3 attempts by default, sharing that same 20s budget.
- **Malformed JSON** is a check failure with the status and content type attached, never a traceback. Framework errors on this API are HTML, so any client calling `.json()` on a non-2xx response gets a decode error instead of the real status.
- **Retries are visible.** Every result carries its attempt count; a check that passed on the third try is not the same as one that passed immediately.
- **Retrying POST** is safe here only because the sole POST this tool sends is a non-mutating trial probe. It never creates a job, list or extraction.
- **Secrets** are read from `EVABOOT_API_KEY` only, never logged, and stripped from error messages.

## CI

[`.github/workflows/reliability.yml`](../.github/workflows/reliability.yml) has two jobs:

- **tests** — ruff lint, ruff format check, and pytest on Python 3.12 and 3.13. Every HTTP call is mocked, so this job passes whether or not Evaboot is up, and needs no secrets.
- **live probe** — fetches the current spec, diffs it against the committed baseline, runs the credential-free checks, validates the example webhook payload, and uploads the JSON report plus the fetched spec as artifacts. A breaking contract change fails the job.

`--include-trial` (the only POST) runs on `workflow_dispatch` and `schedule` only, never on push or pull request. The authenticated step is gated on an `EVABOOT_API_KEY` secret being configured and marked `continue-on-error`, so forks and outside contributors are unaffected. The default public CI passes with no secrets at all.

The schedule is weekly, not hourly. This watches for contract drift on an API we do not own; it is not an uptime monitor, and hammering a bootstrapped company's production API on a cron would be rude and would tell us nothing extra.

## Design decisions

**Black-box, from outside.** The interesting failures for an API *consumer* are the ones visible from outside: a schema that changed, a field that vanished, an endpoint that stopped requiring auth. That needs no access to Evaboot's internals, and it is exactly the perspective a worker or automation platform has.

**No database.** The only state worth keeping is the previous contract, and that is one normalized JSON file in git. Git already provides history, review and blame for it; a database would add operations for no signal. Latency numbers are reported per-run and deliberately not persisted — see the extensions below.

**Only documented behaviour can fail.** Asserting on the exact shape of an undocumented 401 body would turn a harmless internal refactor at Evaboot into a red build here. Those probes still run, because drift is worth knowing about, but they report at INFO/WARN.

**Bounded retries.** Unbounded retries turn a slow dependency into a stuck CI job and add load to a service that is already struggling. Three attempts with jitter and a total sleep budget is enough to ride out one blip and short enough to fail fast when it is not a blip.

**Production checks are optional.** The whole default path works with no credentials, so anyone can clone this and get a real answer. Quota reads are additive.

**No mock server.** Evaboot has no sandbox. Rather than fake one, the credential-free surface (the spec, the 401, the redirect, the HTML 404) is treated as the test target, and the mock transports live in the test suite where they belong.

## Limitations

- The retry/429 path is exercised against mocks. Evaboot's real 429 and 5xx behaviour is undocumented and unverified; if they emit `Retry-After` or rate-limit headers, this tool will surface them the first time it sees them.
- The schema diff is a change classifier, not an OpenAPI compatibility engine. It compares paths, methods, security, 2xx codes, response schema `$ref`s, component properties, `required` lists and type signatures. It does not reason about `oneOf`/`allOf` composition semantics, parameter-level changes, inline (non-`$ref`) response schemas, or response media types other than `application/json`.
- Type signature comparison is structural. `integer` → `anyOf(integer,null)` is reported as a type change, which is the right alert but not a real subtype analysis.
- Webhook validation rests on one documented-but-unverified assumption (payload equals the GET detail body) and cannot check signatures, ordering, retries or at-least-once delivery, none of which Evaboot documents.
- Latency numbers are single samples from one location. They are useful for spotting something dramatic, not for percentiles or SLOs.
- Async job behaviour (the 200-vs-202 split, `extraction_id` becoming `search_id` across the POST/GET boundary) cannot be exercised live without spending a customer's credits. It is validated structurally through the webhook command instead.
- `docs.evaboot.com` is not machine-readable from every network — it is Cloudflare-gated and returns HTTP 402 to automated agents — so this tool depends only on `api.evaboot.com/openapi.json`.

## Possible production extensions

Deliberately not implemented here:

- Export check results as Prometheus metrics or OpenTelemetry spans instead of a JSON blob, so latency and failure rate become time series.
- Persist per-run latency to build percentile baselines and alert on regression rather than on a single slow sample.
- Run it as a scheduled synthetic monitor from several regions, with Slack or PagerDuty routing for breaking contract changes.
- Post the contract diff as a PR comment when the baseline is updated, so a schema change gets reviewed like code.
- Full-lifecycle async probing in a staging account with its own credentials: create a job, poll it through 202 to 200, receive the webhook on a disposable endpoint, and assert the delivered payload matches the polled one.

