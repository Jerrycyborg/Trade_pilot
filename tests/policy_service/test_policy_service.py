from pathlib import Path

from fastapi.testclient import TestClient


def _client(tmp_path: Path) -> TestClient:
    import os
    os.environ.setdefault("POLICY_DISABLE_TRADING_HOURS", "true")
    db_file = tmp_path / "policy.db"
    import policy_service.config as config
    import policy_service.database as database
    import policy_service.main as main

    config.settings = config.PolicySettings(database_url=f"sqlite+pysqlite:///{db_file}")
    database.settings = config.settings
    database.connect_args = {"check_same_thread": False}
    database.engine = database.create_engine(
        config.settings.database_url, future=True, connect_args=database.connect_args
    )
    database.SessionLocal.configure(bind=database.engine)
    database.Base.metadata.create_all(bind=database.engine)
    main.engine = database.engine
    main.SessionLocal = database.SessionLocal
    return TestClient(main.app)


def _request(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "signal_id": "sig-1",
        "symbol": "AAPL",
        "candidate_action": "BUY",
        "confidence": 0.8,
        "size_pct": 0.01,
        "market_context": {
            "data_age_seconds": 5,
            "market_open": True,
            "event_blackout_active": False,
            "liquidity_score": 0.9,
            "symbol_allowed": True,
        },
        "portfolio_context": {
            "gross_exposure_pct": 0.2,
            "daily_drawdown_pct": 0.01,
        },
    }
    payload.update(overrides)
    return payload


def test_stale_data_rejection(tmp_path: Path) -> None:
    client = _client(tmp_path)
    response = client.post(
        "/v1/policy/evaluate",
        json=_request(market_context={**_request()["market_context"], "data_age_seconds": 45}),
    )
    assert response.status_code == 200
    assert response.json()["decision"] == "REJECT"
    assert "stale_data" in response.json()["reasons"]


def test_max_size_rejection(tmp_path: Path) -> None:
    client = _client(tmp_path)
    response = client.post("/v1/policy/evaluate", json=_request(size_pct=0.05))
    assert response.status_code == 200
    assert response.json()["decision"] == "REJECT"
    assert "max_size_exceeded" in response.json()["reasons"]


def test_confidence_review(tmp_path: Path) -> None:
    client = _client(tmp_path)
    response = client.post("/v1/policy/evaluate", json=_request(confidence=0.4))
    assert response.status_code == 200
    assert response.json()["decision"] == "REVIEW"
    assert "confidence_below_floor" in response.json()["reasons"]


def test_approve_path(tmp_path: Path) -> None:
    client = _client(tmp_path)
    response = client.post("/v1/policy/evaluate", json=_request())
    assert response.status_code == 200
    body = response.json()
    assert body["decision"] == "APPROVE"
    assert body["approved_size_pct"] == 0.01
    assert body["policy_version"] == "risk_policy_v1"


def test_list_evaluations_returns_newest_first_and_filters(tmp_path: Path) -> None:
    client = _client(tmp_path)
    approved = client.post("/v1/policy/evaluate", json=_request(signal_id="sig-1", symbol="AAPL"))
    review = client.post(
        "/v1/policy/evaluate",
        json=_request(signal_id="sig-2", symbol="MSFT", confidence=0.4),
    )
    rejected = client.post(
        "/v1/policy/evaluate",
        json=_request(signal_id="sig-3", symbol="AAPL", size_pct=0.05),
    )

    assert approved.status_code == 200
    assert review.status_code == 200
    assert rejected.status_code == 200

    listed = client.get("/v1/policy/evaluations", params={"limit": 2})
    assert listed.status_code == 200
    body = listed.json()
    assert len(body) == 2
    assert body[0]["signal_id"] == "sig-3"
    assert body[1]["signal_id"] == "sig-2"

    filtered = client.get("/v1/policy/evaluations", params={"symbol": "aapl", "decision": "reject"})
    assert filtered.status_code == 200
    filtered_body = filtered.json()
    assert len(filtered_body) == 1
    assert filtered_body[0]["signal_id"] == "sig-3"
    assert "max_size_exceeded" in filtered_body[0]["reasons"]
