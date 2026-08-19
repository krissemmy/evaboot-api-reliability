"""Validate a webhook / async-job payload against documented response schemas.

Evaboot publishes no webhook payload schema: ``webhook_url`` appears only as an
optional request field on four inputs, and the OpenAPI document says nothing
about what is delivered. Evaboot's docs site states the webhook receives the
same data as the job's GET detail endpoint, so validation here is against those
documented response schemas. That single assumption is printed with the result
rather than hidden in code.
"""

from __future__ import annotations

from .reporting import CheckResult, Report, Status
from .schema import validate_payload

CANDIDATES = (
    "EmailFinderJobOut",
    "EmailFinderJobInProgressOut",
    "EmailValidationJobOut",
    "EmailValidationJobInProgressOut",
    "ExtractionDetailOut",
    "ExtractionInProgressOut",
)

ASSUMPTION = (
    "assumption: a webhook body equals the job's GET detail body "
    "(Evaboot publishes no webhook schema)"
)

_FAMILIES = {
    "email_finder": ["EmailFinderJobOut", "EmailFinderJobInProgressOut"],
    "email_validation": ["EmailValidationJobOut", "EmailValidationJobInProgressOut"],
}


def candidates_for(payload: object) -> list[str]:
    """Pick the documented shape using documented discriminators.

    ``prospects`` separates the completed (200) body from the in-progress (202)
    body, and ``job_type`` / ``search_id`` separate the three job families. When a
    discriminator matches, exactly one schema is returned on purpose: the
    in-progress schemas are subsets of the completed ones, so a "fewest errors
    wins" search would quietly grade a malformed completed payload as a perfectly
    valid in-progress one.
    """
    if not isinstance(payload, dict):
        return list(CANDIDATES)
    complete = "prospects" in payload
    if "search_id" in payload:
        return ["ExtractionDetailOut" if complete else "ExtractionInProgressOut"]
    family = _FAMILIES.get(payload.get("job_type"))
    if family:
        return [family[0] if complete else family[1]]
    # No discriminator: try everything, shape-appropriate candidates first.
    preferred = [n for n in CANDIDATES if ("InProgress" in n) is not complete]
    return preferred + [n for n in CANDIDATES if n not in preferred]


def validate_webhook(spec: dict, payload: object, schema_name: str | None = None) -> Report:
    report = Report(target="webhook payload")
    names = [schema_name] if schema_name else candidates_for(payload)
    scored = [(name, validate_payload(spec, name, payload)) for name in names]
    best, errors = scored[0] if len(scored) == 1 else min(scored, key=lambda pair: len(pair[1]))

    report.checks.append(
        CheckResult(
            "webhook.schema_match",
            Status.PASS if not errors else Status.FAIL,
            f"matches {best}" if not errors else f"closest documented shape is {best}",
            data={"schema": best, "considered": names},
        )
    )
    if errors:
        report.checks.append(
            CheckResult(
                "webhook.validation_errors",
                Status.FAIL,
                "; ".join(errors[:5]) + (f" (+{len(errors) - 5} more)" if len(errors) > 5 else ""),
                data={"errors": errors},
            )
        )

    if isinstance(payload, dict):
        known = set(
            spec.get("components", {}).get("schemas", {}).get(best, {}).get("properties", {})
        )
        extra = sorted(set(payload) - known)
        report.checks.append(
            CheckResult(
                "webhook.unexpected_keys",
                Status.INFO,
                ", ".join(extra) if extra else "none",
                data={"keys": extra},
            )
        )
    return report
