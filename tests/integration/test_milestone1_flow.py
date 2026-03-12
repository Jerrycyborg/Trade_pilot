from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker


def _build_clients(
    tmp_path: Path,
) -> tuple[TestClient, TestClient, TestClient, object, object, object, object]:
    strategy_client = _build_strategy_client(tmp_path / "strategy.db")
    policy_client, policy_models, policy_database = _build_policy_client(tmp_path / "policy.db")
    execution_client, execution_models, execution_database = _build_execution_client(
        tmp_path / "execution.db"
    )
    return (
        strategy_client,
        policy_client,
        execution_client,
        policy_models,
        execution_models,
        policy_database,
        execution_database,
    )


def _build_strategy_client(db_path: Path) -> TestClient:
    import strategy_service.config as config
    import strategy_service.database as database
    import strategy_service.main as main

    config.settings = config.StrategySettings(database_url=f"sqlite+pysqlite:///{db_path}")
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


def _policy_payload(signal: dict[str, object], *, stale_seconds: int = 5) -> dict[str, object]:
    return {
        "signal_id": signal["signal_id"],
        "symbol": signal["symbol"],
        "candidate_action": signal["candidate_action"],
        "confidence": signal["confidence"],
        "size_pct": signal["size_pct"],
        "market_context": {
            "data_age_seconds": stale_seconds,
            "market_open": True,
            "event_blackout_active": False,
            "liquidity_score": 0.95,
            "symbol_allowed": True,
        },
        "portfolio_context": {
            "gross_exposure_pct": 0.15,
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


def test_signal_policy_execution_flow_persists_order_and_events(tmp_path: Path) -> None:
    (
        strategy_client,
        policy_client,
        execution_client,
        policy_models,
        execution_models,
        policy_database,
        execution_database,
    ) = _build_clients(tmp_path)

    signal_response = strategy_client.post("/v1/signals/generate", json={"symbol": "AAPL"})
    assert signal_response.status_code == 200
    signal = signal_response.json()

    policy_response = policy_client.post("/v1/policy/evaluate", json=_policy_payload(signal))
    assert policy_response.status_code == 200
    policy_decision = policy_response.json()
    assert policy_decision["decision"] == "APPROVE"

    order_response = execution_client.post(
        "/v1/orders",
        json=_execution_payload(signal),
        headers={"Idempotency-Key": "integration-approve"},
    )
    assert order_response.status_code == 200
    order = order_response.json()
    assert order["status"] == "ACCEPTED"

    stored_response = execution_client.get(f"/v1/orders/{order['order_id']}")
    assert stored_response.status_code == 200
    assert stored_response.json()["status"] == "ACCEPTED"

    with execution_database.SessionLocal() as session:
        order_count = session.scalar(select(func.count()).select_from(execution_models.OrderRecord))
        event_count = session.scalar(
            select(func.count()).select_from(execution_models.ExecutionEventRecord)
        )
        stored_order = session.scalar(
            select(execution_models.OrderRecord).where(
                execution_models.OrderRecord.order_id == order["order_id"]
            )
        )

    with policy_database.SessionLocal() as session:
        evaluation_count = session.scalar(
            select(func.count()).select_from(policy_models.PolicyEvaluationRecord)
        )
        stored_evaluation = session.scalar(
            select(policy_models.PolicyEvaluationRecord).where(
                policy_models.PolicyEvaluationRecord.signal_id == signal["signal_id"]
            )
        )

    assert order_count == 1
    assert event_count == 3
    assert stored_order is not None
    assert stored_order.status == "ACCEPTED"
    assert stored_order.external_order_id
    assert evaluation_count == 1
    assert stored_evaluation is not None
    assert stored_evaluation.decision == "APPROVE"
    assert stored_evaluation.policy_version == "risk_policy_v1"


def test_stale_data_rejection_blocks_execution_flow(tmp_path: Path) -> None:
    (
        strategy_client,
        policy_client,
        execution_client,
        policy_models,
        execution_models,
        policy_database,
        execution_database,
    ) = _build_clients(tmp_path)

    signal = strategy_client.post("/v1/signals/generate", json={"symbol": "AAPL"}).json()
    policy_response = policy_client.post(
        "/v1/policy/evaluate", json=_policy_payload(signal, stale_seconds=45)
    )
    assert policy_response.status_code == 200
    decision = policy_response.json()
    assert decision["decision"] == "REJECT"
    assert "stale_data" in decision["reasons"]

    with execution_database.SessionLocal() as session:
        order_count = session.scalar(select(func.count()).select_from(execution_models.OrderRecord))
        event_count = session.scalar(
            select(func.count()).select_from(execution_models.ExecutionEventRecord)
        )

    with policy_database.SessionLocal() as session:
        stored_evaluation = session.scalar(
            select(policy_models.PolicyEvaluationRecord).where(
                policy_models.PolicyEvaluationRecord.signal_id == signal["signal_id"]
            )
        )

    assert order_count == 0
    assert event_count == 0
    assert stored_evaluation is not None
    assert stored_evaluation.decision == "REJECT"
    assert "stale_data" in stored_evaluation.reasons_json

    evaluations = policy_client.get(
        "/v1/policy/evaluations", params={"decision": "reject", "symbol": "aapl"}
    )
    assert evaluations.status_code == 200
    assert evaluations.json()[0]["signal_id"] == signal["signal_id"]


def test_duplicate_idempotency_returns_same_order_and_single_persisted_record(
    tmp_path: Path,
) -> None:
    (
        strategy_client,
        policy_client,
        execution_client,
        policy_models,
        execution_models,
        policy_database,
        execution_database,
    ) = _build_clients(tmp_path)

    signal = strategy_client.post("/v1/signals/generate", json={"symbol": "AAPL"}).json()
    decision = policy_client.post("/v1/policy/evaluate", json=_policy_payload(signal)).json()
    assert decision["decision"] == "APPROVE"

    headers = {"Idempotency-Key": "integration-duplicate"}
    first = execution_client.post("/v1/orders", json=_execution_payload(signal), headers=headers)
    second = execution_client.post("/v1/orders", json=_execution_payload(signal), headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["order_id"] == second.json()["order_id"]

    with execution_database.SessionLocal() as session:
        order_count = session.scalar(select(func.count()).select_from(execution_models.OrderRecord))
        event_count = session.scalar(
            select(func.count()).select_from(execution_models.ExecutionEventRecord)
        )

    with policy_database.SessionLocal() as session:
        evaluation_count = session.scalar(
            select(func.count()).select_from(policy_models.PolicyEvaluationRecord)
        )

    assert order_count == 1
    assert event_count == 3
    assert evaluation_count == 1


def test_dashboard_read_surfaces_expose_latest_persisted_records(tmp_path: Path) -> None:
    (
        strategy_client,
        policy_client,
        execution_client,
        _policy_models,
        _execution_models,
        _policy_database,
        _execution_database,
    ) = _build_clients(tmp_path)

    approved_signal = strategy_client.post("/v1/signals/generate", json={"symbol": "AAPL"}).json()
    rejected_signal = strategy_client.post("/v1/signals/generate", json={"symbol": "REJECT"}).json()

    approved_decision = policy_client.post("/v1/policy/evaluate", json=_policy_payload(approved_signal))
    rejected_decision = policy_client.post(
        "/v1/policy/evaluate",
        json=_policy_payload(rejected_signal, stale_seconds=45),
    )
    assert approved_decision.status_code == 200
    assert rejected_decision.status_code == 200

    approved_order = execution_client.post(
        "/v1/orders",
        json=_execution_payload(approved_signal),
        headers={"Idempotency-Key": "dashboard-accept"},
    )
    rejected_order = execution_client.post(
        "/v1/orders",
        json=_execution_payload(rejected_signal),
        headers={"Idempotency-Key": "dashboard-reject"},
    )
    assert approved_order.status_code == 200
    assert rejected_order.status_code == 200

    signals = strategy_client.get("/v1/signals", params={"limit": 2})
    evaluations = policy_client.get("/v1/policy/evaluations", params={"limit": 2})
    orders = execution_client.get("/v1/orders", params={"limit": 2})

    assert signals.status_code == 200
    assert evaluations.status_code == 200
    assert orders.status_code == 200
    assert {row["signal_id"] for row in signals.json()} == {
        approved_signal["signal_id"],
        rejected_signal["signal_id"],
    }
    assert {row["decision"] for row in evaluations.json()} == {"APPROVE", "REJECT"}
    assert any(row["rejection_reason"] == "symbol_rejected" for row in orders.json())
