"""Configuration for the execution service."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ExecutionSettings:
    database_url: str = os.getenv(
        "EXECUTION_DATABASE_URL", "sqlite+pysqlite:///./execution-service.db"
    )
    max_qty: int = int(os.getenv("EXECUTION_MAX_QTY", "1000"))


settings = ExecutionSettings()
