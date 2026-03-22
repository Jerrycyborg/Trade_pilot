"""Earnings blackout calendar via yfinance. Fails open — never blocks on error."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def is_earnings_blackout(symbol: str, blackout_days: int = 2) -> bool:
    """Return True if today is within blackout_days of the nearest earnings date.

    Uses yfinance ticker.calendar which returns a dict with key 'Earnings Date'
    containing a list of datetime.date objects.
    Fails open — returns False on any error so it never blocks trading.
    """
    try:
        import yfinance as yf
        cal = yf.Ticker(symbol).calendar
        if not cal:
            return False

        # yfinance returns dict: {'Earnings Date': [date, ...], ...}
        earnings_dates = cal.get("Earnings Date", [])
        if not earnings_dates:
            return False

        today = datetime.now(timezone.utc).date()
        for ed in earnings_dates:
            # ed is datetime.date or Timestamp
            ed_date = ed.date() if hasattr(ed, "date") and callable(ed.date) else ed
            delta = abs((ed_date - today).days)
            if delta <= blackout_days:
                logger.info(
                    "Earnings blackout: %s earnings %s (%d day delta)", symbol, ed_date, delta
                )
                return True
        return False
    except Exception as exc:
        logger.debug("Earnings calendar lookup failed for %s: %s — failing open", symbol, exc)
        return False
