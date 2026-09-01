"""A durable, bounded learning cycle with no deployment authority."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

from .bounds import ChallengerBounds
from .campaign import CampaignResult, evaluate_campaign
from .generate import perturbations


class LearningStore(Protocol):
    """Only append-only research writes are visible to the learner."""

    def record_challenger_proposal(self, **values: Any) -> int: ...

    def record_learning_cycle(
        self,
        *,
        report: dict[str, Any],
        account_id: str | None = None,
    ) -> int: ...


@dataclass(frozen=True)
class LearningThresholds:
    min_paper_round_trips: int = 20
    min_out_of_sample_trades: int = 30
    min_parameter_stability: float = 0.5
    min_deflated_sharpe: float = 0.95


@dataclass(frozen=True)
class LearningCycleReport:
    campaign_id: str
    status: str
    strategy_id: str
    symbol: str
    base_version: str
    as_of: datetime
    paper_feedback: dict[str, Any]
    veto: dict[str, Any]
    campaign: dict[str, Any] | None
    qualified_proposals: tuple[str, ...] = ()
    recorded_proposal_ids: tuple[int, ...] = ()
    deployment_authority: bool = False
    promotion_authority: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "campaign_id": self.campaign_id,
            "status": self.status,
            "strategy_id": self.strategy_id,
            "symbol": self.symbol,
            "base_version": self.base_version,
            "as_of": self.as_of.isoformat(),
            "paper_feedback": self.paper_feedback,
            "veto": self.veto,
            "campaign": self.campaign,
            "qualified_proposals": list(self.qualified_proposals),
            "recorded_proposal_ids": list(self.recorded_proposal_ids),
            "deployment_authority": self.deployment_authority,
            "promotion_authority": self.promotion_authority,
        }
        digest_payload = {k: v for k, v in payload.items() if k != "recorded_proposal_ids"}
        payload["content_hash"] = hashlib.sha256(
            json.dumps(
                digest_payload,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        payload["verdict"] = (
            "The cycle may record bounded proposals only. It cannot register a "
            "sleeve, promote a strategy, change risk policy, enable live mode, "
            "edit code, or contact a broker."
        )
        return payload


def _campaign_id(
    strategy_id: str,
    symbol: str,
    base_version: str,
    champion: dict[str, float],
    as_of: datetime,
) -> str:
    material = {
        "strategy_id": strategy_id,
        "symbol": symbol.upper(),
        "base_version": base_version,
        "champion": champion,
        "as_of": as_of.isoformat(),
    }
    digest = hashlib.sha256(
        json.dumps(material, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return "learn-" + digest[:16]


def _veto_payload(decision: Any) -> dict[str, Any]:
    if not hasattr(decision, "rejected") or not hasattr(decision, "unchecked"):
        raise TypeError("learning requires an explicit VetoDecision")
    if hasattr(decision, "to_dict"):
        return dict(decision.to_dict())
    return {
        "rejected": bool(decision.rejected),
        "unchecked": list(decision.unchecked),
    }


def _is_qualified(result: Any, thresholds: LearningThresholds) -> bool:
    return (
        result.evaluated
        and result.deflated_sharpe_campaign is not None
        and result.deflated_sharpe_campaign >= thresholds.min_deflated_sharpe
        and result.out_of_sample_sharpe is not None
        and result.out_of_sample_sharpe > 0
        and result.out_of_sample_trades >= thresholds.min_out_of_sample_trades
        and result.parameter_stability is not None
        and result.parameter_stability >= thresholds.min_parameter_stability
    )


def run_learning_cycle(
    *,
    strategy_id: str,
    symbol: str,
    base_version: str,
    champion: dict[str, float],
    paper_feedback: dict[str, Any],
    veto_decision: Any,
    run_walk_forward: Callable[[Any], Any],
    deflate: Callable[[list[float], list[float]], float | None],
    store: LearningStore | None,
    account_id: str = "default",
    as_of: datetime | None = None,
    thresholds: LearningThresholds | None = None,
    bounds: ChallengerBounds | None = None,
) -> LearningCycleReport:
    """Learn offline from paper outcomes, validate, and append inert proposals."""
    moment = as_of or datetime.now(timezone.utc)
    active = thresholds or LearningThresholds()
    campaign_id = _campaign_id(
        strategy_id,
        symbol,
        base_version,
        champion,
        moment,
    )
    veto = _veto_payload(veto_decision)

    def finish(
        status: str,
        campaign: CampaignResult | None = None,
        qualified: tuple[str, ...] = (),
        recorded: tuple[int, ...] = (),
    ) -> LearningCycleReport:
        report = LearningCycleReport(
            campaign_id=campaign_id,
            status=status,
            strategy_id=strategy_id,
            symbol=symbol.upper(),
            base_version=base_version,
            as_of=moment,
            paper_feedback=dict(paper_feedback),
            veto=veto,
            campaign=campaign.to_dict() if campaign else None,
            qualified_proposals=qualified,
            recorded_proposal_ids=recorded,
        )
        if store is not None:
            store.record_learning_cycle(report=report.to_dict(), account_id=account_id)
        return report

    if bool(veto["rejected"]):
        return finish("VETOED")
    if list(veto.get("unchecked") or []):
        return finish("VETO_INCOMPLETE")

    paper_trades = int(paper_feedback.get("trades") or 0)
    if paper_trades < active.min_paper_round_trips:
        return finish("INSUFFICIENT_PAPER_EVIDENCE")

    challengers = perturbations(
        strategy_id=strategy_id,
        symbol=symbol,
        base_version=base_version,
        champion=champion,
        bounds=bounds,
        generated_at=moment,
    )
    if not challengers:
        return finish("NO_CHALLENGERS")

    campaign = evaluate_campaign(
        challengers,
        run_walk_forward=run_walk_forward,
        deflate=deflate,
        min_deflated_sharpe=active.min_deflated_sharpe,
    )
    qualified = tuple(
        result.challenger.challenger_id
        for result in campaign.results
        if _is_qualified(result, active)
    )
    recorded: list[int] = []
    if store is not None:
        for result in campaign.results:
            recorded.append(
                store.record_challenger_proposal(
                    campaign_id=campaign_id,
                    challenger=result.challenger.to_dict(),
                    deflated_sharpe_campaign=result.deflated_sharpe_campaign,
                    deflated_sharpe_own_search=result.deflated_sharpe_own_search,
                    pooled_trials=campaign.pooled_trials,
                    out_of_sample_sharpe=result.out_of_sample_sharpe,
                    survived=result.challenger.challenger_id in qualified,
                    account_id=account_id,
                )
            )
    status = "RECORDED" if store is not None else "EVALUATED_UNRECORDED"
    return finish(status, campaign, qualified, tuple(recorded))
