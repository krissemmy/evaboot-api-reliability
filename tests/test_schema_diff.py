from __future__ import annotations

import copy

from conftest import MINI_SPEC

from evaboot_probe.reporting import Level
from evaboot_probe.schema import diff_specs, digest, normalize, structural_errors, worst_level


def changed(**mutate) -> dict:
    """MINI_SPEC with a mutation applied by the given callable."""
    spec = copy.deepcopy(MINI_SPEC)
    for _name, fn in mutate.items():
        fn(spec)
    return spec


def find(changes, target):
    return [c for c in changes if c.target == target]


def test_identical_specs_have_no_changes():
    assert diff_specs(MINI_SPEC, copy.deepcopy(MINI_SPEC)) == []
    assert worst_level([]) is None


def test_path_removed_is_breaking():
    after = changed(drop=lambda s: s["paths"].pop("/v1/quota/"))
    change = find(diff_specs(MINI_SPEC, after), "/v1/quota/")[0]
    assert change.level is Level.BREAKING
    assert change.detail == "Path removed"


def test_path_added_is_info():
    after = changed(
        add=lambda s: s["paths"].update({"/v1/new/": {"get": {"responses": {"200": {}}}}})
    )
    assert find(diff_specs(MINI_SPEC, after), "/v1/new/")[0].level is Level.INFO


def test_method_removed_is_breaking_and_added_is_info():
    after = changed(
        swap=lambda s: s["paths"]["/v1/quota/"].update(
            {"post": s["paths"]["/v1/quota/"].pop("get")}
        )
    )
    changes = diff_specs(MINI_SPEC, after)
    assert find(changes, "GET /v1/quota/")[0].level is Level.BREAKING
    assert find(changes, "POST /v1/quota/")[0].level is Level.INFO


def test_security_added_is_breaking_removed_is_info():
    after = changed(
        secure=lambda s: s["paths"]["/trial/email-validation/"]["post"].update(
            {"security": [{"PublicBearer": []}]}
        )
    )
    change = find(diff_specs(MINI_SPEC, after), "POST /trial/email-validation/")[0]
    assert change.level is Level.BREAKING
    assert change.detail == "Security requirement added"

    change = find(diff_specs(after, MINI_SPEC), "POST /trial/email-validation/")[0]
    assert change.level is Level.INFO


def test_2xx_response_removed_is_breaking_added_is_info():
    after = changed(drop=lambda s: s["paths"]["/v1/quota/"]["get"]["responses"].pop("200"))
    change = find(diff_specs(MINI_SPEC, after), "GET /v1/quota/")[0]
    assert change.level is Level.BREAKING
    assert "200 removed" in change.detail

    after = changed(add=lambda s: s["paths"]["/v1/quota/"]["get"]["responses"].update({"202": {}}))
    assert find(diff_specs(MINI_SPEC, after), "GET /v1/quota/")[0].level is Level.INFO


def test_response_ref_swap_is_potentially_breaking():
    def swap(spec):
        content = spec["paths"]["/v1/quota/"]["get"]["responses"]["200"]["content"]
        content["application/json"]["schema"]["$ref"] = "#/components/schemas/QuotaInfo"

    change = find(diff_specs(MINI_SPEC, changed(swap=swap)), "GET /v1/quota/")[0]
    assert change.level is Level.POTENTIALLY_BREAKING
    assert "QuotaOut -> QuotaInfo" in change.detail


def test_response_field_removal_is_breaking_even_when_nested():
    after = changed(
        drop=lambda s: s["components"]["schemas"]["QuotaInfo"]["properties"].pop("remaining")
    )
    change = find(diff_specs(MINI_SPEC, after), "QuotaInfo.remaining")[0]
    assert change.level is Level.BREAKING
    assert change.detail == "Field removed from response schema"


