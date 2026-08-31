"""Earnings blackout calendar via yfinance.

The first live paper run found this gate absent twice over: the calendar
lookup failed under an egress policy that blocks yfinance and the failure was
logged at debug — a guard that was not there, silently — and the strategy
worker never consulted it at all, hard-coding `event_blackout_active=False`
into every policy request. Both are fixed at this seam:

- The check returns a verdict, not a bool. `BlackoutCheck.checked` says
  whether the calendar actually answered; "no blackout" and "could not ask"
  are different findings and are not merged.
- The failure mode is explicit configuration, not a hard-coded silent choice:
  `EARNINGS_GATE_FAIL_CLOSED=true` treats an unanswerable calendar as a
  blackout (entries refused by policy), the default `false` fails open — and
  says so at WARNING the first time, so an operator can see the gate is open.
  An unparseable value is refused rather than ignored: a guard running on a
  default the operator believes they replaced is a guard nobody configured.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Earnings dates do not move intraday, and the worker asks once per symbol per
# cycle. A verdict is cached so the calendar is consulted rather than hammered;
# a failure is retried much sooner than a success is refreshed, so a recovered
# calendar closes the gap quickly.
_SUCCESS_TTL_SECONDS = 6 * 3600.0
_FAILURE_TTL_SECONDS = 600.0

_lock = threading.Lock()
_cache: dict[tuple[str, int], tuple[float, "BlackoutCheck"]] = {}
_warned_open: set[str] = set()


@dataclass(frozen=True)
class BlackoutCheck:
    """What the gate can honestly say about one symbol right now."""

    active: bool
    """Entries should be treated as inside an earnings blackout. True either
    because the calendar said so, or because it could not answer and the gate
    is configured to fail closed."""

    checked: bool
    """The calendar actually answered. False means `active` is the configured
    failure posture, not a fact about earnings."""

    reason: str


def reset_earnings_gate() -> None:
    """Forget cached verdicts and warning state. A test seam."""
    with _lock:
        _cache.clear()
        _warned_open.clear()


def _fail_closed() -> bool:
    raw = os.getenv("EARNINGS_GATE_FAIL_CLOSED", "false").strip().lower()
    if raw not in {"true", "false"}:
        raise ValueError(
            f"EARNINGS_GATE_FAIL_CLOSED must be 'true' or 'false', got {raw!r}"
        )
    return raw == "true"


def check_earnings_blackout(symbol: str, blackout_days: int = 2) -> BlackoutCheck:
    """The gate's verdict for one symbol, cached, never raising on lookup.

    Only a misconfigured failure mode raises — before any lookup, so garbage
    configuration surfaces on the first call rather than on the first outage.
    """
    fail_closed = _fail_closed()
    key = (symbol.upper(), int(blackout_days))
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit is not None and hit[0] > now:
            return hit[1]

    check = _consult_calendar(symbol.upper(), blackout_days, fail_closed)
    ttl = _SUCCESS_TTL_SECONDS if check.checked else _FAILURE_TTL_SECONDS
    with _lock:
        _cache[key] = (now + ttl, check)
    return check


def _consult_calendar(symbol: str, blackout_days: int, fail_closed: bool) -> BlackoutCheck:
    try:
        import yfinance as yf

        cal = yf.Ticker(symbol).calendar
        earnings_dates = (cal or {}).get("Earnings Date", [])
        today = datetime.now(timezone.utc).date()
        for ed in earnings_dates:
            # ed is datetime.date or Timestamp
            ed_date = ed.date() if hasattr(ed, "date") and callable(ed.date) else ed
            delta = abs((ed_date - today).days)
            if delta <= blackout_days:
                logger.info(
                    "Earnings blackout: %s earnings %s (%d day delta)",
                    symbol, ed_date, delta,
                )
                _note_recovered(symbol)
                return BlackoutCheck(
                    active=True, checked=True, reason=f"earnings {ed_date} ({delta}d away)"
                )
        _note_recovered(symbol)
        return BlackoutCheck(
            active=False, checked=True, reason="no earnings inside the blackout window"
        )
    except Exception as exc:
        return _unanswered(symbol, exc, fail_closed)


def _unanswered(symbol: str, exc: Exception, fail_closed: bool) -> BlackoutCheck:
    """The calendar could not answer. Whatever the posture, it is not silent."""
    if fail_closed:
        logger.warning(
            "Earnings calendar unreachable for %s (%s) — failing CLOSED: entries "
            "are treated as inside a blackout until it recovers", symbol, exc,
        )
        return BlackoutCheck(
            active=True, checked=False, reason=f"calendar unreachable, fail-closed: {exc}"
        )
    with _lock:
        first = symbol not in _warned_open
        _warned_open.add(symbol)
    # WARNING on the transition, debug on repeats: visible without drowning the
    # log at one line per symbol per cycle for the life of an outage.
    log = logger.warning if first else logger.debug
    log(
        "The earnings gate is OPEN for %s: calendar unreachable (%s). Entries are "
        "NOT protected around earnings until it recovers. Set "
        "EARNINGS_GATE_FAIL_CLOSED=true to refuse entries instead.", symbol, exc,
    )
    return BlackoutCheck(
        active=False, checked=False, reason=f"calendar unreachable, failing open: {exc}"
    )


def _note_recovered(symbol: str) -> None:
    with _lock:
        was_open = symbol in _warned_open
        _warned_open.discard(symbol)
    if was_open:
        logger.info("Earnings gate recovered for %s — the calendar answers again", symbol)


def is_earnings_blackout(symbol: str, blackout_days: int = 2) -> bool:
    """The verdict's `active` alone, for callers that only gate on it.

    Prefer `check_earnings_blackout`: this collapses "no blackout" and "could
    not ask, failing open" into the same False.
    """
    return check_earnings_blackout(symbol, blackout_days).active
