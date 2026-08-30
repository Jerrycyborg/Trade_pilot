"""Backtest engine for the EMA/RSI/MACD strategy.

Two things this engine takes seriously, because an intraday strategy lives or
dies on them:

**Costs.** Every entry and exit pays commission, half the bid-ask spread and
slippage. A 15-minute strategy may round-trip several times a day, so a cost
that is negligible per trade is decisive over a year. The result reports the
gross return (costs off) alongside the net, and the gap between them is the
number to look at first.

**Annualisation.** Sharpe is annualised from the actual bar size. Using the
daily constant of 252 on 15-minute bars understates the ratio by roughly 5x and
makes a working strategy look broken.

There is no look-ahead: indicators at bar i use only bars[:i+1], and a signal on
bar i is executed at the open of bar i+1.
"""

from __future__ import annotations

import math
import zoneinfo
from dataclasses import dataclass, field
from datetime import datetime, timezone

from market_data.indicators import compute_atr, compute_ema, compute_macd, compute_rsi
from market_data.models import OHLCVBar

from .models import (
    BacktestRequest,
    BacktestResult,
    CostScenario,
    CostSensitivityResult,
    TradeRecord,
)

# Sessions are grouped in market time so a bar at 21:30 UTC lands on the US
# trading day it actually belongs to.
MARKET_TZ = zoneinfo.ZoneInfo("America/New_York")

# EMA-50 plus MACD's 26+9 warm-up: below this the indicators return defaults
# rather than signal, so no position is taken.
MIN_WARMUP_BARS = 51


def _compute_signals(bars: list[OHLCVBar], request: BacktestRequest) -> list[str]:
    """BUY/SELL/HOLD per bar, using only data available at that bar."""
    signals: list[str] = []
    for i in range(len(bars)):
        slice_ = bars[: i + 1]
        closes = [b.close for b in slice_]

        if len(closes) < MIN_WARMUP_BARS:
            signals.append("HOLD")
            continue

        ema_20 = compute_ema(closes, 20)
        ema_50 = compute_ema(closes, 50)
        rsi = compute_rsi(closes)
        _, _, macd_hist = compute_macd(closes)

        buy = ema_20 > ema_50 and 45 < rsi < 70 and macd_hist > 0
        sell = ema_20 < ema_50 and 30 < rsi < 55 and macd_hist < 0

        signals.append("BUY" if buy else "SELL" if sell else "HOLD")

    return signals


@dataclass
class _Simulation:
    """Raw output of one pass over the bars at a given cost level."""

    final_equity: float
    equity_curve: list[float]
    trades: list[TradeRecord] = field(default_factory=list)
    total_costs: float = 0.0


def _session_date(stamp: datetime) -> str:
    """The trading session a bar belongs to, in market time."""
    aware = stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
    return aware.astimezone(MARKET_TZ).date().isoformat()


