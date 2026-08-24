# Evaboot API Reliability Probe

Enrichment and agentic workflows lean on three things that fail quietly: HTTP APIs, asynchronous jobs, and webhooks. When a response field disappears or a job's success shape changes, the breakage usually surfaces inside a worker, hours later, as an empty result set rather than an error.

This is a small black-box probe around [Evaboot's](https://evaboot.com) public API. It checks that the live public surface behaves the way the published contract says it does, reports when that contract changes and whether the change is breaking, and validates async-job/webhook payloads against the documented response schemas. It holds no credentials, keeps no state beyond one committed baseline file, and never calls an endpoint that spends money.

```
GitHub Actions / CLI
        |
        v
 evaboot-probe
   |    |    |
   |    |    +--> webhook validation   (payload vs documented response schemas)
   |    +-------> OpenAPI contract diff (live spec vs committed baseline)
   +------------> reliability checks    (status, latency, retries, headers)
                      |
                      v
              api.evaboot.com
```

Evaboot's OpenAPI document is public and unauthenticated at `https://api.evaboot.com/openapi.json` (OpenAPI 3.1, 37 paths, 43 operations, 45 component schemas), and it is the source of truth for everything here.

## Quickstart

With [uv](https://docs.astral.sh/uv/):

```bash
uv sync --extra dev
uv run evaboot-probe check
uv run pytest -q
```

Or with the stdlib toolchain, if you would rather not install anything extra:

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"

evaboot-probe check                      # GET-only, no credentials needed
evaboot-probe schema fetch --out schemas/openapi.current.json
evaboot-probe schema diff --current schemas/openapi.current.json
evaboot-probe webhook validate docs/webhook-example.json

pytest -q                                # fully offline, all HTTP mocked
```

Authenticated checks are additive and optional:

```bash
export EVABOOT_API_KEY=...   # environment only; never a flag, never a file
evaboot-probe check
```

## Commands

| Command | What it does |
| --- | --- |
| `check [--include-trial] [--format text\|json\|markdown]` | Runs every applicable check. Sends no POST request to Evaboot without `--include-trial`. |
| `schema fetch [--out FILE]` | Fetches the live spec, structurally validates it, writes a normalized copy plus its SHA-256. |
| `schema diff [--current FILE] [--fail-on breaking\|potentially-breaking\|any]` | Classifies every difference against the committed baseline as INFO, POTENTIALLY BREAKING or BREAKING. |
| `webhook validate PAYLOAD.json [--as SCHEMA]` | Validates a payload against the documented job response schemas. |

Exit codes: `0` passed (WARN, INFO and SKIP do not fail a run), `1` a documented contract was violated or a diff hit `--fail-on`, `2` usage or configuration error, `3` target unreachable after retries. `3` is separate on purpose — "Evaboot is unreachable from this runner" and "Evaboot changed its response schema" are different incidents with different owners.

Two runtime dependencies (`httpx`, `jsonschema`), Python 3.12+.

## CI

[.github/workflows/reliability.yml](.github/workflows/reliability.yml) has two jobs:

| Trigger | `test` (offline, mocked) | `live probe` (talks to Evaboot) |
| --- | --- | --- |
| `push` to `main` | runs | skipped |
| `pull_request` | runs | runs — GET-only |
| `workflow_dispatch` | runs | runs — includes the opt-in `/trial/*` POST |
| `schedule` (weekly, Monday) | runs | runs — includes the opt-in `/trial/*` POST |

`test` mocks every HTTP call, so it costs Evaboot nothing and runs on every push. `live probe` is the one job that sends real requests, so it is deliberately skipped on push: a merge to `main` was already probed on its pull request, and probing again on the same merge is repeat load for no new signal.

**What a red `live probe` means.** `schema diff`, `check` and `webhook validate` each run with `continue-on-error`, so one of them failing never hides the others — every step publishes its findings to the job summary, and a final step decides pass/fail only once all three have reported. A red run means: open the job summary and read what changed or failed. It is either a real finding (Evaboot changed their contract, as happened on 2026-08-24 — see [docs/01-research-findings.md](docs/01-research-findings.md)) or a genuine outage, never something to fix by re-running the job. Details on why the job is wired this way are in [docs/04-design-notes.md](docs/04-design-notes.md).

**Merging.** `main` requires a pull request with one approval; there is no required status check configured, so a passing `test`/`live probe` is a strong signal but not an enforced gate today. Read a red `live probe` on a PR before merging — it will not block the merge button by itself.

## Documentation

| Document | Contents |
| --- | --- |
| [docs/03-checks.md](docs/03-checks.md) | Every check and what it asserts, full command reference, contract-diff classification table, exit codes |
| [docs/04-design-notes.md](docs/04-design-notes.md) | Architecture, failure handling (timeouts, 429, `Retry-After`, 5xx, malformed JSON), design decisions, limitations, production extensions |
| [docs/01-research-findings.md](docs/01-research-findings.md) | What Evaboot's API actually documents, what was measured against the live API, and what is assumed — tagged line by line |
| [docs/02-plan.md](docs/02-plan.md) | The implementation plan this was built from, including the scope revisions applied |

Read [docs/01-research-findings.md](docs/01-research-findings.md) first if you care where a given behaviour comes from. Nothing in this tool is invented: checks assert documented behaviour, undocumented-but-observed behaviour is reported at INFO/WARN and can never fail a run, and the single assumption (a webhook body equals the job's GET detail body) is printed with every webhook result.

## License

MIT. See [LICENSE](LICENSE).
