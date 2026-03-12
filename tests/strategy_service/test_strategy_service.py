from pathlib import Path

from fastapi.testclient import TestClient


def _client(tmp_path: Path) -> TestClient:
    db_file = tmp_path / "strategy.db"
    import strategy_service.config as config
    import strategy_service.database as database
    import strategy_service.main as main

    config.settings = config.StrategySettings(database_url=f"sqlite+pysqlite:///{db_file}")
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


def test_generate_signal_returns_valid_candidate(tmp_path: Path) -> None:
    client = _client(tmp_path)
    response = client.post("/v1/signals/generate", json={"symbol": "AAPL"})
    assert response.status_code == 200
    body = response.json()
    assert body["symbol"] == "AAPL"
    assert body["candidate_action"] in {"BUY", "SELL"}
    assert 0.0 <= body["confidence"] <= 1.0

def test_generate_signal_uses_unique_signal_ids(tmp_path: Path) -> None:
    client = _client(tmp_path)
    first = client.post("/v1/signals/generate", json={"symbol": "AAPL"})
    second = client.post("/v1/signals/generate", json={"symbol": "AAPL"})
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["signal_id"] != second.json()["signal_id"]
    assert first.json()["candidate_action"] == second.json()["candidate_action"]
    assert first.json()["confidence"] == second.json()["confidence"]


def test_list_signals_returns_newest_first_and_filters_symbol(tmp_path: Path) -> None:
    client = _client(tmp_path)
    first = client.post("/v1/signals/generate", json={"symbol": "AAPL"})
    second = client.post("/v1/signals/generate", json={"symbol": "MSFT"})
    third = client.post("/v1/signals/generate", json={"symbol": "AAPL"})

    assert first.status_code == 200
    assert second.status_code == 200
    assert third.status_code == 200

    listed = client.get("/v1/signals", params={"limit": 2})
    assert listed.status_code == 200
    body = listed.json()
    assert len(body) == 2
    assert body[0]["signal_id"] == third.json()["signal_id"]
    assert body[1]["signal_id"] == second.json()["signal_id"]

    filtered = client.get("/v1/signals", params={"symbol": "aapl"})
    assert filtered.status_code == 200
    filtered_body = filtered.json()
    assert len(filtered_body) == 2
    assert [row["signal_id"] for row in filtered_body] == [
        third.json()["signal_id"],
        first.json()["signal_id"],
    ]
