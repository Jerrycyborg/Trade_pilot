from __future__ import annotations

from contracts import RiskAssessment, SignalCandidate
from market_data.models import OHLCVBar

from .policy_config import is_market_hours

SECTOR_MAP: dict[str, str] = {
    "SPY": "equity_index",
    "QQQ": "equity_index",
    "IWM": "equity_index",
    "TLT": "bonds",
    "BND": "bonds",
    "SHY": "bonds",
    "GLD": "commodities",
    "AAPL": "tech",
    "MSFT": "tech",
    "GOOGL": "tech",
    "AMZN": "tech",
    "NVDA": "tech",
    "META": "tech",
}


def compute_atr_size_pct(
    atr: float,
    entry_price: float,
    buying_power: float,
    risk_per_trade_pct: float = 0.01,
    atr_stop_multiplier: float = 2.0,
) -> float:
    """
    Kelly-lite: risk exactly `risk_per_trade_pct` of buying_power per ATR stop.
    stop_distance = atr * atr_stop_multiplier
    shares = (buying_power * risk_per_trade_pct) / stop_distance
    notional = shares * entry_price
    size_pct = notional / buying_power
    Returns 0.0 if atr or entry_price are zero.
    """
    if atr <= 0.0 or entry_price <= 0.0 or buying_power <= 0.0:
        return 0.0
    stop_distance = atr * atr_stop_multiplier
    if stop_distance <= 0.0:
        return 0.0
    shares = (buying_power * risk_per_trade_pct) / stop_distance
    notional = shares * entry_price
    return notional / buying_power


def evaluate_risk(
    signal: SignalCandidate,
    portfolio_state: dict[str, object],
    weekly_spend: float,
    config: dict[str, object],
    price_bars: list[OHLCVBar] | None = None,
) -> RiskAssessment:
    if config.get("kill_switch"):
        return RiskAssessment(
            approved=False, reason="kill_switch_active", adjusted_size_pct=0.0, tier=3
        )

    allowlist = {str(symbol).upper() for symbol in config.get("symbol_allowlist", [])}
    if signal.symbol.upper() not in allowlist:
        return RiskAssessment(
            approved=False, reason="symbol_not_allowed", adjusted_size_pct=0.0, tier=3
        )

    if not is_market_hours(config):
        return RiskAssessment(
            approved=False, reason="outside_trading_hours", adjusted_size_pct=0.0, tier=2
        )

    proposed_notional = float(portfolio_state.get("buying_power", 100_000.0)) * float(
        signal.size_pct
    )
    weekly_cap = float(config.get("weekly_notional_cap_usd", 0.0))
    if weekly_spend + proposed_notional > weekly_cap:
        return RiskAssessment(
            approved=False, reason="weekly_notional_cap_exceeded", adjusted_size_pct=0.0, tier=3
        )

    positions = portfolio_state.get("positions", [])
    if len(positions) >= int(config.get("max_concurrent_positions", 10)):
        return RiskAssessment(
            approved=False, reason="max_concurrent_positions_reached", adjusted_size_pct=0.0, tier=2
        )

    max_position_size_pct = float(config.get("max_position_size_pct", 5.0)) / 100.0
    adjusted_size_pct = min(float(signal.size_pct), max_position_size_pct)

    # ATR-based position sizing override
    if price_bars is not None and len(price_bars) >= 15:
        from market_data.indicators import compute_atr

        highs = [b.high for b in price_bars]
        lows = [b.low for b in price_bars]
        closes = [b.close for b in price_bars]
        atr = compute_atr(highs, lows, closes)
        entry_price = closes[-1]
        buying_power = float(portfolio_state.get("buying_power", 100_000.0))
        risk_per_trade_pct = float(config.get("risk_per_trade_pct", 0.01))
        atr_stop_multiplier = float(config.get("atr_stop_multiplier", 2.0))
        atr_size = compute_atr_size_pct(
            atr, entry_price, buying_power, risk_per_trade_pct, atr_stop_multiplier
        )
        if atr_size > 0.0:
            adjusted_size_pct = min(atr_size, max_position_size_pct)

    current_drawdown = float(portfolio_state.get("daily_drawdown_pct", 0.0))
    if current_drawdown > float(config.get("max_daily_drawdown_pct", 3.0)) / 100.0:
        return RiskAssessment(
            approved=False, reason="daily_drawdown_limit", adjusted_size_pct=0.0, tier=3
        )

    # Sector concentration check
    max_sector = int(config.get("max_sector_concentration", 2))
    symbol_sector = SECTOR_MAP.get(signal.symbol.upper(), "other")
    if symbol_sector != "other":
        existing_positions = portfolio_state.get("positions", [])
        sector_count = sum(
            1
            for pos in existing_positions
            if SECTOR_MAP.get((pos.get("symbol") or "").upper(), "other") == symbol_sector
        )
        if sector_count >= max_sector:
            return RiskAssessment(
                approved=False,
                reason=f"sector_concentration_limit ({symbol_sector})",
                adjusted_size_pct=0.0,
                tier=1,
            )

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
