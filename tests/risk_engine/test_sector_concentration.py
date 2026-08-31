"""Tests for sector concentration risk check."""
from datetime import datetime, timezone

from autonomy_orchestrator.risk_engine import evaluate_risk
from contracts import CandidateAction, SignalCandidate

BASE_CONFIG = {
    "weekly_notional_cap_usd": 10000,
    "max_position_size_pct": 5.0,
    "max_concurrent_positions": 10,
    "max_daily_drawdown_pct": 3.0,
    "kill_switch": False,
    "max_sector_concentration": 2,
    "symbol_allowlist": ["AAPL", "MSFT", "NVDA"],
    "trading_hours": {"enabled": False},
    "approval_tiers": {},
}


def _signal(symbol: str) -> SignalCandidate:
    return SignalCandidate(
        signal_id="test-001",
        symbol=symbol,
        ts=datetime.now(timezone.utc),
        candidate_action=CandidateAction.BUY,
        confidence=0.8,
        size_pct=0.02,
        model_version="test",
    )


def _portfolio(positions: list[dict]) -> dict:
    return {
        "buying_power": 50000.0,
        "positions": positions,
        "daily_drawdown_pct": 0.0,
    }


def test_sector_concentration_reject():
    portfolio = _portfolio([
        {"symbol": "AAPL", "net_qty": 10, "market_value": 1000},
        {"symbol": "MSFT", "net_qty": 5, "market_value": 800},
    ])
    result = evaluate_risk(_signal("NVDA"), portfolio, weekly_spend=0.0, config=BASE_CONFIG)
    assert not result.approved
    assert "sector_concentration" in result.reason


def test_sector_concentration_allow():
    portfolio = _portfolio([
        {"symbol": "AAPL", "net_qty": 10, "market_value": 1000},
    ])
    result = evaluate_risk(_signal("MSFT"), portfolio, weekly_spend=0.0, config=BASE_CONFIG)
    if not result.approved:
        assert "sector_concentration" not in result.reason
