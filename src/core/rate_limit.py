"""In-memory token-bucket rate limiter keyed by (user, agent).

Good enough for a single process. With several replicas this moves to Redis (or the API gateway),
keeping the same key and header contract.
"""
import math
import time
from dataclasses import dataclass

from fastapi import Request

from src.config import Settings
from src.core.errors import ProblemException


@dataclass
class _Bucket:
    tokens: float
    updated: float


class RateLimiter:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._buckets: dict[str, _Bucket] = {}

    def _capacity(self, agent: bool) -> int:
        return self.settings.agent_rate_limit_per_minute if agent else self.settings.rate_limit_per_minute

    def check(self, request: Request, principal) -> None:
        capacity = self._capacity(principal.agent_id is not None)
        refill_per_sec = capacity / 60.0
        now = time.monotonic()
        bucket = self._buckets.setdefault(principal.rate_key, _Bucket(capacity, now))
        bucket.tokens = min(capacity, bucket.tokens + (now - bucket.updated) * refill_per_sec)
        bucket.updated = now
        allowed = bucket.tokens >= 1
        if allowed:
            bucket.tokens -= 1
        reset = math.ceil((capacity - bucket.tokens) / refill_per_sec) if refill_per_sec else 60
        # Exposed on every response by the middleware (IETF RateLimit header fields draft).
        request.state.rate_limit = {"RateLimit-Limit": str(capacity),
                                    "RateLimit-Remaining": str(int(bucket.tokens)),
                                    "RateLimit-Reset": str(reset)}
        if not allowed:
            retry_after = str(max(1, math.ceil((1 - bucket.tokens) / refill_per_sec)))
            raise ProblemException(429, "rate-limited", "Too many requests",
                                   f"Rate limit of {capacity} requests/minute exceeded.",
                                   headers={"Retry-After": retry_after, **request.state.rate_limit})

    def reset(self) -> None:
        self._buckets.clear()
