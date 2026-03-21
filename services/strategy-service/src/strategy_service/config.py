"""Configuration for the strategy service."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class StrategySettings:
    database_url: str = field(
        default_factory=lambda: os.getenv(
            "STRATEGY_DATABASE_URL", "sqlite+pysqlite:///./strategy-service.db"
        )
    )
    # AI signal generation
    anthropic_api_key: str = field(
        default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", "")
    )
    claude_model: str = field(
        default_factory=lambda: os.getenv("STRATEGY_CLAUDE_MODEL", "claude-haiku-4-5-20251001")
    )
    fallback_to_deterministic: bool = field(
        default_factory=lambda: os.getenv("STRATEGY_FALLBACK_DETERMINISTIC", "true").lower() == "true"
    )
    # Symbol watchlist (comma-separated)
    watchlist_raw: str = field(
        default_factory=lambda: os.getenv(
            "STRATEGY_WATCHLIST", "AAPL,MSFT,GOOGL,BTC/USD,ETH/USD"
        )
    )
    # Research service URL
    research_service_url: str = field(
        default_factory=lambda: os.getenv("RESEARCH_SERVICE_URL", "http://localhost:8005")
    )
    # Worker / scheduler
    worker_enabled: bool = field(
        default_factory=lambda: os.getenv("WORKER_ENABLED", "false").lower() == "true"
    )
    worker_interval_minutes: int = field(
        default_factory=lambda: int(os.getenv("WORKER_INTERVAL_MINUTES", "15"))
    )
    # Downstream service URLs
    policy_service_url: str = field(
        default_factory=lambda: os.getenv("POLICY_SERVICE_URL", "http://localhost:8001")
    )
    execution_service_url: str = field(
        default_factory=lambda: os.getenv("EXECUTION_SERVICE_URL", "http://localhost:8002")
    )
    portfolio_service_url: str = field(
        default_factory=lambda: os.getenv("PORTFOLIO_SERVICE_URL", "http://localhost:8004")
    )
    sentiment_service_url: str = field(
        default_factory=lambda: os.getenv("SENTIMENT_SERVICE_URL", "http://localhost:8008")
    )
    sentiment_weight: float = field(
        default_factory=lambda: float(os.getenv("SENTIMENT_WEIGHT", "0.3"))
    )

    @property
    def watchlist(self) -> list[str]:
        return [s.strip().upper() for s in self.watchlist_raw.split(",") if s.strip()]

    @property
    def use_ai(self) -> bool:
        return bool(self.anthropic_api_key)


settings = StrategySettings()
