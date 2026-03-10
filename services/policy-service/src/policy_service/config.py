"""Configuration for the policy service."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class PolicySettings:
    database_url: str = os.getenv("POLICY_DATABASE_URL", "sqlite+pysqlite:///./policy-service.db")
    max_size_pct: float = float(os.getenv("POLICY_MAX_SIZE_PCT", "0.02"))
    confidence_floor: float = float(os.getenv("POLICY_CONFIDENCE_FLOOR", "0.60"))
    max_data_age_seconds: int = int(os.getenv("POLICY_MAX_DATA_AGE_SECONDS", "30"))
    min_liquidity_score: float = float(os.getenv("POLICY_MIN_LIQUIDITY_SCORE", "0.70"))
    max_daily_drawdown_pct: float = float(os.getenv("POLICY_MAX_DAILY_DRAWDOWN_PCT", "0.03"))


settings = PolicySettings()
