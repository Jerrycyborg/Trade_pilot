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
