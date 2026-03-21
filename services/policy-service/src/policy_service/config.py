"""Configuration for the policy service."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[4]


@dataclass(frozen=True)
class PolicySettings:
    database_url: str = field(
        default_factory=lambda: os.getenv("POLICY_DATABASE_URL", "sqlite+pysqlite:///./policy-service.db")
    )
    max_size_pct: float = field(
        default_factory=lambda: float(os.getenv("POLICY_MAX_SIZE_PCT", "0.02"))
    )
    confidence_floor: float = field(
        default_factory=lambda: float(os.getenv("POLICY_CONFIDENCE_FLOOR", "0.60"))
    )
    max_data_age_seconds: int = field(
        default_factory=lambda: int(os.getenv("POLICY_MAX_DATA_AGE_SECONDS", "30"))
    )
    min_liquidity_score: float = field(
        default_factory=lambda: float(os.getenv("POLICY_MIN_LIQUIDITY_SCORE", "0.70"))
    )
    max_daily_drawdown_pct: float = field(
        default_factory=lambda: float(os.getenv("POLICY_MAX_DAILY_DRAWDOWN_PCT", "0.03"))
    )
    # Milestone 2: risk-tier routing
    auto_approve_low_risk: bool = field(
        default_factory=lambda: os.getenv("POLICY_AUTO_APPROVE_LOW", "true").lower() == "true"
    )
    auto_reject_high_risk: bool = field(
        default_factory=lambda: os.getenv("POLICY_AUTO_REJECT_HIGH", "true").lower() == "true"
    )
    # Alpaca market clock integration (optional)
    use_alpaca_clock: bool = field(
        default_factory=lambda: os.getenv("POLICY_USE_ALPACA_CLOCK", "false").lower() == "true"
    )
    alpaca_api_key: str = field(
        default_factory=lambda: os.getenv("ALPACA_API_KEY", "")
    )
    alpaca_secret_key: str = field(
        default_factory=lambda: os.getenv("ALPACA_SECRET_KEY", "")
    )
    alpaca_paper: bool = field(
        default_factory=lambda: os.getenv("ALPACA_PAPER", "true").lower() == "true"
    )
    audit_logger_url: str = field(
        default_factory=lambda: os.getenv("AUDIT_LOGGER_URL", "http://localhost:8006")
    )
    policy_config_path: Path = field(
        default_factory=lambda: Path(os.getenv("POLICY_BASELINE_PATH", ROOT / "config" / "policy-baseline.yaml"))
    )


settings = PolicySettings()


def load_policy_baseline() -> dict[str, object]:
    if not settings.policy_config_path.exists():
        return {}
    return yaml.safe_load(settings.policy_config_path.read_text(encoding="utf-8")) or {}


def merged_policy_config() -> dict[str, object]:
    baseline = load_policy_baseline()
    baseline["max_position_size_pct"] = float(
        os.getenv("POLICY_MAX_SIZE_PCT", str(baseline.get("max_position_size_pct", settings.max_size_pct * 100)))
    )
    baseline["max_daily_drawdown_pct"] = float(
        os.getenv(
            "POLICY_MAX_DAILY_DRAWDOWN_PCT",
            str(baseline.get("max_daily_drawdown_pct", settings.max_daily_drawdown_pct * 100)),
        )
    )
    baseline["kill_switch"] = os.getenv(
        "POLICY_KILL_SWITCH", str(baseline.get("kill_switch", False))
    ).lower() == "true"
    # Allow tests/CI to disable trading hours via env var
    if os.getenv("POLICY_WEEKLY_CAP_USD"):
        baseline["weekly_notional_cap_usd"] = float(os.getenv("POLICY_WEEKLY_CAP_USD"))
    if os.getenv("POLICY_DISABLE_TRADING_HOURS", "false").lower() == "true":
        th = dict(baseline.get("trading_hours", {}))
        th["enabled"] = False
        baseline["trading_hours"] = th
    return baseline
