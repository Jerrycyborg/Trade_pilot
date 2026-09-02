"""Input sanitisation helpers."""

from __future__ import annotations

import re

from fastapi import HTTPException


def sanitize_symbol(s: str) -> str:
    """Uppercase, strip whitespace, validate symbol format."""
    cleaned = s.strip().upper()
    if len(cleaned) > 20 or not re.fullmatch(
        r"[A-Z0-9]+(?:[.-][A-Z0-9]+)*(?:/[A-Z0-9]+(?:[.-][A-Z0-9]+)*)?",
        cleaned,
    ):
        raise HTTPException(status_code=422, detail=f"Invalid symbol format: {s!r}")
    return cleaned


def validate_positive_amount(value: float, field_name: str = "amount") -> float:
    """Validate monetary amount is positive with max 2 decimal places."""
    if value <= 0:
        raise HTTPException(status_code=422, detail=f"{field_name} must be positive")
    rounded = round(value, 2)
    return rounded
