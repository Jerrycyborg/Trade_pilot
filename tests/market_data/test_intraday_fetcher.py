from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from market_data.config import YAHOO_INTRADAY_MAX_DAYS, MarketDataSettings
from market_data.fetcher import (
    AlpacaFetcher,
    DataUnavailableError,
    YahooFinanceFetcher,
    _nearest_yahoo_interval,
    fetch_bars,
    get_fetcher,
)
from market_data.models import OHLCVBar


def _mock_bar(close: float = 100.0) -> MagicMock:
    bar = MagicMock()
    bar.timestamp = datetime.now(timezone.utc)
    bar.open = close - 1
    bar.high = close + 1
    bar.low = close - 2
    bar.close = close
    bar.volume = 1000.0
    return bar


def _ohlcv(symbol: str = "AAPL", close: float = 100.0, minutes_ago: int = 0) -> OHLCVBar:
    return OHLCVBar(
        symbol=symbol,
        timestamp=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
        open=close,
        high=close,
        low=close,
        close=close,
        volume=1000.0,
    )


@pytest.fixture
def intraday_env(monkeypatch: pytest.MonkeyPatch) -> MarketDataSettings:
    monkeypatch.setenv("MARKET_DATA_TIMEFRAME", "intraday")
    monkeypatch.setenv("INTRADAY_MINUTES", "15")
    monkeypatch.setenv("INTRADAY_LOOKBACK_DAYS", "5")
    return MarketDataSettings()


def test_fetch_intraday_calls_alpaca_minute_bars() -> None:
    settings = MarketDataSettings(alpaca_api_key="key", alpaca_secret_key="secret")
    fetcher = AlpacaFetcher(settings)
    mock_bar = _mock_bar()

    with patch("alpaca.data.historical.StockHistoricalDataClient") as mock_client:
        mock_instance = MagicMock()
        mock_client.return_value = mock_instance
        mock_instance.get_stock_bars.return_value = {"AAPL": [mock_bar]}

        bars = fetcher.fetch_intraday("AAPL", period_days=1, timeframe_minutes=15)

    assert isinstance(bars, list)
    assert len(bars) == 1
    assert bars[0].symbol == "AAPL"
    assert bars[0].close == 100.0


def test_intraday_without_alpaca_uses_yahoo_intraday(intraday_env: MarketDataSettings) -> None:
    """Regression: intraday used to silently degrade to DAILY bars without an
    Alpaca key, so an 'intraday' deployment traded on daily indicators."""
    assert intraday_env.is_intraday
    fetcher = get_fetcher(intraday_env)
    assert isinstance(fetcher, YahooFinanceFetcher)

    expected = [_ohlcv()]
    with patch.object(
        YahooFinanceFetcher, "fetch_intraday", return_value=expected
    ) as mock_intraday:
        with patch.object(YahooFinanceFetcher, "fetch") as mock_daily:
            result = fetch_bars("AAPL", intraday_env)

    assert result == expected
    mock_daily.assert_not_called()
    mock_intraday.assert_called_once_with("AAPL", period_days=5, timeframe_minutes=15)


def test_intraday_degrades_to_daily_only_when_intraday_unavailable(
    intraday_env: MarketDataSettings,
) -> None:
    with patch.object(
        YahooFinanceFetcher, "fetch_intraday", side_effect=DataUnavailableError("no data")
    ):
        with patch.object(YahooFinanceFetcher, "fetch", return_value=[]) as mock_daily:
            result = fetch_bars("AAPL", intraday_env)

    assert result == []
    mock_daily.assert_called_once_with(
        "AAPL", period_days=intraday_env.default_lookback_days
    )


def test_alpaca_intraday_failure_falls_back_to_yahoo_intraday(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MARKET_DATA_TIMEFRAME", "intraday")
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
    settings = MarketDataSettings()
    assert settings.has_alpaca_credentials

    expected = [_ohlcv(close=42.0)]
    with patch.object(
        AlpacaFetcher, "fetch_intraday", side_effect=DataUnavailableError("alpaca down")
    ):
        with patch.object(YahooFinanceFetcher, "fetch_intraday", return_value=expected):
            result = fetch_bars("AAPL", settings)

    assert result == expected


def test_daily_timeframe_is_unaffected() -> None:
    settings = MarketDataSettings()
    assert not settings.is_intraday
    with patch.object(YahooFinanceFetcher, "fetch", return_value=[]) as mock_daily:
        with patch.object(YahooFinanceFetcher, "fetch_intraday") as mock_intraday:
            fetch_bars("AAPL", settings)
    mock_intraday.assert_not_called()
    mock_daily.assert_called_once()


class TestYahooIntradayLimits:
    def test_interval_snaps_to_supported_resolution(self) -> None:
        assert _nearest_yahoo_interval(15) == 15
        assert _nearest_yahoo_interval(3) == 2  # 3m is not offered; snap down
        assert _nearest_yahoo_interval(45) == 30

    def test_one_minute_window_is_clamped_to_seven_days(self) -> None:
        """yfinance returns an empty frame past its per-interval window, so a
        60-day request for 1m bars must be clamped rather than sent as-is."""
        captured: dict[str, str] = {}

        def _capture(self, symbol, period, interval):  # noqa: ANN001
            captured["period"] = period
            captured["interval"] = interval
            return []

        with patch.object(YahooFinanceFetcher, "_history", _capture):
            YahooFinanceFetcher().fetch_intraday("AAPL", period_days=60, timeframe_minutes=1)

        assert captured["interval"] == "1m"
        assert captured["period"] == f"{YAHOO_INTRADAY_MAX_DAYS[1]}d"


def test_yahoo_latest_price_reads_last_minute_bar() -> None:
    bar = _ohlcv(close=123.45)
    with patch.object(YahooFinanceFetcher, "_history", return_value=[bar]):
        snapshot = YahooFinanceFetcher().latest_price("AAPL")

    assert snapshot is not None
    assert snapshot.price == 123.45
    assert snapshot.source == "yahoo_1m"


def test_yahoo_latest_price_returns_none_when_unavailable() -> None:
    with patch.object(
        YahooFinanceFetcher, "_history", side_effect=DataUnavailableError("blocked")
    ):
        assert YahooFinanceFetcher().latest_price("AAPL") is None


def test_settings_env_is_clean() -> None:
    """Guard: the intraday env vars must not leak between tests."""
    assert os.getenv("MARKET_DATA_TIMEFRAME") in (None, "daily")
