from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class NotificationSettings:
    webhook_url: str = field(default_factory=lambda: os.getenv("WEBHOOK_URL", ""))


settings = NotificationSettings()
