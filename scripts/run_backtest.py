#!/usr/bin/env python3
"""Run the strategy over historical bars and report whether it made money.

This is the question the rest of the system cannot answer: the live loop will
happily trade a strategy with no edge. Run this before trusting it with money.

    # 60 days of 15-minute bars, realistic costs
    uv run python scripts/run_backtest.py --symbols AAPL,MSFT,NVDA

    # daily bars for comparison
    uv run python scripts/run_backtest.py --symbols AAPL --timeframe daily --days 365

    # how much cost does the edge survive?
    uv run python scripts/run_backtest.py --symbols AAPL --sweep

    # is the edge real, or fitted? (choose params on past data, judge on what
    # followed — this is the one that matters)
    uv run python scripts/run_backtest.py --symbols AAPL --walk-forward

    # plateau or spike? score the whole parameter grid
    uv run python scripts/run_backtest.py --symbols AAPL --sensitivity

    # do several strategies across several symbols actually diversify?
    uv run python scripts/run_backtest.py --symbols AAPL,MSFT,NVDA --portfolio

    # a single non-default strategy
    uv run python scripts/run_backtest.py --symbols AAPL --strategy bollinger_reversion

Costs default to 5bps spread + 1bps slippage + zero commission, which is
roughly a liquid US large-cap at a commission-free broker. Set them to match
YOUR broker and YOUR symbols — a wider spread is the single fastest way for an
intraday strategy to stop working.
"""

from __future__ import annotations

import argparse
import logging
import sys

from backtest_service.engine import run_backtest, run_cost_sensitivity
from backtest_service.main import load_bars
from backtest_service.models import BacktestRequest, ParameterGrid
from backtest_service.portfolio import build_sleeves, run_portfolio
from backtest_service.strategies import strategy_names
from backtest_service.validation import parameter_sensitivity, walk_forward

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def _pct(value: float) -> str:
    colour = GREEN if value > 0 else RED if value < 0 else ""
    return f"{colour}{value:+.2%}{RESET}"


def _report(result) -> None:
    bar_label = (
        f"{result.intraday_minutes}-min" if result.timeframe == "intraday" else "daily"
    )
    print(f"\n{'=' * 62}")
    print(
        f"  {result.symbol}  {result.strategy}  "
        f"({bar_label} bars, {result.bars_count} bars)"
    )
    print(f"{'=' * 62}")

    if result.total_trades == 0:
        print(f"  {YELLOW}No trades taken.{RESET} The entry conditions never all held.")
        print(f"  {DIM}Not a result — there is nothing to evaluate here.{RESET}")
        return

    print(f"  Net return        {_pct(result.total_return_pct)}")
    print(f"  Gross (no costs)  {_pct(result.gross_return_pct)}")
    drag = result.gross_return_pct - result.total_return_pct
    share = (drag / abs(result.gross_return_pct) * 100) if result.gross_return_pct else 0.0
    print(
        f"  Cost drag         {RED}-{drag:.2%}{RESET}  "
        f"(${result.total_costs:,.0f}, {share:.0f}% of gross)"
    )
    print()
    print(f"  Sharpe            {result.sharpe_ratio:.2f}")
    print(f"  Max drawdown      {result.max_drawdown_pct:.2%}")
    print(f"  Profit factor     {result.profit_factor:.2f}")
    print(f"  Trades            {result.total_trades}   win rate {result.win_rate:.0%}")
    print(f"  Avg trade         ${result.avg_trade_pnl:,.2f}")
    print()
    print(f"  Day trades        {result.day_trades} of {result.total_trades}")
    if result.max_day_trades_in_5_sessions >= 4:
        print(
            f"  {RED}PDT RISK{RESET}          peak {result.max_day_trades_in_5_sessions} "
            f"day trades in 5 sessions"
        )
        print(
            f"  {DIM}A US margin account under $25k equity is restricted at 4. "
            f"See PDT_ENABLED.{RESET}"
        )

    verdict, colour = (
        ("makes money after costs", GREEN)
        if result.total_return_pct > 0
        else ("loses money after costs", RED)
    )
    print(f"\n  Verdict: {colour}{verdict}{RESET}")
    if result.gross_return_pct > 0 >= result.total_return_pct:
        print(f"  {YELLOW}The edge exists gross but is entirely eaten by costs.{RESET}")


