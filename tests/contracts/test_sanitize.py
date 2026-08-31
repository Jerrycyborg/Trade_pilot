"""Tests for input sanitisation helpers."""

import pytest
from contracts.sanitize import sanitize_symbol, validate_positive_amount
from fastapi import HTTPException


def test_sanitize_symbol_uppercase():
    assert sanitize_symbol("aapl") == "AAPL"


def test_sanitize_symbol_strips_whitespace():
    assert sanitize_symbol("  MSFT  ") == "MSFT"


def test_sanitize_symbol_allows_slash():
    assert sanitize_symbol("btc/usd") == "BTC/USD"


def test_sanitize_symbol_rejects_injection():
    with pytest.raises(HTTPException) as exc:
        sanitize_symbol("AAPL; DROP TABLE")
    assert exc.value.status_code == 422


def test_sanitize_symbol_rejects_too_long():
    with pytest.raises(HTTPException):
        sanitize_symbol("A" * 21)


def test_validate_positive_amount_ok():
    assert validate_positive_amount(100.555) == 100.56


def test_validate_positive_amount_rejects_negative():
    with pytest.raises(HTTPException) as exc:
        validate_positive_amount(-10.0)
    assert exc.value.status_code == 422


def test_validate_positive_amount_rejects_zero():
    with pytest.raises(HTTPException):
        validate_positive_amount(0.0)
