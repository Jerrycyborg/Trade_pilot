"""Simple in-memory per-IP token bucket rate limiter. No Redis dependency."""
from __future__ import annotations

import time
from collections import defaultdict
from threading import Lock

from fastapi import HTTPException, Request


class TokenBucket:
    """Per-key token bucket: max_tokens replenished at rate tokens/minute."""

    def __init__(self, max_tokens: int = 10, refill_rate_per_minute: int = 10) -> None:
        self._max = max_tokens
        self._rate = refill_rate_per_minute / 60.0
        self._buckets: dict[str, tuple[float, float]] = defaultdict(
            lambda: (float(max_tokens), time.monotonic())
        )
        self._lock = Lock()

    def consume(self, key: str) -> bool:
        """Try to consume 1 token. Returns True if allowed, False if rate-limited."""
        with self._lock:
            tokens, last_refill = self._buckets[key]
            now = time.monotonic()
            elapsed = now - last_refill
            tokens = min(self._max, tokens + elapsed * self._rate)
            if tokens < 1.0:
                self._buckets[key] = (tokens, now)
                return False
            self._buckets[key] = (tokens - 1.0, now)
            return True


_write_bucket = TokenBucket(max_tokens=10, refill_rate_per_minute=10)


def rate_limit_write(request: Request) -> None:
    """FastAPI dependency: rate-limits write endpoints to 10 req/min per IP."""
    ip = getattr(request.client, "host", "unknown") if request.client else "unknown"
    if not _write_bucket.consume(ip):
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded. Max 10 write requests per minute.",
        )
