from __future__ import annotations

from evaboot_probe.reporting import Status
from evaboot_probe.webhook import candidates_for, validate_webhook

FINDER_COMPLETE = {
    "id": "job-123",
    "status": "complete",
    "created_at": "2026-08-19T00:00:00Z",
    "updated_at": "2026-08-19T00:04:00Z",
    "progress": 100,
    "job_type": "email_finder",
    "prospects": [
        {
            "id": "p1",
            "first_name": "Ada",
            "last_name": "Lovelace",
            "company_name": "Analytical Engines",
            "found_email": "ada@example.com",
            "email_validity": "valid",
            "status": "found",
        }
    ],
}

FINDER_IN_PROGRESS = {
    "id": "job-123",
    "status": "running",
    "progress": 40,
    "created_at": "2026-08-19T00:00:00Z",
    "updated_at": "2026-08-19T00:02:00Z",
    "job_type": "email_finder",
}


def by_name(report):
    return {check.name: check for check in report.checks}


def test_completed_email_finder_payload_matches(baseline_spec):
    report = validate_webhook(baseline_spec, FINDER_COMPLETE)
    checks = by_name(report)
    assert report.overall is Status.PASS
    assert checks["webhook.schema_match"].data["schema"] == "EmailFinderJobOut"


def test_in_progress_payload_matches_the_202_shape(baseline_spec):
    report = validate_webhook(baseline_spec, FINDER_IN_PROGRESS)
    assert report.overall is Status.PASS
    assert by_name(report)["webhook.schema_match"].data["schema"] == "EmailFinderJobInProgressOut"


def test_extraction_payload_is_detected_by_search_id(baseline_spec):
    payload = {"search_id": "abc", "status": "complete", "prospects": [], "total_prospects": 0}
    report = validate_webhook(baseline_spec, payload)
    assert report.overall is Status.PASS
    assert by_name(report)["webhook.schema_match"].data["schema"] == "ExtractionDetailOut"


def test_missing_required_field_is_reported(baseline_spec):
    payload = {k: v for k, v in FINDER_COMPLETE.items() if k != "created_at"}
    report = validate_webhook(baseline_spec, payload)
    checks = by_name(report)
    assert report.overall is Status.FAIL
    assert "created_at" in checks["webhook.validation_errors"].detail


def test_wrong_type_is_reported_with_a_path(baseline_spec):
    payload = {**FINDER_COMPLETE, "prospects": [{"first_name": 42}]}
    report = validate_webhook(baseline_spec, payload)
    errors = by_name(report)["webhook.validation_errors"].data["errors"]
    assert any(error.startswith("prospects.0.first_name") for error in errors)


def test_unexpected_keys_are_informational(baseline_spec):
    payload = {**FINDER_COMPLETE, "signature": "abc123"}
    report = validate_webhook(baseline_spec, payload)
    checks = by_name(report)
    assert report.overall is Status.PASS
    assert checks["webhook.unexpected_keys"].status is Status.INFO
    assert checks["webhook.unexpected_keys"].data["keys"] == ["signature"]


def test_explicit_schema_override_is_respected(baseline_spec):
    report = validate_webhook(baseline_spec, FINDER_COMPLETE, "ExtractionDetailOut")
    checks = by_name(report)
    assert report.overall is Status.FAIL
    assert checks["webhook.schema_match"].data["considered"] == ["ExtractionDetailOut"]


def test_non_object_payload_does_not_crash(baseline_spec):
    report = validate_webhook(baseline_spec, ["not", "an", "object"])
    assert report.overall is Status.FAIL


def test_candidate_selection_uses_documented_discriminators():
    assert candidates_for(FINDER_COMPLETE) == ["EmailFinderJobOut"]
    assert candidates_for(FINDER_IN_PROGRESS) == ["EmailFinderJobInProgressOut"]
    assert candidates_for({"search_id": "x", "prospects": []}) == ["ExtractionDetailOut"]
    assert candidates_for({"job_type": "email_validation", "prospects": []}) == [
        "EmailValidationJobOut"
    ]
    # Without a discriminator every documented shape is tried.
    assert len(candidates_for({})) == 6
    assert "InProgress" in candidates_for({})[0]


def test_broken_completed_payload_is_not_downgraded_to_in_progress(baseline_spec):
    """The 202 schema is a subset of the 200 schema, so a payload that claims to
    be complete must be judged as complete rather than silently matching 202."""
    payload = {**FINDER_COMPLETE, "prospects": [{"first_name": 42}]}
    report = validate_webhook(baseline_spec, payload)
    checks = by_name(report)
    assert checks["webhook.schema_match"].data["schema"] == "EmailFinderJobOut"
    assert report.overall is Status.FAIL
