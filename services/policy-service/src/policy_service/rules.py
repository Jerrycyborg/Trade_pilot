"""Rule evaluation for the policy service."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from contracts import PolicyDecision, PolicyEvaluationRequest

from .config import merged_policy_config, settings

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

    config = merged_policy_config()
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
    max_size_pct = float(config.get("max_position_size_pct", settings.max_size_pct * 100)) / 100.0
    if request.size_pct >= max_size_pct:
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
    allowlist = {str(symbol).upper() for symbol in config.get("symbol_allowlist", [])}
    if config.get("kill_switch"):
        hard_reasons.append("kill_switch_active")
    if allowlist and request.symbol.upper() not in allowlist:
        hard_reasons.append("symbol_not_allowed")
    elif not request.market_context.symbol_allowed:
        hard_reasons.append("symbol_not_allowed")
    if not _within_trading_hours(config):
        hard_reasons.append("outside_trading_hours")
    if request.portfolio_context.daily_drawdown_pct >= float(config.get("max_daily_drawdown_pct", settings.max_daily_drawdown_pct * 100)) / 100.0:
        hard_reasons.append("daily_drawdown_limit")
    weekly_cap = float(config.get("weekly_notional_cap_usd", 0.0))
    weekly_spend = _weekly_spend()
    proposed_notional = request.size_pct * 100_000.0
    if weekly_spend + proposed_notional > weekly_cap:
        hard_reasons.append("weekly_notional_cap_exceeded")

    if hard_reasons:
        return (
            PolicyDecision(
                signal_id=request.signal_id,
                decision="REJECT",
                reasons=hard_reasons,
                approved_size_pct=0.0,
                tier=3,
            ),
            hard_reasons,
        )

    approved_size_pct = min(request.size_pct, max_size_pct)
    tier = _decision_tier(config, approved_size_pct * 100_000.0)

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
                tier=tier,
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
                tier=tier,
            ),
            review_reasons,
        )

    return (
        PolicyDecision(
            signal_id=request.signal_id,
            decision="APPROVE",
            reasons=[],
            approved_size_pct=approved_size_pct,
            tier=tier,
        ),
        [],
    )


def _within_trading_hours(config: dict[str, object]) -> bool:
    trading_hours = dict(config.get("trading_hours", {}))
    if not trading_hours or not trading_hours.get("enabled", True):
        return True
    try:
        import zoneinfo

        zone = zoneinfo.ZoneInfo(str(trading_hours.get("timezone", "America/New_York")))
    except Exception:
        return True
    now = datetime.now(zone)
    if now.strftime("%a") not in trading_hours.get("days", ["Mon", "Tue", "Wed", "Thu", "Fri"]):
        return False
    start_hour, start_minute = [int(part) for part in str(trading_hours.get("start", "09:30")).split(":")]
    end_hour, end_minute = [int(part) for part in str(trading_hours.get("end", "16:00")).split(":")]
    start = now.replace(hour=start_hour, minute=start_minute, second=0, microsecond=0)
    end = now.replace(hour=end_hour, minute=end_minute, second=0, microsecond=0)
    return start <= now <= end


def _weekly_spend() -> float:
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    try:
        response = httpx.get(
            f"{settings.audit_logger_url}/v1/audit/logs",
            params={"event_type": "trade.executed", "since": since, "limit": 1000},
            timeout=3.0,
        )
        response.raise_for_status()
        return sum(float(row.get("metadata", {}).get("amount_usd", 0.0)) for row in response.json())
    except Exception as exc:
        logger.debug("Weekly spend lookup failed: %s", exc)
        return 0.0


def _decision_tier(config: dict[str, object], amount_usd: float) -> int:
    thresholds = dict(config.get("approval_tiers", {}))
    if amount_usd >= float(thresholds.get("tier3_hard_approval_required_usd", 500)):
        return 3
    if amount_usd >= float(thresholds.get("tier1_alert_threshold_usd", 200)):
        return 2
    return 1
