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
    # The per-sleeve position cap, enforced server-side on entries. A strategy
    # that signals the same direction every cycle re-enters every cycle unless
    # something that is not the strategy stops it — the first live paper run
    # stacked one sleeve's short from 6 to 19 shares in three cycles. Defaults
    # to max_qty: no sleeve may hold more than the largest single order the
    # broker accepts. Reduce-only orders are exempt — exits must stay possible.
    max_position_qty: int = int(
        os.getenv("EXECUTION_MAX_POSITION_QTY", os.getenv("EXECUTION_MAX_QTY", "1000"))
    )


settings = ExecutionSettings()
