"""Market session clock.

Whether the market is open gates every entry decision, so it must be answered
from something real. Alpaca's clock endpoint is authoritative (it knows the
holiday calendar and early closes) and is used whenever credentials exist.
Without credentials we fall back to a weekday/session-hours heuristic, which
does NOT know about market holidays or half-days — a limitation callers should
be aware of, and the returned reason makes it explicit.
"""

from __future__ import annotations

import logging
import time
import zoneinfo
from dataclasses import dataclass
from datetime import datetime
from datetime import time as dtime

from .config import MarketDataSettings

logger = logging.getLogger(__name__)

_EASTERN = "America/New_York"
_REGULAR_OPEN = dtime(9, 30)
_REGULAR_CLOSE = dtime(16, 0)
_CLOCK_CACHE_TTL = 30.0

_clock_cache: tuple[bool, float] | None = None


@dataclass(frozen=True)
class MarketSession:
    is_open: bool
    source: str
    reason: str = ""


def market_session(
    settings: MarketDataSettings | None = None,
    now: datetime | None = None,
) -> MarketSession:
    """Current session state, preferring the broker's authoritative clock."""
    resolved = settings or MarketDataSettings()
    if resolved.has_alpaca_credentials:
        session = _alpaca_session(resolved)
        if session is not None:
            return session
    return _heuristic_session(now)


def is_market_open(
    settings: MarketDataSettings | None = None,
    now: datetime | None = None,
) -> bool:
    return market_session(settings, now).is_open


def _alpaca_session(settings: MarketDataSettings) -> MarketSession | None:
    """Alpaca clock with a short TTL cache — this is polled every cycle."""
    global _clock_cache
    monotonic_now = time.monotonic()
    if _clock_cache is not None and (monotonic_now - _clock_cache[1]) < _CLOCK_CACHE_TTL:
        return MarketSession(is_open=_clock_cache[0], source="alpaca_clock_cached")
    try:
        from alpaca.trading.client import TradingClient

        client = TradingClient(
            api_key=settings.alpaca_api_key,
            secret_key=settings.alpaca_secret_key,
            paper=settings.alpaca_paper,
        )
        is_open = bool(client.get_clock().is_open)
        _clock_cache = (is_open, monotonic_now)
        return MarketSession(is_open=is_open, source="alpaca_clock")
    except Exception as exc:
        logger.warning("Alpaca clock unavailable (%s) — using session heuristic", exc)
        return None


def _heuristic_session(now: datetime | None = None) -> MarketSession:
    """Weekday 09:30-16:00 ET. Holiday-blind by design; see the module docstring."""
    try:
        eastern = zoneinfo.ZoneInfo(_EASTERN)
    except Exception:
        return MarketSession(
            is_open=True, source="heuristic", reason="timezone_database_unavailable"
        )
    current = (now or datetime.now(eastern)).astimezone(eastern)
    if current.weekday() >= 5:
        return MarketSession(is_open=False, source="heuristic", reason="weekend")
    if not (_REGULAR_OPEN <= current.time() <= _REGULAR_CLOSE):
        return MarketSession(is_open=False, source="heuristic", reason="outside_session_hours")
    return MarketSession(
        is_open=True, source="heuristic", reason="holiday_calendar_not_checked"
    )


def reset_clock_cache() -> None:
    """Drop the cached Alpaca clock answer. Used by tests."""
    global _clock_cache
    _clock_cache = None
