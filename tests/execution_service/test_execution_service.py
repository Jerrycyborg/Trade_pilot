from pathlib import Path
from uuid import uuid4

from brokers import PaperBroker
from contracts import ExecutionOrderRequest
from execution_service.routing import BrokerRouter


def _setup():
    db_file = Path("/tmp") / f"execution-{uuid4()}.db"
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
    # Orders now resolve their route server-side, so the router is what the
    # test has to supply — setting main.broker alone would leave the real
    # router in place, reading whatever lifecycle state the environment had.
    paper = PaperBroker()
    main.broker = paper
    main.router = BrokerRouter(store=None, simulated=paper)
    return main


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


def test_duplicate_submission_same_payload() -> None:
    main = _setup()
    first = main.create_order(ExecutionOrderRequest(**_request()), idempotency_key="idem-1")
    second = main.create_order(ExecutionOrderRequest(**_request()), idempotency_key="idem-1")
    assert first.order_id == second.order_id


def test_duplicate_submission_different_payload() -> None:
    import pytest
    from fastapi import HTTPException

    main = _setup()
    first = main.create_order(ExecutionOrderRequest(**_request()), idempotency_key="idem-2")
    assert first.status == "ACCEPTED"
    with pytest.raises(HTTPException) as exc:
        main.create_order(ExecutionOrderRequest(**_request(qty=11)), idempotency_key="idem-2")
    assert exc.value.status_code == 409


def test_valid_order_accepted() -> None:
    main = _setup()
    response = main.create_order(ExecutionOrderRequest(**_request()), idempotency_key="idem-3")
    assert response.status == "ACCEPTED"
    lookup = main.get_order(response.order_id)
    assert lookup.order_id == response.order_id


def test_duplicate_submission_same_payload_does_not_create_extra_events() -> None:
    main = _setup()
    first = main.create_order(ExecutionOrderRequest(**_request()), idempotency_key="idem-5")
    second = main.create_order(ExecutionOrderRequest(**_request()), idempotency_key="idem-5")
    # The point of an idempotency key: the second call returns the first order
    # rather than creating another one. This was computed and never checked.
    assert second.order_id == first.order_id

    import execution_service.database as database
    import execution_service.models as models
    from sqlalchemy import func, select

    with database.SessionLocal() as session:
        order_count = session.scalar(select(func.count()).select_from(models.OrderRecord))
        event_count = session.scalar(select(func.count()).select_from(models.ExecutionEventRecord))
        stored_order = session.scalar(
            select(models.OrderRecord).where(models.OrderRecord.order_id == first.order_id)
        )

    assert order_count == 1
    # ACCEPTED order creates 3 events: order.submitted, order.accepted, fill.recorded
    assert event_count == 3
    assert stored_order is not None
    assert stored_order.external_order_id


def test_rejected_order_path() -> None:
    main = _setup()
    response = main.create_order(
        ExecutionOrderRequest(**_request(symbol="REJECT")),
        idempotency_key="idem-4",
    )
    assert response.status == "REJECTED"
    assert response.rejection_reason == "symbol_rejected"


def test_list_orders_returns_newest_first_and_filters() -> None:
    main = _setup()
    accepted = main.create_order(
        ExecutionOrderRequest(**_request(signal_id="sig-1", symbol="AAPL")),
        idempotency_key="idem-6",
    )
    rejected = main.create_order(
        ExecutionOrderRequest(**_request(signal_id="sig-2", symbol="REJECT")),
        idempotency_key="idem-7",
    )

    listed = main.list_orders(limit=2)
    assert len(listed) == 2
    assert listed[0].order_id == rejected.order_id
    assert listed[1].order_id == accepted.order_id

    filtered = main.list_orders(limit=20, status="rejected", symbol="reject")
    assert len(filtered) == 1
    assert filtered[0].order_id == rejected.order_id
    assert filtered[0].rejection_reason == "symbol_rejected"


def test_close_order_endpoint_uses_the_resolved_adapter(monkeypatch) -> None:
    main = _setup()
    captured = {}

    class Adapter:
        def close_position(
            self, *, position_id: str, instrument_id: int, units=None, symbol: str
        ) -> bool:
            captured.update(
                position_id=position_id,
                instrument_id=instrument_id,
                symbol=symbol,
                units=units,
            )
            return True

    class Decision:
        is_live = False
        reason = "paper"

    class Routed:
        places_order = True
        adapter = Adapter()
        adapter_name = "paper"
        decision = Decision()

    monkeypatch.setattr(main.router, "route", lambda **_kwargs: Routed())
    response = main.close_order(
        main.ClosePositionRequest(
            symbol="AAPL",
            position_id="pos-1",
            signal_id="sig-exit",
            strategy_id="ema_rsi_macd",
            account_id="default",
        ),
    )
    assert response["status"] == "closed"
    assert captured == {
        "position_id": "pos-1",
        "instrument_id": 0,
        "symbol": "AAPL",
        "units": None,
    }
