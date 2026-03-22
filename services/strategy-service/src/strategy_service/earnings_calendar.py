"""Earnings blackout calendar via yfinance. Fails open — never blocks on error."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def is_earnings_blackout(symbol: str, blackout_days: int = 2) -> bool:
    """Return True if today is within blackout_days of the nearest earnings date."""
    try:
        import yfinance as yf

        ticker = yf.Ticker(symbol)
        cal = ticker.calendar
        if cal is None:
            return False

        today = datetime.now(timezone.utc).date()
        for col in (cal.columns if hasattr(cal, "columns") else []):
            try:
                if hasattr(col, "date"):
                    ed = col.date()
                else:
                    continue
                if abs((ed - today).days) <= blackout_days:
                    logger.info(
                        "Earnings blackout: %s earnings %s (%d day delta)",
                        symbol,
                        ed,
                        abs((ed - today).days),
                    )
                    return True
            except Exception:
                continue
        return False
    except Exception as exc:
        logger.debug("Earnings calendar lookup failed for %s: %s — failing open", symbol, exc)
        return False
