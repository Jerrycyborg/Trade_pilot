from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
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
        "signal_id": "sig-boundary-1",
        "symbol": "AAPL",
        "side": "BUY",
        "qty": 10,
        "order_type": "MARKET",
        "time_in_force": "DAY",
    }
    payload.update(overrides)
    return payload


def test_fills_persisted_correctly_live_postgres() -> None:
    client, models, database = _client()
    created = client.post(
        "/v1/orders",
        json=_request(),
        headers={"Idempotency-Key": "portfolio-fill"},
    )
    assert created.status_code == 200
    order = created.json()

    fills_response = client.get(f"/v1/orders/{order['order_id']}/fills")
    assert fills_response.status_code == 200
    fills = fills_response.json()
    assert len(fills) == 1
    assert fills[0]["order_id"] == order["order_id"]
    assert fills[0]["signal_id"] == "sig-boundary-1"
    assert fills[0]["qty"] == 10
    assert fills[0]["price"] == 100.0

    all_fills = client.get("/v1/fills")
    assert all_fills.status_code == 200
    assert len(all_fills.json()) == 1

    with database.SessionLocal() as session:
        stored_fill = session.scalar(
            select(models.FillRecord).where(models.FillRecord.order_id == order["order_id"])
        )

    assert stored_fill is not None
    assert stored_fill.external_order_id
    assert stored_fill.symbol == "AAPL"


def test_execution_events_persisted_live_postgres() -> None:
    client, models, database = _client()
    created = client.post(
        "/v1/orders",
        json=_request(signal_id="sig-boundary-events"),
        headers={"Idempotency-Key": "portfolio-events"},
    )
    assert created.status_code == 200
    order = created.json()

    events_response = client.get("/v1/execution/events")
    assert events_response.status_code == 200
    events = [event for event in events_response.json() if event["order_id"] == order["order_id"]]
    assert len(events) == 3
    assert {event["event_type"] for event in events} == {
        "order.submitted",
        "order.accepted",
        "fill.recorded",
    }

    with database.SessionLocal() as session:
        stored_events = session.scalars(
            select(models.ExecutionEventRecord).where(
                models.ExecutionEventRecord.order_id == order["order_id"]
            )
        ).all()

    assert len(stored_events) == 3


def test_portfolio_facing_boundary_behavior_live_postgres() -> None:
    client, models, database = _client()
    created = client.post(
        "/v1/orders",
        json=_request(symbol="REJECT", signal_id="sig-boundary-reject"),
        headers={"Idempotency-Key": "portfolio-reject"},
    )
    assert created.status_code == 200
    order = created.json()
    assert order["status"] == "REJECTED"

    fills_response = client.get(f"/v1/orders/{order['order_id']}/fills")
    assert fills_response.status_code == 200
    assert fills_response.json() == []

    all_fills = client.get("/v1/fills")
    assert all_fills.status_code == 200
    assert all_fills.json() == []

    events_response = client.get("/v1/execution/events")
    assert events_response.status_code == 200
    events = [event for event in events_response.json() if event["order_id"] == order["order_id"]]
    assert len(events) == 2
    assert {event["event_type"] for event in events} == {"order.submitted", "order.rejected"}

    with database.SessionLocal() as session:
        stored_fills = session.scalars(select(models.FillRecord)).all()
        stored_events = session.scalars(
            select(models.ExecutionEventRecord).where(
                models.ExecutionEventRecord.order_id == order["order_id"]
            )
        ).all()

    assert stored_fills == []
    assert len(stored_events) == 2