def _simulate(
    request: BacktestRequest,
    bars: list[OHLCVBar],
    signals: list[str],
    cost_per_side: float,
) -> _Simulation:
    """Run the strategy once at the given one-way cost fraction."""
    equity = request.initial_capital
    position = 0.0
    entry_price = 0.0
    entry_cost = 0.0
    entry_stamp: datetime | None = None
    stop_price = 0.0

    trades: list[TradeRecord] = []
    equity_curve: list[float] = [equity]
    total_costs = 0.0

    for i in range(len(bars) - 1):
        bar = bars[i]
        next_bar = bars[i + 1]
        signal = signals[i]

        if position == 0.0:
            if signal == "BUY":
                slice_ = bars[: i + 1]
                atr = compute_atr(
                    [b.high for b in slice_],
                    [b.low for b in slice_],
                    [b.close for b in slice_],
                )
                quote = next_bar.open
                stop_distance = atr * request.atr_stop_multiplier
                if quote <= 0 or stop_distance <= 0:
                    equity_curve.append(equity)
                    continue

                # Risk a fixed fraction of equity across the ATR stop distance.
                shares = (equity * request.risk_per_trade_pct) / stop_distance
                fill = quote * (1 + cost_per_side)
                if shares * fill > equity:
                    shares = equity / fill
                if shares <= 0:
                    equity_curve.append(equity)
                    continue

                cost = shares * quote * cost_per_side
                total_costs += cost
                equity -= shares * fill

                position = shares
                entry_price = quote
                entry_cost = cost
                entry_stamp = next_bar.timestamp
                stop_price = quote - stop_distance
        else:
            hit_stop = bar.close <= stop_price
            if signal == "SELL" or hit_stop:
                quote = next_bar.open
                fill = quote * (1 - cost_per_side)
                cost = position * quote * cost_per_side
                total_costs += cost
                proceeds = position * fill
                equity += proceeds

                gross_entry = position * entry_price
                pnl = proceeds - (gross_entry + entry_cost)
                trades.append(
                    TradeRecord(
                        entry_date=entry_stamp or bar.timestamp,
                        exit_date=next_bar.timestamp,
                        symbol=request.symbol,
                        action="BUY_SELL",
                        entry_price=round(entry_price, 4),
                        exit_price=round(quote, 4),
                        pnl=round(pnl, 2),
                        pnl_pct=round((quote - entry_price) / entry_price, 4),
                        costs=round(entry_cost + cost, 2),
                        exit_reason="stop" if hit_stop else "signal",
                        same_day=(
                            entry_stamp is not None
                            and _session_date(entry_stamp) == _session_date(next_bar.timestamp)
                        ),
                    )
                )
                position = 0.0
                entry_price = 0.0
                entry_cost = 0.0
                entry_stamp = None
                stop_price = 0.0

        equity_curve.append(equity + position * bars[i].close)

    # Close anything still open at the final bar.
    if position > 0.0:
        last = bars[-1]
        quote = last.close
        cost = position * quote * cost_per_side
        total_costs += cost
        proceeds = position * quote * (1 - cost_per_side)
        equity += proceeds
        gross_entry = position * entry_price
        trades.append(
            TradeRecord(
                entry_date=entry_stamp or last.timestamp,
                exit_date=last.timestamp,
                symbol=request.symbol,
                action="BUY_SELL",
                entry_price=round(entry_price, 4),
                exit_price=round(quote, 4),
                pnl=round(proceeds - (gross_entry + entry_cost), 2),
                pnl_pct=round((quote - entry_price) / entry_price, 4),
                costs=round(entry_cost + cost, 2),
                exit_reason="end_of_data",
                same_day=(
                    entry_stamp is not None
                    and _session_date(entry_stamp) == _session_date(last.timestamp)
                ),
            )
        )
        equity_curve.append(equity)

    return _Simulation(
        final_equity=equity,
        equity_curve=equity_curve,
        trades=trades,
        total_costs=total_costs,
    )


def _max_day_trades_in_window(
    trades: list[TradeRecord], bars: list[OHLCVBar], window: int = 5
) -> int:
    """Peak day-trade count over any `window` consecutive sessions.

    This is the quantity the US pattern-day-trader rule is written against: 4 or
    more day trades in 5 business days flags an account under $25k equity.
    """
    day_trade_dates = [_session_date(t.exit_date) for t in trades if t.same_day]
    if not day_trade_dates:
        return 0

    counts: dict[str, int] = {}
    for date in day_trade_dates:
        counts[date] = counts.get(date, 0) + 1

    sessions = sorted({_session_date(b.timestamp) for b in bars})
    peak = 0
    for index in range(len(sessions)):
        span = sessions[max(0, index - window + 1) : index + 1]
        peak = max(peak, sum(counts.get(date, 0) for date in span))
    return peak


def _compute_sharpe(equity_curve: list[float], periods_per_year: float) -> float:
    """Annualised Sharpe from per-bar returns, risk-free = 0."""
    if len(equity_curve) < 2:
        return 0.0
    returns = [
        (equity_curve[i] - equity_curve[i - 1]) / equity_curve[i - 1]
        for i in range(1, len(equity_curve))
        if equity_curve[i - 1] > 0
    ]
    if not returns:
        return 0.0
    n = len(returns)
    mean = sum(returns) / n
    variance = sum((r - mean) ** 2 for r in returns) / n
    std = math.sqrt(variance)
    if std == 0.0:
        return 0.0
    return (mean / std) * math.sqrt(periods_per_year)


