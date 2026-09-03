#!/usr/bin/env python
"""Does the champion rule have an edge? Run this on a machine with internet.

The sandbox this was written in cannot reach Yahoo or Alpaca, so this script
exists to be run where the data is reachable. It answers one question and
writes the answer to a JSON file you can hand back for analysis.

    uv run python scripts/run_edge_study.py
    uv run python scripts/run_edge_study.py --symbols AAPL,MSFT,NVDA --years 8
    uv run python scripts/run_edge_study.py --out study.json

What it does, per symbol: fetch multi-year daily bars, run the existing
anchored walk-forward (parameters chosen on past data, judged only on the data
that followed, with an embargo between them), at two cost levels. It reports
the Deflated Sharpe — the Sharpe adjusted for how many parameter combinations
were tried — because a raw Sharpe picked as the best of N trials is inflated by
construction.

It also answers a second question the first version could only flag: the live
worker suppresses a BUY when ADX is below 20 or unmeasurable, and when volume
does not exceed its 20-bar average. Those gates were applied on faith and the
backtest never modelled them, so every run measured a more permissive strategy
than the one that trades. Each cost case now runs twice — gated and ungated —
and the difference is the gates' contribution.

The gated variant is what live actually does. Read that row as the forecast and
the ungated one as the counterfactual.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from itertools import product
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for pkg in ("backtest-service", "strategy-service"):
    sys.path.insert(0, str(ROOT / "services" / pkg / "src"))
sys.path.insert(0, str(ROOT / "libs" / "market_data" / "src"))
sys.path.insert(0, str(ROOT / "libs" / "contracts" / "src"))

from backtest_service.models import BacktestRequest  # noqa: E402
from backtest_service.validation import walk_forward  # noqa: E402
from market_data.models import OHLCVBar  # noqa: E402

# Round-trip cost in basis points, split into the per-side spread the engine
# charges. Derived in ADR-006 from Schwarz et al. (JF 2025), who measured real
# retail round trips at 7-46 bps for identical simultaneous orders.
COST_CASES = {
    "base": {"spread_bps": 10.0, "slippage_bps": 0.0},
    "stress": {"spread_bps": 20.0, "slippage_bps": 0.0},
}

# Off reproduces the backtest's historical behaviour; on reproduces live.
GATE_CASES = {
    "ungated": {"regime_gate": False, "volume_gate": False},
    "gated": {"regime_gate": True, "volume_gate": True},
}


def fetch_daily_bars(symbol: str, years: int) -> list[OHLCVBar]:
    """Daily bars from Yahoo. Adjusted for splits and dividends."""
    try:
        import yfinance as yf
    except ImportError:  # pragma: no cover - environment guidance
        raise SystemExit("yfinance is required: uv pip install yfinance") from None

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=int(years * 365.25))
    frame = yf.Ticker(symbol).history(
        start=start.date(), end=end.date(), interval="1d", auto_adjust=True
    )
    if frame is None or frame.empty:
        raise RuntimeError(f"No bars returned for {symbol}")

    bars: list[OHLCVBar] = []
    for stamp, row in frame.iterrows():
        moment = stamp.to_pydatetime()
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        bars.append(
            OHLCVBar(
                symbol=symbol.upper(),
                timestamp=moment.astimezone(timezone.utc),
                open=float(row["Open"]),
                high=float(row["High"]),
                low=float(row["Low"]),
                close=float(row["Close"]),
                volume=float(row["Volume"]),
            )
        )
    return bars


def study_symbol(symbol: str, bars: list[OHLCVBar], splits: int) -> dict:
    out: dict[str, object] = {"symbol": symbol, "bars": len(bars), "cases": {}}
    for (cost_name, costs), (gate_name, gates) in product(
        COST_CASES.items(), GATE_CASES.items()
    ):
        case = f"{cost_name}/{gate_name}"
        request = BacktestRequest(
            symbol=symbol,
            strategy="ema_rsi_macd",
            timeframe="daily",
            commission_pct=0.0,
            **costs,
            **gates,
        )
        try:
            result = walk_forward(request, bars, n_splits=splits)
        except Exception as exc:
            out["cases"][case] = {"error": str(exc)}
            continue
        out["cases"][case] = {
            "oos_sharpe": round(result.out_of_sample_sharpe, 3),
            "deflated_sharpe": (
                round(result.deflated_sharpe_ratio, 3)
                if result.deflated_sharpe_ratio is not None
                else None
            ),
            "probabilistic_sharpe": (
                round(result.probabilistic_sharpe_ratio, 3)
                if result.probabilistic_sharpe_ratio is not None
                else None
            ),
            "oos_return_pct": round(result.out_of_sample_return_pct, 2),
            "oos_max_drawdown_pct": round(result.out_of_sample_max_drawdown_pct, 2),
            "oos_trades": result.out_of_sample_trades,
            "in_sample_sharpe": round(result.in_sample_sharpe, 3),
            "sharpe_degradation": round(result.sharpe_degradation, 3),
            "n_trials": result.n_trials,
            "n_folds": result.n_folds,
        }
    return out


def verdict(results: list[dict]) -> dict:
    """The kill criterion from TASK-009, applied mechanically."""
    # Judge the configuration that actually trades: gated, at stress cost.
    trades = sum(
        case.get("oos_trades", 0)
        for row in results
        for name, case in row["cases"].items()
        if name == "stress/gated" and "oos_trades" in case
    )

    stress = [
        case.get("deflated_sharpe")
        for row in results
        for name, case in row["cases"].items()
        if name == "stress/gated" and case.get("deflated_sharpe") is not None
    ]
    positive = [d for d in stress if d > 0]

    if trades < 100:
        call = (
            "NO VERDICT — under 100 out-of-sample trades; the sample "
            "cannot support a claim either way"
        )
    elif not positive:
        call = "DEAD — no symbol clears a positive Deflated Sharpe at the stress cost case"
    else:
        call = (
            f"SURVIVES on {len(positive)}/{len(stress)} symbols at stress "
            "cost — worth continuing to Phase 2"
        )
    return {"total_oos_trades": trades, "verdict": call}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default="AAPL,MSFT,GOOGL,NVDA,AMZN,META,AMD,AVGO")
    parser.add_argument("--years", type=int, default=8)
    parser.add_argument("--splits", type=int, default=5)
    parser.add_argument("--out", default="edge_study.json")
    args = parser.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    results = []
    for symbol in symbols:
        print(f"[{symbol}] fetching {args.years}y daily…", flush=True)
        try:
            bars = fetch_daily_bars(symbol, args.years)
        except Exception as exc:
            print(f"[{symbol}] SKIPPED: {exc}", flush=True)
            results.append({"symbol": symbol, "error": str(exc)})
            continue
        print(f"[{symbol}] {len(bars)} bars — walk-forward…", flush=True)
        row = study_symbol(symbol, bars, args.splits)
        results.append(row)
        for case, stats in row["cases"].items():
            if "error" in stats:
                print(f"  {case}: ERROR {stats['error']}")
            else:
                print(
                    f"  {case}: OOS Sharpe {stats['oos_sharpe']:+.2f} | "
                    f"Deflated {stats['deflated_sharpe']} | "
                    f"{stats['oos_trades']} trades | "
                    f"ret {stats['oos_return_pct']:+.1f}% | "
                    f"maxDD {stats['oos_max_drawdown_pct']:.1f}%",
                    flush=True,
                )

    summary = verdict([r for r in results if "cases" in r])
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "years": args.years,
        "splits": args.splits,
        "cost_cases": COST_CASES,
        "caveat": (
            "Measures the ungated rule. The live worker also applies an ADX<20 "
            "regime gate and a volume-confirmation gate that the backtest does "
            "not model, so live takes strictly fewer trades than shown."
        ),
        "results": results,
        "summary": summary,
    }
    Path(args.out).write_text(json.dumps(payload, indent=2))

    print("\n" + "=" * 68)
    print(f"Out-of-sample trades across symbols: {summary['total_oos_trades']}")
    print(summary["verdict"])
    print(f"\nWritten to {args.out} — send this back for analysis.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
