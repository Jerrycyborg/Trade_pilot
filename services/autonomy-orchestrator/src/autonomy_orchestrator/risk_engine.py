from __future__ import annotations

from contracts import RiskAssessment, SignalCandidate

from .policy_config import is_market_hours


def evaluate_risk(
    signal: SignalCandidate,
    portfolio_state: dict[str, object],
    weekly_spend: float,
    config: dict[str, object],
) -> RiskAssessment:
    if config.get("kill_switch"):
        return RiskAssessment(approved=False, reason="kill_switch_active", adjusted_size_pct=0.0, tier=3)

    allowlist = {str(symbol).upper() for symbol in config.get("symbol_allowlist", [])}
    if signal.symbol.upper() not in allowlist:
        return RiskAssessment(approved=False, reason="symbol_not_allowed", adjusted_size_pct=0.0, tier=3)

    if not is_market_hours(config):
        return RiskAssessment(approved=False, reason="outside_trading_hours", adjusted_size_pct=0.0, tier=2)

    proposed_notional = float(portfolio_state.get("buying_power", 100_000.0)) * float(signal.size_pct)
    weekly_cap = float(config.get("weekly_notional_cap_usd", 0.0))
    if weekly_spend + proposed_notional > weekly_cap:
        return RiskAssessment(approved=False, reason="weekly_notional_cap_exceeded", adjusted_size_pct=0.0, tier=3)

    positions = portfolio_state.get("positions", [])
    if len(positions) >= int(config.get("max_concurrent_positions", 10)):
        return RiskAssessment(approved=False, reason="max_concurrent_positions_reached", adjusted_size_pct=0.0, tier=2)

    max_position_size_pct = float(config.get("max_position_size_pct", 5.0)) / 100.0
    adjusted_size_pct = min(float(signal.size_pct), max_position_size_pct)

    current_drawdown = float(portfolio_state.get("daily_drawdown_pct", 0.0))
    if current_drawdown > float(config.get("max_daily_drawdown_pct", 3.0)) / 100.0:
        return RiskAssessment(approved=False, reason="daily_drawdown_limit", adjusted_size_pct=0.0, tier=3)

    tier = 1
    proposed_amount = float(portfolio_state.get("buying_power", 100_000.0)) * adjusted_size_pct
    thresholds = dict(config.get("approval_tiers", {}))
    if proposed_amount >= float(thresholds.get("tier3_hard_approval_required_usd", 500)):
        tier = 3
    elif proposed_amount >= float(thresholds.get("tier1_alert_threshold_usd", 200)):
        tier = 2
    return RiskAssessment(
        approved=True,
        reason="approved",
        adjusted_size_pct=round(adjusted_size_pct, 4),
        tier=tier,
    )
