from datetime import datetime, timezone

from autonomy_orchestrator.risk_engine import evaluate_risk
from contracts import SignalCandidate


def _signal(**overrides):
    payload = {
        "signal_id": "sig-1",
        "symbol": "AAPL",
        "ts": datetime.now(timezone.utc),
        "candidate_action": "BUY",
        "confidence": 0.8,
        "size_pct": 0.02,
        "model_version": "test",
    }
    payload.update(overrides)
    return SignalCandidate(**payload)


def test_risk_engine_blocks_allowlist_miss() -> None:
    result = evaluate_risk(
        _signal(symbol="TSLA"),
        {"positions": [], "buying_power": 100_000.0, "daily_drawdown_pct": 0.0},
        0.0,
        {
            "kill_switch": False,
            "symbol_allowlist": ["AAPL"],
            "trading_hours": {"enabled": False},
            "weekly_notional_cap_usd": 500.0,
            "max_concurrent_positions": 10,
            "max_position_size_pct": 5.0,
            "max_daily_drawdown_pct": 3.0,
            "approval_tiers": {},
        },
    )
    assert not result.approved
    assert result.reason == "symbol_not_allowed"


def test_risk_engine_caps_position_size() -> None:
    result = evaluate_risk(
        _signal(size_pct=0.1),
        {"positions": [], "buying_power": 100_000.0, "daily_drawdown_pct": 0.0},
        0.0,
        {
            "kill_switch": False,
            "symbol_allowlist": ["AAPL"],
            "trading_hours": {"enabled": False},
            "weekly_notional_cap_usd": 50_000.0,
            "max_concurrent_positions": 10,
            "max_position_size_pct": 5.0,
            "max_daily_drawdown_pct": 3.0,
            "approval_tiers": {
                "tier1_alert_threshold_usd": 200,
                "tier3_hard_approval_required_usd": 500,
            },
        },
    )
    assert result.approved
    assert result.adjusted_size_pct == 0.05
    assert result.tier == 3
