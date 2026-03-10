from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker


def _build_clients(
    tmp_path: Path,
) -> tuple[
    TestClient, TestClient, TestClient, TestClient, object, object, object, object, object
]:
    strategy_client = _build_strategy_client()
    policy_client, policy_models, policy_database = _build_policy_client(tmp_path / "policy.db")
    execution_client, execution_models, execution_database = _build_execution_client(
        tmp_path / "execution.db"
    )
    portfolio_client, portfolio_models, portfolio_database = _build_portfolio_client(
        tmp_path / "portfolio.db", tmp_path / "execution.db"
    )
    return (
        strategy_client,
        policy_client,
        execution_client,
        portfolio_client,
        policy_models,
        execution_models,
        portfolio_models,
        policy_database,
        portfolio_database,
    )


def _build_strategy_client() -> TestClient:
    from strategy_service.main import app

    return TestClient(app)


def _build_policy_client(db_path: Path) -> tuple[TestClient, object, object]:
    import policy_service.config as config
    import policy_service.database as database
    import policy_service.main as main
    import policy_service.models as models

    config.settings = config.PolicySettings(database_url=f"sqlite+pysqlite:///{db_path}")
    database.settings = config.settings
    database.connect_args = {"check_same_thread": False}
    database.engine = create_engine(
        config.settings.database_url, future=True, connect_args=database.connect_args
    )
    database.SessionLocal = sessionmaker(
        bind=database.engine, autoflush=False, autocommit=False, future=True
    )
    database.Base.metadata.create_all(bind=database.engine)
    main.engine = database.engine
    main.SessionLocal = database.SessionLocal
    return TestClient(main.app), models, database


def _build_execution_client(db_path: Path) -> tuple[TestClient, object, object]:
    import execution_service.config as config
    import execution_service.database as database
    import execution_service.main as main
    import execution_service.models as models

    config.settings = config.ExecutionSettings(database_url=f"sqlite+pysqlite:///{db_path}")
    database.settings = config.settings
    database.connect_args = {"check_same_thread": False}
    database.engine = create_engine(
        config.settings.database_url, future=True, connect_args=database.connect_args
    )
    database.SessionLocal = sessionmaker(
        bind=database.engine, autoflush=False, autocommit=False, future=True
    )
    database.Base.metadata.create_all(bind=database.engine)
    main.engine = database.engine
    main.SessionLocal = database.SessionLocal
    return TestClient(main.app), models, database


def _build_portfolio_client(
    portfolio_db_path: Path, execution_db_path: Path
) -> tuple[TestClient, object, object]:
    import portfolio_service.config as config
    import portfolio_service.database as portfolio_database
    import portfolio_service.execution_reader as execution_reader
    import portfolio_service.main as main
    import portfolio_service.models as models

    config.settings = config.PortfolioSettings(
        database_url=f"sqlite+pysqlite:///{portfolio_db_path}",
        execution_database_url=f"sqlite+pysqlite:///{execution_db_path}",
    )
    portfolio_database.engine = create_engine(
        config.settings.database_url, future=True, connect_args={"check_same_thread": False}
    )
    portfolio_database.SessionLocal = sessionmaker(
        bind=portfolio_database.engine, autoflush=False, autocommit=False, future=True
    )
    portfolio_database.Base.metadata.create_all(bind=portfolio_database.engine)

    execution_reader.execution_engine = create_engine(
        config.settings.execution_database_url,
        future=True,
        connect_args={"check_same_thread": False},
    )
    execution_reader.ExecutionSessionLocal = sessionmaker(
        bind=execution_reader.execution_engine, autoflush=False, autocommit=False, future=True
    )

    main.engine = portfolio_database.engine
    main.SessionLocal = portfolio_database.SessionLocal
    return TestClient(main.app), models, portfolio_database


