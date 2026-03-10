"""Rule evaluation for the policy service."""

from __future__ import annotations

from contracts import PolicyDecision, PolicyEvaluationRequest

from .config import settings


def evaluate_policy(request: PolicyEvaluationRequest) -> tuple[PolicyDecision, list[str]]:
    """Evaluate deterministic hard reject and review rules."""

    hard_reasons: list[str] = []
    review_reasons: list[str] = []

    if request.market_context.data_age_seconds > settings.max_data_age_seconds:
        hard_reasons.append("stale_data")
    if request.size_pct > settings.max_size_pct:
        hard_reasons.append("max_size_exceeded")
    if request.market_context.liquidity_score < settings.min_liquidity_score:
        hard_reasons.append("liquidity_too_low")
    if not request.market_context.market_open:
        hard_reasons.append("market_closed")
    if request.market_context.event_blackout_active:
        hard_reasons.append("event_blackout")
    if not request.market_context.symbol_allowed:
        hard_reasons.append("symbol_not_allowed")
    if request.portfolio_context.daily_drawdown_pct >= settings.max_daily_drawdown_pct:
        hard_reasons.append("daily_drawdown_limit")

    if request.confidence < settings.confidence_floor:
        review_reasons.append("confidence_below_floor")

    if hard_reasons:
        return (
            PolicyDecision(
                signal_id=request.signal_id,
                decision="REJECT",
                reasons=hard_reasons,
                approved_size_pct=0.0,
            ),
            hard_reasons,
        )

    approved_size_pct = min(request.size_pct, settings.max_size_pct)
    if review_reasons:
        return (
            PolicyDecision(
                signal_id=request.signal_id,
                decision="REVIEW",
                reasons=review_reasons,
                approved_size_pct=approved_size_pct,
            ),
            review_reasons,
        )

    return (
        PolicyDecision(
            signal_id=request.signal_id,
            decision="APPROVE",
            reasons=[],
            approved_size_pct=approved_size_pct,
        ),
        [],
    )
