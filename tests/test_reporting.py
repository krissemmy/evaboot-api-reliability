from __future__ import annotations

import pytest

from evaboot_probe.reporting import (
    Change,
    CheckResult,
    Level,
    Report,
    Status,
    render_changes,
    render_report,
    use_color,
)

REPORT = Report(
    target="https://api.evaboot.com",
    checks=[
        CheckResult("openapi.document", Status.PASS, "OpenAPI 3.1.0", latency_ms=908),
        CheckResult("auth.enforced", Status.PASS, "HTTP 401", latency_ms=137, attempts=2),
        CheckResult("ratelimit.headers", Status.INFO, "none observed"),
        CheckResult("quota.readable", Status.SKIP, "EVABOOT_API_KEY not set"),
    ],
)


def test_status_column_is_fixed_width_so_rows_align():
    lines = render_report(REPORT, "text", color=False).splitlines()
    # Column 2-6 of every status row, including the footer's overall status.
    statuses = [line[2:6] for line in lines if line[:2] == "  " and line[2:6].isupper()]
    assert statuses == ["PASS", "PASS", "INFO", "SKIP", "PASS"]


def test_details_are_indented_under_the_name_column():
    lines = render_report(REPORT, "text", color=False).splitlines()
    assert "        OpenAPI 3.1.0" in lines


def test_retries_are_visible_in_the_row():
    assert "2 attempts" in render_report(REPORT, "text", color=False)


def test_footer_counts_statuses_and_request_time():
    footer = render_report(REPORT, "text", color=False).splitlines()[-1]
    assert footer.strip().startswith("PASS  overall")
    assert "2 passed, 1 info, 1 skipped" in footer
    assert "1.0 s in requests" in footer


def test_failed_checks_are_counted_first_and_flip_the_overall():
    report = Report(target="x", checks=[*REPORT.checks, CheckResult("boom", Status.FAIL, "nope")])
    footer = render_report(report, "text", color=False).splitlines()[-1]
    assert footer.strip().startswith("FAIL  overall   1 failed,")


def test_request_time_is_omitted_when_nothing_was_measured():
    report = Report(target="x", checks=[CheckResult("a", Status.INFO)])
    assert "in requests" not in render_report(report, "text", color=False)


def test_color_is_off_by_default_when_not_a_tty(capsys):
    assert "\033[" not in render_report(REPORT, "text")


def test_color_wraps_only_the_status_token():
    colored = render_report(REPORT, "text", color=True)
    assert "\033[32mPASS\033[0m  openapi.document" in colored


def test_markdown_overall_carries_the_counts():
    assert "Overall: **PASS** (2 passed, 1 info, 1 skipped)" in render_report(REPORT, "markdown")


def test_json_output_is_unaffected_by_color():
    assert render_report(REPORT, "json", color=True) == render_report(REPORT, "json", color=False)


def test_diff_levels_are_colored_only_on_request():
    changes = [Change(Level.BREAKING, "GET /v1/quota/", "Path removed")]
    assert "\033[" not in render_changes(changes, "text", color=False)
    assert "\033[1;31mBREAKING (1)\033[0m" in render_changes(changes, "text", color=True)


@pytest.mark.parametrize(
    "env,expected",
    [({}, False), ({"NO_COLOR": "1"}, False), ({"FORCE_COLOR": "1"}, True)],
)
def test_use_color_respects_conventions(monkeypatch, env, expected):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    class NotATty:
        def isatty(self):
            return False

    assert use_color(NotATty()) is expected


def test_no_color_beats_force_color(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("FORCE_COLOR", "1")
    assert use_color() is False
