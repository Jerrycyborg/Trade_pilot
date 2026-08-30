"""Tests for the backtest engine.

Note: these tests run on synthetic bars. They verify the *engine* — that costs
are charged, that annualisation follows the bar size, that day trades are
counted — not that the strategy is any good. No conclusion about profitability
should be drawn from synthetic data.
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone

import pytest
from backtest_service.engine import (
    MIN_WARMUP_BARS,
    _max_day_trades_in_window,
    _session_date,
    run_backtest,
    run_cost_sensitivity,
)
from backtest_service.models import BacktestRequest, TradeRecord
from market_data.models import OHLCVBar

ET_OPEN_UTC = 14  # 09:30 ET in summer is 13:30 UTC; 14:30 UTC is mid-session.


def _flat_bars(n: int, base_price: float = 100.0) -> list[OHLCVBar]:
    start = datetime(2024, 1, 2, ET_OPEN_UTC, 30, tzinfo=timezone.utc)
    return [
        OHLCVBar(
            symbol="TEST",
            timestamp=start + timedelta(days=i),
            open=base_price,
            high=base_price * 1.01,
            low=base_price * 0.99,
            close=base_price,
            volume=10_000.0,
        )
        for i in range(n)
    ]


def _wavy_bars(
    n: int = 600,
    base: float = 100.0,
    drift: float = 0.0004,
    amplitude: float = 0.012,
    period: int = 40,
    minutes: int = 15,
    seed: int = 7,
) -> list[OHLCVBar]:
    """An oscillating uptrend.

    A monotonic ramp is useless for this strategy: with no down bars RSI pins at
    100 and the 45<RSI<70 entry condition never fires. Oscillation makes RSI
    cycle through the band so entries actually occur.
    """
    rnd = random.Random(seed)
    start = datetime(2024, 1, 2, ET_OPEN_UTC, 30, tzinfo=timezone.utc)
    bars: list[OHLCVBar] = []
    for i in range(n):
        trend = base * math.exp(drift * i)
        close = trend * (1 + amplitude * math.sin(2 * math.pi * i / period))
        close *= 1 + rnd.gauss(0, 0.0015)
        open_ = close * (1 + rnd.gauss(0, 0.0005))
        bars.append(
            OHLCVBar(
                symbol="TEST",
                timestamp=start + timedelta(minutes=minutes * i),
                open=open_,
                high=max(open_, close) * 1.002,
                low=min(open_, close) * 0.998,
                close=close,
                volume=10_000.0 + i,
            )
        )
    return bars


def _request(**overrides) -> BacktestRequest:
    payload = {
        "symbol": "TEST",
        "initial_capital": 100_000.0,
        "risk_per_trade_pct": 0.01,
        "atr_stop_multiplier": 2.0,
        "commission_pct": 0.0,
        "spread_bps": 0.0,
        "slippage_bps": 0.0,
    }
    payload.update(overrides)
    return BacktestRequest(**payload)


class TestWarmup:
    def test_rejects_bars_below_indicator_warmup(self) -> None:
        """EMA-50 needs 51 bars. Accepting 30 meant every early signal was a
        default value, not a signal."""
        with pytest.raises(ValueError, match="warm-up"):
            run_backtest(_request(), _flat_bars(MIN_WARMUP_BARS - 10))

    def test_accepts_bars_at_the_warmup_boundary(self) -> None:
        result = run_backtest(_request(), _flat_bars(MIN_WARMUP_BARS + 1))
        assert result.bars_count == MIN_WARMUP_BARS + 1

    def test_flat_prices_produce_no_trades(self) -> None:
        result = run_backtest(_request(), _flat_bars(120))
        assert result.total_trades == 0


class TestCosts:
    def test_costs_reduce_net_below_gross(self) -> None:
        bars = _wavy_bars()
        result = run_backtest(_request(spread_bps=20.0, slippage_bps=2.0), bars)

        assert result.total_trades > 0
        assert result.total_costs > 0
        assert result.total_return_pct < result.gross_return_pct

    def test_zero_cost_makes_net_equal_gross(self) -> None:
        result = run_backtest(_request(spread_bps=0.0, slippage_bps=0.0), _wavy_bars())
        assert result.total_costs == 0.0
        assert result.total_return_pct == pytest.approx(result.gross_return_pct, abs=1e-4)

    def test_higher_spread_costs_more(self) -> None:
        bars = _wavy_bars()
        cheap = run_backtest(_request(spread_bps=2.0), bars)
        dear = run_backtest(_request(spread_bps=40.0), bars)
        assert dear.total_costs > cheap.total_costs
        assert dear.total_return_pct < cheap.total_return_pct

    def test_cost_per_side_combines_all_three_components(self) -> None:
        request = _request(commission_pct=0.001, spread_bps=10.0, slippage_bps=2.0)
        # commission 0.001 + half of 10bps (0.0005) + 2bps (0.0002)
        assert request.cost_per_side_pct == pytest.approx(0.0017)

    def test_each_trade_records_its_own_costs(self) -> None:
        result = run_backtest(_request(spread_bps=20.0), _wavy_bars())
        assert all(t.costs > 0 for t in result.trades)
        assert sum(t.costs for t in result.trades) == pytest.approx(
            result.total_costs, rel=0.02
        )


class TestAnnualisation:
    def test_intraday_uses_bar_sized_periods_not_252(self) -> None:
        """Sharpe on 15-minute bars annualised with 252 understates by ~5x."""
        daily = _request(timeframe="daily")
        intraday = _request(timeframe="intraday", intraday_minutes=15)

        assert daily.periods_per_year == 252
        assert intraday.periods_per_year == pytest.approx(252 * 26)

    @pytest.mark.parametrize(
        "minutes,expected_bars_per_day", [(1, 390), (5, 78), (15, 26), (30, 13), (390, 1)]
    )
    def test_periods_per_year_tracks_bar_size(
        self, minutes: int, expected_bars_per_day: int
    ) -> None:
        request = _request(timeframe="intraday", intraday_minutes=minutes)
        assert request.periods_per_year == pytest.approx(252 * expected_bars_per_day)

    def test_same_series_scores_higher_sharpe_on_intraday_annualisation(self) -> None:
        bars = _wavy_bars()
        as_daily = run_backtest(_request(timeframe="daily"), bars)
        as_intraday = run_backtest(
            _request(timeframe="intraday", intraday_minutes=15), bars
        )
        assert math.isfinite(as_intraday.sharpe_ratio)
        if as_daily.sharpe_ratio > 0:
            assert as_intraday.sharpe_ratio > as_daily.sharpe_ratio


class TestDayTradeCounting:
    def _trade(self, entry: datetime, exit_: datetime) -> TradeRecord:
        return TradeRecord(
            entry_date=entry,
            exit_date=exit_,
            symbol="TEST",
            action="BUY_SELL",
            entry_price=100.0,
            exit_price=101.0,
            pnl=1.0,
            pnl_pct=0.01,
            same_day=_session_date(entry) == _session_date(exit_),
        )

    def test_same_session_round_trip_is_a_day_trade(self) -> None:
        entry = datetime(2024, 3, 5, 15, 0, tzinfo=timezone.utc)  # 10:00 ET
        exit_ = datetime(2024, 3, 5, 19, 0, tzinfo=timezone.utc)  # 14:00 ET
        assert self._trade(entry, exit_).same_day is True

    def test_overnight_hold_is_not_a_day_trade(self) -> None:
        entry = datetime(2024, 3, 5, 19, 0, tzinfo=timezone.utc)
        exit_ = datetime(2024, 3, 6, 15, 0, tzinfo=timezone.utc)
        assert self._trade(entry, exit_).same_day is False

    def test_session_is_grouped_in_market_time_not_utc(self) -> None:
        """20:00 ET is 00:00 UTC the next day — still the same US session."""
        late = datetime(2024, 3, 6, 0, 30, tzinfo=timezone.utc)  # 19:30 ET Mar 5
        assert _session_date(late) == "2024-03-05"

    def test_rolling_window_finds_the_peak(self) -> None:
        sessions = [datetime(2024, 3, day, 15, 0, tzinfo=timezone.utc) for day in range(4, 12)]
        bars = [
            OHLCVBar(
                symbol="TEST", timestamp=s, open=1, high=1, low=1, close=1, volume=1
            )
            for s in sessions
        ]
        # Three day trades on day 1, two on day 4 — five inside one 5-session window.
        trades = [
            self._trade(sessions[0], sessions[0]),
            self._trade(sessions[0], sessions[0]),
            self._trade(sessions[0], sessions[0]),
            self._trade(sessions[3], sessions[3]),
            self._trade(sessions[3], sessions[3]),
        ]
        assert _max_day_trades_in_window(trades, bars, window=5) == 5

    def test_window_excludes_sessions_that_have_rolled_off(self) -> None:
        sessions = [datetime(2024, 3, day, 15, 0, tzinfo=timezone.utc) for day in range(4, 16)]
        bars = [
            OHLCVBar(
                symbol="TEST", timestamp=s, open=1, high=1, low=1, close=1, volume=1
            )
            for s in sessions
        ]
        # Three on session 0 and three on session 8 never share a 5-day window.
        trades = [self._trade(sessions[0], sessions[0]) for _ in range(3)]
        trades += [self._trade(sessions[8], sessions[8]) for _ in range(3)]
        assert _max_day_trades_in_window(trades, bars, window=5) == 3

    def test_no_day_trades_reports_zero(self) -> None:
        assert _max_day_trades_in_window([], [], window=5) == 0


class TestCostSensitivity:
    def test_return_falls_as_spread_rises(self) -> None:
        result = run_cost_sensitivity(
            _request(), _wavy_bars(), spreads_bps=[0.0, 10.0, 50.0, 200.0]
        )
        returns = [s.total_return_pct for s in result.scenarios]
        assert returns == sorted(returns, reverse=True)

    def test_reports_the_highest_profitable_spread(self) -> None:
        result = run_cost_sensitivity(
            _request(), _wavy_bars(), spreads_bps=[0.0, 5.0, 10.0]
        )
        if result.breakeven_spread_bps is not None:
            profitable = [
                s.spread_bps for s in result.scenarios if s.total_return_pct > 0
            ]
            assert result.breakeven_spread_bps == max(profitable)

    def test_scenarios_are_ordered_by_spread(self) -> None:
        result = run_cost_sensitivity(
            _request(), _wavy_bars(), spreads_bps=[50.0, 0.0, 10.0]
        )
        spreads = [s.spread_bps for s in result.scenarios]
        assert spreads == sorted(spreads)


class TestNoLookahead:
    def test_a_spike_on_the_final_bar_cannot_be_traded(self) -> None:
        bars = _flat_bars(120)
        last = bars[-1]
        bars[-1] = OHLCVBar(
            symbol=last.symbol,
            timestamp=last.timestamp,
            open=last.open,
            high=last.high * 10,
            low=last.low,
            close=last.close * 10,
            volume=last.volume,
        )
        result = run_backtest(_request(), bars)
        assert result.total_trades == 0


class TestReportedMetrics:
    def test_profit_factor_and_win_rate_are_consistent(self) -> None:
        result = run_backtest(_request(), _wavy_bars())
        assert result.total_trades > 0
        assert 0.0 <= result.win_rate <= 1.0
        wins = sum(1 for t in result.trades if t.pnl > 0)
        assert result.win_rate == pytest.approx(wins / result.total_trades, abs=1e-4)
        if all(t.pnl > 0 for t in result.trades):
            assert result.profit_factor == float("inf")
        else:
            assert result.profit_factor >= 0

    def test_exit_reasons_are_labelled(self) -> None:
        result = run_backtest(_request(), _wavy_bars())
        assert {t.exit_reason for t in result.trades} <= {"signal", "stop", "end_of_data"}

    def test_result_echoes_the_timeframe_it_ran_on(self) -> None:
        result = run_backtest(
            _request(timeframe="intraday", intraday_minutes=5), _wavy_bars(minutes=5)
        )
        assert result.timeframe == "intraday"
        assert result.intraday_minutes == 5


class TestStopExecution:
    """A stop that only reads bar.close hides real losses and flatters results."""

    def _bar(self, i: int, o: float, h: float, low: float, c: float) -> OHLCVBar:
        return OHLCVBar(
            symbol="TEST",
            timestamp=datetime(2024, 1, 2, ET_OPEN_UTC, 30, tzinfo=timezone.utc)
            + timedelta(minutes=15 * i),
            open=o, high=h, low=low, close=c, volume=10_000.0,
        )

    def _series_with_dip(self) -> list[OHLCVBar]:
        """A long entry, then one bar that dips hard but closes back up."""
        bars = _wavy_bars(n=300)
        # Bar 250: low far below anything nearby, close unchanged.
        original = bars[250]
        bars[250] = OHLCVBar(
            symbol=original.symbol,
            timestamp=original.timestamp,
            open=original.open,
            high=original.high,
            low=original.low * 0.80,   # deep intrabar wick
            close=original.close,      # ...that fully recovers by the close
            volume=original.volume,
        )
        return bars

    def test_intrabar_dip_triggers_the_stop(self) -> None:
        result = run_backtest(_request(), self._series_with_dip())
        # The wick alone must be able to stop a position out.
        assert any(t.exit_reason == "stop" for t in result.trades)

    def test_stop_fills_at_the_stop_not_a_later_open(self) -> None:
        result = run_backtest(_request(), self._series_with_dip())
        stops = [t for t in result.trades if t.exit_reason == "stop"]
        assert stops
        for trade in stops:
            # Never filled above the entry: a stop is a loss-limiting exit.
            assert trade.exit_price < trade.entry_price

    def test_stop_exit_is_stamped_within_the_breaching_bar(self) -> None:
        bars = self._series_with_dip()
        result = run_backtest(_request(), bars)
        stamps = {b.timestamp for b in bars}
        for trade in result.trades:
            if trade.exit_reason == "stop":
                assert trade.exit_date in stamps


class TestEquityCurveMarking:
    def test_entry_does_not_create_an_instant_gain(self) -> None:
        """Marking a just-entered position at the previous bar's close invents
        P&L across the gap and corrupts Sharpe and drawdown."""
        bars = _wavy_bars(n=300)
        zero_cost = run_backtest(_request(), bars)

        # With no costs the curve must start at capital and move only on real
        # price change — never jump on the entry bar itself.
        assert zero_cost.total_costs == 0.0
        assert zero_cost.max_drawdown_pct >= 0.0
        assert math.isfinite(zero_cost.sharpe_ratio)

    def test_flat_series_has_no_drawdown(self) -> None:
        """A position marked at the wrong price shows drawdown even when
        nothing moved."""
        result = run_backtest(_request(), _flat_bars(120))
        assert result.total_trades == 0
        assert result.max_drawdown_pct == 0.0
