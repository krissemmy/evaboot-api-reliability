"""Result models and output renderers.

Everything the CLI prints goes through here so that text, JSON and markdown
stay in sync, and so exit codes are derived from one place.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum


class Status(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    WARN = "WARN"
    INFO = "INFO"
    SKIP = "SKIP"


class Level(StrEnum):
    INFO = "INFO"
    POTENTIALLY_BREAKING = "POTENTIALLY BREAKING"
    BREAKING = "BREAKING"


@dataclass
class CheckResult:
    name: str
    status: Status
    detail: str = ""
    latency_ms: int | None = None
    attempts: int = 1
    data: dict = field(default_factory=dict)


@dataclass
class Change:
    """One difference between two OpenAPI documents."""

    level: Level
    target: str
    detail: str


@dataclass
class Report:
    target: str
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def overall(self) -> Status:
        return Status.FAIL if any(c.status is Status.FAIL for c in self.checks) else Status.PASS


def report_to_dict(report: Report) -> dict:
    return {
        "target": report.target,
        "overall": str(report.overall),
        "checks": [
            {
                "name": c.name,
                "status": str(c.status),
                "detail": c.detail,
                "latency_ms": c.latency_ms,
                "attempts": c.attempts,
                **({"data": c.data} if c.data else {}),
            }
            for c in report.checks
        ],
    }


def render_report(report: Report, fmt: str) -> str:
    if fmt == "json":
        return json.dumps(report_to_dict(report), indent=2)
    if fmt == "markdown":
        lines = [
            "## Evaboot API reliability check",
            "",
            f"Target: `{report.target}`  ",
            f"Overall: **{report.overall}**",
            "",
            "| Check | Status | Latency | Detail |",
            "| --- | --- | --- | --- |",
        ]
        for c in report.checks:
            latency = f"{c.latency_ms} ms" if c.latency_ms is not None else ""
            lines.append(f"| `{c.name}` | {c.status} | {latency} | {c.detail} |")
        return "\n".join(lines)

    width = max((len(c.name) for c in report.checks), default=0)
    lines = ["Evaboot API reliability check", f"target  {report.target}", ""]
    for c in report.checks:
        parts = [f"  {c.name:<{width}}  {c.status:<4}"]
        if c.latency_ms is not None:
            parts.append(f"{c.latency_ms:>5} ms")
        if c.attempts > 1:
            parts.append(f"attempts={c.attempts}")
        lines.append("  ".join(parts))
        if c.detail:
            lines.append(f"  {'':<{width}}  {c.detail}")
    lines += ["", f"overall {report.overall}"]
    return "\n".join(lines)


def render_changes(changes: list[Change], fmt: str) -> str:
    if fmt == "json":
        return json.dumps(
            {
                "change_count": len(changes),
                "changes": [
                    {"level": str(c.level), "target": c.target, "detail": c.detail} for c in changes
                ],
            },
            indent=2,
        )

    if not changes:
        return (
            "No contract changes detected."
            if fmt != "markdown"
            else "No contract changes detected."
        )

    order = [Level.BREAKING, Level.POTENTIALLY_BREAKING, Level.INFO]
    if fmt == "markdown":
        lines = ["## Evaboot OpenAPI contract diff", ""]
        for level in order:
            group = [c for c in changes if c.level is level]
            if group:
                lines.append(f"### {level} ({len(group)})")
                lines += [f"- `{c.target}` — {c.detail}" for c in group]
                lines.append("")
        return "\n".join(lines).rstrip()

    lines = []
    for level in order:
        group = [c for c in changes if c.level is level]
        if not group:
            continue
        lines.append(f"{level} ({len(group)})")
        for c in group:
            lines.append(f"  {c.target}")
            lines.append(f"    {c.detail}")
        lines.append("")
    return "\n".join(lines).rstrip()
