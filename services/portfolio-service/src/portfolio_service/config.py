"""Configuration for the portfolio service."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class PortfolioSettings:
    database_url: str = os.getenv(
        "PORTFOLIO_DATABASE_URL", "sqlite+pysqlite:///./portfolio-service.db"
    )
    execution_database_url: str = os.getenv(
        "PORTFOLIO_EXECUTION_DATABASE_URL",
        os.getenv("EXECUTION_DATABASE_URL", "sqlite+pysqlite:///./execution-service.db"),
    )


settings = PortfolioSettings()
