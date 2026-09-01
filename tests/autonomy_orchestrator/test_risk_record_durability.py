"""Tracked stops and targets survive the process that registered them.

The first orchestrator drill restarted the orchestrator between an entry and
its stop check: the position survived at the broker, the in-memory stop
record did not, and the lot sat unwatched until an operator noticed. A risk
control whose memory is process-local protects positions only for the
process's lifetime — which is not the position's lifetime.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from autonomy_orchestrator.stop_loss_monitor import StopLossMonitor, StopLossRecord
from autonomy_orchestrator.take_profit_monitor import TakeProfitMonitor, TakeProfitRecord


def _stop(symbol: str = "NVDA") -> StopLossRecord:
    return StopLossRecord(
        symbol=symbol, entry_price=220.0, stop_price=205.07,
        position_id="ord-1", qty=4.0, side="BUY",
        created_at=datetime.now(timezone.utc),
    )


def _target(symbol: str = "NVDA") -> TakeProfitRecord:
    return TakeProfitRecord(
        symbol=symbol, entry_price=220.0, target_price=228.8,
        position_id="ord-1", qty=4.0, side="BUY",
        created_at=datetime.now(timezone.utc),
    )


class TestStopsSurviveARestart:
    def test_a_registered_stop_is_restored_by_a_new_process(self, tmp_path: Path) -> None:
        path = tmp_path / "stops.json"
        StopLossMonitor("http://b", "k", state_path=path).register(_stop())

        restored = StopLossMonitor("http://b", "k", state_path=path).get("NVDA")
        assert restored is not None
        assert restored.stop_price == 205.07
        assert restored.side == "BUY"
        assert restored.created_at.tzinfo is not None

    def test_a_fired_stop_does_not_come_back(self, tmp_path: Path) -> None:
        """check_all removes fired records via remove(); a record that
        resurrected on restart would re-close an already-closed position."""
        path = tmp_path / "stops.json"
        first = StopLossMonitor("http://b", "k", state_path=path)
        first.register(_stop())
        first.remove("NVDA")

        assert StopLossMonitor("http://b", "k", state_path=path).records() == {}

    def test_a_corrupt_state_file_starts_empty_not_crashed(self, tmp_path: Path) -> None:
        """The monitor must come up — a crash loop protects nothing at all —
        but load_records logs the orphaned symbols at ERROR."""
        path = tmp_path / "stops.json"
        path.write_text("{ not json")
        monitor = StopLossMonitor("http://b", "k", state_path=path)
        assert monitor.records() == {}
        monitor.register(_stop())  # and the file is usable again
        assert StopLossMonitor("http://b", "k", state_path=path).get("NVDA") is not None

    def test_without_a_state_path_nothing_is_written(self, tmp_path: Path) -> None:
        monitor = StopLossMonitor("http://b", "k")
        monitor.register(_stop())
        assert list(tmp_path.iterdir()) == []


class TestTargetsSurviveARestart:
    def test_a_registered_target_is_restored(self, tmp_path: Path) -> None:
        path = tmp_path / "targets.json"
        TakeProfitMonitor("http://b", "k", state_path=path).register(_target())

        restored = TakeProfitMonitor("http://b", "k", state_path=path).get("NVDA")
        assert restored is not None
        assert restored.target_price == 228.8

    def test_the_monitors_do_not_share_a_book(self, tmp_path: Path) -> None:
        stops = tmp_path / "stops.json"
        targets = tmp_path / "targets.json"
        StopLossMonitor("http://b", "k", state_path=stops).register(_stop())
        TakeProfitMonitor("http://b", "k", state_path=targets).register(_target("MSFT"))

        assert StopLossMonitor("http://b", "k", state_path=stops).get("MSFT") is None
        assert TakeProfitMonitor("http://b", "k", state_path=targets).get("NVDA") is None


class TestAClosedPositionLeavesNoOrphanedRecords:
    """Found by the post-fix orchestrator drill: the stop fired and removed
    its own record, while the take-profit record for the same dead position
    stayed on file. An orphaned record triggers on price alone — it books
    phantom P&L into the monthly ceilings, burns a PDT day-trade slot, and
    sends a close for a position that no longer exists."""

    def _monitors(self, tmp_path: Path, monkeypatch):
        from autonomy_orchestrator import main as m

        stops = StopLossMonitor("http://b", "k", state_path=tmp_path / "stops.json")
        targets = TakeProfitMonitor("http://b", "k", state_path=tmp_path / "targets.json")
        monkeypatch.setattr(m.state, "stop_loss_monitor", stops)
        monkeypatch.setattr(m.state, "take_profit_monitor", targets)

        class _Prices:
            def __init__(self, price: float) -> None:
                self._price = price

            def get_price(self, _symbol: str) -> float:
                return self._price

        async def _quiet(*_a, **_k) -> None:
            return None

        class _DayTrades:
            def record_close(self, _symbol: str) -> None:
                return None

        monkeypatch.setattr(m, "_notify_smart", _quiet)
        monkeypatch.setattr(m, "_day_trades", lambda: _DayTrades())
        return m, stops, targets, _Prices

    @pytest.mark.asyncio
    async def test_a_fired_stop_also_clears_the_take_profit_record(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        m, stops, targets, prices = self._monitors(tmp_path, monkeypatch)
        stops.register(_stop())
        targets.register(_target())

        async def confirmed(_record) -> bool:
            return True

        monkeypatch.setattr(stops, "_trigger_exit", confirmed)
        monkeypatch.setattr(m, "_price_source", lambda: prices(190.0))

        await m._run_stop_loss_check()

        assert stops.get("NVDA") is None
        assert targets.get("NVDA") is None, (
            "the orphaned target would fire later on price alone"
        )

    @pytest.mark.asyncio
    async def test_a_hit_target_also_clears_the_stop_record(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        m, stops, targets, prices = self._monitors(tmp_path, monkeypatch)
        stops.register(_stop())
        targets.register(_target())

        async def confirmed(_record) -> bool:
            return True

        monkeypatch.setattr(targets, "_trigger_close", confirmed)
        monkeypatch.setattr(m, "_price_source", lambda: prices(230.0))

        await m._run_take_profit_check()

        assert targets.get("NVDA") is None
        assert stops.get("NVDA") is None, (
            "the orphaned stop would book a phantom loss on the next dip"
        )
