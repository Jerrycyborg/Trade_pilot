from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]


@dataclass(frozen=True)
class OrchestratorSettings:
    orchestrator_interval_minutes: int = field(
        default_factory=lambda: int(os.getenv("ORCHESTRATOR_INTERVAL_MINUTES", "5"))
    )
    policy_config_path: Path = field(
        default_factory=lambda: Path(
            os.getenv("POLICY_BASELINE_PATH", ROOT / "config" / "policy-baseline.yaml")
        )
    )
    broker_url: str = field(
        default_factory=lambda: os.getenv(
            "BROKER_URL",
            os.getenv("EXECUTION_SERVICE_URL", "http://localhost:8002"),
        )
    )
    strategy_service_url: str = field(
        default_factory=lambda: os.getenv("STRATEGY_SERVICE_URL", "http://localhost:8003")
    )
    policy_service_url: str = field(
        default_factory=lambda: os.getenv("POLICY_SERVICE_URL", "http://localhost:8001")
    )
    execution_service_url: str = field(
        default_factory=lambda: os.getenv("EXECUTION_SERVICE_URL", "http://localhost:8002")
    )
    portfolio_service_url: str = field(
        default_factory=lambda: os.getenv("PORTFOLIO_SERVICE_URL", "http://localhost:8004")
    )
    audit_logger_url: str = field(
        default_factory=lambda: os.getenv("AUDIT_LOGGER_URL", "http://localhost:8006")
    )
    approval_gateway_url: str = field(
        default_factory=lambda: os.getenv("APPROVAL_GATEWAY_URL", "http://localhost:8010")
    )
    notification_service_url: str = field(
        default_factory=lambda: os.getenv("NOTIFICATION_SERVICE_URL", "http://localhost:8009")
    )
    internal_api_key: str = field(default_factory=lambda: os.getenv("INTERNAL_API_KEY", ""))
    account_id: str = field(default_factory=lambda: os.getenv("TRADING_ACCOUNT_ID", "default"))
    monthly_loss_limit_usd: float = field(
        default_factory=lambda: float(os.getenv("MONTHLY_LOSS_LIMIT_USD", "10.0"))
    )
    monthly_profit_target_usd: float = field(
        default_factory=lambda: float(os.getenv("MONTHLY_PROFIT_TARGET_USD", "20.0"))
    )
    take_profit_target_usd: float = field(
        default_factory=lambda: float(os.getenv("TAKE_PROFIT_TARGET_USD", "20.0"))
    )
    wallet_size_usd: float = field(
        default_factory=lambda: float(os.getenv("WALLET_SIZE_USD", "50.0"))
    )


settings = OrchestratorSettings()
