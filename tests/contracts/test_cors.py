from __future__ import annotations

import pytest
from contracts.cors import cors_origins


def test_cors_defaults_to_no_cross_origin_access(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CORS_ALLOWED_ORIGINS", raising=False)
    assert cors_origins() == []


def test_cors_rejects_wildcard_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("BROKER", "paper")
    with pytest.raises(RuntimeError, match="paper development/test"):
        cors_origins()


def test_cors_allows_opt_in_wildcard_for_paper_tests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("BROKER", "paper")
    assert cors_origins() == ["*"]


@pytest.mark.parametrize(
    "origin",
    [
        "https://user:password@example.com",
        "https://example.com/path",
        "javascript:alert(1)",
        "https://example.com?redirect=evil",
    ],
)
def test_cors_rejects_invalid_origins(
    monkeypatch: pytest.MonkeyPatch,
    origin: str,
) -> None:
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", origin)
    with pytest.raises(RuntimeError, match="Invalid CORS origin"):
        cors_origins()


def test_cors_accepts_explicit_origins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "CORS_ALLOWED_ORIGINS",
        "https://trade.example.com, http://127.0.0.1:8080/",
    )
    assert cors_origins() == [
        "https://trade.example.com",
        "http://127.0.0.1:8080",
    ]
