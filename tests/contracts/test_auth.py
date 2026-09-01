"""Authentication must fail closed before any trading service mutates state."""

from __future__ import annotations

import pytest
from contracts.auth import verify_admin_key, verify_internal_key
from fastapi import HTTPException


def _clear(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "INTERNAL_API_KEY",
        "ADMIN_API_KEY",
        "ALLOW_INSECURE_DEV_AUTH",
        "APP_ENV",
        "BROKER",
    ):
        monkeypatch.delenv(name, raising=False)


def test_missing_internal_key_configuration_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear(monkeypatch)
    with pytest.raises(HTTPException) as error:
        verify_internal_key(None)
    assert error.value.status_code == 503


def test_wrong_internal_key_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("INTERNAL_API_KEY", "configured-internal")
    with pytest.raises(HTTPException) as error:
        verify_internal_key("wrong")
    assert error.value.status_code == 401


def test_development_bypass_is_explicit_and_paper_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("ALLOW_INSECURE_DEV_AUTH", "true")
    monkeypatch.setenv("BROKER", "paper")
    verify_internal_key(None)

    monkeypatch.setenv("BROKER", "alpaca")
    with pytest.raises(HTTPException) as error:
        verify_internal_key(None)
    assert error.value.status_code == 503


def test_admin_key_must_be_distinct(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("INTERNAL_API_KEY", "same-secret")
    monkeypatch.setenv("ADMIN_API_KEY", "same-secret")
    with pytest.raises(HTTPException) as error:
        verify_admin_key("same-secret", "same-secret")
    assert error.value.status_code == 503


def test_both_distinct_admin_credentials_are_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("INTERNAL_API_KEY", "internal-secret")
    monkeypatch.setenv("ADMIN_API_KEY", "admin-secret")
    verify_admin_key("internal-secret", "admin-secret")
    with pytest.raises(HTTPException) as error:
        verify_admin_key("internal-secret", "wrong")
    assert error.value.status_code == 401
