"""Promotion evidence, derived by the server from durable records.

The previous API took an ``Evidence`` object as a request body, and every gate
read exactly what the caller sent. Promotion to live was available to anyone
who could construct::

    {"deflated_sharpe_ratio": 0.99, "out_of_sample_trades": 500}

The gates were real and the inputs were fiction. Nothing here accepts a
performance number from a request. A promotion names a sleeve and, where a
backtest is involved, the IDs of validation artifacts that were written when
the validation actually ran. Everything else is read from the journal and the
artifact table, scoped, and frozen into an immutable snapshot.

**Scope is mandatory, not optional.** Every derivation filters on strategy,
symbol, environment and account. A figure computed without `environment` would
average a simulator's fills together with a venue's; one computed without
`strategy_id` would credit a sleeve with another sleeve's trades. Acceptance
criterion 7 is enforced here, by construction, rather than by remembering to
filter.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

#: Artifact kinds a promotion may cite.
WALK_FORWARD = "walk_forward"
PORTFOLIO_CORRELATION = "portfolio_correlation"


class EvidenceError(RuntimeError):
    """Evidence could not be derived. Never a reason to promote anyway."""


@dataclass
class DerivedEvidence:
    """What the server established, and where each part came from.

    `metrics` feeds the gates. `source_artifacts` is the audit trail: which
    rows, with which hashes, produced those numbers. A reviewer months later
    can establish that the figures behind a promotion are the ones that were
    measured, rather than taking the transition's word for it.
    """

    scope: dict[str, Any]
    metrics: dict[str, Any]
    source_artifacts: list[dict[str, Any]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return not self.problems

    def content_hash(self) -> str:
        payload = {
            "scope": self.scope,
            "metrics": self.metrics,
            "source_artifacts": self.source_artifacts,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()


def _scope_matches(artifact: dict[str, Any], sleeve: Any, expected_kind: str) -> str | None:
    """Whether an artifact actually describes this sleeve. Returns a problem, or None.

    An artifact for another symbol, another strategy or another environment is
    not weaker evidence — it is evidence about something else, and accepting it
    would let a result measured on one instrument promote another.
    """
    if artifact["kind"] != expected_kind:
        return f"artifact {artifact['id']} is a {artifact['kind']}, expected {expected_kind}"
    if artifact["strategy_id"] != sleeve.strategy_id:
        return (
            f"artifact {artifact['id']} is for strategy {artifact['strategy_id']!r}, "
            f"sleeve is {sleeve.strategy_id!r}"
        )
    if artifact["symbol"].upper() != sleeve.symbol.upper():
        return (
            f"artifact {artifact['id']} is for {artifact['symbol']}, "
            f"sleeve is {sleeve.symbol}"
        )
    if sleeve.strategy_version and artifact["strategy_version"] != sleeve.strategy_version:
        return (
            f"artifact {artifact['id']} is for strategy version "
            f"{artifact['strategy_version']!r}, sleeve is {sleeve.strategy_version!r}"
        )
    return None


def derive_backtest_evidence(
    *,
    store: Any,
    sleeve: Any,
    artifact_ids: list[int],
) -> DerivedEvidence:
    """Evidence for candidate -> paper, from stored walk-forward artifacts.

    No numbers are taken from the caller. The caller says *which validation
    run* to read; the server reads it.
    """
    scope = {
        "strategy_id": sleeve.strategy_id,
        "strategy_version": sleeve.strategy_version,
        "symbol": sleeve.symbol,
        "asset_class": sleeve.asset_class,
        "environment": "backtest",
        "broker": "none",
        "account_id": sleeve.account_id,
        "portfolio_id": "none",
    }

    if not artifact_ids:
        return DerivedEvidence(
            scope=scope,
            metrics={},
            problems=[
                "no validation artifact cited — run the walk-forward and record "
                "its result before requesting promotion"
            ],
        )

    problems: list[str] = []
    artifacts: list[dict[str, Any]] = []
    for artifact_id in artifact_ids:
        artifact = store.validation_artifact(artifact_id)
        if artifact is None:
            problems.append(f"artifact {artifact_id} does not exist")
            continue
        mismatch = _scope_matches(artifact, sleeve, WALK_FORWARD)
        if mismatch:
            problems.append(mismatch)
            continue
        artifacts.append(artifact)

    if not artifacts:
        return DerivedEvidence(scope=scope, metrics={}, problems=problems or ["no usable artifact"])

    # Most recent wins if several are cited; the others stay in the audit trail.
    latest = max(artifacts, key=lambda a: a["created_at"])
    payload = latest["payload"] or {}

    windows = [(_parse(a["window_start"]), _parse(a["window_end"])) for a in artifacts]
    scope["window_start"] = min(w[0] for w in windows)
    scope["window_end"] = max(w[1] for w in windows)
    scope["data_version"] = latest.get("data_version", "")
    scope["model_version"] = latest.get("model_version", "")

    metrics = {
        "deflated_sharpe_ratio": payload.get("deflated_sharpe_ratio"),
        "probabilistic_sharpe_ratio": payload.get("probabilistic_sharpe_ratio"),
        "out_of_sample_sharpe": payload.get("out_of_sample_sharpe"),
        "out_of_sample_return_pct": payload.get("out_of_sample_return_pct"),
        "out_of_sample_trades": payload.get("out_of_sample_trades"),
        "out_of_sample_max_drawdown_pct": payload.get("out_of_sample_max_drawdown_pct"),
        "n_trials": payload.get("n_trials"),
        "parameter_stability": payload.get("parameter_stability"),
        "n_folds": payload.get("n_folds"),
    }

    return DerivedEvidence(
        scope=scope,
        metrics=metrics,
        source_artifacts=[
            {
                "id": a["id"],
                "kind": a["kind"],
                "content_hash": a["content_hash"],
                "created_at": str(a["created_at"]),
                "produced_by": a.get("produced_by", ""),
            }
            for a in artifacts
        ],
        problems=problems,
    )


def derive_paper_evidence(
    *,
    store: Any,
    journal: Any,
    sleeve: Any,
    window_start: datetime,
    window_end: datetime | None = None,
    correlation_artifact_id: int | None = None,
    expected_interval_minutes: float = 15.0,
    timeframe: str = "15m",
) -> DerivedEvidence:
    """Evidence for paper -> live, from what the paper run actually did.

    Reads simulated fills, misses, partial fills, realized P&L, fees and
    implementation shortfall from the journal — scoped to this sleeve in the
    *paper* environment, so live or backtest records cannot leak in. Journal
    completeness over the same window is part of the evidence: a window with a
    gap is not a window you can conclude anything from.
    """
    end = window_end or datetime.now(timezone.utc)
    scope = {
        "strategy_id": sleeve.strategy_id,
        "strategy_version": sleeve.strategy_version,
        "symbol": sleeve.symbol,
        "asset_class": sleeve.asset_class,
        "environment": "paper",
        "broker": "paper",
        "account_id": sleeve.account_id,
        "portfolio_id": sleeve.account_id,
        "window_start": window_start,
        "window_end": end,
        "data_version": "",
        "model_version": sleeve.strategy_version,
    }

    problems: list[str] = []

    execution = journal.scoped_execution_metrics(
        strategy_id=sleeve.strategy_id,
        symbol=sleeve.symbol,
        environment="paper",
        account_id=sleeve.account_id,
        strategy_version=sleeve.strategy_version,
        window_start=window_start,
        window_end=end,
    )
    if not execution.get("available"):
        problems.append(f"execution records unavailable: {execution.get('reason')}")

    performance: dict[str, Any] = {}
    try:
        from attribution import load_round_trips, performance_from_trades

        round_trips = load_round_trips(
            journal,
            strategy_id=sleeve.strategy_id,
            strategy_version=sleeve.strategy_version,
            symbol=sleeve.symbol,
            environment="paper",
            account_id=sleeve.account_id,
            window_start=window_start,
            window_end=end,
        )
        performance = performance_from_trades(round_trips)
    except Exception as exc:
        problems.append(f"paper round-trip P&L unavailable: {exc}")

    completeness = journal.completeness(
        symbol=sleeve.symbol,
        timeframe=timeframe,
        window_start=window_start,
        window_end=end,
        expected_interval_minutes=expected_interval_minutes,
    )
    if not completeness.get("available"):
        # Cannot read the journal at all: the evidence is meaningless, not
        # merely weak.
        problems.append(f"journal completeness unavailable: {completeness.get('reason')}")
    # An *incomplete* journal is reported as a metric, not as a derivation
    # problem, and the gate refuses on it. The difference matters to whoever is
    # reading the refusal: a derivation problem short-circuits every other
    # check, so an operator would fix the journal, re-run, and only then
    # discover the five other reasons the promotion was never going to pass.
    # A window with a hole still cannot support a promotion — that is enforced
    # by `gates.evaluate_to_live`, which reads `journal_complete` below.

    source_artifacts: list[dict[str, Any]] = []
    correlation = None
    if correlation_artifact_id is not None:
        artifact = store.validation_artifact(correlation_artifact_id)
        if artifact is None:
            problems.append(f"correlation artifact {correlation_artifact_id} does not exist")
        else:
            mismatch = _scope_matches(artifact, sleeve, PORTFOLIO_CORRELATION)
            if mismatch:
                problems.append(mismatch)
            else:
                correlation = (artifact["payload"] or {}).get("max_correlation_with_live")
                source_artifacts.append(
                    {
                        "id": artifact["id"],
                        "kind": artifact["kind"],
                        "content_hash": artifact["content_hash"],
                        "created_at": str(artifact["created_at"]),
                    }
                )

    paper_days = max(0.0, (end - _aware(window_start)).total_seconds() / 86400.0)

    metrics = {
        "paper_days": round(paper_days, 2),
        "paper_orders": execution.get("orders", 0),
        "paper_fills": execution.get("fills", 0),
        "paper_misses": execution.get("misses", 0),
        "partial_fills": execution.get("partial_fills", 0),
        "cancellations": execution.get("cancellations", 0),
        "rejections": execution.get("rejections", 0),
        "fill_rate": execution.get("fill_rate"),
        "closed_round_trips": performance.get("trades", 0),
        "gross_realized_pnl": performance.get("realized_total", 0.0),
        "realized_pnl": round(
            float(performance.get("realized_total") or 0.0)
            - float(execution.get("fees") or 0.0),
            4,
        ),
        "execution_cash_effect": execution.get("execution_cash_effect", 0.0),
        "fees": execution.get("fees", 0.0),
        "measured_shortfall_bps": execution.get("mean_shortfall_bps"),
        "worst_shortfall_bps": execution.get("worst_shortfall_bps"),
        "journal_complete": completeness.get("complete"),
        "journal_gap_count": completeness.get("gap_count"),
        "max_correlation_with_live": correlation,
    }

    return DerivedEvidence(
        scope=scope, metrics=metrics, source_artifacts=source_artifacts, problems=problems
    )


def _parse(value: Any) -> datetime:
    if isinstance(value, datetime):
        return _aware(value)
    return _aware(datetime.fromisoformat(str(value)))


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
