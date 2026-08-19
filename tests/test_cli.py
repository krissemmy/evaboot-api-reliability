from __future__ import annotations

import copy
import json

import httpx
import pytest
from conftest import MINI_SPEC, evaboot_routes, json_response, make_transport

from evaboot_probe import cli


@pytest.fixture(autouse=True)
def no_api_key(monkeypatch):
    monkeypatch.delenv("EVABOOT_API_KEY", raising=False)


def patch_transport(monkeypatch, transport: httpx.BaseTransport) -> None:
    real = cli.ProbeClient

    def factory(**kwargs):
        return real(**{**kwargs, "transport": transport, "sleep": lambda _s: None})

    monkeypatch.setattr(cli, "ProbeClient", factory)


def write_spec(path, spec) -> str:
    path.write_text(json.dumps(spec), encoding="utf-8")
    return str(path)


def test_check_passing_run_exits_zero(monkeypatch, capsys):
    patch_transport(monkeypatch, make_transport(evaboot_routes()))
    assert cli.main(["check"]) == 0
    assert "overall PASS" in capsys.readouterr().out


def test_check_failing_run_exits_one(monkeypatch, capsys):
    routes = evaboot_routes({("GET", "/v1/quota/"): json_response(200, {"quota": {}})})
    patch_transport(monkeypatch, make_transport(routes))
    assert cli.main(["check"]) == 1
    assert "FAIL" in capsys.readouterr().out


def test_check_unreachable_target_exits_three(monkeypatch, capsys):
    def handler(_request):
        raise httpx.ConnectError("connection refused")

    patch_transport(monkeypatch, httpx.MockTransport(handler))
    assert cli.main(["check", "--max-attempts", "2"]) == 3
    assert "unreachable" in capsys.readouterr().err


def test_check_bad_base_url_is_a_usage_error(capsys):
    assert cli.main(["check", "--base-url", "notaurl"]) == 2
    assert "bad --base-url" in capsys.readouterr().err


