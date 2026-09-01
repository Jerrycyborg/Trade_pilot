from __future__ import annotations

import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit


@dataclass(frozen=True)
class NotificationSettings:
    webhook_url: str = field(default_factory=lambda: os.getenv("WEBHOOK_URL", ""))
    webhook_allowed_hosts_raw: str = field(
        default_factory=lambda: os.getenv("WEBHOOK_ALLOWED_HOSTS", "")
    )

    @property
    def verified_webhook_url(self) -> str:
        """Return an explicitly allowlisted HTTPS destination, or refuse it."""
        raw = self.webhook_url.strip()
        if not raw:
            return ""
        parsed = urlsplit(raw)
        allowed = {
            host.strip().lower()
            for host in self.webhook_allowed_hosts_raw.split(",")
            if host.strip()
        }
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.hostname.lower() not in allowed
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise ValueError(
                "WEBHOOK_URL requires HTTPS and an exact WEBHOOK_ALLOWED_HOSTS entry"
            )
        return raw


settings = NotificationSettings()
