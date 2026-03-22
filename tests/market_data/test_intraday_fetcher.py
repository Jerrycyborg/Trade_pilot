from __future__ import annotations

import os
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from market_data.config import MarketDataSettings
from market_data.fetcher import AlpacaFetcher, YahooFinanceFetcher, fetch_bars, get_fetcher


def _mock_bar(close: float = 100.0) -> MagicMock:
    bar = MagicMock()
    bar.timestamp = datetime.now(timezone.utc)
    bar.open = close - 1
    bar.high = close + 1
    bar.low = close - 2
    bar.close = close
    bar.volume = 1000.0
    return bar


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


def test_fetch_bars_falls_back_to_daily_without_alpaca_key() -> None:
    settings = MarketDataSettings()
    assert settings.timeframe == "daily"

    os.environ["MARKET_DATA_TIMEFRAME"] = "intraday"
    try:
        settings2 = MarketDataSettings()
        assert settings2.timeframe == "intraday"
        fetcher = get_fetcher(settings2)
        assert isinstance(fetcher, YahooFinanceFetcher)

        with patch.object(YahooFinanceFetcher, "fetch", return_value=[]) as mock_fetch:
            result = fetch_bars("AAPL", settings2)

        assert result == []
        mock_fetch.assert_called_once_with("AAPL", period_days=settings2.default_lookback_days)
    finally:
        os.environ.pop("MARKET_DATA_TIMEFRAME", None)
