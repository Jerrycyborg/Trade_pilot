from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker


POSTGRES_URL = os.getenv("TEST_EXECUTION_POSTGRES_URL")

pytestmark = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="set TEST_EXECUTION_POSTGRES_URL to run live Postgres execution integration tests",
)


def _client() -> tuple[TestClient, object, object]:
    import execution_service.config as config
    import execution_service.database as database
    import execution_service.main as main
    import execution_service.models as models

    assert POSTGRES_URL is not None
    config.settings = config.ExecutionSettings(database_url=POSTGRES_URL)
    database.settings = config.settings
    database.connect_args = {}
    database.engine = create_engine(config.settings.database_url, future=True)
    database.SessionLocal = sessionmaker(
        bind=database.engine, autoflush=False, autocommit=False, future=True
    )
    database.Base.metadata.drop_all(bind=database.engine)
    database.Base.metadata.create_all(bind=database.engine)
    main.engine = database.engine
    main.SessionLocal = database.SessionLocal
    return TestClient(main.app), models, database


def _request(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "signal_id": "sig-postgres-1",
        "symbol": "AAPL",
        "side": "BUY",
        "qty": 10,
        "order_type": "MARKET",
        "time_in_force": "DAY",
    }
    payload.update(overrides)
    return payload


def test_accepted_order_persistence_live_postgres() -> None:
    client, models, database = _client()
    response = client.post(
        "/v1/orders",
        json=_request(),
        headers={"Idempotency-Key": "pg-accept"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ACCEPTED"

    with database.SessionLocal() as session:
        stored = session.scalar(
            select(models.OrderRecord).where(models.OrderRecord.order_id == body["order_id"])
        )
        fills = session.scalar(select(func.count()).select_from(models.FillRecord))

    assert stored is not None
    assert stored.status == "ACCEPTED"
    assert stored.external_order_id
    assert fills == 0


def test_rejected_order_persistence_live_postgres() -> None:
    client, models, database = _client()
    response = client.post(
        "/v1/orders",
        json=_request(symbol="REJECT"),
        headers={"Idempotency-Key": "pg-reject"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "REJECTED"

    with database.SessionLocal() as session:
        stored = session.scalar(
            select(models.OrderRecord).where(models.OrderRecord.order_id == body["order_id"])
        )

    assert stored is not None
    assert stored.status == "REJECTED"
    assert stored.rejection_reason == "symbol_rejected"


def test_idempotent_duplicate_submission_live_postgres() -> None:
    client, models, database = _client()
    headers = {"Idempotency-Key": "pg-dup"}
    first = client.post("/v1/orders", json=_request(), headers=headers)
    second = client.post("/v1/orders", json=_request(), headers=headers)
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["order_id"] == second.json()["order_id"]

    with database.SessionLocal() as session:
        order_count = session.scalar(select(func.count()).select_from(models.OrderRecord))
        event_count = session.scalar(select(func.count()).select_from(models.ExecutionEventRecord))

    assert order_count == 1
    assert event_count == 2


def test_execution_event_persistence_live_postgres() -> None:
    client, models, database = _client()
    response = client.post(
        "/v1/orders",
        json=_request(signal_id="sig-postgres-events"),
        headers={"Idempotency-Key": "pg-events"},
    )
    assert response.status_code == 200
    body = response.json()

    with database.SessionLocal() as session:
        events = session.scalars(
            select(models.ExecutionEventRecord).where(
                models.ExecutionEventRecord.order_id == body["order_id"]
            )
        ).all()

    assert len(events) == 2
    submitted = next(event for event in events if event.event_type == "order.submitted")
    terminal = next(event for event in events if event.event_type == "order.accepted")
    submitted_payload = json.loads(submitted.payload_json)
    terminal_payload = json.loads(terminal.payload_json)

    assert submitted.signal_id == "sig-postgres-events"
    assert submitted.external_order_id
    assert submitted.order_status == "ACCEPTED"
    assert submitted_payload["event_type"] == "order.submitted"
    assert terminal_payload["event_type"] == "order.accepted"
    assert terminal_payload["order_status"] == "ACCEPTED"
