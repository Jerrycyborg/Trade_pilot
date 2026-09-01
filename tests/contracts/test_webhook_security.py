from __future__ import annotations

import pytest
from notification_service.config import NotificationSettings


def test_webhook_requires_https_and_an_exact_allowlist() -> None:
    settings = NotificationSettings(
        webhook_url="https://hooks.example.com/trade",
        webhook_allowed_hosts_raw="hooks.example.com",
    )
    assert settings.verified_webhook_url == "https://hooks.example.com/trade"


@pytest.mark.parametrize(
    "url",
    [
        "http://hooks.example.com/trade",
        "https://127.0.0.1/internal",
        "https://user:password@hooks.example.com/trade",
        "https://hooks.example.com/trade#secret",
        "https://hooks.example.com.evil.test/trade",
    ],
)
def test_webhook_rejects_untrusted_destinations(url: str) -> None:
    settings = NotificationSettings(
        webhook_url=url,
        webhook_allowed_hosts_raw="hooks.example.com",
    )
    with pytest.raises(ValueError, match="exact"):
        _ = settings.verified_webhook_url
