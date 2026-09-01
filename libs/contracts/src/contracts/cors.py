"""CORS policy for browser-facing service endpoints.

Production defaults to no cross-origin access. Explicit origins are deployment
configuration, not application code, and wildcard access is limited to an
opt-in paper-only development environment.
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit


def cors_origins() -> list[str]:
    """Return validated, explicit browser origins for FastAPI middleware."""
    raw = os.getenv("CORS_ALLOWED_ORIGINS", "")
    origins = [value.strip().rstrip("/") for value in raw.split(",") if value.strip()]
    if not origins:
        return []

    app_env = os.getenv("APP_ENV", "production").strip().lower()
    broker = os.getenv("BROKER", "paper").strip().lower()
    if "*" in origins:
        if origins != ["*"]:
            raise RuntimeError("CORS wildcard cannot be combined with explicit origins")
        if app_env not in {"development", "test"} or broker != "paper":
            raise RuntimeError("CORS wildcard is only allowed in paper development/test")
        return origins

    for origin in origins:
        parsed = urlsplit(origin)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise RuntimeError(f"Invalid CORS origin: {origin!r}")
    return origins
