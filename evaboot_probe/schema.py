"""Fetch, normalize and diff Evaboot's OpenAPI document.

This is a change *classifier*, not an OpenAPI compatibility engine. It compares
the structure that clients actually depend on (paths, methods, security,
required request fields, 2xx codes, response schema targets, properties and
their type signatures) and leaves deeper composition semantics alone.
"""

from __future__ import annotations

import hashlib
import json

from .client import Attempt, ProbeClient
from .reporting import Change, Level

SPEC_PATH = "/openapi.json"
HTTP_METHODS = ("get", "post", "put", "patch", "delete")


class SpecError(Exception):
    """The document we fetched or loaded is not a usable OpenAPI spec."""


def fetch_spec(client: ProbeClient, path: str = SPEC_PATH) -> tuple[dict, Attempt]:
    attempt = client.request("GET", path)
    if attempt.response.status_code != 200:
        raise SpecError(f"GET {path} returned HTTP {attempt.response.status_code}")
    try:
        spec = attempt.response.json()
    except ValueError as exc:
        raise SpecError(f"GET {path} did not return valid JSON: {exc}") from exc
    if not isinstance(spec, dict):
        raise SpecError(f"GET {path} returned JSON but not an object")
    return spec, attempt


def load_spec(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            spec = json.load(handle)
    except OSError as exc:
        raise SpecError(f"cannot read {path}: {exc}") from exc
    except ValueError as exc:
        raise SpecError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(spec, dict):
        raise SpecError(f"{path} is not a JSON object")
    return spec


def normalize(spec: dict) -> str:
    """Stable serialization, so a diff reflects content and not key ordering."""
    return json.dumps(spec, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def digest(spec: dict) -> str:
    return hashlib.sha256(normalize(spec).encode("utf-8")).hexdigest()


def structural_errors(spec: dict) -> list[str]:
    """Cheap sanity checks. Not a full OpenAPI validation."""
    errors: list[str] = []
    version = spec.get("openapi")
    if not (isinstance(version, str) and version.startswith("3.")):
        errors.append(f"unexpected openapi version: {version!r}")

    paths = spec.get("paths")
    if not isinstance(paths, dict) or not paths:
        errors.append("paths is missing or empty")
    else:
        for path, operations in paths.items():
            if not isinstance(operations, dict):
                errors.append(f"{path}: operations are not an object")
                continue
            for method, operation in operations.items():
                if method in HTTP_METHODS and not (
                    isinstance(operation, dict) and operation.get("responses")
                ):
                    errors.append(f"{method.upper()} {path}: no responses declared")

    schemas = spec.get("components", {}).get("schemas")
    if not isinstance(schemas, dict) or not schemas:
        errors.append("components.schemas is missing or empty")
        schemas = {}
    if not spec.get("components", {}).get("securitySchemes"):
        errors.append("components.securitySchemes is missing")

    for name in sorted(_refs(spec)):
        if name not in schemas:
            errors.append(f"dangling $ref: #/components/schemas/{name}")
    return errors


def _refs(node: object) -> set[str]:
    """Every component schema name referenced anywhere under ``node``."""
    found: set[str] = set()
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            ref = current.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
                found.add(ref.rsplit("/", 1)[-1])
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return found


def _expand(spec: dict, names: set[str]) -> set[str]:
    schemas = spec.get("components", {}).get("schemas", {})
    seen: set[str] = set()
    todo = list(names)
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        todo.extend(_refs(schemas.get(name, {})))
    return seen


def _reachable(spec: dict) -> tuple[set[str], set[str]]:
    """Component schemas reachable from responses, and from request bodies."""
    response_refs: set[str] = set()
    request_refs: set[str] = set()
    for operations in spec.get("paths", {}).values():
        if not isinstance(operations, dict):
            continue
        for method, operation in operations.items():
            if method not in HTTP_METHODS or not isinstance(operation, dict):
                continue
            response_refs |= _refs(operation.get("responses", {}))
            request_refs |= _refs(operation.get("requestBody") or {})
    return _expand(spec, response_refs), _expand(spec, request_refs)


def _type_signature(schema: dict) -> str:
    if not isinstance(schema, dict):
        return "unknown"
    if "$ref" in schema:
        return "$ref:" + str(schema["$ref"]).rsplit("/", 1)[-1]
    declared = schema.get("type")
    if declared == "array":
        return f"array[{_type_signature(schema.get('items', {}))}]"
    if declared:
        return str(declared)
    for keyword in ("anyOf", "oneOf", "allOf"):
        if keyword in schema:
            members = sorted(_type_signature(member) for member in schema[keyword])
            return f"{keyword}({','.join(members)})"
    return "unknown"


def _json_schema_ref(response: dict) -> str | None:
    schema = response.get("content", {}).get("application/json", {}).get("schema", {})
    ref = schema.get("$ref")
    return str(ref).rsplit("/", 1)[-1] if isinstance(ref, str) else None


def diff_specs(old: dict, new: dict) -> list[Change]:
    changes: list[Change] = []
    old_paths = old.get("paths", {})
    new_paths = new.get("paths", {})

    for path in sorted(set(old_paths) - set(new_paths)):
        changes.append(Change(Level.BREAKING, path, "Path removed"))
    for path in sorted(set(new_paths) - set(old_paths)):
        changes.append(Change(Level.INFO, path, "Path added"))

    for path in sorted(set(old_paths) & set(new_paths)):
        old_ops = {m: o for m, o in old_paths[path].items() if m in HTTP_METHODS}
        new_ops = {m: o for m, o in new_paths[path].items() if m in HTTP_METHODS}
        for method in sorted(set(old_ops) - set(new_ops)):
            changes.append(Change(Level.BREAKING, f"{method.upper()} {path}", "Method removed"))
        for method in sorted(set(new_ops) - set(old_ops)):
            changes.append(Change(Level.INFO, f"{method.upper()} {path}", "Method added"))
        for method in sorted(set(old_ops) & set(new_ops)):
            changes += _diff_operation(f"{method.upper()} {path}", old_ops[method], new_ops[method])

    changes += _diff_schemas(old, new)
    return changes


def _diff_operation(target: str, old: dict, new: dict) -> list[Change]:
    changes: list[Change] = []

    was_secured = bool(old.get("security"))
    is_secured = bool(new.get("security"))
    if is_secured and not was_secured:
        changes.append(Change(Level.BREAKING, target, "Security requirement added"))
    elif was_secured and not is_secured:
        changes.append(Change(Level.INFO, target, "Security requirement removed"))

    old_responses = old.get("responses", {})
    new_responses = new.get("responses", {})
    old_2xx = {c for c in old_responses if str(c).startswith("2")}
    new_2xx = {c for c in new_responses if str(c).startswith("2")}
    for code in sorted(old_2xx - new_2xx):
        changes.append(Change(Level.BREAKING, target, f"Success response {code} removed"))
    for code in sorted(new_2xx - old_2xx):
        changes.append(Change(Level.INFO, target, f"Success response {code} added"))

    for code in sorted(old_2xx & new_2xx):
        old_ref = _json_schema_ref(old_responses[code])
        new_ref = _json_schema_ref(new_responses[code])
        if old_ref != new_ref:
            changes.append(
                Change(
                    Level.POTENTIALLY_BREAKING,
                    target,
                    f"Response {code} schema changed: {old_ref} -> {new_ref}",
                )
            )
    return changes


def _diff_schemas(old: dict, new: dict) -> list[Change]:
    """Diff component schemas, attributing impact by where they are used.

    A field disappearing from a response breaks readers; the same field
    disappearing from a request body only means clients can stop sending it.
    """
    changes: list[Change] = []
    old_schemas = old.get("components", {}).get("schemas", {})
    new_schemas = new.get("components", {}).get("schemas", {})
    old_response_side, old_request_side = _reachable(old)
    new_response_side, new_request_side = _reachable(new)

    for name in sorted(set(old_schemas) - set(new_schemas)):
        changes.append(Change(Level.INFO, name, "Schema removed"))
    for name in sorted(set(new_schemas) - set(old_schemas)):
        changes.append(Change(Level.INFO, name, "Schema added"))

    for name in sorted(set(old_schemas) & set(new_schemas)):
        in_response = name in old_response_side or name in new_response_side
        in_request = name in old_request_side or name in new_request_side
        old_props = old_schemas[name].get("properties", {})
        new_props = new_schemas[name].get("properties", {})

        for prop in sorted(set(old_props) - set(new_props)):
            level = Level.BREAKING if in_response else Level.INFO
            where = "response" if in_response else "request"
            changes.append(Change(level, f"{name}.{prop}", f"Field removed from {where} schema"))
        for prop in sorted(set(new_props) - set(old_props)):
            changes.append(Change(Level.INFO, f"{name}.{prop}", "Field added"))
        for prop in sorted(set(old_props) & set(new_props)):
            before = _type_signature(old_props[prop])
            after = _type_signature(new_props[prop])
            if before != after:
                changes.append(
                    Change(
                        Level.POTENTIALLY_BREAKING,
                        f"{name}.{prop}",
                        f"Type changed: {before} -> {after}",
                    )
                )

        old_required = set(old_schemas[name].get("required", []))
        new_required = set(new_schemas[name].get("required", []))
        for prop in sorted(new_required - old_required):
            level = Level.BREAKING if in_request else Level.INFO
            detail = (
                "Now required in request"
                if in_request
                else "Now required in response (clients unaffected)"
            )
            changes.append(Change(level, f"{name}.{prop}", detail))
        for prop in sorted(old_required - new_required):
            level = Level.POTENTIALLY_BREAKING if in_response else Level.INFO
            detail = (
                "No longer guaranteed in response"
                if in_response
                else "No longer required in request"
            )
            changes.append(Change(level, f"{name}.{prop}", detail))

    return changes


def worst_level(changes: list[Change]) -> Level | None:
    for level in (Level.BREAKING, Level.POTENTIALLY_BREAKING, Level.INFO):
        if any(c.level is level for c in changes):
            return level
    return None


def validate_payload(spec: dict, schema_name: str, payload: object) -> list[str]:
    """Validate ``payload`` against one component schema of ``spec``.

    OpenAPI 3.1 schemas are JSON Schema 2020-12, so the spec document doubles as
    the schema document: pointing ``$ref`` at the root resolves every internal
    ``#/components/schemas/...`` reference without a separate registry.
    """
    from jsonschema import Draft202012Validator

    schemas = spec.get("components", {}).get("schemas", {})
    if schema_name not in schemas:
        raise SpecError(f"spec has no component schema named {schema_name!r}")
    validator = Draft202012Validator({**spec, "$ref": f"#/components/schemas/{schema_name}"})
    return [
        f"{'.'.join(str(p) for p in error.absolute_path) or '<root>'}: {error.message}"
        for error in sorted(validator.iter_errors(payload), key=lambda e: list(e.absolute_path))
    ]
