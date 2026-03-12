"""Configuration for the policy service."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


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


settings = PolicySettings()
