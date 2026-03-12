from pathlib import Path

from fastapi.testclient import TestClient


def _client(tmp_path: Path) -> TestClient:
    db_file = tmp_path / "execution.db"
    import execution_service.config as config
    import execution_service.database as database
    import execution_service.main as main

    config.settings = config.ExecutionSettings(database_url=f"sqlite+pysqlite:///{db_file}")
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
        "side": "BUY",
        "qty": 10,
        "order_type": "MARKET",
        "time_in_force": "DAY",
    }
    payload.update(overrides)
    return payload


def test_duplicate_submission_same_payload(tmp_path: Path) -> None:
    client = _client(tmp_path)
    headers = {"Idempotency-Key": "idem-1"}
    first = client.post("/v1/orders", json=_request(), headers=headers)
    second = client.post("/v1/orders", json=_request(), headers=headers)
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["order_id"] == second.json()["order_id"]


def test_duplicate_submission_different_payload(tmp_path: Path) -> None:
    client = _client(tmp_path)
    headers = {"Idempotency-Key": "idem-2"}
    first = client.post("/v1/orders", json=_request(), headers=headers)
    second = client.post("/v1/orders", json=_request(qty=11), headers=headers)
    assert first.status_code == 200
    assert second.status_code == 409


def test_valid_order_accepted(tmp_path: Path) -> None:
    client = _client(tmp_path)
    response = client.post("/v1/orders", json=_request(), headers={"Idempotency-Key": "idem-3"})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ACCEPTED"
    lookup = client.get(f"/v1/orders/{body['order_id']}")
    assert lookup.status_code == 200
    assert lookup.json()["order_id"] == body["order_id"]


def test_duplicate_submission_same_payload_does_not_create_extra_events(tmp_path: Path) -> None:
    client = _client(tmp_path)
    headers = {"Idempotency-Key": "idem-5"}
    first = client.post("/v1/orders", json=_request(), headers=headers)
    second = client.post("/v1/orders", json=_request(), headers=headers)
    assert first.status_code == 200
    assert second.status_code == 200

    import execution_service.database as database
    import execution_service.models as models
    from sqlalchemy import func, select

    with database.SessionLocal() as session:
        order_count = session.scalar(select(func.count()).select_from(models.OrderRecord))
        event_count = session.scalar(select(func.count()).select_from(models.ExecutionEventRecord))
        stored_order = session.scalar(
            select(models.OrderRecord).where(models.OrderRecord.order_id == first.json()["order_id"])
        )

    assert order_count == 1
    # ACCEPTED order creates 3 events: order.submitted, order.accepted, fill.recorded
    assert event_count == 3
    assert stored_order is not None
    assert stored_order.external_order_id


def test_rejected_order_path(tmp_path: Path) -> None:
    client = _client(tmp_path)
    response = client.post(
        "/v1/orders",
        json=_request(symbol="REJECT"),
        headers={"Idempotency-Key": "idem-4"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "REJECTED"
    assert body["rejection_reason"] == "symbol_rejected"


def test_list_orders_returns_newest_first_and_filters(tmp_path: Path) -> None:
    client = _client(tmp_path)
    accepted = client.post(
        "/v1/orders", json=_request(signal_id="sig-1", symbol="AAPL"), headers={"Idempotency-Key": "idem-6"}
    )
    rejected = client.post(
        "/v1/orders",
        json=_request(signal_id="sig-2", symbol="REJECT"),
        headers={"Idempotency-Key": "idem-7"},
    )

    assert accepted.status_code == 200
    assert rejected.status_code == 200

    listed = client.get("/v1/orders", params={"limit": 2})
    assert listed.status_code == 200
    body = listed.json()
    assert len(body) == 2
    assert body[0]["order_id"] == rejected.json()["order_id"]
    assert body[1]["order_id"] == accepted.json()["order_id"]

    filtered = client.get("/v1/orders", params={"status": "rejected", "symbol": "reject"})
    assert filtered.status_code == 200
    filtered_body = filtered.json()
    assert len(filtered_body) == 1
    assert filtered_body[0]["order_id"] == rejected.json()["order_id"]
    assert filtered_body[0]["rejection_reason"] == "symbol_rejected"
