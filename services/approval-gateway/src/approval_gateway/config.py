from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]


@dataclass(frozen=True)
class ApprovalSettings:
    database_url: str = field(
        default_factory=lambda: os.getenv(
            "APPROVAL_DATABASE_URL", "sqlite+pysqlite:///./approval.db"
        )
    )
    policy_config_path: Path = field(
        default_factory=lambda: Path(
            os.getenv("POLICY_BASELINE_PATH", ROOT / "config" / "policy-baseline.yaml")
        )
    )


settings = ApprovalSettings()
