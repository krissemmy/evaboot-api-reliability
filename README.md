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
uv venv && uv pip install -e ".[dev]"
uv run evaboot-probe check
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

Two runtime dependencies (`httpx`, `jsonschema`), Python 3.12+. CI runs lint, format and the offline test suite on every push, then a live probe that fails on a breaking contract change; see [.github/workflows/reliability.yml](.github/workflows/reliability.yml).

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
