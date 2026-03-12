"""Configuration for research-service."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ResearchSettings:
    database_url: str = field(
        default_factory=lambda: os.getenv(
            "RESEARCH_DATABASE_URL", "sqlite+pysqlite:///./research-service.db"
        )
    )
    anthropic_api_key: str = field(
        default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", "")
    )
    cache_ttl_seconds: int = field(
        default_factory=lambda: int(os.getenv("RESEARCH_CACHE_TTL_SECONDS", "1800"))
    )
    claude_model: str = field(
        default_factory=lambda: os.getenv("RESEARCH_CLAUDE_MODEL", "claude-opus-4-6")
    )
    max_symbols_per_request: int = field(
        default_factory=lambda: int(os.getenv("RESEARCH_MAX_SYMBOLS", "10"))
    )


settings = ResearchSettings()
