"""Per-caller rate limits: a token bucket per API key (MCP) or per user (chat).

Each caller may make `per_minute` requests, refilled continuously, with bursts
up to that many. The state lives in the process, so with several replicas the
effective limit is per replica; a shared limit would need Redis or similar.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

_PRUNE_EVERY = 1000
_IDLE_SECONDS = 600


@dataclass
class _Bucket:
    tokens: float
    updated: float


class RateLimiter:
    def __init__(self, per_minute: int) -> None:
        # 0 or less turns limiting off.
        self.per_minute = per_minute
        self._buckets: dict[str, _Bucket] = {}
        self._calls = 0

    def check(self, caller: str) -> float | None:
        """Take one request from the caller's allowance.

        Returns None if allowed, or how many seconds until the next request would be.
        """
        if self.per_minute <= 0:
            return None
        now = time.monotonic()
        rate = self.per_minute / 60.0
        bucket = self._buckets.get(caller)
        if bucket is None:
            bucket = self._buckets[caller] = _Bucket(tokens=float(self.per_minute), updated=now)
        else:
            bucket.tokens = min(float(self.per_minute), bucket.tokens + (now - bucket.updated) * rate)
            bucket.updated = now
        self._prune(now)
        if bucket.tokens >= 1:
            bucket.tokens -= 1
            return None
        return (1 - bucket.tokens) / rate

    def _prune(self, now: float) -> None:
        # Forget callers who have been quiet a while, so memory stays bounded.
        self._calls += 1
        if self._calls % _PRUNE_EVERY == 0:
            for key in [k for k, b in self._buckets.items() if now - b.updated > _IDLE_SECONDS]:
                del self._buckets[key]
