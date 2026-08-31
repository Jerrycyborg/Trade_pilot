"""A blocked or shadowed order must be recorded, not merely refused.

This path had no coverage. Every execution-service test used a simulated-only
router, so orders were always *placed* and `_record_unplaced` never touched a
database. CI found it on PostgreSQL: the insert passed None for a NOT NULL,
UNIQUE column and raised NotNullViolation, meaning a correctly-blocked order
returned a 500 instead of being journalled.

The tests here run against the real order table, and two of them insert more
than one unplaced order, because a single one would not have caught the UNIQUE
half of the problem either.
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest
from brokers import PaperBroker
from contracts import OrderStatus
from execution_service.routing import BrokerRouter

POSTGRES_URL = os.getenv("TEST_LIFECYCLE_POSTGRES_URL", "")


class BlockingStore:
    """A lifecycle authority that admits nothing — an empty roster."""

    def get(self, strategy_id, symbol, account_id=None):
        return None

    def live_mode_enabled(self, account_id=None):
        return False

    def reconciliation_state(self, broker, environment, account_id=None):
        class _Halt:
            halted = False
            halt_reason = ""

        return _Halt()


def _client(db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import execution_service.config as config
    import execution_service.database as database
    import execution_service.main as main
    from fastapi.testclient import TestClient

    config.settings = config.ExecutionSettings(database_url=db_url)
    database.settings = config.settings
    database.connect_args = {"check_same_thread": False} if db_url.startswith("sqlite") else {}
    database.engine = database.create_engine(
        config.settings.database_url, future=True, connect_args=database.connect_args
    )
    database.SessionLocal.configure(bind=database.engine)
    database.Base.metadata.drop_all(bind=database.engine)
    database.Base.metadata.create_all(bind=database.engine)
    monkeypatch.setattr(main, "engine", database.engine)
    monkeypatch.setattr(main, "SessionLocal", database.SessionLocal)
    monkeypatch.setattr(
        main,
        "router",
        BrokerRouter(
            store=BlockingStore(),
            simulated=PaperBroker(state_path=tmp_path / "paper.json"),
        ),
    )
    return TestClient(main.app)


def _order(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "signal_id": f"sig-{uuid4()}",
        "symbol": "AAPL",
        "side": "BUY",
        "qty": 1,
        "order_type": "MARKET",
        "strategy_id": "ema_rsi_macd",
    }
    payload.update(overrides)
    return payload


def _headers() -> dict[str, str]:
    return {
        "Idempotency-Key": f"k-{uuid4()}",
        "X-Internal-Key": os.environ.get("INTERNAL_API_KEY", ""),
    }


DB_PARAMS = [
    pytest.param("sqlite", id="sqlite"),
    pytest.param(
        "postgres",
        id="postgres",
        marks=pytest.mark.skipif(
            not POSTGRES_URL, reason="set TEST_LIFECYCLE_POSTGRES_URL"
        ),
    ),
]


@pytest.fixture(params=DB_PARAMS)
def client(request, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Both backends. SQLite alone is what let the NOT NULL bug through."""
    url = (
        f"sqlite+pysqlite:///{tmp_path}/exec.db"
        if request.param == "sqlite"
        else POSTGRES_URL
    )
    return _client(url, tmp_path, monkeypatch)


def test_a_blocked_order_is_persisted_rather_than_erroring(client) -> None:
    response = client.post("/v1/orders", json=_order(), headers=_headers())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == OrderStatus.REJECTED.value
    assert "sleeve_not_registered" in body["rejection_reason"]


def test_several_blocked_orders_can_coexist(client) -> None:
    """external_order_id is UNIQUE. A shared placeholder would let exactly one
    blocked order be stored and collide on the second."""
    for _ in range(3):
        response = client.post("/v1/orders", json=_order(), headers=_headers())
        assert response.status_code == 200, response.text
    assert len(client.get("/v1/orders").json()) == 3


def test_the_blocked_order_carries_no_broker_id(client) -> None:
    """Nothing was sent, so nothing may look like a broker's acknowledgement."""
    response = client.post("/v1/orders", json=_order(), headers=_headers())
    order_id = response.json()["order_id"]
    stored = [o for o in client.get("/v1/orders").json() if o["order_id"] == order_id]
    assert stored, "the blocked order was not stored at all"


def test_no_fill_is_recorded_for_a_blocked_order(client) -> None:
    client.post("/v1/orders", json=_order(), headers=_headers())
    assert client.get("/v1/fills").json() == []
