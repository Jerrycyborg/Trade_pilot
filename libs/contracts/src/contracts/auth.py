"""Shared auth dependencies for all internal services."""
from __future__ import annotations

import os

from fastapi import Header, HTTPException


def verify_internal_key(x_internal_key: str | None = Header(default=None)) -> None:
    """Reject requests with wrong internal API key."""
    expected = os.environ.get("INTERNAL_API_KEY", "")
    if not expected:
        return  # not configured — skip check (dev mode)
    if x_internal_key != expected:
        raise HTTPException(status_code=401, detail="Invalid internal API key")


def verify_admin_key(
    x_internal_key: str | None = Header(default=None),
    x_admin_key: str | None = Header(default=None),
) -> None:
    """Require both internal + admin key. Used for kill switch and live-mode."""
    verify_internal_key(x_internal_key)
    expected_admin = os.environ.get("ADMIN_API_KEY", "")
    if not expected_admin:
        return  # not configured — skip check (dev mode)
    if x_admin_key != expected_admin:
        raise HTTPException(status_code=401, detail="Invalid admin API key")
