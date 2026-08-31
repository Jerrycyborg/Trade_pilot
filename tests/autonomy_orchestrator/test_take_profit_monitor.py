from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from autonomy_orchestrator.take_profit_monitor import TakeProfitMonitor, TakeProfitRecord


class _Prices:
    """Minimal price source: what the monitor now consumes instead of bars."""

    def __init__(self, price: float | None) -> None:
        self._price = price

    def get_price(self, symbol: str) -> float | None:
        return self._price


def _record(symbol: str = "AAPL", entry: float = 100.0, target: float = 110.0, qty: float = 5.0):
    return TakeProfitRecord(
        symbol=symbol,
        entry_price=entry,
        target_price=target,
        position_id="p1",
        qty=qty,
        created_at=datetime.now(timezone.utc),
    )


def test_register_and_get() -> None:
    monitor = TakeProfitMonitor("http://localhost:8002", "key")
    monitor.register(_record())
    assert monitor.get("AAPL") is not None
    assert monitor.get("AAPL").target_price == 110.0


def test_no_trigger_below_target() -> None:
    import asyncio

    monitor = TakeProfitMonitor("http://localhost:8002", "key")
    monitor.register(_record("AAPL", 100.0, 110.0))
    result = asyncio.run(monitor.check_all(_Prices(105.0)))
    assert result == []
    assert monitor.get("AAPL") is not None


def test_triggers_at_target() -> None:
    import asyncio

    monitor = TakeProfitMonitor("http://localhost:8002", "key")
    monitor.register(_record("AAPL", 100.0, 110.0))
    with patch.object(monitor, "_trigger_close", new_callable=AsyncMock) as mock_close:
        result = asyncio.run(monitor.check_all(_Prices(112.0)))
    assert "AAPL" in result
    assert monitor.get("AAPL") is None
    mock_close.assert_awaited_once()


def test_default_qty_zero() -> None:
    record = TakeProfitRecord(
        symbol="X",
        entry_price=50.0,
        target_price=60.0,
        position_id="p",
        created_at=datetime.now(timezone.utc),
    )
    assert record.qty == 0.0


def test_failed_close_keeps_record_tracked() -> None:
    import asyncio

    monitor = TakeProfitMonitor("http://localhost:8002", "key")
    monitor.register(_record("AAPL", 100.0, 110.0))
    with patch.object(monitor, "_trigger_close", new=AsyncMock(return_value=False)):
        result = asyncio.run(monitor.check_all(_Prices(111.0)))
    assert result == []
    assert monitor.get("AAPL") is not None
