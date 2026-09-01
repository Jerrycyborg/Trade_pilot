from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    root = Path(__file__).resolve().parents[2]
    module_path = (
        root
        / "services"
        / "backtest-service"
        / "src"
        / "backtest_service"
        / "main.py"
    )
    spec = importlib.util.spec_from_file_location("backtest_auth_main", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load backtest service")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return TestClient(module.app)


def test_backtest_rejects_missing_internal_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(monkeypatch)
    response = client.post("/backtest", json={})
    assert response.status_code == 401


def test_backtest_rejects_invalid_internal_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(monkeypatch)
    response = client.post(
        "/backtest",
        json={},
        headers={"X-Internal-Key": "wrong-key"},
    )
    assert response.status_code == 401
