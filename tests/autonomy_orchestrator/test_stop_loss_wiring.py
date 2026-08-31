from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest
from autonomy_orchestrator.stop_loss_monitor import StopLossMonitor, StopLossRecord


def test_stop_loss_record_qty_field() -> None:
    record = StopLossRecord(
        symbol="AAPL",
        entry_price=100.0,
        stop_price=98.0,
        position_id="order-1",
        qty=10.5,
        created_at=datetime.now(timezone.utc),
    )

    assert record.qty == 10.5


def test_stop_loss_record_default_qty_zero() -> None:
    record = StopLossRecord(
        symbol="AAPL",
        entry_price=100.0,
        stop_price=98.0,
        position_id="order-1",
        created_at=datetime.now(timezone.utc),
    )

    assert record.qty == 0.0


@pytest.mark.asyncio
async def test_trigger_exit_includes_qty_in_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    monitor = StopLossMonitor("http://localhost:8002", "internal-key")
    record = StopLossRecord(
        symbol="AAPL",
        entry_price=100.0,
        stop_price=98.0,
        position_id="order-1",
        qty=10.5,
        created_at=datetime.now(timezone.utc),
    )
    captured: dict[str, object] = {}

    async def fake_post(self, url: str, *, json: dict[str, object], headers: dict[str, str]):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers

        class Response:
            def raise_for_status(self) -> None:
                return None

        return Response()

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    await monitor._trigger_exit(record)

    payload = captured["json"]
    assert isinstance(payload, dict)
    assert "qty" in payload
    assert payload["qty"] == 10.5


def test_stop_loss_registered_with_stop_below_entry() -> None:
    monitor = StopLossMonitor("http://localhost:8002", "internal-key")
    record = StopLossRecord(
        symbol="AAPL",
        entry_price=100.0,
        stop_price=98.0,
        position_id="order-1",
        created_at=datetime.now(timezone.utc),
    )

    monitor.register(record)

    stored = monitor.get("AAPL")
    assert stored is not None
    assert stored.stop_price < stored.entry_price