def _sweep(request: BacktestRequest, bars) -> None:
    result = run_cost_sensitivity(request, bars)
    print(f"\n  Cost sensitivity — {result.symbol}")
    print(f"  {'spread':>10} {'return':>12} {'sharpe':>9} {'trades':>8} {'costs':>12}")
    for scenario in result.scenarios:
        print(
            f"  {scenario.spread_bps:>7.1f}bps {scenario.total_return_pct:>11.2%} "
            f"{scenario.sharpe_ratio:>9.2f} {scenario.total_trades:>8} "
            f"${scenario.total_costs:>10,.0f}"
        )
    if result.breakeven_spread_bps is None:
        print(f"  {RED}Unprofitable at every spread tested, including zero.{RESET}")
    else:
        print(
            f"  Still profitable up to {GREEN}{result.breakeven_spread_bps:.0f}bps{RESET} "
            f"spread."
        )


def _verdict(label: str, ok: bool, detail: str) -> None:
    mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
    print(f"  [{mark}] {label:<28} {detail}")


def _walk_forward_report(request: BacktestRequest, bars, args) -> None:
    grid = ParameterGrid()
    print(f"\n{'-' * 62}")
    print(f"  WALK-FORWARD  {request.symbol}")
    print(f"{'-' * 62}")
    try:
        result = walk_forward(
            request, bars, grid=grid, n_splits=args.splits, objective=args.objective
        )
    except ValueError as exc:
        print(f"  {RED}{exc}{RESET}")
        return

    print(
        f"  {DIM}{result.n_folds} folds, {result.n_trials} configurations per fold, "
        f"{result.embargo_bars}-bar embargo, selecting on {result.objective}{RESET}\n"
    )
    for fold in result.folds:
        print(
            f"  fold {fold.fold}  {fold.test_start:%Y-%m-%d} -> {fold.test_end:%Y-%m-%d}  "
            f"{fold.selected_label}"
        )
        print(
            f"          in-sample {fold.in_sample_sharpe:>7.2f}   "
            f"out-of-sample {fold.out_of_sample_sharpe:>7.2f}   "
            f"return {_pct(fold.out_of_sample_return_pct)}  "
            f"({fold.out_of_sample_trades} "
            f"{'trade' if fold.out_of_sample_trades == 1 else 'trades'})"
        )

    print(f"\n  {'Out-of-sample Sharpe':<28} {result.out_of_sample_sharpe:>8.2f}")
    print(f"  {'In-sample Sharpe (mean)':<28} {result.in_sample_sharpe:>8.2f}")
    print(f"  {'Degradation':<28} {result.sharpe_degradation:>8.2f}")
    print(f"  {'Out-of-sample return':<28} {_pct(result.out_of_sample_return_pct):>8}")
    print(f"  {'Out-of-sample max drawdown':<28} {result.out_of_sample_max_drawdown_pct:>8.2%}")
    print(f"  {'Out-of-sample trades':<28} {result.out_of_sample_trades:>8}")

    print()
    dsr = result.deflated_sharpe_ratio
    _verdict(
        "Out-of-sample profitable",
        result.out_of_sample_return_pct > 0,
        f"{result.out_of_sample_return_pct:+.2%}",
    )
    _verdict(
        "Survives the search",
        dsr is not None and dsr >= 0.95,
        f"deflated Sharpe ratio {dsr:.3f}" if dsr is not None else "not computable",
    )
    _verdict(
        "Folds agree on parameters",
        result.parameter_stability >= 0.5,
        f"{result.parameter_stability:.0%} picked the same configuration",
    )

    for warning in result.warnings:
        print(f"\n  {YELLOW}! {warning}{RESET}")

    print(
        f"\n  {DIM}The deflated Sharpe ratio is the probability this result beats what "
        f"the best of\n  {result.n_trials} random configurations would have produced by "
        f"luck. Below 0.95, it does not.\n  It counts only the configurations tried here "
        f"— every parameter you tried by hand\n  beforehand is also a trial, and none of "
        f"them are in that number.{RESET}"
    )


