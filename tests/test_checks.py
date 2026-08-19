from __future__ import annotations

import httpx
from conftest import (
    ERROR_ENVELOPE,
    HTML_HEADERS,
    JSON_HEADERS,
    MINI_SPEC,
    evaboot_routes,
    html_response,
    json_response,
    make_transport,
)

from evaboot_probe.checks import has_error_envelope, run_checks
from evaboot_probe.client import ProbeClient
from evaboot_probe.reporting import Status

QUOTA_BODY = {
    "success": True,
    "quota": {"daily_limit": 2500, "remaining": 2500, "has_valid_salesnav": True},
}


def client_for(routes, record=None, **kwargs):
    return ProbeClient(transport=make_transport(routes, record), sleep=lambda _s: None, **kwargs)


def client_with_handler(handler, **kwargs):
    return ProbeClient(transport=httpx.MockTransport(handler), sleep=lambda _s: None, **kwargs)


def by_name(report):
    return {check.name: check for check in report.checks}


def auth_handler(quota_body: dict | None, scheme: str = "bearer"):
    """Serve the spec, 401 unauthenticated requests, and answer an authenticated
    quota read with `quota_body` when the Authorization scheme matches."""
    prefix = "Token " if scheme == "token" else "Bearer "

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/openapi.json":
            return httpx.Response(200, json=MINI_SPEC, headers=JSON_HEADERS)
        if request.url.path == "/v1/quota/":
            authorized = request.headers.get("authorization", "").startswith(prefix)
            if quota_body is not None and authorized:
                return httpx.Response(200, json=quota_body, headers=JSON_HEADERS)
            return httpx.Response(401, json=ERROR_ENVELOPE, headers=JSON_HEADERS)
        return httpx.Response(404, text="x", headers=HTML_HEADERS)

    return handler


def test_default_run_passes_and_sends_no_post(recorded):
    report = run_checks(client_for(evaboot_routes(), recorded))
    checks = by_name(report)
    assert report.overall is Status.PASS
    assert [method for method, _path in recorded].count("POST") == 0
    assert checks["openapi.document"].status is Status.PASS
    assert checks["auth.enforced"].status is Status.PASS
    assert checks["trial.email_validation"].status is Status.SKIP
    assert checks["ratelimit.headers"].detail.startswith("none observed")


def test_open_quota_endpoint_fails_the_run():
    routes = evaboot_routes({("GET", "/v1/quota/"): json_response(200, QUOTA_BODY)})
    report = run_checks(client_for(routes))
    assert by_name(report)["auth.enforced"].status is Status.FAIL
    assert report.overall is Status.FAIL


def test_error_envelope_drift_warns_but_does_not_fail():
    routes = evaboot_routes({("GET", "/v1/quota/"): json_response(401, {"detail": "nope"})})
    report = run_checks(client_for(routes))
    checks = by_name(report)
    assert checks["auth.enforced"].status is Status.PASS
    assert checks["auth.error_envelope"].status is Status.WARN
    assert report.overall is Status.PASS


def test_html_framework_errors_stay_informational():
    routes = evaboot_routes({("GET", "/v1/evaboot-probe-unknown-path/"): html_response(500)})
    report = run_checks(client_for(routes))
    assert by_name(report)["errors.unknown_path"].status is Status.INFO
    assert report.overall is Status.PASS


def test_unexpected_auth_status_is_a_warning():
    routes = evaboot_routes({("GET", "/v1/quota/"): json_response(418, ERROR_ENVELOPE)})
    report = run_checks(client_for(routes))
    assert by_name(report)["auth.enforced"].status is Status.WARN
    assert report.overall is Status.PASS


def test_invalid_json_spec_fails_the_run():
    routes = evaboot_routes(
        {"/openapi.json": lambda: httpx.Response(200, content=b"{nope", headers=JSON_HEADERS)}
    )
    report = run_checks(client_for(routes))
    assert by_name(report)["openapi.document"].status is Status.FAIL
    assert report.overall is Status.FAIL


def test_structurally_broken_spec_fails_the_run():
    routes = evaboot_routes(
        {"/openapi.json": json_response(200, {"openapi": "3.1.0", "paths": {}, "components": {}})}
    )
    report = run_checks(client_for(routes))
    check = by_name(report)["openapi.document"]
    assert check.status is Status.FAIL
    assert "paths" in check.detail


