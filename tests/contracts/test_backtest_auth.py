from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    root = Path(__file__).resolve().parents[2]
    service_src = root / "services" / "backtest-service" / "src"
    sys.path.insert(0, str(service_src))
    try:
        module = importlib.import_module("backtest_service.main")
    finally:
        sys.path.remove(str(service_src))
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
