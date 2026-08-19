from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = REPO_ROOT / "schemas" / "openapi.baseline.json"

MINI_SPEC = {
    "openapi": "3.1.0",
    "info": {"title": "Evaboot API", "version": "1.0"},
    "paths": {
        "/v1/quota/": {
            "get": {
                "security": [{"PublicBearer": []}],
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/QuotaOut"}
                            }
                        }
                    }
                },
            }
        },
        "/trial/email-validation/": {
            "post": {
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/EmailValidationTrialOut"}
                            }
                        }
                    }
                }
            }
        },
    },
    "components": {
        "securitySchemes": {"PublicBearer": {"type": "http", "scheme": "bearer"}},
        "schemas": {
            "QuotaOut": {
                "type": "object",
                "properties": {
                    "success": {"type": "boolean"},
                    "quota": {"$ref": "#/components/schemas/QuotaInfo"},
                },
                "required": ["quota"],
            },
            "QuotaInfo": {
                "type": "object",
                "properties": {
                    "daily_limit": {"type": "integer"},
                    "remaining": {"type": "integer"},
                    "has_valid_salesnav": {"type": "boolean"},
                },
            },
            "EmailValidationTrialOut": {
                "type": "object",
                "properties": {"email": {"type": "string"}, "status": {"type": "string"}},
            },
        },
    },
}

JSON_HEADERS = {"content-type": "application/json; charset=utf-8"}
HTML_HEADERS = {"content-type": "text/html; charset=utf-8"}
ERROR_ENVELOPE = {
    "success": False,
    "error": {"type": "AuthenticationFailed", "message": "Unauthorized"},
}


def json_response(status: int, body: object, headers: dict | None = None):
    return lambda: httpx.Response(
        status, content=json.dumps(body), headers={**JSON_HEADERS, **(headers or {})}
    )


def html_response(status: int, text: str = "nope", headers: dict | None = None):
    return lambda: httpx.Response(status, text=text, headers={**HTML_HEADERS, **(headers or {})})


def make_transport(routes: dict, record: list | None = None) -> httpx.MockTransport:
    """Route (METHOD, path) or path to a zero-arg Response factory."""

    def handler(request: httpx.Request) -> httpx.Response:
        if record is not None:
            record.append((request.method, request.url.path))
        factory = routes.get((request.method, request.url.path)) or routes.get(request.url.path)
        if factory is None:
            return httpx.Response(404, text="unrouted", headers=HTML_HEADERS)
        return factory()

    return httpx.MockTransport(handler)


def evaboot_routes(overrides: dict | None = None) -> dict:
    """The live public surface as measured on 2026-08-19."""
    routes = {
        "/openapi.json": json_response(200, MINI_SPEC),
        ("GET", "/v1/quota/"): json_response(401, ERROR_ENVELOPE),
        ("GET", "/v1/quota"): html_response(301, "", {"location": "/v1/quota/"}),
        ("GET", "/v1/evaboot-probe-unknown-path/"): html_response(404),
        ("POST", "/trial/email-validation/"): json_response(
            200, {"email": "noreply@example.com", "status": "invalid"}
        ),
    }
    routes.update(overrides or {})
    return routes


@pytest.fixture
def baseline_spec() -> dict:
    with BASELINE_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


@pytest.fixture
def recorded() -> list:
    return []
