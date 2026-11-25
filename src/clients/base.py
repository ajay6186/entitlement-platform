"""Shared resilience for upstream calls: timeout, bounded retry with backoff+jitter, circuit breaker."""
import asyncio
import logging
import time

import httpx
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from src.config import Settings
from src.core.logging import correlation_id_var

log = logging.getLogger(__name__)
TRANSIENT_STATUS = {429, 502, 503, 504}


class UpstreamError(Exception):
    def __init__(self, system: str, message: str):
        super().__init__(f"{system}: {message}")
        self.system = system


class UpstreamUnavailable(UpstreamError):
    """Timeout, connection failure, 5xx after retries, or circuit open."""


class UpstreamConflict(UpstreamError):
    """Upstream rejected a write because of a version conflict."""


class _Transient(Exception):
    pass


class CircuitBreaker:
    """closed -> (N consecutive failures) -> open -> (reset timeout) -> half-open -> one trial call."""

    def __init__(self, name: str, failure_threshold: int, reset_seconds: float):
        self.name, self.failure_threshold, self.reset_seconds = name, failure_threshold, reset_seconds
        self.failures = 0
        self.opened_at: float | None = None

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed"
        return "half_open" if time.monotonic() - self.opened_at >= self.reset_seconds else "open"

    def allow(self) -> bool:
        return self.state != "open"

    def record_success(self) -> None:
        self.failures, self.opened_at = 0, None

    def record_failure(self) -> None:
        self.failures += 1
        if self.state == "half_open" or self.failures >= self.failure_threshold:
            if self.opened_at is None or self.state == "half_open":
                log.warning("circuit opened", extra={"upstream": self.name, "failures": self.failures})
            self.opened_at = time.monotonic()


class BaseUpstreamClient:
    system = "upstream"
    display_name = "upstream system"

    def __init__(self, http: httpx.AsyncClient, settings: Settings, breaker: CircuitBreaker):
        self.http, self.settings, self.breaker = http, settings, breaker

    async def _send(self, method: str, path: str, **kwargs) -> httpx.Response:
        headers = kwargs.pop("headers", {}) or {}
        if cid := correlation_id_var.get():
            headers["X-Correlation-ID"] = cid
        timeout = self.settings.upstream_connect_timeout + self.settings.upstream_read_timeout
        try:
            # wait_for also bounds in-process (ASGI) transports, which ignore httpx socket timeouts.
            resp = await asyncio.wait_for(self.http.request(method, path, headers=headers, **kwargs), timeout)
        except (httpx.TransportError, asyncio.TimeoutError) as exc:
            raise _Transient(type(exc).__name__) from exc
        if resp.status_code in TRANSIENT_STATUS:
            raise _Transient(f"HTTP {resp.status_code}")
        return resp

    async def request(self, method: str, path: str, *, retry: bool = True, **kwargs) -> httpx.Response:
        """Retry only transient failures, and only when the call is safe to repeat (`retry`)."""
        if not self.breaker.allow():
            raise UpstreamUnavailable(self.system, "circuit open")
        attempts = self.settings.upstream_max_attempts if retry else 1
        started = time.perf_counter()
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(attempts),
                wait=wait_exponential_jitter(initial=self.settings.upstream_backoff_initial,
                                             max=self.settings.upstream_backoff_max),
                retry=retry_if_exception_type(_Transient), reraise=True,
            ):
                with attempt:
                    resp = await self._send(method, path, **kwargs)
        except _Transient as exc:
            self.breaker.record_failure()
            log.warning("upstream call failed", extra={
                "upstream": self.system, "method": method, "path": path, "error": str(exc),
                "attempts": attempts, "breaker": self.breaker.state,
                "elapsed_ms": round((time.perf_counter() - started) * 1000)})
            raise UpstreamUnavailable(self.system, str(exc)) from exc
        self.breaker.record_success()
        return resp
