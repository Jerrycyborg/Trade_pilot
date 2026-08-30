"""Monthly loss/profit attribution.

The limits gate whether the system trades at all, so they must be driven by
what actually happened, not by a constant per triggered stop.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

import autonomy_orchestrator.main as main
from autonomy_orchestrator.stop_loss_monitor import StopLossMonitor, StopLossRecord
from autonomy_orchestrator.take_profit_monitor import TakeProfitMonitor, TakeProfitRecord


class Prices:
    def __init__(self, book: dict[str, float] | None = None) -> None:
        self.book = dict(book or {})

    def get_price(self, symbol: str) -> float | None:
        return self.book.get(symbol.upper())


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch: pytest.MonkeyPatch):
    main.state.monthly_realized_loss_usd = 0.0
    main.state.monthly_realized_profit_usd = 0.0
    # _monthly_limits_ok() resets the counters when the month rolls over, and
    # the default reset month of 0 makes every first call look like a rollover.
    now = datetime.now(timezone.utc)
    main.state.monthly_reset_month = now.month
    main.state.monthly_reset_year = now.year

    async def _no_notify(*args, **kwargs):
        return None

    monkeypatch.setattr(main, "_notify_smart", _no_notify)
    yield
    main.state.monthly_realized_loss_usd = 0.0
    main.state.monthly_realized_profit_usd = 0.0


def _stop_record(qty: float = 10.0, entry: float = 100.0, stop: float = 97.0):
    return StopLossRecord(
        symbol="AAPL",
        entry_price=entry,
        stop_price=stop,
        position_id="AAPL",
        qty=qty,
        created_at=datetime.now(timezone.utc),
    )


class TestRealizedPnl:
    def test_loss_is_entry_minus_exit_times_size(self) -> None:
        assert main._realized_pnl(_stop_record(qty=10, entry=100.0), 96.0) == -40.0

    def test_gain_is_positive(self) -> None:
        assert main._realized_pnl(_stop_record(qty=10, entry=100.0), 105.0) == 50.0

    @pytest.mark.parametrize(
        "record,price",
        [
            (None, 96.0),
            (_stop_record(), None),
            (_stop_record(qty=0.0), 96.0),   # "close whatever is open" — size unknown
            (_stop_record(entry=0.0), 96.0),
        ],
    )
    def test_unknowable_pnl_is_none_not_a_guess(self, record, price) -> None:
        assert main._realized_pnl(record, price) is None


class TestStopLossAttribution:
    @pytest.mark.asyncio
    async def test_books_the_actual_loss(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Regression: every stop added a flat $5, so with the default $10 limit
        two stops halted trading for the month whatever the real loss was."""
        monitor = StopLossMonitor("http://localhost:8002", "k")
        monitor.register(_stop_record(qty=10, entry=100.0, stop=97.0))

        async def _exit(record):
            return None

        monitor._trigger_exit = _exit  # type: ignore[method-assign]
        main.state.stop_loss_monitor = monitor
        prices = Prices({"AAPL": 96.0})
        monkeypatch.setattr(main, "_price_source", lambda: prices)

        await main._run_stop_loss_check()

        assert main.state.monthly_realized_loss_usd == 40.0

    @pytest.mark.asyncio
    async def test_a_stop_that_exits_in_profit_adds_no_loss(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monitor = StopLossMonitor("http://localhost:8002", "k")
        monitor.register(_stop_record(qty=10, entry=100.0, stop=110.0))

        async def _exit(record):
            return None

        monitor._trigger_exit = _exit  # type: ignore[method-assign]
        main.state.stop_loss_monitor = monitor
        # Trailing stop above entry: exits at 105, a gain.
        prices = Prices({"AAPL": 105.0})
        monkeypatch.setattr(main, "_price_source", lambda: prices)

        await main._run_stop_loss_check()

        assert main.state.monthly_realized_loss_usd == 0.0

    @pytest.mark.asyncio
    async def test_unknown_size_is_not_attributed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monitor = StopLossMonitor("http://localhost:8002", "k")
        monitor.register(_stop_record(qty=0.0))

        async def _exit(record):
            return None

        monitor._trigger_exit = _exit  # type: ignore[method-assign]
        main.state.stop_loss_monitor = monitor
        monkeypatch.setattr(main, "_price_source", lambda: Prices({"AAPL": 96.0}))

        await main._run_stop_loss_check()

        assert main.state.monthly_realized_loss_usd == 0.0


class TestTakeProfitAttribution:
    @pytest.mark.asyncio
    async def test_books_the_gain_achieved_not_the_target(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monitor = TakeProfitMonitor("http://localhost:8002", "k")
        monitor.register(
            TakeProfitRecord(
                symbol="AAPL",
                entry_price=100.0,
                target_price=110.0,
                position_id="AAPL",
                qty=10.0,
                target_gain_usd=20.0,
                created_at=datetime.now(timezone.utc),
            )
        )

        async def _close(record):
            return True

        monitor._trigger_close = _close  # type: ignore[method-assign]
        main.state.take_profit_monitor = monitor
        monkeypatch.setattr(main, "_price_source", lambda: Prices({"AAPL": 112.0}))

        await main._run_take_profit_check()

        # 10 shares x $12 actually gained, not the $20 target_gain_usd.
        assert main.state.monthly_realized_profit_usd == 120.0


class TestMonthlyLimitGate:
    @staticmethod
    def _with_limit(monkeypatch: pytest.MonkeyPatch, limit: float) -> None:
        # OrchestratorSettings is a frozen dataclass — swap the whole instance.
        monkeypatch.setattr(
            main, "settings", replace(main.settings, monthly_loss_limit_usd=limit)
        )

    def test_trading_halts_once_the_loss_limit_is_reached(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._with_limit(monkeypatch, 100.0)
        main.state.monthly_realized_loss_usd = 100.0
        assert main._monthly_limits_ok() is False

    def test_trading_continues_below_the_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._with_limit(monkeypatch, 100.0)
        main.state.monthly_realized_loss_usd = 40.0
        assert main._monthly_limits_ok() is True
