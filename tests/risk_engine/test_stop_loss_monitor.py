"""Tests for StopLossMonitor."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from autonomy_orchestrator.stop_loss_monitor import StopLossMonitor, StopLossRecord


def _record(symbol: str = "AAPL", stop_price: float = 95.0, entry_price: float = 100.0) -> StopLossRecord:
    return StopLossRecord(
        symbol=symbol,
        entry_price=entry_price,
        stop_price=stop_price,
        position_id=f"pos-{symbol}",
        created_at=datetime.now(timezone.utc),
    )


class _Prices:
    """Minimal price source: what the monitor now consumes instead of bars."""

    def __init__(self, price: float | None) -> None:
        self._price = price

    def get_price(self, symbol: str) -> float | None:
        return self._price


def _make_prices(price: float | None):
    return _Prices(price)


def test_register_stores_record() -> None:
    monitor = StopLossMonitor("http://localhost:8002", "key")
    rec = _record("AAPL", stop_price=95.0)
    monitor.register(rec)
    assert monitor.get("AAPL") is not None
    assert monitor.get("AAPL").stop_price == 95.0


def test_overwrite_updates_stop() -> None:
    """Registering same symbol twice replaces record."""
    monitor = StopLossMonitor("http://localhost:8002", "key")
    monitor.register(_record("AAPL", stop_price=95.0))
    monitor.register(_record("AAPL", stop_price=90.0))
    assert monitor.get("AAPL").stop_price == 90.0


@pytest.mark.asyncio
async def test_register_and_trigger() -> None:
    """Price below stop triggers _trigger_exit."""
    monitor = StopLossMonitor("http://localhost:8002", "key")
    monitor.register(_record("AAPL", stop_price=95.0, entry_price=100.0))
    prices = _make_prices(94.0)  # below stop

    with patch.object(monitor, "_trigger_exit", new_callable=AsyncMock) as mock_exit:
        triggered = await monitor.check_all(prices)

    assert "AAPL" in triggered
    mock_exit.assert_called_once()
    # Position should be removed after trigger
    assert monitor.get("AAPL") is None


@pytest.mark.asyncio
async def test_no_trigger_above_stop() -> None:
    """Price above stop -> nothing triggered."""
    monitor = StopLossMonitor("http://localhost:8002", "key")
    monitor.register(_record("AAPL", stop_price=95.0, entry_price=100.0))
    prices = _make_prices(98.0)  # above stop

    with patch.object(monitor, "_trigger_exit", new_callable=AsyncMock) as mock_exit:
        triggered = await monitor.check_all(prices)

    assert triggered == []
    mock_exit.assert_not_called()
    assert monitor.get("AAPL") is not None


@pytest.mark.asyncio
async def test_at_stop_price_triggers() -> None:
    """Price exactly at stop price should trigger."""
    monitor = StopLossMonitor("http://localhost:8002", "key")
    monitor.register(_record("AAPL", stop_price=95.0))
    prices = _make_prices(95.0)  # exactly at stop

    with patch.object(monitor, "_trigger_exit", new_callable=AsyncMock):
        triggered = await monitor.check_all(prices)

    assert "AAPL" in triggered
