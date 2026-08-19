"""The black-box checks.

Rules this module follows deliberately:

* Without ``--include-trial`` no POST request is sent to Evaboot at all. The
  default run is GET-only.
* Only behaviour Evaboot actually documents is allowed to FAIL a run. The 401 /
  404 / 405 / 422 bodies are not in the OpenAPI document, so drift there is
  reported as INFO or WARN and never fails the probe.
* Endpoints that spend credits or a customer's daily quota (extractions,
  email-finder, email-validation bulk, search-builder, search-agent, sn/*) are
  never called.
"""

from __future__ import annotations

from .client import ProbeClient, Unreachable
from .reporting import CheckResult, Report, Status
from .schema import SpecError, digest, fetch_spec, structural_errors, validate_payload

QUOTA_PATH = "/v1/quota/"
UNKNOWN_PATH = "/v1/evaboot-probe-unknown-path/"
TRIAL_VALIDATION_PATH = "/trial/email-validation/"
# RFC 2606 reserved domain: no MX record, so no real mailbox is ever touched.
TRIAL_PROBE_EMAIL = "noreply@example.com"


def has_error_envelope(body: object) -> bool:
    """Match the error shape observed on 401/422: {success, error:{type,message}}."""
    if not isinstance(body, dict) or body.get("success") is not False:
        return False
    error = body.get("error")
    return isinstance(error, dict) and "type" in error and "message" in error


def _schema_errors(spec: dict, schema_name: str, body: object) -> list[str]:
    """Validation errors, or the reason validation was impossible.

    A schema disappearing from the spec is itself a finding, so it is reported as
    a failed check rather than raised at the caller.
    """
    try:
        return validate_payload(spec, schema_name, body)
    except SpecError as exc:
        return [str(exc)]


def _json_body(response) -> object | None:
    try:
        return response.json()
    except ValueError:
        return None


def run_checks(client: ProbeClient, *, include_trial: bool = False) -> Report:
    """Run every applicable check. Raises Unreachable if the spec cannot be fetched."""
    report = Report(target=client.base_url)
    spec: dict | None = None
    try:
        spec, attempt = fetch_spec(client)
    except SpecError as exc:
        report.checks.append(CheckResult("openapi.document", Status.FAIL, str(exc)))
    else:
        errors = structural_errors(spec)
        content_type = attempt.response.headers.get("content-type", "")
        if errors:
            status, detail = Status.FAIL, "; ".join(errors[:3])
        elif "json" not in content_type:
            status, detail = Status.WARN, f"valid JSON served as {content_type!r}"
        else:
            status = Status.PASS
            detail = (
                f"OpenAPI {spec.get('openapi')}, {len(spec.get('paths', {}))} paths, "
                f"{len(spec.get('components', {}).get('schemas', {}))} schemas, "
                f"sha256 {digest(spec)[:12]}"
            )
        report.checks.append(
            CheckResult("openapi.document", status, detail, attempt.latency_ms, attempt.attempts)
        )

    report.checks += _auth_checks(client, spec)
    report.checks.append(_unknown_path_check(client))
    report.checks.append(_trailing_slash_check(client))

    if include_trial:
        report.checks += _trial_checks(client, spec)
    else:
        report.checks.append(
            CheckResult(
                "trial.email_validation",
                Status.SKIP,
                "no POST requests are sent without --include-trial",
            )
        )

    seen = client.seen_rate_limit_headers
    if seen:
        report.checks.append(
            CheckResult(
                "ratelimit.headers",
                Status.INFO,
                "rate-limit headers now present: " + ", ".join(sorted(seen)),
                data={"headers": dict(seen)},
            )
        )
    else:
        report.checks.append(
            CheckResult(
                "ratelimit.headers",
                Status.INFO,
                "none observed; Evaboot meters daily quota server-side (see GET /v1/quota/)",
            )
        )
    return report


def _auth_checks(client: ProbeClient, spec: dict | None) -> list[CheckResult]:
    """Unauthenticated: assert the endpoint is closed. With a key: read quota."""
    results: list[CheckResult] = []
    try:
        attempt = client.request("GET", QUOTA_PATH)
    except Unreachable as exc:
        return [CheckResult("auth.enforced", Status.FAIL, str(exc))]
    code = attempt.response.status_code

    if 200 <= code < 300:
        status, detail = Status.FAIL, f"HTTP {code} without credentials: endpoint is open"
    elif code in (401, 403):
        status, detail = Status.PASS, f"HTTP {code} without credentials"
    else:
        status, detail = Status.WARN, f"unexpected HTTP {code} without credentials"
    results.append(
        CheckResult("auth.enforced", status, detail, attempt.latency_ms, attempt.attempts)
    )

    body = _json_body(attempt.response)
    matches = has_error_envelope(body)
    results.append(
        CheckResult(
            "auth.error_envelope",
            Status.INFO if matches else Status.WARN,
            "matches the observed {success,error:{type,message}} envelope"
            if matches
            else "error envelope drifted (undocumented shape, informational only)",
        )
    )

    if not client.api_key:
        results.append(CheckResult("auth.scheme_accepted", Status.SKIP, "EVABOOT_API_KEY not set"))
        results.append(CheckResult("quota.readable", Status.SKIP, "EVABOOT_API_KEY not set"))
        return results

    results += _authenticated_checks(client, spec)
    return results


