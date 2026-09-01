"""Scheduled, offline paper-learning runner.

It reads archived bars and paper fills. It can append challenger proposals and
cycle reports, but receives no transition, deployment, policy, or broker API.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from attribution import load_round_trips, performance_from_trades
from backtest_service.models import BacktestRequest, ParameterGrid, StrategyParams
from backtest_service.stats import deflated_sharpe_ratio
from backtest_service.strategies import get_strategy
from backtest_service.validation import walk_forward
from challengers import LearningThresholds, run_learning_cycle
from market_data import OHLCVBar
from veto import review

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


@dataclass(frozen=True)
class StrategyArtifact:
    strategy_id: str
    strategy_version: str
    timeframe: str
    parameters: dict[str, Any]
    sha256: str


def _registry_root() -> Path:
    return Path(
        os.getenv("LEARNING_STRATEGY_REGISTRY_PATH", "config/strategies")
    ).resolve()


def load_strategy_artifact(
    strategy_id: str,
    strategy_version: str,
) -> StrategyArtifact:
    """Load the exact champion artifact named by the lifecycle sleeve."""
    if not _SAFE_ID.fullmatch(strategy_id) or not _SAFE_ID.fullmatch(strategy_version):
        raise ValueError("unsafe strategy artifact identity")
    root = _registry_root()
    path = (root / f"{strategy_id}--{strategy_version}.json").resolve()
    if root not in path.parents:
        raise ValueError("strategy artifact escaped its registry")
    raw = path.read_bytes()
    if len(raw) > 64 * 1024:
        raise ValueError("strategy artifact is too large")
    digest = hashlib.sha256(raw).hexdigest()
    expected = os.getenv("LEARNING_STRATEGY_SHA256", "").strip().lower()
    if expected and digest != expected:
        raise ValueError("strategy artifact digest mismatch")

    payload = json.loads(raw)
    if payload.get("strategy_id") != strategy_id:
        raise ValueError("strategy artifact id mismatch")
    if payload.get("strategy_version") != strategy_version:
        raise ValueError("strategy artifact version mismatch")
    timeframe = str(payload.get("timeframe") or "")
    if timeframe != "1d" and not re.fullmatch(r"[1-9][0-9]*m", timeframe):
        raise ValueError("unsupported strategy artifact timeframe")
    parameters = payload.get("parameters")
    if not isinstance(parameters, dict):
        raise ValueError("strategy artifact parameters must be an object")
    StrategyParams(**parameters)
    return StrategyArtifact(
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        timeframe=timeframe,
        parameters=dict(parameters),
        sha256=digest,
    )


def _bar(row: dict[str, Any], symbol: str) -> OHLCVBar:
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
        **{name: [getattr(params, name)] for name in ParameterGrid.model_fields}
    )


def run_sleeve_learning_cycle(
    *,
    sleeve: Any,
    store: Any,
    journal: Any,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """Evaluate one paper sleeve and append only inert learning records."""
    if sleeve.state != "paper":
        return {"status": "SKIPPED_NOT_PAPER", "sleeve": sleeve.key}
    if getattr(sleeve, "origin", "human") != "human":
        return {"status": "SKIPPED_CHALLENGER_ORIGIN", "sleeve": sleeve.key}
    if not sleeve.strategy_version:
        return {"status": "SKIPPED_UNVERSIONED", "sleeve": sleeve.key}

    artifact = load_strategy_artifact(sleeve.strategy_id, sleeve.strategy_version)
    moment = as_of or datetime.now(timezone.utc)
    champion_model = StrategyParams(**artifact.parameters)
    fields = get_strategy(sleeve.strategy_id).param_fields
    champion = {name: float(getattr(champion_model, name)) for name in fields}
    veto_decision = review(
        journal,
        sleeve.symbol,
        as_of=moment,
        timeframe=artifact.timeframe,
    )
    trips = load_round_trips(
        journal,
        strategy_id=sleeve.strategy_id,
        strategy_version=sleeve.strategy_version,
        symbol=sleeve.symbol,
        environment="paper",
        account_id=sleeve.account_id,
        window_end=moment,
    )
    paper_feedback = performance_from_trades(trips)
    paper_feedback["strategy_artifact_sha256"] = artifact.sha256
    archived = journal.bars_as_of(sleeve.symbol, artifact.timeframe, moment)
    bars = [_bar(row, sleeve.symbol) for row in archived]
    request = BacktestRequest(
        symbol=sleeve.symbol,
        strategy=sleeve.strategy_id,
        timeframe="daily" if artifact.timeframe == "1d" else "intraday",
    )

    def evaluate(candidate: Any) -> Any:
        merged = champion_model.model_dump()
        merged.update(candidate.parameters)
        params = StrategyParams(**merged)
        return walk_forward(
            request.model_copy(update={"params": params}),
            bars,
            grid=_one_point_grid(params),
            n_splits=max(2, min(10, int(os.getenv("LEARNING_WF_SPLITS", "4")))),
        )

    report = run_learning_cycle(
        strategy_id=sleeve.strategy_id,
        symbol=sleeve.symbol,
        base_version=sleeve.strategy_version,
        champion=champion,
        paper_feedback=paper_feedback,
        veto_decision=veto_decision,
        run_walk_forward=evaluate,
        deflate=deflated_sharpe_ratio,
        store=store,
        account_id=sleeve.account_id,
        as_of=moment,
        thresholds=LearningThresholds(
            min_paper_round_trips=max(
                20,
                int(os.getenv("LEARNING_MIN_PAPER_TRADES", "20")),
            ),
        ),
    )
    return report.to_dict()


def run_paper_learning_cycles(store: Any, journal: Any) -> list[dict[str, Any]]:
    """Run a bounded batch over human-owned paper sleeves."""
    maximum = max(1, min(50, int(os.getenv("LEARNING_MAX_SLEEVES", "10"))))
    sleeves = [
        sleeve
        for sleeve in store.all()
        if sleeve.state == "paper" and getattr(sleeve, "origin", "human") == "human"
    ][:maximum]
    results: list[dict[str, Any]] = []
    for sleeve in sleeves:
        try:
            results.append(
                run_sleeve_learning_cycle(
                    sleeve=sleeve,
                    store=store,
                    journal=journal,
                )
            )
        except Exception as exc:  # one bad sleeve must not stop the bounded batch
            results.append(
                {
                    "status": "SKIPPED_INVALID_ARTIFACT_OR_EVIDENCE",
                    "sleeve": sleeve.key,
                    "error_type": type(exc).__name__,
                }
            )
    return results