def _profit_factor(trades: list[TradeRecord]) -> float:
    gains = sum(t.pnl for t in trades if t.pnl > 0)
    losses = -sum(t.pnl for t in trades if t.pnl < 0)
    if losses <= 0:
        return float("inf") if gains > 0 else 0.0
    return gains / losses


def _max_drawdown(equity_curve: list[float]) -> float:
    peak = equity_curve[0] if equity_curve else 0.0
    worst = 0.0
    for value in equity_curve:
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, (peak - value) / peak)
    return worst


def run_backtest(request: BacktestRequest, bars: list[OHLCVBar]) -> BacktestResult:
    """Simulate the strategy, reporting net and gross results side by side."""
    if len(bars) < MIN_WARMUP_BARS + 1:
        raise ValueError(
            f"Need at least {MIN_WARMUP_BARS + 1} bars for indicator warm-up, got {len(bars)}"
        )

    signals = _compute_signals(bars, request)
    net = _simulate(request, bars, signals, request.cost_per_side_pct)
    # A second pass at zero cost. Computing gross by adding costs back would be
    # wrong: position size depends on equity, which depends on costs already paid.
    gross = _simulate(request, bars, signals, 0.0)

    capital = request.initial_capital
    profit_factor = _profit_factor(net.trades)

    return BacktestResult(
        symbol=request.symbol,
        strategy=request.strategy,
        period_days=request.period_days,
        timeframe=request.timeframe,
        intraday_minutes=request.intraday_minutes,
        bars_count=len(bars),
        initial_capital=capital,
        final_value=round(net.final_equity, 2),
        total_return_pct=round((net.final_equity - capital) / capital, 4),
        gross_return_pct=round((gross.final_equity - capital) / capital, 4),
        total_costs=round(net.total_costs, 2),
        max_drawdown_pct=round(_max_drawdown(net.equity_curve), 4),
        sharpe_ratio=round(_compute_sharpe(net.equity_curve, request.periods_per_year), 4),
        total_trades=len(net.trades),
        win_rate=(
            round(sum(1 for t in net.trades if t.pnl > 0) / len(net.trades), 4)
            if net.trades
            else 0.0
        ),
        profit_factor=(
            round(profit_factor, 4) if profit_factor != float("inf") else float("inf")
        ),
        avg_trade_pnl=(
            round(sum(t.pnl for t in net.trades) / len(net.trades), 2) if net.trades else 0.0
        ),
        day_trades=sum(1 for t in net.trades if t.same_day),
        max_day_trades_in_5_sessions=_max_day_trades_in_window(net.trades, bars),
        trades=net.trades,
        generated_at=datetime.now(timezone.utc),
    )


def run_cost_sensitivity(
    request: BacktestRequest,
    bars: list[OHLCVBar],
    spreads_bps: list[float] | None = None,
) -> CostSensitivityResult:
    """Re-run the strategy across a range of spreads to find where it breaks.

    A strategy profitable only at zero cost has no edge — it has an artefact.
    This locates the spread at which the return crosses zero.
    """
    ladder = spreads_bps if spreads_bps is not None else [0.0, 1.0, 2.0, 5.0, 10.0, 20.0, 50.0]
    signals = _compute_signals(bars, request)
    capital = request.initial_capital

    scenarios: list[CostScenario] = []
    breakeven: float | None = None
    for spread in sorted(ladder):
        variant = request.model_copy(update={"spread_bps": spread})
        run = _simulate(variant, bars, signals, variant.cost_per_side_pct)
        total_return = (run.final_equity - capital) / capital
        scenarios.append(
            CostScenario(
                spread_bps=spread,
                commission_pct=variant.commission_pct,
                total_return_pct=round(total_return, 4),
                sharpe_ratio=round(
                    _compute_sharpe(run.equity_curve, variant.periods_per_year), 4
                ),
                profit_factor=round(min(_profit_factor(run.trades), 1e6), 4),
                total_trades=len(run.trades),
                total_costs=round(run.total_costs, 2),
            )
        )
        if total_return > 0:
            breakeven = spread

    gross = _simulate(request, bars, signals, 0.0)
    return CostSensitivityResult(
        symbol=request.symbol,
        timeframe=request.timeframe,
        gross_return_pct=round((gross.final_equity - capital) / capital, 4),
        scenarios=scenarios,
        breakeven_spread_bps=breakeven,
    )
