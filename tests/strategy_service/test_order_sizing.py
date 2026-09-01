"""Tests for order sizing and the policy market context.

Both were silently wrong: sizing divided by a hardcoded $100, and the market
context reported invented freshness that disabled the policy's stale-data gate.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from market_data.models import OHLCVBar
from strategy_service.worker import TradeWorker, _compute_qty


def _bar(close: float = 100.0, minutes_ago: int = 0) -> OHLCVBar:
    return OHLCVBar(
        symbol="AAPL",
        timestamp=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
        open=close, high=close, low=close, close=close, volume=1000.0,
    )


class TestComputeQty:
    def test_sizes_from_the_actual_price(self) -> None:
        """$10,000 of exposure at $500/share is 20 shares, not 100."""
        assert _compute_qty(size_pct=0.10, buying_power=100_000.0, reference_price=500.0) == 20

    def test_a_cheap_share_gets_more_shares(self) -> None:
        assert _compute_qty(size_pct=0.10, buying_power=100_000.0, reference_price=10.0) == 1_000

    def test_price_of_one_hundred_is_not_special_cased(self) -> None:
        assert _compute_qty(size_pct=0.01, buying_power=100_000.0, reference_price=100.0) == 10

    @pytest.mark.parametrize("price", [None, 0.0, -5.0])
    def test_refuses_to_size_without_a_usable_price(self, price) -> None:
        """Returning a guessed quantity here would submit a real order at an
        unknown size — better to submit nothing."""
        assert _compute_qty(size_pct=0.10, buying_power=100_000.0, reference_price=price) == 0

    def test_sub_share_exposure_rounds_down_to_zero(self) -> None:
        assert _compute_qty(size_pct=0.001, buying_power=100.0, reference_price=500.0) == 0


class TestMarketContext:
    @pytest.mark.asyncio
    async def test_age_comes_from_the_live_price(self, stub_prices) -> None:
        stub_prices.set("AAPL", 200.0)
        context = await TradeWorker()._market_context("AAPL", [_bar(minutes_ago=90)])

        # The live price is current, so the 90-minute-old bar must not set the age.
        assert context.data_age_seconds < 60

    @pytest.mark.asyncio
    async def test_missing_price_reports_stale_rather_than_fresh(self, stub_prices) -> None:
        """Fail closed: an unobservable price must trip the policy's staleness
        rule, not sail through it as if it were seconds old."""
        stub_prices.default_price = None
        context = await TradeWorker()._market_context("AAPL", [])

        assert context.data_age_seconds > 30

    @pytest.mark.asyncio
    async def test_market_open_is_observed_not_assumed(self, stub_prices, monkeypatch) -> None:
        monkeypatch.setattr(
            "strategy_service.worker.market_session",
            lambda _settings: type("S", (), {"is_open": False})(),
        )
        context = await TradeWorker()._market_context("AAPL", [_bar()])
        assert context.market_open is False
