from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from strategy_service.earnings_calendar import is_earnings_blackout


def _make_cal(days_delta: int):
    """Mock yfinance calendar with earnings in `days_delta` days."""
    import pandas as pd

    target = datetime.now(timezone.utc).date() + timedelta(days=days_delta)
    target_ts = pd.Timestamp(target)
    df = pd.DataFrame({"Earnings Date": []}, columns=[target_ts])
    return df


def test_blackout_returns_bool():
    with patch("yfinance.Ticker") as mock:
        mock.return_value.calendar = _make_cal(1)
        result = is_earnings_blackout("AAPL")
    assert isinstance(result, bool)


def test_blackout_fails_open():
    with patch("yfinance.Ticker", side_effect=Exception("network error")):
        result = is_earnings_blackout("AAPL")
    assert result is False


def test_blackout_inactive_when_far():
    with patch("yfinance.Ticker") as mock:
        mock.return_value.calendar = _make_cal(10)
        result = is_earnings_blackout("AAPL", blackout_days=2)
    assert result is False


def test_blackout_active_when_near():
    with patch("yfinance.Ticker") as mock:
        mock.return_value.calendar = _make_cal(1)
        result = is_earnings_blackout("AAPL", blackout_days=2)
    assert result is True