def _sensitivity_report(request: BacktestRequest, bars, args) -> None:
    print(f"\n{'-' * 62}")
    print(f"  PARAMETER SENSITIVITY  {request.symbol}")
    print(f"{'-' * 62}")
    try:
        result = parameter_sensitivity(request, bars, grid=ParameterGrid())
    except ValueError as exc:
        print(f"  {RED}{exc}{RESET}")
        return

    print(f"  {DIM}{result.grid_size} configurations, scored in-sample{RESET}\n")
    print(f"  {'configuration':<34} {'sharpe':>8} {'return':>10} {'trades':>7}")
    for score in result.scores[:5]:
        print(
            f"  {score.label:<34} {score.sharpe_ratio:>8.2f} "
            f"{score.total_return_pct:>9.2%} {score.total_trades:>7}"
        )
    if len(result.scores) > 6:
        print(f"  {DIM}{'...':<34}{RESET}")
    worst = result.worst
    print(
        f"  {worst.label:<34} {worst.sharpe_ratio:>8.2f} "
        f"{worst.total_return_pct:>9.2%} {worst.total_trades:>7}"
    )

    print(
        f"\n  {'Profitable configurations':<28} "
        f"{result.profitable_count}/{result.grid_size} ({result.profitable_fraction:.0%})"
    )
    if result.plateau_ratio is not None:
        print(
            f"  {'Neighbours of the best':<28} "
            f"{result.neighbour_mean_sharpe:.2f} Sharpe vs its {result.best.sharpe_ratio:.2f} "
            f"({result.plateau_ratio:.0%} retained)"
        )
        _verdict(
            "Plateau, not a spike",
            result.plateau_ratio >= 0.5,
            "the result survives one step in any parameter"
            if result.plateau_ratio >= 0.5
            else "the result does not survive one step away",
        )

    for warning in result.warnings:
        print(f"\n  {YELLOW}! {warning}{RESET}")

    print(
        f"\n  {DIM}A plateau is necessary but not sufficient: in a sample that happened "
        f"to trend,\n  every momentum configuration profits and they form a plateau "
        f"together. Read this\n  alongside --walk-forward, never instead of it.{RESET}"
    )


