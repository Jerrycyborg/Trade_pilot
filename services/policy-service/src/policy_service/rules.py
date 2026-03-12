"""Rule evaluation for the policy service."""

from __future__ import annotations

import logging
import time
from typing import Optional

from contracts import PolicyDecision, PolicyEvaluationRequest

from .config import settings

logger = logging.getLogger(__name__)

# Module-level Alpaca clock cache: (is_open, fetched_at_monotonic)
_clock_cache: Optional[tuple[bool, float]] = None
_CLOCK_CACHE_TTL = 30.0  # seconds


def _alpaca_market_open() -> bool:
    """Check Alpaca clock endpoint with a 30-second TTL cache."""
    global _clock_cache
    now = time.monotonic()
    if _clock_cache is not None and (now - _clock_cache[1]) < _CLOCK_CACHE_TTL:
        return _clock_cache[0]
    try:
        from alpaca.trading.client import TradingClient

        client = TradingClient(
            api_key=settings.alpaca_api_key,
            secret_key=settings.alpaca_secret_key,
            paper=settings.alpaca_paper,
        )
        clock = client.get_clock()
        is_open = bool(clock.is_open)
        _clock_cache = (is_open, now)
        return is_open
    except Exception as exc:
        logger.warning("Alpaca clock check failed: %s — assuming market open", exc)
        return True


def evaluate_policy(request: PolicyEvaluationRequest) -> tuple[PolicyDecision, list[str]]:
    """Evaluate hard reject and review rules with risk-tier routing."""

    risk_score = getattr(request, "risk_score", "MEDIUM")

    # ------------------------------------------------------------------
    # Risk-tier fast path: HIGH → immediate reject
    # ------------------------------------------------------------------
    if risk_score == "HIGH" and settings.auto_reject_high_risk:
        reason = "high_risk_auto_reject"
        return (
            PolicyDecision(
                signal_id=request.signal_id,
                decision="REJECT",
                reasons=[reason],
                approved_size_pct=0.0,
            ),
            [reason],
        )

    # ------------------------------------------------------------------
    # Standard hard-reject rules
    # ------------------------------------------------------------------
    hard_reasons: list[str] = []
    review_reasons: list[str] = []

    if request.market_context.data_age_seconds > settings.max_data_age_seconds:
        hard_reasons.append("stale_data")
    if request.size_pct > settings.max_size_pct:
        hard_reasons.append("max_size_exceeded")
    if request.market_context.liquidity_score < settings.min_liquidity_score:
        hard_reasons.append("liquidity_too_low")

    # Market open: use Alpaca clock API if configured, else rely on request payload
    if settings.use_alpaca_clock and settings.alpaca_api_key:
        if not _alpaca_market_open():
            hard_reasons.append("alpaca_market_closed")
    elif not request.market_context.market_open:
        hard_reasons.append("market_closed")

    if request.market_context.event_blackout_active:
        hard_reasons.append("event_blackout")
    if not request.market_context.symbol_allowed:
        hard_reasons.append("symbol_not_allowed")
    if request.portfolio_context.daily_drawdown_pct >= settings.max_daily_drawdown_pct:
        hard_reasons.append("daily_drawdown_limit")

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

    # ------------------------------------------------------------------
    # Risk-tier fast path: LOW → skip confidence floor, auto-approve
    # ------------------------------------------------------------------
    if risk_score == "LOW" and settings.auto_approve_low_risk:
        reason = "low_risk_auto_approved"
        return (
            PolicyDecision(
                signal_id=request.signal_id,
                decision="APPROVE",
                reasons=[reason],
                approved_size_pct=approved_size_pct,
            ),
            [reason],
        )

    # ------------------------------------------------------------------
    # Review rules (MEDIUM / HIGH that pass hard rules)
    # ------------------------------------------------------------------
    if request.confidence < settings.confidence_floor:
        review_reasons.append("confidence_below_floor")

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
