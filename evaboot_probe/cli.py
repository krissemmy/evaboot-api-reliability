"""CLI entry point.

Exit codes:
  0  everything passed (WARN / INFO / SKIP do not fail a run)
  1  a documented contract was violated, or a schema diff hit --fail-on
  2  usage or configuration error
  3  the target could not be reached at all after retries
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import httpx

from . import __version__
from .checks import run_checks
from .client import ProbeClient, Unreachable
from .reporting import Level, Status, render_changes, render_report
from .schema import (
    SPEC_PATH,
    SpecError,
    diff_specs,
    digest,
    fetch_spec,
    load_spec,
    normalize,
    structural_errors,
    worst_level,
)
from .webhook import ASSUMPTION, CANDIDATES, validate_webhook

EXIT_OK = 0
EXIT_CHECK_FAILED = 1
EXIT_USAGE = 2
EXIT_UNREACHABLE = 3

DEFAULT_BASE_URL = "https://api.evaboot.com"
DEFAULT_BASELINE = "schemas/openapi.baseline.json"
DEFAULT_CURRENT = "schemas/openapi.current.json"

FAIL_ON = {
    "breaking": (Level.BREAKING,),
    "potentially-breaking": (Level.BREAKING, Level.POTENTIALLY_BREAKING),
    "any": (Level.BREAKING, Level.POTENTIALLY_BREAKING, Level.INFO),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="evaboot-probe", description=__doc__.split("\n")[0])
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_http_args(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--base-url", default=DEFAULT_BASE_URL)
        sub.add_argument("--timeout", type=float, default=15.0, help="read timeout in seconds")
        sub.add_argument("--max-attempts", type=int, default=3)

    def add_format(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--format", choices=("text", "json", "markdown"), default="text")

    check = subparsers.add_parser("check", help="run reliability checks")
    add_http_args(check)
    add_format(check)
    check.add_argument("--auth-scheme", choices=("bearer", "token"), default="bearer")
    check.add_argument(
        "--include-trial",
        action="store_true",
        help="allow POST requests to the unauthenticated /trial/* endpoints",
    )
    check.set_defaults(handler=cmd_check)

    schema = subparsers.add_parser("schema", help="OpenAPI contract commands")
    schema_sub = schema.add_subparsers(dest="schema_command", required=True)

    fetch = schema_sub.add_parser("fetch", help="fetch, validate and store the OpenAPI document")
    add_http_args(fetch)
    fetch.add_argument("--spec-path", default=SPEC_PATH)
    fetch.add_argument("--out", default=DEFAULT_CURRENT)
    fetch.set_defaults(handler=cmd_schema_fetch)

    diff = schema_sub.add_parser("diff", help="compare a spec against the stored baseline")
    add_http_args(diff)
    add_format(diff)
    diff.add_argument("--baseline", default=DEFAULT_BASELINE)
    diff.add_argument("--current", help="local spec file; omit to fetch the live document")
    diff.add_argument("--fail-on", choices=tuple(FAIL_ON), default="breaking")
    diff.set_defaults(handler=cmd_schema_diff)

    webhook = subparsers.add_parser("webhook", help="webhook payload commands")
    webhook_sub = webhook.add_subparsers(dest="webhook_command", required=True)
    validate = webhook_sub.add_parser("validate", help="validate a payload file")
    add_format(validate)
    validate.add_argument("payload")
    validate.add_argument("--spec", default=DEFAULT_BASELINE)
    validate.add_argument(
        "--as",
        dest="schema_name",
        metavar="SCHEMA",
        help="skip auto-detection and validate against this component schema "
        f"(documented webhook shapes: {', '.join(CANDIDATES)})",
    )
    validate.set_defaults(handler=cmd_webhook_validate)

    return parser


def _client(args: argparse.Namespace, api_key: str | None = None) -> ProbeClient:
    return ProbeClient(
        base_url=args.base_url,
        api_key=api_key,
        auth_scheme=getattr(args, "auth_scheme", "bearer"),
        timeout=args.timeout,
        max_attempts=args.max_attempts,
    )


def cmd_check(args: argparse.Namespace) -> int:
    with _client(args, os.environ.get("EVABOOT_API_KEY") or None) as client:
        report = run_checks(client, include_trial=args.include_trial)
    print(render_report(report, args.format))
    return EXIT_OK if report.overall is Status.PASS else EXIT_CHECK_FAILED


def cmd_schema_fetch(args: argparse.Namespace) -> int:
    with _client(args) as client:
        try:
            spec, attempt = fetch_spec(client, args.spec_path)
        except SpecError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_CHECK_FAILED

    errors = structural_errors(spec)
    try:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(normalize(spec))
    except OSError as exc:
        print(f"error: cannot write {args.out}: {exc}", file=sys.stderr)
        return EXIT_USAGE

    print(f"fetched  {args.base_url}{args.spec_path} in {attempt.latency_ms} ms")
    print(f"wrote    {args.out}")
    print(f"sha256   {digest(spec)}")
    print(f"paths    {len(spec.get('paths', {}))}")
    if errors:
        print("structural problems:")
        for error in errors:
            print(f"  {error}")
        return EXIT_CHECK_FAILED
    return EXIT_OK


def cmd_schema_diff(args: argparse.Namespace) -> int:
    try:
        baseline = load_spec(args.baseline)
    except SpecError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    if args.current:
        try:
            current = load_spec(args.current)
        except SpecError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_USAGE
    else:
        with _client(args) as client:
            try:
                current, _ = fetch_spec(client)
            except SpecError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return EXIT_CHECK_FAILED

    changes = diff_specs(baseline, current)
    print(render_changes(changes, args.format))
    worst = worst_level(changes)
    return EXIT_CHECK_FAILED if worst in FAIL_ON[args.fail_on] else EXIT_OK


def cmd_webhook_validate(args: argparse.Namespace) -> int:
    try:
        spec = load_spec(args.spec)
    except SpecError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    try:
        with open(args.payload, encoding="utf-8") as handle:
            raw = handle.read()
    except OSError as exc:
        print(f"error: cannot read {args.payload}: {exc}", file=sys.stderr)
        return EXIT_USAGE

    try:
        payload = json.loads(raw)
    except ValueError as exc:
        print(f"invalid JSON in {args.payload}: {exc}", file=sys.stderr)
        return EXIT_CHECK_FAILED

    try:
        report = validate_webhook(spec, payload, args.schema_name)
    except SpecError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    report.target = f"{args.payload} ({ASSUMPTION})"
    print(render_report(report, args.format))
    return EXIT_OK if report.overall is Status.PASS else EXIT_CHECK_FAILED


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except Unreachable as exc:
        print(f"unreachable: {exc}", file=sys.stderr)
        return EXIT_UNREACHABLE
    except (httpx.InvalidURL, httpx.UnsupportedProtocol) as exc:
        print(f"error: bad --base-url: {exc}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
