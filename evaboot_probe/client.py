"""HTTP client with bounded retries.

Evaboot's OpenAPI document declares only 200 and 202 responses, so there is no
documented 429 or 5xx behaviour and no rate-limit headers were observed on any
response. The retry policy here is therefore generic HTTP semantics (RFC 9110)
rather than anything Evaboot publishes, and it is exercised against mock
transports in the test suite instead of against their production API.
"""

from __future__ import annotations

import datetime as dt
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from email.utils import parsedate_to_datetime

import httpx

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

# Deterministic 4xx (401, 403, 404, 405, 422) are never retried: the answer will
# not change, and retrying only adds latency to a failing CI job. 3xx is data,
# not an error, so it is returned to the caller untouched.
RETRY_EXCEPTIONS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
)

BACKOFF_BASE_S = 0.5
BACKOFF_CAP_S = 8.0
MAX_TOTAL_WAIT_S = 20.0

# Evaboot serves none of these today. Recording them centrally means the day one
# appears, the probe reports it instead of silently ignoring it.
RATE_LIMIT_HEADER_PREFIXES = ("x-ratelimit", "ratelimit-", "x-rate-limit", "retry-after")


class Unreachable(Exception):
    """The target did not answer at all after the retry budget was spent."""


@dataclass
class Attempt:
    """A response plus what it cost to get it."""

    response: httpx.Response
    attempts: int
    latency_ms: int
    total_wait_s: float = 0.0


def backoff_delay(attempt: int, rand: Callable[[], float] = random.random) -> float:
    """Full-jitter exponential backoff for retry number ``attempt`` (0-based)."""
    return min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2**attempt) * rand()


def parse_retry_after(value: str | None) -> float | None:
    """Parse a Retry-After header (delta-seconds or HTTP-date). None if unusable."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(int(value)))
    except ValueError:
        pass
    try:
        target = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if target is None:
        return None
    now = dt.datetime.now(tz=target.tzinfo or dt.UTC)
    return max(0.0, (target - now).total_seconds())


class ProbeClient:
    """Small httpx wrapper: explicit timeouts, bounded retries, redacted errors."""

    def __init__(
        self,
        base_url: str = "https://api.evaboot.com",
        api_key: str | None = None,
        auth_scheme: str = "bearer",
        timeout: float = 15.0,
        max_attempts: int = 3,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rand: Callable[[], float] = random.random,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.auth_scheme = auth_scheme
        self.max_attempts = max(1, max_attempts)
        self._sleep = sleep
        self._rand = rand
        self.seen_rate_limit_headers: dict[str, str] = {}
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout, connect=5.0),
            transport=transport,
            follow_redirects=False,
            headers={"User-Agent": "evaboot-probe/0.1 (+reliability check)"},
        )

    def __enter__(self) -> ProbeClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def redact(self, text: str) -> str:
        return text.replace(self.api_key, "***") if self.api_key else text

    def auth_header(self, scheme: str | None = None) -> dict[str, str]:
        if not self.api_key:
            return {}
        prefix = "Token" if (scheme or self.auth_scheme) == "token" else "Bearer"
        return {"Authorization": f"{prefix} {self.api_key}"}

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> Attempt:
        """Send a request, retrying transient failures within a bounded budget.

        Retrying POST is safe for this tool because the only POSTs it ever sends
        are non-mutating probes (the opt-in trial endpoints); it never creates a
        job, list or extraction.
        """
        total_wait = 0.0
        last_exc: Exception | None = None

        for i in range(self.max_attempts):
            started = time.monotonic()
            try:
                response = self._client.request(method, path, json=json_body, headers=headers)
            except RETRY_EXCEPTIONS as exc:
                last_exc = exc
                if not self._should_wait(i, total_wait):
                    break
                total_wait += self._wait(i, None)
                continue

            latency_ms = int((time.monotonic() - started) * 1000)
            self._note_rate_limit_headers(response)
            if response.status_code in RETRY_STATUSES and self._should_wait(i, total_wait):
                retry_after = parse_retry_after(response.headers.get("retry-after"))
                if retry_after is not None and total_wait + retry_after > MAX_TOTAL_WAIT_S:
                    # Waiting this long would make the probe the outage. Report the
                    # 429 and let the caller decide.
                    return Attempt(response, i + 1, latency_ms, total_wait)
                total_wait += self._wait(i, retry_after)
                continue

            return Attempt(response, i + 1, latency_ms, total_wait)

        detail = type(last_exc).__name__ if last_exc else "retry budget exhausted"
        message = (
            f"{method} {self.base_url}{path} failed after {self.max_attempts} attempt(s): {detail}"
        )
        raise Unreachable(self.redact(message)) from last_exc

    def _note_rate_limit_headers(self, response: httpx.Response) -> None:
        for name, value in response.headers.items():
            if name.lower().startswith(RATE_LIMIT_HEADER_PREFIXES):
                self.seen_rate_limit_headers[name.lower()] = value

    def _should_wait(self, attempt_index: int, total_wait: float) -> bool:
        return attempt_index < self.max_attempts - 1 and total_wait < MAX_TOTAL_WAIT_S

    def _wait(self, attempt_index: int, retry_after: float | None) -> float:
        delay = retry_after if retry_after is not None else backoff_delay(attempt_index, self._rand)
        self._sleep(delay)
        return delay
