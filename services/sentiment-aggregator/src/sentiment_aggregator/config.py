from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class SentimentSettings:
    newsapi_key: str = field(default_factory=lambda: os.getenv("NEWSAPI_KEY", ""))
    alphavantage_key: str = field(default_factory=lambda: os.getenv("ALPHAVANTAGE_KEY", ""))
    etoro_api_key: str = field(default_factory=lambda: os.getenv("ETORO_API_KEY", ""))
    etoro_user_key: str = field(default_factory=lambda: os.getenv("ETORO_USER_KEY", ""))
    cache_ttl_seconds: int = 300


settings = SentimentSettings()
