"""Result models and output renderers.

Everything the CLI prints goes through here so that text, JSON and markdown
stay in sync, and so exit codes are derived from one place.
"""

from __future__ import annotations

import json
import os
import sys
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


# Every status value is four characters wide, so the left column aligns without
# padding, and colour codes never disturb it.
_STATUS_COLOR = {
    Status.PASS: "\033[32m",
    Status.FAIL: "\033[1;31m",
    Status.WARN: "\033[33m",
    Status.INFO: "\033[2m",
    Status.SKIP: "\033[2m",
}
_LEVEL_COLOR = {
    Level.BREAKING: "\033[1;31m",
    Level.POTENTIALLY_BREAKING: "\033[33m",
    Level.INFO: "\033[2m",
}
_RESET = "\033[0m"


def use_color(stream=None) -> bool:
    """Colour only when a human is watching. Honours NO_COLOR and FORCE_COLOR."""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    stream = stream or sys.stdout
    return bool(getattr(stream, "isatty", lambda: False)())


def _paint(text: str, code: str | None, color: bool) -> str:
    return f"{code}{text}{_RESET}" if color and code else text


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


def _counts(report: Report) -> str:
    labels = (
        (Status.FAIL, "failed"),
        (Status.PASS, "passed"),
        (Status.WARN, "warnings"),
        (Status.INFO, "info"),
        (Status.SKIP, "skipped"),
    )
    parts = []
    for status, label in labels:
        count = sum(1 for check in report.checks if check.status is status)
        if count:
            parts.append(f"{count} {label}")
    return ", ".join(parts)


def _request_time(report: Report) -> str:
    total_ms = sum(check.latency_ms or 0 for check in report.checks)
    if not total_ms:
        return ""
    if total_ms >= 1000:
        return f"{total_ms / 1000:.1f} s in requests"
    return f"{total_ms} ms in requests"


def render_report(report: Report, fmt: str, color: bool | None = None) -> str:
    if fmt == "json":
        return json.dumps(report_to_dict(report), indent=2)
    if fmt == "markdown":
        lines = [
            "## Evaboot API reliability check",
            "",
            f"Target: `{report.target}`  ",
            f"Overall: **{report.overall}** ({_counts(report)})",
            "",
            "| Check | Status | Latency | Detail |",
            "| --- | --- | --- | --- |",
        ]
        for c in report.checks:
            latency = f"{c.latency_ms} ms" if c.latency_ms is not None else ""
            lines.append(f"| `{c.name}` | {c.status} | {latency} | {c.detail} |")
        return "\n".join(lines)

    color = use_color() if color is None else color
    width = max((len(c.name) for c in report.checks), default=0)
    lines = ["Evaboot API reliability check", f"target  {report.target}", ""]
    for check in report.checks:
        latency = f"{check.latency_ms} ms" if check.latency_ms is not None else ""
        attempts = f"  {check.attempts} attempts" if check.attempts > 1 else ""
        status = _paint(str(check.status), _STATUS_COLOR.get(check.status), color)
        lines.append(f"  {status}  {check.name:<{width}}  {latency:>8}{attempts}".rstrip())
        if check.detail:
            lines.append(f"        {check.detail}")

    overall = _paint(str(report.overall), _STATUS_COLOR.get(report.overall), color)
    footer = f"  {overall}  overall   {_counts(report)}"
    request_time = _request_time(report)
    lines += ["", f"{footer}   ({request_time})" if request_time else footer]
    return "\n".join(lines)


def render_changes(changes: list[Change], fmt: str, color: bool | None = None) -> str:
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

    color = use_color() if color is None else color
    lines = []
    for level in order:
        group = [c for c in changes if c.level is level]
        if not group:
            continue
        lines.append(_paint(f"{level} ({len(group)})", _LEVEL_COLOR.get(level), color))
        for c in group:
            lines.append(f"  {c.target}")
            lines.append(f"    {c.detail}")
        lines.append("")
    return "\n".join(lines).rstrip()
