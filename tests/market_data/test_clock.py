"""Tests for the market session clock."""

from __future__ import annotations

import zoneinfo
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from market_data.clock import market_session, reset_clock_cache
from market_data.config import MarketDataSettings

ET = zoneinfo.ZoneInfo("America/New_York")


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    reset_clock_cache()


class TestHeuristicSession:
    @pytest.mark.parametrize(
        "moment,expected",
        [
            (datetime(2026, 8, 28, 9, 30, tzinfo=ET), True),   # open bell, Friday
            (datetime(2026, 8, 28, 12, 0, tzinfo=ET), True),
            (datetime(2026, 8, 28, 16, 0, tzinfo=ET), True),   # closing bell
            (datetime(2026, 8, 28, 9, 29, tzinfo=ET), False),  # pre-market
            (datetime(2026, 8, 28, 16, 1, tzinfo=ET), False),  # after hours
            (datetime(2026, 8, 29, 12, 0, tzinfo=ET), False),  # Saturday
            (datetime(2026, 8, 30, 12, 0, tzinfo=ET), False),  # Sunday
        ],
    )
    def test_session_hours(self, moment: datetime, expected: bool) -> None:
        assert market_session(MarketDataSettings(), now=moment).is_open is expected

    def test_holiday_blindness_is_declared(self) -> None:
        """The heuristic cannot see holidays; callers are told so explicitly."""
        session = market_session(MarketDataSettings(), now=datetime(2026, 8, 28, 12, 0, tzinfo=ET))
        assert session.source == "heuristic"
        assert session.reason == "holiday_calendar_not_checked"

    def test_utc_input_is_converted_to_eastern(self) -> None:
        from datetime import timezone

        # 20:00 UTC on a summer Friday is 16:00 ET — still open.
        moment = datetime(2026, 8, 28, 20, 0, tzinfo=timezone.utc)
        assert market_session(MarketDataSettings(), now=moment).is_open is True


class TestAlpacaClock:
    def _settings(self) -> MarketDataSettings:
        return MarketDataSettings(alpaca_api_key="key", alpaca_secret_key="secret")

    def test_prefers_the_broker_clock_when_credentials_exist(self) -> None:
        client = MagicMock()
        client.get_clock.return_value = MagicMock(is_open=True)

        with patch("alpaca.trading.client.TradingClient", return_value=client):
            # A Sunday: the heuristic would say closed, the broker says open.
            session = market_session(self._settings(), now=datetime(2026, 8, 30, 12, 0, tzinfo=ET))

        assert session.is_open is True
        assert session.source == "alpaca_clock"

    def test_result_is_cached_between_calls(self) -> None:
        client = MagicMock()
        client.get_clock.return_value = MagicMock(is_open=True)

        with patch("alpaca.trading.client.TradingClient", return_value=client) as ctor:
            market_session(self._settings())
            second = market_session(self._settings())

        assert ctor.call_count == 1
        assert second.source == "alpaca_clock_cached"

    def test_falls_back_to_the_heuristic_when_the_clock_errors(self) -> None:
        with patch("alpaca.trading.client.TradingClient", side_effect=RuntimeError("down")):
            session = market_session(
                self._settings(), now=datetime(2026, 8, 29, 12, 0, tzinfo=ET)
            )

        assert session.source == "heuristic"
        assert session.is_open is False