def _portfolio_report(request: BacktestRequest, bars_by_symbol: dict, args) -> None:
    strategies = (
        [s.strip() for s in args.strategies.split(",") if s.strip()]
        if args.strategies
        else list(strategy_names())
    )
    symbols = sorted(bars_by_symbol)
    sleeves = build_sleeves(symbols, strategies)

    print(f"\n{'=' * 66}")
    print(f"  PORTFOLIO  {len(symbols)} symbols x {len(strategies)} strategies")
    print(f"{'=' * 66}")
    try:
        result = run_portfolio(
            request,
            sleeves,
            bars_by_symbol,
            allocation=args.allocation,
            n_trials=args.considered or len(sleeves),
        )
    except ValueError as exc:
        print(f"  {RED}{exc}{RESET}")
        return

    print(f"  {DIM}{result.allocation} allocation, {result.aligned_bars} aligned bars{RESET}\n")
    print(f"  {'sleeve':<28} {'weight':>7} {'sharpe':>8} {'return':>10} {'trades':>7}")
    for sleeve, weight in zip(result.sleeves, result.weights, strict=True):
        print(
            f"  {sleeve.label:<28} {weight:>6.1%} {sleeve.sharpe_ratio:>8.2f} "
            f"{sleeve.total_return_pct:>9.2%} {sleeve.total_trades:>7}"
        )

    print(f"\n  {'Combined Sharpe':<30} {result.sharpe_ratio:>8.2f}")
    print(
        f"  {'Best single sleeve':<30} {result.best_sleeve_sharpe:>8.2f}  "
        f"({result.best_sleeve_label})"
    )
    print(f"  {'Combined return':<30} {_pct(result.total_return_pct):>8}")
    print(f"  {'Combined max drawdown':<30} {result.max_drawdown_pct:>8.2%}")
    if result.diversification_ratio is not None:
        print(f"  {'Diversification ratio':<30} {result.diversification_ratio:>8.2f}")

    if result.correlations:
        print(f"\n  {DIM}Most correlated pairs — this is the whole mechanism:{RESET}")
        for pair in result.correlations[:3]:
            colour = RED if pair.correlation >= 0.7 else ""
            print(f"    {pair.left:<26} {pair.right:<26} {colour}{pair.correlation:+.2f}{RESET}")

    print()
    _verdict(
        "Beats its best sleeve",
        result.sharpe_ratio >= result.best_sleeve_sharpe,
        f"{result.sharpe_ratio:.2f} vs {result.best_sleeve_sharpe:.2f}",
    )
    if result.diversification_ratio is not None:
        _verdict(
            "Sleeves actually diversify",
            result.diversification_ratio >= 1.05,
            f"diversification ratio {result.diversification_ratio:.2f}",
        )
    if result.max_correlation is not None:
        _verdict(
            "No redundant pair",
            result.max_correlation < 0.7,
            f"highest correlation {result.max_correlation:+.2f}",
        )
    dsr = result.deflated_sharpe_ratio
    _verdict(
        "Survives the search",
        dsr is not None and dsr >= 0.95,
        f"deflated Sharpe ratio {dsr:.3f}" if dsr is not None else "not computable",
    )

    for warning in result.warnings:
        print(f"\n  {YELLOW}! {warning}{RESET}")

    print(
        f"\n  {DIM}Sleeves are simulated independently and combined by weight, so each\n"
        f"  gets its own capital and they never compete for it. A real account\n"
        f"  has one balance. Read the combined return as an upper bound.{RESET}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default="AAPL,MSFT,NVDA")
    parser.add_argument("--timeframe", choices=("intraday", "daily"), default="intraday")
    parser.add_argument(
        "--strategy",
        choices=tuple(strategy_names()),
        default="ema_rsi_macd",
        help="which rule to run (single-strategy commands; --portfolio uses "
             "--strategies instead)",
    )
    parser.add_argument("--minutes", type=int, default=15, help="intraday bar size")
    parser.add_argument("--days", type=int, default=59, help="history to fetch")
    parser.add_argument("--capital", type=float, default=100_000.0)
    parser.add_argument("--risk", type=float, default=0.01, help="risk per trade")
    parser.add_argument("--spread-bps", type=float, default=5.0)
    parser.add_argument("--slippage-bps", type=float, default=1.0)
    parser.add_argument("--commission-pct", type=float, default=0.0)
    parser.add_argument("--sweep", action="store_true", help="run a cost sensitivity ladder")
    parser.add_argument(
        "--walk-forward",
        action="store_true",
        help="choose parameters on past data and judge them on what followed",
    )
    parser.add_argument(
        "--sensitivity",
        action="store_true",
        help="score the whole parameter grid: plateau or spike?",
    )
    parser.add_argument("--splits", type=int, default=4, help="walk-forward folds")
    parser.add_argument(
        "--portfolio",
        action="store_true",
        help="combine strategies across symbols and measure the diversification",
    )
    parser.add_argument(
        "--strategies",
        default="",
        help=f"comma-separated; default all ({','.join(strategy_names())})",
    )
    parser.add_argument(
        "--allocation", choices=("equal", "inverse_volatility"), default="equal"
    )
    parser.add_argument(
        "--considered",
        type=int,
        default=None,
        help="how many symbol/strategy combinations you screened before "
             "choosing these — sets the multiple-testing bar honestly",
    )
    parser.add_argument(
        "--objective",
        choices=("sharpe", "return", "profit_factor"),
        default="sharpe",
        help="what the walk-forward selects parameters on",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO if args.verbose else logging.CRITICAL)
    logging.getLogger("yfinance").setLevel(
        logging.INFO if args.verbose else logging.CRITICAL
    )

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    failures = 0
    bars_by_symbol: dict[str, list] = {}
    last_request: BacktestRequest | None = None

    for symbol in symbols:
        request = BacktestRequest(
            symbol=symbol,
            strategy=args.strategy,
            timeframe=args.timeframe,
            intraday_minutes=args.minutes,
            period_days=args.days,
            initial_capital=args.capital,
            risk_per_trade_pct=args.risk,
            spread_bps=args.spread_bps,
            slippage_bps=args.slippage_bps,
            commission_pct=args.commission_pct,
        )
        try:
            bars = load_bars(request)
        except Exception as exc:
            print(f"\n  {RED}{symbol}: no data — {exc}{RESET}")
            failures += 1
            continue

        # The portfolio needs every symbol's bars at once, so it runs after the
        # loop rather than inside it.
        bars_by_symbol[symbol] = bars
        last_request = request
        if args.portfolio:
            continue

        try:
            if not args.walk_forward and not args.sensitivity:
                _report(run_backtest(request, bars))
            if args.sweep:
                _sweep(request, bars)
            if args.walk_forward:
                _walk_forward_report(request, bars, args)
            if args.sensitivity:
                _sensitivity_report(request, bars, args)
        except ValueError as exc:
            print(f"\n  {RED}{symbol}: {exc}{RESET}")
            failures += 1

    if args.portfolio and bars_by_symbol and last_request is not None:
        _portfolio_report(last_request, bars_by_symbol, args)

    print(
        f"\n{DIM}Costs assumed: {args.spread_bps}bps spread, {args.slippage_bps}bps "
        f"slippage, {args.commission_pct:.3%} commission. Past results do not "
        f"predict future returns.{RESET}\n"
    )
    return 1 if failures == len(symbols) else 0


if __name__ == "__main__":
    sys.exit(main())
