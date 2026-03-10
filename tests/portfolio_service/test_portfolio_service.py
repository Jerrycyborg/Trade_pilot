from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker


def _client(tmp_path: Path) -> tuple[TestClient, object, object, object]:
    portfolio_db = tmp_path / "portfolio.db"
    execution_db = tmp_path / "execution.db"

    import execution_service.database as execution_database
    import execution_service.models as execution_models
    import portfolio_service.config as config
    import portfolio_service.database as portfolio_database
    import portfolio_service.execution_reader as execution_reader
    import portfolio_service.main as main
    import portfolio_service.models as portfolio_models

    config.settings = config.PortfolioSettings(
        database_url=f"sqlite+pysqlite:///{portfolio_db}",
        execution_database_url=f"sqlite+pysqlite:///{execution_db}",
    )

    portfolio_database.engine = create_engine(
        config.settings.database_url, future=True, connect_args={"check_same_thread": False}
    )
    portfolio_database.SessionLocal = sessionmaker(
        bind=portfolio_database.engine, autoflush=False, autocommit=False, future=True
    )
    portfolio_database.Base.metadata.create_all(bind=portfolio_database.engine)

    execution_database.engine = create_engine(
        config.settings.execution_database_url, future=True, connect_args={"check_same_thread": False}
    )
    execution_database.SessionLocal = sessionmaker(
        bind=execution_database.engine, autoflush=False, autocommit=False, future=True
    )
    execution_database.Base.metadata.create_all(bind=execution_database.engine)

    execution_reader.execution_engine = create_engine(
        config.settings.execution_database_url, future=True, connect_args={"check_same_thread": False}
    )
    execution_reader.ExecutionSessionLocal = sessionmaker(
        bind=execution_reader.execution_engine, autoflush=False, autocommit=False, future=True
    )

    main.engine = portfolio_database.engine
    main.SessionLocal = portfolio_database.SessionLocal
    return TestClient(main.app), execution_models, execution_database, portfolio_models


def _insert_fill(execution_database, execution_models, **overrides: object) -> None:
    base = {
        "fill_id": f"fill-{overrides.get('fill_id', '1')}",
        "order_id": f"order-{overrides.get('fill_id', '1')}",
        "external_order_id": f"broker-{overrides.get('fill_id', '1')}",
        "signal_id": f"sig-{overrides.get('fill_id', '1')}",
        "symbol": "AAPL",
        "side": "BUY",
        "qty": 10,
        "price": 100.0,
        "filled_at": datetime.now(timezone.utc),
    }
    base.update(overrides)
    with execution_database.SessionLocal() as session:
        session.add(execution_models.FillRecord(**base))
        session.commit()


def _insert_order(execution_database, execution_models, **overrides: object) -> None:
    base = {
        "order_id": "rejected-order",
        "signal_id": "sig-rejected",
        "symbol": "REJECT",
        "side": "BUY",
        "qty": 10,
        "order_type": "MARKET",
        "time_in_force": "DAY",
        "status": "REJECTED",
        "external_order_id": "broker-rejected",
        "idempotency_key": "idem-rejected",
        "payload_hash": "hash-rejected",
        "rejection_reason": "symbol_rejected",
        "created_at": datetime.now(timezone.utc),
    }
    base.update(overrides)
    with execution_database.SessionLocal() as session:
        session.add(execution_models.OrderRecord(**base))
        session.commit()


def test_single_buy_fill_creates_position(tmp_path: Path) -> None:
    client, execution_models, execution_database, _ = _client(tmp_path)
    _insert_fill(execution_database, execution_models, fill_id="1")
    reconcile = client.post("/v1/portfolio/reconcile", json={"latest_quotes": {"AAPL": 101.0}})
    assert reconcile.status_code == 200
    positions = client.get("/v1/portfolio/positions")
    body = positions.json()
    assert len(body) == 1
    assert body[0]["symbol"] == "AAPL"
    assert body[0]["net_qty"] == 10
    assert body[0]["average_cost"] == 100.0


def test_multiple_fills_update_average_cost(tmp_path: Path) -> None:
    client, execution_models, execution_database, _ = _client(tmp_path)
    _insert_fill(execution_database, execution_models, fill_id="1", qty=10, price=100.0)
    _insert_fill(execution_database, execution_models, fill_id="2", qty=20, price=110.0)
    reconcile = client.post("/v1/portfolio/reconcile", json={"latest_quotes": {"AAPL": 110.0}})
    assert reconcile.status_code == 200
    position = client.get("/v1/portfolio/positions").json()[0]
    assert position["net_qty"] == 30
    assert round(position["average_cost"], 6) == round((10 * 100.0 + 20 * 110.0) / 30, 6)


