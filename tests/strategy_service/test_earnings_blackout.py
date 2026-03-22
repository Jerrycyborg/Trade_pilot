"""Tests for earnings_calendar.is_earnings_blackout.

Uses realistic yfinance dict shape: {'Earnings Date': [datetime.date, ...]}
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from strategy_service.earnings_calendar import is_earnings_blackout


def _mock_ticker(days_delta: int | None):
    """Return a mock yf.Ticker whose .calendar matches real yfinance dict shape."""
    mock = MagicMock()
    if days_delta is None:
        mock.calendar = {}
    else:
        target = datetime.now(timezone.utc).date() + timedelta(days=days_delta)
        mock.calendar = {"Earnings Date": [target]}
    return mock


def test_blackout_returns_bool():
    with patch("yfinance.Ticker", return_value=_mock_ticker(1)):
        result = is_earnings_blackout("AAPL")
    assert isinstance(result, bool)


def test_blackout_fails_open():
    with patch("yfinance.Ticker", side_effect=Exception("network error")):
        result = is_earnings_blackout("AAPL")
    assert result is False


def test_blackout_inactive_when_far():
    with patch("yfinance.Ticker", return_value=_mock_ticker(10)):
        result = is_earnings_blackout("AAPL", blackout_days=2)
    assert result is False


def test_blackout_active_when_near():
    with patch("yfinance.Ticker", return_value=_mock_ticker(1)):
        result = is_earnings_blackout("AAPL", blackout_days=2)
    assert result is True


def test_blackout_no_earnings_date():
    with patch("yfinance.Ticker", return_value=_mock_ticker(None)):
        result = is_earnings_blackout("AAPL")
    assert result is False


def test_blackout_on_exact_day():
    with patch("yfinance.Ticker", return_value=_mock_ticker(0)):
        result = is_earnings_blackout("AAPL", blackout_days=2)
    assert result is True
