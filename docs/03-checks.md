# Checks and contract reference

What each command asserts, how contract changes are classified, and what the exit
codes mean. Start from the [README](../README.md) for installation and usage.

## What it checks

Credential-free, GET-only by default:

| Check | What it asserts | Fails the run? |
| --- | --- | --- |
| `openapi.document` | `GET /openapi.json` is 200 JSON, parses, is OpenAPI 3.x, has paths, schemas and security schemes, and no dangling `$ref` | yes |
| `auth.enforced` | `GET /v1/quota/` without credentials does **not** return 2xx | yes |
| `auth.error_envelope` | the 401 body still matches the observed `{success, error:{type, message}}` shape | no — INFO/WARN |
| `errors.unknown_path` | records the status and content type of an unknown path (currently `404 text/html`) | no — INFO |
| `redirect.trailing_slash` | records that `/v1/quota` 301-redirects to `/v1/quota/` | no — INFO |
| `ratelimit.headers` | reports which rate-limit header families appear (currently none) | no — INFO |
| `trial.email_validation` | *(opt-in)* `POST /trial/email-validation/` returns 200 matching `EmailValidationTrialOut` | yes |
| `trial.validation_envelope` | *(opt-in)* an empty body returns 422 with the observed error envelope | no — INFO/WARN |
| `auth.scheme_accepted` | *(needs a key)* whether the API accepts `Bearer` or `Token` | yes |
| `quota.readable` | *(needs a key)* `GET /v1/quota/` returns 200 matching the documented `QuotaOut` | yes |
| `quota.budget` | *(needs a key)* daily quota remaining, and whether a Sales Navigator seat is attached | no — WARN |

Only behaviour Evaboot actually documents can fail a run. The 401/404/405/422 bodies are not in the OpenAPI document, so drift there is reported and never fails.

Endpoints this tool never calls: `extractions/*`, bulk `email-finder/*` and `email-validation/*`, `search-builder`, `search-agent`, and everything under `sn/*`. They spend credits, consume a daily quota a customer needs, run LLM calls, or delete data. A health check has no business doing any of that.

## Command reference

```
evaboot-probe check [--include-trial] [--format text|json|markdown]
                    [--auth-scheme bearer|token] [--timeout 10] [--max-attempts 3]
```
Runs every applicable check. Without `--include-trial` it sends no POST request to Evaboot at all.

```
evaboot-probe schema fetch [--out schemas/openapi.current.json]
```
Fetches the live document, structurally validates it, and writes a normalized copy (recursively key-sorted, 2-space JSON) with its SHA-256. Normalization is what makes the diff mean something: without it, a serializer change looks like a contract change.

```
evaboot-probe schema diff [--baseline schemas/openapi.baseline.json]
                          [--current FILE | (fetch live)]
                          [--fail-on breaking|potentially-breaking|any]
```
Classifies every difference as INFO, POTENTIALLY BREAKING or BREAKING:

| Change | Level |
| --- | --- |
| Path or method removed; 2xx response removed; response field removed; new required request field; security added | BREAKING |
| Response schema `$ref` swapped; field type changed (including nullable widening); response field dropped from `required` | POTENTIALLY BREAKING |
| Path, method, field or 2xx response added; request field removed; security removed | INFO |

Field-level changes are attributed by usage: a field vanishing from a response breaks readers, while the same field vanishing from a request body just means clients can stop sending it. Because reachability is computed transitively through `$ref`s, a field removed from a nested component is still reported against the responses that expose it:

```
BREAKING (1)
  EmailFinderProspectOut.company_name
    Field removed from response schema
```

```
evaboot-probe webhook validate PAYLOAD.json [--spec ...] [--as SCHEMA]
```
Validates a payload against the documented response schemas — `EmailFinderJobOut`, `EmailValidationJobOut`, `ExtractionDetailOut` and their in-progress variants — using `jsonschema` (OpenAPI 3.1 schemas *are* JSON Schema 2020-12, so the spec document is the schema document).

**This is where the one real assumption lives.** Evaboot publishes no webhook payload schema: `webhook_url` exists only as an optional request field on four inputs, and there is no documented signature header, retry policy or delivery guarantee. Their docs site states the webhook receives the same data as the job's GET detail endpoint, so that is what gets validated, and the assumption is printed with every result. Nothing here is invented — every field checked comes from the fetched spec.

Shape detection uses documented discriminators only (`job_type`, `search_id`, presence of `prospects`). A payload carrying `prospects` is judged against the completed schema and never falls back to the in-progress one, because the in-progress schema is a strict subset: "whichever schema produces fewest errors" would grade a malformed completed payload as a valid in-progress one.

### Exit codes

| Code | Meaning |
| --- | --- |
| 0 | passed (WARN, INFO and SKIP do not fail a run) |
| 1 | a documented contract was violated, or a diff hit `--fail-on` |
| 2 | usage or configuration error (bad path, unreadable baseline, unknown schema name) |
| 3 | the target could not be reached at all after retries |

`3` is separate on purpose. "Evaboot is unreachable from this runner" and "Evaboot changed its response schema" are different incidents with different owners, and a red CI job should say which one happened.