def _policy_payload(signal: dict[str, object]) -> dict[str, object]:
    return {
        "signal_id": signal["signal_id"],
        "symbol": signal["symbol"],
        "candidate_action": signal["candidate_action"],
        "confidence": signal["confidence"],
        "size_pct": signal["size_pct"],
        "market_context": {
            "data_age_seconds": 5,
            "market_open": True,
            "event_blackout_active": False,
            "liquidity_score": 0.95,
            "symbol_allowed": True,
        },
        "portfolio_context": {
            "gross_exposure_pct": 0.1,
            "daily_drawdown_pct": 0.01,
        },
    }


def _execution_payload(signal: dict[str, object]) -> dict[str, object]:
    return {
        "signal_id": signal["signal_id"],
        "symbol": signal["symbol"],
        "side": signal["candidate_action"],
        "qty": 10,
        "order_type": "MARKET",
        "time_in_force": "DAY",
    }


def test_milestone1_acceptance_flow(tmp_path: Path) -> None:
    (
        strategy_client,
        policy_client,
        execution_client,
        portfolio_client,
        policy_models,
        execution_models,
        portfolio_models,
        policy_database,
        portfolio_database,
    ) = _build_clients(tmp_path)

    signal_response = strategy_client.post("/v1/signals/generate", json={"symbol": "AAPL"})
    assert signal_response.status_code == 200
    signal = signal_response.json()
    assert signal["candidate_action"] == "BUY"

    policy_response = policy_client.post("/v1/policy/evaluate", json=_policy_payload(signal))
    assert policy_response.status_code == 200
    decision = policy_response.json()
    assert decision["decision"] == "APPROVE"

    order_response = execution_client.post(
        "/v1/orders",
        json=_execution_payload(signal),
        headers={"Idempotency-Key": "acceptance-flow"},
    )
    assert order_response.status_code == 200
    order = order_response.json()
    assert order["status"] == "ACCEPTED"

    fills_response = execution_client.get(f"/v1/orders/{order['order_id']}/fills")
    assert fills_response.status_code == 200
    fills = fills_response.json()
    assert len(fills) == 1
    assert fills[0]["qty"] == 10
    assert fills[0]["price"] == 100.0

    reconcile_response = portfolio_client.post(
        "/v1/portfolio/reconcile",
        json={"latest_quotes": {"AAPL": 101.0}},
    )
    assert reconcile_response.status_code == 200
    snapshot_response = portfolio_client.get("/v1/portfolio/snapshot")
    assert snapshot_response.status_code == 200
    snapshot = snapshot_response.json()
    assert snapshot["gross_exposure"] == 1010.0
    assert snapshot["realized_pnl"] == 0.0
    assert snapshot["unrealized_pnl"] == 10.0
    assert snapshot["positions"][0]["symbol"] == "AAPL"
    assert snapshot["positions"][0]["net_qty"] == 10

    with execution_models.SessionLocal() as session:
        stored_order = session.scalar(
            select(execution_models.OrderRecord).where(
                execution_models.OrderRecord.order_id == order["order_id"]
            )
        )
        stored_fills = session.scalars(select(execution_models.FillRecord)).all()
        stored_events = session.scalars(select(execution_models.ExecutionEventRecord)).all()

    with policy_database.SessionLocal() as session:
        policy_eval = session.scalar(
            select(policy_models.PolicyEvaluationRecord).where(
                policy_models.PolicyEvaluationRecord.signal_id == signal["signal_id"]
            )
        )

    with portfolio_database.SessionLocal() as session:
        positions = session.scalars(select(portfolio_models.PositionRecordModel)).all()
        snapshots = session.scalars(select(portfolio_models.PortfolioSnapshotRecord)).all()

    assert stored_order is not None
    assert stored_order.status == "ACCEPTED"
    assert len(stored_fills) == 1
    assert len(stored_events) == 3
    assert policy_eval is not None
    assert policy_eval.decision == "APPROVE"
    assert len(positions) == 1
    assert len(snapshots) == 1
