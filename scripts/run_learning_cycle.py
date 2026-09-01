#!/usr/bin/env python3
"""Run one bounded offline learning cycle from the paper-trading archive."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from attribution import load_round_trips, performance_from_trades
from backtest_service.models import BacktestRequest, ParameterGrid, StrategyParams
from backtest_service.stats import deflated_sharpe_ratio
from backtest_service.strategies import get_strategy
from backtest_service.validation import walk_forward
from challengers import LearningThresholds, run_learning_cycle
from journal import get_journal
from lifecycle.store import PostgresLifecycleStore
from market_data import OHLCVBar
from veto import review


def _bar(row: dict, symbol: str) -> OHLCVBar:
    return OHLCVBar(
        symbol=symbol,
        timestamp=row["bar_ts"],
        open=row["open"],
        high=row["high"],
        low=row["low"],
        close=row["close"],
        volume=row.get("volume") or 0.0,
    )


def _one_point_grid(params: StrategyParams) -> ParameterGrid:
    return ParameterGrid(
        **{
            name: [getattr(params, name)]
            for name in ParameterGrid.model_fields
        }
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--strategy", default="ema_rsi_macd")
    parser.add_argument("--base-version", required=True)
    parser.add_argument("--champion-json", required=True)
    parser.add_argument("--timeframe", default="1d")
    parser.add_argument("--splits", type=int, default=4)
    parser.add_argument("--account-id", default="default")
    parser.add_argument("--min-paper-trades", type=int, default=20)
    parser.add_argument("--no-record", action="store_true")
    args = parser.parse_args()

    symbol = args.symbol.strip().upper()
    moment = datetime.now(timezone.utc)
    champion_model = StrategyParams(**json.loads(args.champion_json))
    fields = get_strategy(args.strategy).param_fields
    champion = {
        name: float(getattr(champion_model, name))
        for name in fields
    }

    journal = get_journal()
    veto_decision = review(
        journal,
        symbol,
        as_of=moment,
        timeframe=args.timeframe,
    )
    trips = load_round_trips(
        journal,
        strategy_id=args.strategy,
        symbol=symbol,
        environment="paper",
        account_id=args.account_id,
        window_end=moment,
    )
    paper_feedback = performance_from_trades(trips)
    archived = journal.bars_as_of(symbol, args.timeframe, moment)
    bars = [_bar(row, symbol) for row in archived]

    request = BacktestRequest(
        symbol=symbol,
        strategy=args.strategy,
        timeframe="daily" if args.timeframe == "1d" else "intraday",
    )

    def run(candidate):
        merged = champion_model.model_dump()
        merged.update(candidate.parameters)
        params = StrategyParams(**merged)
        return walk_forward(
            request.model_copy(update={"params": params}),
            bars,
            grid=_one_point_grid(params),
            n_splits=args.splits,
        )

    store = None if args.no_record else PostgresLifecycleStore()
    result = run_learning_cycle(
        strategy_id=args.strategy,
        symbol=symbol,
        base_version=args.base_version,
        champion=champion,
        paper_feedback=paper_feedback,
        veto_decision=veto_decision,
        run_walk_forward=run,
        deflate=deflated_sharpe_ratio,
        store=store,
        account_id=args.account_id,
        as_of=moment,
        thresholds=LearningThresholds(
            min_paper_round_trips=args.min_paper_trades,
        ),
    )
    print(json.dumps(result.to_dict(), indent=2, sort_keys=True, default=str))
    return 0 if result.status in {"RECORDED", "EVALUATED_UNRECORDED"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
