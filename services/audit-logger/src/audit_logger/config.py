from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class AuditLoggerSettings:
    database_url: str = field(
        default_factory=lambda: os.getenv("AUDIT_DATABASE_URL", "sqlite+pysqlite:///./audit.db")
    )


settings = AuditLoggerSettings()