def test_rejected_orders_do_not_affect_positions(tmp_path: Path) -> None:
    client, execution_models, execution_database, _ = _client(tmp_path)
    _insert_order(execution_database, execution_models)
    reconcile = client.post("/v1/portfolio/reconcile", json={})
    assert reconcile.status_code == 200
    assert client.get("/v1/portfolio/positions").json() == []


def test_partial_fills_update_position_incrementally(tmp_path: Path) -> None:
    client, execution_models, execution_database, _ = _client(tmp_path)
    now = datetime.now(timezone.utc)
    _insert_fill(execution_database, execution_models, fill_id="1", qty=4, price=100.0, filled_at=now)
    _insert_fill(
        execution_database,
        execution_models,
        fill_id="2",
        qty=6,
        price=102.0,
        filled_at=now + timedelta(seconds=1),
    )
    reconcile = client.post("/v1/portfolio/reconcile", json={"latest_quotes": {"AAPL": 103.0}})
    assert reconcile.status_code == 200
    position = client.get("/v1/portfolio/positions").json()[0]
    assert position["net_qty"] == 10
    assert round(position["average_cost"], 6) == round((4 * 100.0 + 6 * 102.0) / 10, 6)


def test_reconcile_is_idempotent(tmp_path: Path) -> None:
    client, execution_models, execution_database, portfolio_models = _client(tmp_path)
    _insert_fill(execution_database, execution_models, fill_id="1")
    first = client.post("/v1/portfolio/reconcile", json={"latest_quotes": {"AAPL": 100.0}})
    second = client.post("/v1/portfolio/reconcile", json={"latest_quotes": {"AAPL": 100.0}})
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["idempotent"] is True
    assert first.json()["reconcile_key"] == second.json()["reconcile_key"]

    import portfolio_service.database as portfolio_database

    with portfolio_database.SessionLocal() as session:
        snapshots = session.scalars(select(portfolio_models.PortfolioSnapshotRecord)).all()
        pnl_history = session.scalars(select(portfolio_models.PnLHistoryRecord)).all()

    assert len(snapshots) == 1
    assert len(pnl_history) == 1


def test_snapshot_generation_works(tmp_path: Path) -> None:
    client, execution_models, execution_database, _ = _client(tmp_path)
    _insert_fill(execution_database, execution_models, fill_id="1", qty=10, price=100.0)
    _insert_fill(execution_database, execution_models, fill_id="2", side="SELL", qty=4, price=105.0)
    reconcile = client.post("/v1/portfolio/reconcile", json={"latest_quotes": {"AAPL": 106.0}})
    assert reconcile.status_code == 200
    snapshot = client.get("/v1/portfolio/snapshot")
    assert snapshot.status_code == 200
    body = snapshot.json()
    assert body["gross_exposure"] == 636.0
    assert round(body["realized_pnl"], 6) == 20.0
    assert round(body["unrealized_pnl"], 6) == 36.0


def test_sell_after_multiple_buys_updates_realized_and_remaining_cost(tmp_path: Path) -> None:
    client, execution_models, execution_database, _ = _client(tmp_path)
    _insert_fill(execution_database, execution_models, fill_id="1", qty=10, price=100.0)
    _insert_fill(execution_database, execution_models, fill_id="2", qty=10, price=110.0)
    _insert_fill(execution_database, execution_models, fill_id="3", side="SELL", qty=5, price=120.0)
    reconcile = client.post("/v1/portfolio/reconcile", json={"latest_quotes": {"AAPL": 120.0}})
    assert reconcile.status_code == 200
    position = client.get("/v1/portfolio/positions").json()[0]
    assert position["net_qty"] == 15
    assert round(position["average_cost"], 6) == 105.0
    assert round(position["realized_pnl"], 6) == 75.0


def test_quote_fallback_to_last_fill_price(tmp_path: Path) -> None:
    client, execution_models, execution_database, _ = _client(tmp_path)
    _insert_fill(execution_database, execution_models, fill_id="1", qty=10, price=99.5)
    reconcile = client.post("/v1/portfolio/reconcile", json={})
    assert reconcile.status_code == 200
    position = client.get("/v1/portfolio/positions").json()[0]
    assert position["market_price"] == 99.5
    assert position["unrealized_pnl"] == 0.0


def test_repeated_reconcile_idempotency_returns_same_snapshot(tmp_path: Path) -> None:
    client, execution_models, execution_database, _ = _client(tmp_path)
    _insert_fill(execution_database, execution_models, fill_id="1", qty=10, price=100.0)
    first = client.post("/v1/portfolio/reconcile", json={"latest_quotes": {"AAPL": 101.0}})
    second = client.post("/v1/portfolio/reconcile", json={"latest_quotes": {"AAPL": 101.0}})
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["idempotent"] is True
    assert first.json()["snapshot"] == second.json()["snapshot"]