def test_spec_served_as_html_content_type_warns():
    routes = evaboot_routes(
        {
            "/openapi.json": lambda: httpx.Response(
                200, json=MINI_SPEC, headers={"content-type": "text/plain"}
            )
        }
    )
    report = run_checks(client_for(routes))
    assert by_name(report)["openapi.document"].status is Status.WARN


def test_rate_limit_headers_are_surfaced_if_they_appear():
    routes = evaboot_routes(
        {
            ("GET", "/v1/quota/"): json_response(
                401, ERROR_ENVELOPE, {"x-ratelimit-remaining": "0", "retry-after": "60"}
            )
        }
    )
    report = run_checks(client_for(routes))
    check = by_name(report)["ratelimit.headers"]
    assert check.status is Status.INFO
    assert "x-ratelimit-remaining" in check.detail
    assert check.data["headers"]["retry-after"] == "60"


def test_include_trial_sends_post_and_validates_body(recorded):
    report = run_checks(client_for(evaboot_routes(), recorded), include_trial=True)
    checks = by_name(report)
    assert ("POST", "/trial/email-validation/") in recorded
    assert checks["trial.email_validation"].status is Status.PASS
    # This mock answers 200 rather than the observed 422, so envelope drift warns.
    assert checks["trial.validation_envelope"].status is Status.WARN
    assert report.overall is Status.PASS


def test_trial_response_violating_documented_schema_fails():
    routes = evaboot_routes(
        {("POST", "/trial/email-validation/"): json_response(200, {"email": 42})}
    )
    report = run_checks(client_for(routes), include_trial=True)
    assert by_name(report)["trial.email_validation"].status is Status.FAIL


def test_missing_component_schema_fails_without_a_traceback():
    """If the documented schema disappears from the spec, that is a finding, not a crash."""
    spec = {**MINI_SPEC, "components": {**MINI_SPEC["components"], "schemas": {}}}
    spec["components"]["schemas"] = {
        k: v
        for k, v in MINI_SPEC["components"]["schemas"].items()
        if k != "EmailValidationTrialOut"
    }
    routes = evaboot_routes({"/openapi.json": json_response(200, spec)})
    report = run_checks(client_for(routes), include_trial=True)
    check = by_name(report)["trial.email_validation"]
    assert check.status is Status.FAIL
    assert "no component schema" in check.detail


def test_trial_server_error_fails_after_retries():
    routes = evaboot_routes({("POST", "/trial/email-validation/"): json_response(503, {})})
    report = run_checks(client_for(routes), include_trial=True)
    check = by_name(report)["trial.email_validation"]
    assert check.status is Status.FAIL
    assert check.attempts == 3


def test_authenticated_run_detects_token_scheme_and_reads_quota():
    report = run_checks(client_with_handler(auth_handler(QUOTA_BODY, "token"), api_key="secret"))
    checks = by_name(report)
    assert checks["auth.scheme_accepted"].detail == "Authorization: Token <key>"
    assert checks["quota.readable"].status is Status.PASS
    assert checks["quota.budget"].status is Status.PASS
    assert report.overall is Status.PASS


def test_authenticated_quota_body_violating_spec_fails():
    report = run_checks(client_with_handler(auth_handler({"success": True}), api_key="secret"))
    check = by_name(report)["quota.readable"]
    assert check.status is Status.FAIL
    assert "quota" in check.detail


def test_exhausted_quota_warns():
    body = {"quota": {"daily_limit": 2500, "remaining": 0, "has_valid_salesnav": True}}
    report = run_checks(client_with_handler(auth_handler(body), api_key="secret"))
    checks = by_name(report)
    assert checks["quota.budget"].status is Status.WARN
    assert checks["quota.budget"].detail == "daily quota exhausted"
    assert report.overall is Status.PASS


def test_missing_salesnav_seat_warns():
    body = {"quota": {"daily_limit": 0, "remaining": 0, "has_valid_salesnav": False}}
    report = run_checks(client_with_handler(auth_handler(body), api_key="secret"))
    assert "Sales Navigator" in by_name(report)["quota.budget"].detail


def test_rejected_key_fails_scheme_detection():
    report = run_checks(client_with_handler(auth_handler(None), api_key="stale-key"))
    checks = by_name(report)
    assert checks["auth.scheme_accepted"].status is Status.FAIL
    assert checks["quota.readable"].status is Status.SKIP
    assert report.overall is Status.FAIL


def test_has_error_envelope():
    assert has_error_envelope(ERROR_ENVELOPE)
    assert not has_error_envelope({"success": True})
    assert not has_error_envelope({"success": False, "error": "nope"})
    assert not has_error_envelope("not a dict")
