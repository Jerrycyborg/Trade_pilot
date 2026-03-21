from pathlib import Path

from fastapi.testclient import TestClient


def _client(tmp_path: Path) -> TestClient:
    db_file = tmp_path / "audit.db"
    import audit_logger.config as config
    import audit_logger.database as database
    import audit_logger.main as main

    config.settings = config.AuditLoggerSettings(database_url=f"sqlite+pysqlite:///{db_file}")
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


def test_audit_logger_round_trip(tmp_path: Path) -> None:
    client = _client(tmp_path)
    created = client.post(
        "/v1/audit/log",
        json={
            "event_type": "trade.executed",
            "symbol": "AAPL",
            "signal_id": "sig-1",
            "decision": "APPROVE",
            "reasoning": "smoke",
            "metadata": {"amount_usd": 100.0},
        },
    )
    assert created.status_code == 200
    event_id = created.json()["event_id"]

    listed = client.get("/v1/audit/logs", params={"symbol": "AAPL", "event_type": "trade.executed"})
    assert listed.status_code == 200
    assert listed.json()[0]["event_id"] == event_id

    fetched = client.get(f"/v1/audit/logs/{event_id}")
    assert fetched.status_code == 200
    assert fetched.json()["metadata"]["amount_usd"] == 100.0