def test_check_json_output_is_machine_readable(monkeypatch, capsys):
    patch_transport(monkeypatch, make_transport(evaboot_routes()))
    assert cli.main(["check", "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["overall"] == "PASS"
    assert {c["name"] for c in payload["checks"]} >= {"openapi.document", "auth.enforced"}


def test_check_markdown_output_renders_a_table(monkeypatch, capsys):
    patch_transport(monkeypatch, make_transport(evaboot_routes()))
    assert cli.main(["check", "--format", "markdown"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("## Evaboot API reliability check")
    assert "| `openapi.document` |" in out


def test_schema_fetch_writes_normalized_spec(monkeypatch, tmp_path, capsys):
    patch_transport(monkeypatch, make_transport(evaboot_routes()))
    out = tmp_path / "current.json"
    assert cli.main(["schema", "fetch", "--out", str(out)]) == 0
    assert json.loads(out.read_text()) == MINI_SPEC
    assert out.read_text().endswith("\n")
    assert "sha256" in capsys.readouterr().out


def test_schema_fetch_rejects_a_broken_document(monkeypatch, tmp_path):
    routes = evaboot_routes({"/openapi.json": json_response(200, {"openapi": "3.1.0"})})
    patch_transport(monkeypatch, make_transport(routes))
    assert cli.main(["schema", "fetch", "--out", str(tmp_path / "c.json")]) == 1


def test_schema_fetch_unwritable_path_is_a_usage_error(monkeypatch, tmp_path):
    patch_transport(monkeypatch, make_transport(evaboot_routes()))
    assert cli.main(["schema", "fetch", "--out", str(tmp_path / "missing-dir" / "c.json")]) == 2


def test_schema_diff_identical_specs_exit_zero(tmp_path, capsys):
    baseline = write_spec(tmp_path / "baseline.json", MINI_SPEC)
    current = write_spec(tmp_path / "current.json", MINI_SPEC)
    assert cli.main(["schema", "diff", "--baseline", baseline, "--current", current]) == 0
    assert "No contract changes" in capsys.readouterr().out


def test_schema_diff_breaking_change_exits_one(tmp_path, capsys):
    after = copy.deepcopy(MINI_SPEC)
    after["paths"].pop("/v1/quota/")
    baseline = write_spec(tmp_path / "baseline.json", MINI_SPEC)
    current = write_spec(tmp_path / "current.json", after)
    assert cli.main(["schema", "diff", "--baseline", baseline, "--current", current]) == 1
    assert "BREAKING" in capsys.readouterr().out


def test_schema_diff_fail_on_threshold(tmp_path):
    after = copy.deepcopy(MINI_SPEC)
    after["paths"]["/v1/new/"] = {"get": {"responses": {"200": {}}}}
    baseline = write_spec(tmp_path / "baseline.json", MINI_SPEC)
    current = write_spec(tmp_path / "current.json", after)
    args = ["schema", "diff", "--baseline", baseline, "--current", current]
    assert cli.main(args) == 0
    assert cli.main([*args, "--fail-on", "any"]) == 1


def test_schema_diff_fetches_live_spec_when_no_current_given(monkeypatch, tmp_path):
    patch_transport(monkeypatch, make_transport(evaboot_routes()))
    baseline = write_spec(tmp_path / "baseline.json", MINI_SPEC)
    assert cli.main(["schema", "diff", "--baseline", baseline]) == 0


def test_schema_diff_missing_baseline_is_a_usage_error(tmp_path, capsys):
    assert cli.main(["schema", "diff", "--baseline", str(tmp_path / "nope.json")]) == 2
    assert "cannot read" in capsys.readouterr().err


def test_schema_diff_markdown_groups_by_level(tmp_path, capsys):
    after = copy.deepcopy(MINI_SPEC)
    after["paths"].pop("/v1/quota/")
    baseline = write_spec(tmp_path / "b.json", MINI_SPEC)
    current = write_spec(tmp_path / "c.json", after)
    cli.main(
        ["schema", "diff", "--baseline", baseline, "--current", current, "--format", "markdown"]
    )
    assert "### BREAKING (1)" in capsys.readouterr().out


def test_webhook_validate_accepts_a_documented_payload(tmp_path, capsys):
    spec = write_spec(tmp_path / "spec.json", MINI_SPEC)
    payload = tmp_path / "payload.json"
    payload.write_text(json.dumps({"email": "a@example.com", "status": "valid"}))
    code = cli.main(
        ["webhook", "validate", str(payload), "--spec", spec, "--as", "EmailValidationTrialOut"]
    )
    assert code == 0
    assert "EmailValidationTrialOut" in capsys.readouterr().out


def test_webhook_validate_rejects_a_bad_payload(tmp_path):
    spec = write_spec(tmp_path / "spec.json", MINI_SPEC)
    payload = tmp_path / "payload.json"
    payload.write_text(json.dumps({"email": 42}))
    code = cli.main(
        ["webhook", "validate", str(payload), "--spec", spec, "--as", "EmailValidationTrialOut"]
    )
    assert code == 1


def test_webhook_validate_reports_invalid_json_as_a_failure(tmp_path, capsys):
    spec = write_spec(tmp_path / "spec.json", MINI_SPEC)
    payload = tmp_path / "payload.json"
    payload.write_text("{not json")
    assert cli.main(["webhook", "validate", str(payload), "--spec", spec]) == 1
    assert "invalid JSON" in capsys.readouterr().err


def test_webhook_validate_missing_payload_file_is_a_usage_error(tmp_path):
    spec = write_spec(tmp_path / "spec.json", MINI_SPEC)
    assert cli.main(["webhook", "validate", str(tmp_path / "nope.json"), "--spec", spec]) == 2


def test_webhook_validate_unknown_schema_name_is_a_usage_error(tmp_path, capsys):
    """--as is choice-restricted, but a spec missing that schema must not traceback."""
    spec = write_spec(tmp_path / "spec.json", MINI_SPEC)
    payload = tmp_path / "payload.json"
    payload.write_text(json.dumps({"id": "x"}))
    assert cli.main(["webhook", "validate", str(payload), "--spec", spec]) == 2
    assert "no component schema" in capsys.readouterr().err


def test_webhook_validate_against_the_real_baseline(capsys):
    from conftest import BASELINE_PATH

    payload = BASELINE_PATH.parent.parent / "docs" / "webhook-example.json"
    if not payload.exists():
        pytest.skip("example payload not present")
    assert cli.main(["webhook", "validate", str(payload), "--spec", str(BASELINE_PATH)]) == 0
