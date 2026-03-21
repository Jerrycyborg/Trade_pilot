"""Tests for StopLossMonitor."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

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


def _make_fetcher(close_price: float):
    bar = MagicMock()
    bar.close = close_price
    fetcher = MagicMock()
    fetcher.fetch.return_value = [bar]
    return fetcher


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
    fetcher = _make_fetcher(close_price=94.0)  # below stop

    with patch.object(monitor, "_trigger_exit", new_callable=AsyncMock) as mock_exit:
        triggered = await monitor.check_all(fetcher)

    assert "AAPL" in triggered
    mock_exit.assert_called_once()
    # Position should be removed after trigger
    assert monitor.get("AAPL") is None


@pytest.mark.asyncio
async def test_no_trigger_above_stop() -> None:
    """Price above stop -> nothing triggered."""
    monitor = StopLossMonitor("http://localhost:8002", "key")
    monitor.register(_record("AAPL", stop_price=95.0, entry_price=100.0))
    fetcher = _make_fetcher(close_price=98.0)  # above stop

    with patch.object(monitor, "_trigger_exit", new_callable=AsyncMock) as mock_exit:
        triggered = await monitor.check_all(fetcher)

    assert triggered == []
    mock_exit.assert_not_called()
    assert monitor.get("AAPL") is not None


@pytest.mark.asyncio
async def test_at_stop_price_triggers() -> None:
    """Price exactly at stop price should trigger."""
    monitor = StopLossMonitor("http://localhost:8002", "key")
    monitor.register(_record("AAPL", stop_price=95.0))
    fetcher = _make_fetcher(close_price=95.0)  # exactly at stop

    with patch.object(monitor, "_trigger_exit", new_callable=AsyncMock):
        triggered = await monitor.check_all(fetcher)

    assert "AAPL" in triggered
