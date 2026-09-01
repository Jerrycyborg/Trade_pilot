"""Fail-closed authentication dependencies shared by internal services."""

from __future__ import annotations

import hmac
import os

from fastapi import Header, HTTPException


def _insecure_development_bypass_enabled() -> bool:
    """Allow an explicit paper-only bypass for local tests and development."""
    app_env = os.environ.get("APP_ENV", "").strip().lower()
    broker = os.environ.get("BROKER", "paper").strip().lower()
    allowed = os.environ.get("ALLOW_INSECURE_DEV_AUTH", "false").strip().lower()
    return app_env in {"development", "test"} and broker == "paper" and allowed == "true"


def _required_secret(name: str) -> str | None:
    value = os.environ.get(name, "")
    if value:
        return value
    if _insecure_development_bypass_enabled():
        return None
    raise HTTPException(
        status_code=503,
        detail=f"{name} is not configured; authentication fails closed",
    )


def _matches(supplied: str | None, expected: str) -> bool:
    return supplied is not None and hmac.compare_digest(supplied, expected)


def verify_internal_key(x_internal_key: str | None = Header(default=None)) -> None:
    """Require the configured internal API key using constant-time comparison."""
    expected = _required_secret("INTERNAL_API_KEY")
    if expected is None:
        return
    if not _matches(x_internal_key, expected):
        raise HTTPException(status_code=401, detail="Invalid internal API key")


def verify_admin_key(
    x_internal_key: str | None = Header(default=None),
    x_admin_key: str | None = Header(default=None),
) -> None:
    """Require distinct internal and admin credentials for privileged actions."""
    verify_internal_key(x_internal_key)
    expected_internal = _required_secret("INTERNAL_API_KEY")
    expected_admin = _required_secret("ADMIN_API_KEY")
    if expected_internal is None or expected_admin is None:
        return
    if hmac.compare_digest(expected_internal, expected_admin):
        raise HTTPException(
            status_code=503,
            detail="ADMIN_API_KEY must be distinct from INTERNAL_API_KEY",
        )
    if not _matches(x_admin_key, expected_admin):
        raise HTTPException(status_code=401, detail="Invalid admin API key")
