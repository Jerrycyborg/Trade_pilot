from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker


def _build_clients(
    tmp_path: Path,
) -> tuple[
    TestClient,
    TestClient,
    TestClient,
    TestClient,
    object,
    object,
    object,
    object,
    object,
    object,
]:
    strategy_client = _build_strategy_client(tmp_path / "strategy.db")
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
        execution_database,
        portfolio_database,
    )


def _build_strategy_client(db_path: Path) -> TestClient:
    import strategy_service.config as config
    import strategy_service.database as database
    import strategy_service.main as main

    config.settings = config.StrategySettings(
        database_url=f"sqlite+pysqlite:///{db_path}",
        anthropic_api_key="",
    )
    main.settings = config.settings
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

    return TestClient(main.app)


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


def _generate_signal(symbol: str) -> dict[str, object]:
    import strategy_service.main as main

    signal = asyncio.run(main.generate_signal(main.SignalGenerationRequest(symbol=symbol)))
    return signal.model_dump(mode="json")


def _list_signals(*, limit: int = 20) -> list[dict[str, object]]:
    import strategy_service.main as main

    return [row.model_dump(mode="json") for row in main.list_signals(limit=limit)]


def _policy_evaluate(payload: dict[str, object]) -> dict[str, object]:
    import policy_service.main as main

    decision = main.evaluate(main.PolicyEvaluationRequest(**payload))
    return decision.model_dump(mode="json")


def _list_evaluations(*, limit: int = 20, symbol: str | None = None, decision: str | None = None) -> list[dict[str, object]]:
    import policy_service.main as main

    return [
        row.model_dump(mode="json")
        for row in main.list_evaluations(limit=limit, symbol=symbol, decision=decision)
    ]


def _create_order(signal: dict[str, object], *, idempotency_key: str) -> dict[str, object]:
    import execution_service.main as main

    order = main.create_order(
        main.ExecutionOrderRequest(**_execution_payload(signal)),
        idempotency_key=idempotency_key,
    )
    return order.model_dump(mode="json")


def _list_orders(*, limit: int = 20) -> list[dict[str, object]]:
    import execution_service.main as main

    return [row.model_dump(mode="json") for row in main.list_orders(limit=limit)]


def _get_order_fills(order_id: str) -> list[dict[str, object]]:
    import execution_service.main as main

    return [row.model_dump(mode="json") for row in main.get_order_fills(order_id)]


def _reconcile_portfolio(latest_quotes: dict[str, float]) -> dict[str, object]:
    import portfolio_service.main as main

    response = main.reconcile(main.PortfolioReconcileRequest(latest_quotes=latest_quotes))
    return response.model_dump(mode="json")


def _get_snapshot() -> dict[str, object]:
    import portfolio_service.main as main

    return main.get_snapshot().model_dump(mode="json")


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
        execution_database,
        portfolio_database,
    ) = _build_clients(tmp_path)

    signal = _generate_signal("AAPL")
    assert signal["candidate_action"] == "BUY"

    decision = _policy_evaluate(_policy_payload(signal))
    assert decision["decision"] == "APPROVE"

    order = _create_order(signal, idempotency_key="acceptance-flow")
    assert order["status"] == "ACCEPTED"

    fills = _get_order_fills(order["order_id"])
    assert len(fills) == 1
    assert fills[0]["qty"] == 10
    assert fills[0]["price"] == 100.0

    _reconcile_portfolio({"AAPL": 101.0})
    snapshot = _get_snapshot()
    assert snapshot["gross_exposure"] == 1010.0
    assert snapshot["realized_pnl"] == 0.0
    assert snapshot["unrealized_pnl"] == 10.0
    assert snapshot["positions"][0]["symbol"] == "AAPL"
    assert snapshot["positions"][0]["net_qty"] == 10

    assert _list_signals(limit=1)[0]["signal_id"] == signal["signal_id"]
    assert _list_evaluations(limit=1)[0]["signal_id"] == signal["signal_id"]
    assert _list_orders(limit=1)[0]["order_id"] == order["order_id"]

    with execution_database.SessionLocal() as session:
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


def test_review_path_is_visible_without_execution_or_portfolio_mutation(tmp_path: Path) -> None:
    (
        strategy_client,
        policy_client,
        execution_client,
        portfolio_client,
        policy_models,
        execution_models,
        portfolio_models,
        policy_database,
        execution_database,
        portfolio_database,
    ) = _build_clients(tmp_path)

    signal = _generate_signal("MSFT")

    review_payload = _policy_payload(signal)
    review_payload["confidence"] = 0.4
    decision = _policy_evaluate(review_payload)
    assert decision["decision"] == "REVIEW"
    assert "confidence_below_floor" in decision["reasons"]

    evaluations = _list_evaluations(symbol="MSFT", decision="review")
    assert evaluations[0]["signal_id"] == signal["signal_id"]

    assert _list_orders() == []

    _reconcile_portfolio({})
    snapshot = _get_snapshot()
    assert snapshot["positions"] == []
    assert snapshot["gross_exposure"] == 0.0

    with execution_database.SessionLocal() as session:
        order_count = session.scalar(select(func.count()).select_from(execution_models.OrderRecord))

    with policy_database.SessionLocal() as session:
        policy_eval = session.scalar(
            select(policy_models.PolicyEvaluationRecord).where(
                policy_models.PolicyEvaluationRecord.signal_id == signal["signal_id"]
            )
        )

    with portfolio_database.SessionLocal() as session:
        positions = session.scalars(select(portfolio_models.PositionRecordModel)).all()

    assert order_count == 0
    assert policy_eval is not None
    assert policy_eval.decision == "REVIEW"
    assert positions == []