def test_request_only_field_removal_is_info():
    # Every schema in MINI_SPEC is response-side, so add a request-only one.
    spec = copy.deepcopy(MINI_SPEC)
    spec["components"]["schemas"]["ValidationIn"] = {
        "type": "object",
        "properties": {"email": {"type": "string"}, "hint": {"type": "string"}},
        "required": ["email"],
    }
    spec["paths"]["/trial/email-validation/"]["post"]["requestBody"] = {
        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ValidationIn"}}}
    }
    after = copy.deepcopy(spec)
    after["components"]["schemas"]["ValidationIn"]["properties"].pop("hint")
    change = find(diff_specs(spec, after), "ValidationIn.hint")[0]
    assert change.level is Level.INFO
    assert "request" in change.detail


def test_new_required_request_field_is_breaking():
    spec = copy.deepcopy(MINI_SPEC)
    spec["components"]["schemas"]["ValidationIn"] = {
        "type": "object",
        "properties": {"email": {"type": "string"}, "hint": {"type": "string"}},
        "required": ["email"],
    }
    spec["paths"]["/trial/email-validation/"]["post"]["requestBody"] = {
        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ValidationIn"}}}
    }
    after = copy.deepcopy(spec)
    after["components"]["schemas"]["ValidationIn"]["required"].append("hint")
    change = find(diff_specs(spec, after), "ValidationIn.hint")[0]
    assert change.level is Level.BREAKING
    assert change.detail == "Now required in request"


def test_response_field_dropped_from_required_is_potentially_breaking():
    after = changed(relax=lambda s: s["components"]["schemas"]["QuotaOut"]["required"].clear())
    change = find(diff_specs(MINI_SPEC, after), "QuotaOut.quota")[0]
    assert change.level is Level.POTENTIALLY_BREAKING


def test_type_change_is_potentially_breaking():
    after = changed(
        retype=lambda s: s["components"]["schemas"]["QuotaInfo"]["properties"].update(
            {"remaining": {"type": "string"}}
        )
    )
    change = find(diff_specs(MINI_SPEC, after), "QuotaInfo.remaining")[0]
    assert change.level is Level.POTENTIALLY_BREAKING
    assert change.detail == "Type changed: integer -> string"


def test_nullable_widening_shows_up_as_a_type_change():
    after = changed(
        nullable=lambda s: s["components"]["schemas"]["QuotaInfo"]["properties"].update(
            {"remaining": {"anyOf": [{"type": "integer"}, {"type": "null"}]}}
        )
    )
    change = find(diff_specs(MINI_SPEC, after), "QuotaInfo.remaining")[0]
    assert change.level is Level.POTENTIALLY_BREAKING
    assert change.detail == "Type changed: integer -> anyOf(integer,null)"


def test_worst_level_ranking():
    changes = diff_specs(MINI_SPEC, changed(drop=lambda s: s["paths"].pop("/v1/quota/")))
    assert worst_level(changes) is Level.BREAKING


def test_normalize_is_stable_and_digest_ignores_key_order():
    reordered = {"components": MINI_SPEC["components"], **MINI_SPEC}
    assert normalize(MINI_SPEC) == normalize(reordered)
    assert digest(MINI_SPEC) == digest(reordered)
    assert normalize(MINI_SPEC).endswith("\n")


def test_structural_errors_on_the_real_baseline(baseline_spec):
    assert structural_errors(baseline_spec) == []


def test_structural_errors_flag_dangling_refs_and_missing_pieces():
    broken = {
        "openapi": "2.0",
        "paths": {
            "/x": {
                "get": {
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Ghost"}
                                }
                            }
                        }
                    }
                }
            }
        },
        "components": {"schemas": {"Real": {"type": "object"}}},
    }
    errors = structural_errors(broken)
    assert any("openapi version" in e for e in errors)
    assert any("securitySchemes" in e for e in errors)
    assert any("dangling $ref: #/components/schemas/Ghost" in e for e in errors)


def test_real_baseline_against_itself_is_clean(baseline_spec):
    assert diff_specs(baseline_spec, copy.deepcopy(baseline_spec)) == []


def test_removing_a_nested_prospect_field_from_the_real_spec_is_breaking(baseline_spec):
    after = copy.deepcopy(baseline_spec)
    after["components"]["schemas"]["EmailFinderProspectOut"]["properties"].pop("company_name")
    change = find(diff_specs(baseline_spec, after), "EmailFinderProspectOut.company_name")[0]
    assert change.level is Level.BREAKING
