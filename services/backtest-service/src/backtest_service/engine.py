"""Backtest engine using pandas for signal generation (no look-ahead)."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from market_data.indicators import compute_atr, compute_ema, compute_macd, compute_rsi
from market_data.models import OHLCVBar

from .models import BacktestRequest, BacktestResult, TradeRecord

if TYPE_CHECKING:
    pass


def _compute_signals(bars: list[OHLCVBar], request: BacktestRequest) -> list[str]:
    """
    Compute BUY/SELL/HOLD for each bar using only bars[:i+1] (no look-ahead).
    Returns list of signal strings aligned with bars.
    """
    signals = []
    for i in range(len(bars)):
        slice_ = bars[: i + 1]
        closes = [b.close for b in slice_]
        highs = [b.high for b in slice_]
        lows = [b.low for b in slice_]

        if len(closes) < 51:  # need at least 51 bars for EMA50
            signals.append("HOLD")
            continue

        ema_20 = compute_ema(closes, 20)
        ema_50 = compute_ema(closes, 50)
        rsi = compute_rsi(closes)
        _, _, macd_hist = compute_macd(closes)

        buy = ema_20 > ema_50 and 45 < rsi < 70 and macd_hist > 0
        sell = ema_20 < ema_50 and 30 < rsi < 55 and macd_hist < 0

        if buy:
            signals.append("BUY")
        elif sell:
            signals.append("SELL")
        else:
            signals.append("HOLD")

    return signals


def run_backtest(request: BacktestRequest, bars: list[OHLCVBar]) -> BacktestResult:
    """
    Simulate strategy on historical bars. No look-ahead: indicators computed on bars[:i+1].
    Entry: BUY signal on bar i -> buy at bar i+1 open.
    Exit: SELL signal or ATR stop -> sell at bar i+1 open.
    """
    if len(bars) < 30:
        raise ValueError(f"Need at least 30 bars, got {len(bars)}")

    signals = _compute_signals(bars, request)

    equity = request.initial_capital
    position = 0.0   # shares held
    entry_price = 0.0
    entry_date: datetime | None = None
    stop_price = 0.0

    trades: list[TradeRecord] = []
    equity_curve: list[float] = [equity]
    peak_equity = equity

    for i in range(len(bars) - 1):
        bar = bars[i]
        next_bar = bars[i + 1]
        sig = signals[i]

        if position == 0.0:
            # Flat — check for entry
            if sig == "BUY":
                # Compute ATR for stop sizing (no look-ahead)
                slice_ = bars[: i + 1]
                atr = compute_atr(
                    [b.high for b in slice_],
                    [b.low for b in slice_],
                    [b.close for b in slice_],
                )
                exec_price = next_bar.open
                if exec_price <= 0:
                    continue
                stop_distance = atr * request.atr_stop_multiplier
                if stop_distance <= 0:
                    continue
                # ATR-based sizing
                risk_amount = equity * request.risk_per_trade_pct
                shares = risk_amount / stop_distance
                cost = shares * exec_price * (1 + request.commission_pct)
                if cost > equity:
                    shares = equity / (exec_price * (1 + request.commission_pct))
                if shares <= 0:
                    continue
                position = shares
                entry_price = exec_price
                entry_date = next_bar.timestamp
                stop_price = exec_price - stop_distance
                equity -= shares * exec_price * (1 + request.commission_pct)
        else:
            # In position — check for exit (stop or SELL signal)
            current_close = bar.close
            exit_triggered = sig == "SELL" or current_close <= stop_price
            if exit_triggered:
                exec_price = next_bar.open
                proceeds = position * exec_price * (1 - request.commission_pct)
                equity += proceeds
                pnl = proceeds - position * entry_price * (1 + request.commission_pct)
                pnl_pct = (exec_price - entry_price) / entry_price

                trades.append(TradeRecord(
                    entry_date=entry_date or bar.timestamp,
                    exit_date=next_bar.timestamp,
                    symbol=request.symbol,
                    action="BUY_SELL",
                    entry_price=entry_price,
                    exit_price=exec_price,
                    pnl=round(pnl, 2),
                    pnl_pct=round(pnl_pct, 4),
                ))
                position = 0.0
                entry_price = 0.0
                entry_date = None
                stop_price = 0.0

        # Mark-to-market equity
        mtm_equity = equity + (position * bars[i].close if position > 0 else 0.0)
        equity_curve.append(mtm_equity)
        if mtm_equity > peak_equity:
            peak_equity = mtm_equity

    # Close any open position at last bar
    if position > 0.0:
        last_bar = bars[-1]
        proceeds = position * last_bar.close * (1 - request.commission_pct)
        equity += proceeds
        pnl = proceeds - position * entry_price * (1 + request.commission_pct)
        pnl_pct = (last_bar.close - entry_price) / entry_price
        trades.append(TradeRecord(
            entry_date=entry_date or last_bar.timestamp,
            exit_date=last_bar.timestamp,
            symbol=request.symbol,
            action="BUY_SELL",
            entry_price=entry_price,
            exit_price=last_bar.close,
            pnl=round(pnl, 2),
            pnl_pct=round(pnl_pct, 4),
        ))
        equity_curve.append(equity)

    final_value = equity
    total_return_pct = (final_value - request.initial_capital) / request.initial_capital

    # Max drawdown
    peak = equity_curve[0]
    max_drawdown = 0.0
    for val in equity_curve:
        if val > peak:
            peak = val
        dd = (peak - val) / peak if peak > 0 else 0.0
        if dd > max_drawdown:
            max_drawdown = dd

    # Sharpe ratio (annualized, daily returns, risk-free=0)
    sharpe = _compute_sharpe(equity_curve)

    win_rate = 0.0
    if trades:
        wins = sum(1 for t in trades if t.pnl > 0)
        win_rate = round(wins / len(trades), 4)

    return BacktestResult(
        symbol=request.symbol,
        strategy=request.strategy,
        period_days=request.period_days,
        initial_capital=request.initial_capital,
        final_value=round(final_value, 2),
        total_return_pct=round(total_return_pct, 4),
        max_drawdown_pct=round(max_drawdown, 4),
        sharpe_ratio=round(sharpe, 4),
        total_trades=len(trades),
        win_rate=win_rate,
        trades=trades,
        generated_at=datetime.now(timezone.utc),
    )


def _compute_sharpe(equity_curve: list[float]) -> float:
    """Annualized Sharpe ratio from equity curve (daily returns, rf=0)."""
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
    std = math.sqrt(variance) if variance > 0 else 0.0
    if std == 0.0:
        return 0.0
    return (mean / std) * math.sqrt(252)
