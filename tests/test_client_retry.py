from __future__ import annotations

import datetime as dt

import httpx
import pytest

from evaboot_probe.client import (
    BACKOFF_CAP_S,
    ProbeClient,
    Unreachable,
    backoff_delay,
    parse_retry_after,
)


def build(responses, **kwargs):
    """Client whose transport replays `responses` (Response or Exception) in order."""
    slept: list[float] = []
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    client = ProbeClient(
        transport=httpx.MockTransport(handler),
        sleep=slept.append,
        rand=lambda: 1.0,  # no jitter, so delays are exact
        **kwargs,
    )
    return client, slept


def test_retries_429_and_honours_retry_after():
    client, slept = build(
        [
            httpx.Response(429, headers={"retry-after": "2"}),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    attempt = client.request("GET", "/openapi.json")
    assert attempt.response.status_code == 200
    assert attempt.attempts == 2
    assert slept == [2.0]
    assert attempt.total_wait_s == 2.0


def test_retries_429_without_retry_after_uses_exponential_backoff():
    client, slept = build(
        [httpx.Response(429), httpx.Response(429), httpx.Response(200)], max_attempts=3
    )
    attempt = client.request("GET", "/openapi.json")
    assert attempt.attempts == 3
    assert slept == [0.5, 1.0]


def test_retries_500_then_succeeds():
    client, slept = build([httpx.Response(500), httpx.Response(200)])
    assert client.request("GET", "/openapi.json").attempts == 2
    assert len(slept) == 1


def test_does_not_retry_deterministic_4xx():
    client, slept = build([httpx.Response(422, json={"success": False})])
    attempt = client.request("POST", "/trial/email-validation/", json_body={})
    assert attempt.response.status_code == 422
    assert attempt.attempts == 1
    assert slept == []


def test_does_not_follow_or_retry_redirects():
    client, slept = build([httpx.Response(301, headers={"location": "/v1/quota/"})])
    attempt = client.request("GET", "/v1/quota")
    assert attempt.response.status_code == 301
    assert slept == []


def test_retries_timeouts_then_raises_unreachable():
    client, slept = build(
        [httpx.ReadTimeout("timed out"), httpx.ConnectError("refused"), httpx.ReadTimeout("again")],
        max_attempts=3,
    )
    with pytest.raises(Unreachable) as excinfo:
        client.request("GET", "/openapi.json")
    assert "3 attempt(s)" in str(excinfo.value)
    assert len(slept) == 2


def test_timeout_then_success_is_not_an_error():
    client, _ = build([httpx.ReadTimeout("timed out"), httpx.Response(200)])
    assert client.request("GET", "/openapi.json").attempts == 2


def test_absurd_retry_after_is_reported_not_awaited():
    client, slept = build([httpx.Response(429, headers={"retry-after": "600"})])
    attempt = client.request("GET", "/openapi.json")
    assert attempt.response.status_code == 429
    assert slept == []


def test_api_key_is_redacted_from_errors():
    client, _ = build([httpx.ConnectError("boom")], max_attempts=1, api_key="secret-key-123")
    with pytest.raises(Unreachable) as excinfo:
        client.request("GET", "/v1/quota/")
    assert "secret-key-123" not in str(excinfo.value)


def test_auth_header_schemes():
    client, _ = build([], api_key="k")
    assert client.auth_header() == {"Authorization": "Bearer k"}
    assert client.auth_header("token") == {"Authorization": "Token k"}
    client_no_key, _ = build([])
    assert client_no_key.auth_header() == {}


def test_backoff_is_bounded():
    assert backoff_delay(0, rand=lambda: 1.0) == 0.5
    assert backoff_delay(10, rand=lambda: 1.0) == BACKOFF_CAP_S
    assert 0.0 <= backoff_delay(3, rand=lambda: 0.0) <= BACKOFF_CAP_S


@pytest.mark.parametrize(
    "value,expected", [("5", 5.0), ("0", 0.0), ("", None), (None, None), ("soon", None)]
)
def test_parse_retry_after_seconds(value, expected):
    assert parse_retry_after(value) == expected


def test_parse_retry_after_http_date():
    future = dt.datetime.now(tz=dt.UTC) + dt.timedelta(seconds=30)
    header = future.strftime("%a, %d %b %Y %H:%M:%S GMT")
    parsed = parse_retry_after(header)
    assert parsed is not None and 25 <= parsed <= 31


def test_parse_retry_after_past_date_is_zero():
    past = (dt.datetime.now(tz=dt.UTC) - dt.timedelta(hours=1)).strftime(
        "%a, %d %b %Y %H:%M:%S GMT"
    )
    assert parse_retry_after(past) == 0.0
