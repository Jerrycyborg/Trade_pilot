from fastapi.testclient import TestClient

from strategy_service.main import app


def test_generate_signal_returns_valid_candidate() -> None:
    client = TestClient(app)
    response = client.post("/v1/signals/generate", json={"symbol": "AAPL"})
    assert response.status_code == 200
    body = response.json()
    assert body["symbol"] == "AAPL"
    assert body["candidate_action"] in {"BUY", "SELL"}
    assert 0.0 <= body["confidence"] <= 1.0


def test_generate_signal_uses_unique_signal_ids() -> None:
    client = TestClient(app)
    first = client.post("/v1/signals/generate", json={"symbol": "AAPL"})
    second = client.post("/v1/signals/generate", json={"symbol": "AAPL"})
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["signal_id"] != second.json()["signal_id"]
    assert first.json()["candidate_action"] == second.json()["candidate_action"]
    assert first.json()["confidence"] == second.json()["confidence"]