def _authenticated_checks(client: ProbeClient, spec: dict | None) -> list[CheckResult]:
    """The spec declares HTTP bearer; Evaboot's help centre says 'Token'. Find out."""
    results: list[CheckResult] = []
    accepted: str | None = None
    attempt = None
    for scheme in (client.auth_scheme, "token" if client.auth_scheme == "bearer" else "bearer"):
        try:
            attempt = client.request("GET", QUOTA_PATH, headers=client.auth_header(scheme))
        except Unreachable as exc:
            return [CheckResult("auth.scheme_accepted", Status.FAIL, str(exc))]
        if attempt.response.status_code != 401:
            accepted = scheme
            break

    if accepted is None:
        results.append(
            CheckResult("auth.scheme_accepted", Status.FAIL, "both Bearer and Token got HTTP 401")
        )
        results.append(CheckResult("quota.readable", Status.SKIP, "no accepted auth scheme"))
        return results

    results.append(
        CheckResult(
            "auth.scheme_accepted",
            Status.PASS,
            f"Authorization: {accepted.capitalize()} <key>",
            attempt.latency_ms,
            attempt.attempts,
        )
    )

    code = attempt.response.status_code
    body = _json_body(attempt.response)
    if code != 200:
        results.append(CheckResult("quota.readable", Status.FAIL, f"HTTP {code} with a valid key"))
        return results
    if spec is None:
        results.append(
            CheckResult("quota.readable", Status.WARN, "spec unavailable, not validated")
        )
        return results

    errors = _schema_errors(spec, "QuotaOut", body)
    results.append(
        CheckResult(
            "quota.readable",
            Status.FAIL if errors else Status.PASS,
            "; ".join(errors[:3]) if errors else "HTTP 200, body matches QuotaOut",
            attempt.latency_ms,
            attempt.attempts,
        )
    )

    quota = body.get("quota", {}) if isinstance(body, dict) else {}
    remaining = quota.get("remaining", 0)
    has_seat = quota.get("has_valid_salesnav", False)
    if not has_seat:
        status, detail = Status.WARN, "no valid Sales Navigator seat: remaining is always 0"
    elif not remaining:
        status, detail = Status.WARN, "daily quota exhausted"
    else:
        status, detail = Status.PASS, f"{remaining} of {quota.get('daily_limit')} remaining today"
    results.append(CheckResult("quota.budget", status, detail, data={"quota": quota}))
    return results


def _unknown_path_check(client: ProbeClient) -> CheckResult:
    """Framework-level errors are HTML, not the JSON envelope. Drift signal only."""
    try:
        attempt = client.request("GET", UNKNOWN_PATH)
    except Unreachable as exc:
        return CheckResult("errors.unknown_path", Status.WARN, str(exc))
    content_type = attempt.response.headers.get("content-type", "")
    return CheckResult(
        "errors.unknown_path",
        Status.INFO,
        f"HTTP {attempt.response.status_code}, content-type {content_type!r} "
        f"(clients must not call .json() on non-2xx)",
        attempt.latency_ms,
        attempt.attempts,
    )


def _trailing_slash_check(client: ProbeClient) -> CheckResult:
    """A missing trailing slash 301s; a client that does not follow redirects on
    POST silently sends nothing. Informational, not a contract."""
    try:
        attempt = client.request("GET", QUOTA_PATH.rstrip("/"))
    except Unreachable as exc:
        return CheckResult("redirect.trailing_slash", Status.WARN, str(exc))
    code = attempt.response.status_code
    location = attempt.response.headers.get("location", "")
    detail = f"HTTP {code}" + (f" -> {location}" if location else "")
    return CheckResult(
        "redirect.trailing_slash", Status.INFO, detail, attempt.latency_ms, attempt.attempts
    )


def _trial_checks(client: ProbeClient, spec: dict | None) -> list[CheckResult]:
    results: list[CheckResult] = []
    try:
        attempt = client.request("POST", TRIAL_VALIDATION_PATH, json_body={})
    except Unreachable as exc:
        results.append(CheckResult("trial.validation_envelope", Status.WARN, str(exc)))
    else:
        body = _json_body(attempt.response)
        ok = attempt.response.status_code == 422 and has_error_envelope(body)
        results.append(
            CheckResult(
                "trial.validation_envelope",
                Status.INFO if ok else Status.WARN,
                "HTTP 422 with the observed error envelope"
                if ok
                else f"HTTP {attempt.response.status_code}, envelope drifted "
                f"(undocumented shape, informational only)",
                attempt.latency_ms,
                attempt.attempts,
            )
        )

    try:
        attempt = client.request(
            "POST", TRIAL_VALIDATION_PATH, json_body={"email": TRIAL_PROBE_EMAIL}
        )
    except Unreachable as exc:
        results.append(CheckResult("trial.email_validation", Status.FAIL, str(exc)))
        return results
    code = attempt.response.status_code
    body = _json_body(attempt.response)
    if code != 200:
        detail = f"HTTP {code}, documented response is 200"
        status = Status.FAIL
    elif spec is None:
        status, detail = Status.WARN, "HTTP 200, spec unavailable so body not validated"
    else:
        errors = _schema_errors(spec, "EmailValidationTrialOut", body)
        status = Status.FAIL if errors else Status.PASS
        detail = (
            "; ".join(errors[:3]) if errors else "HTTP 200, body matches EmailValidationTrialOut"
        )
    results.append(
        CheckResult("trial.email_validation", status, detail, attempt.latency_ms, attempt.attempts)
    )
    return results
