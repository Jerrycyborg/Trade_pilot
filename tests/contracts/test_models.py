from datetime import datetime, timezone

import pytest
from contracts import ExecutionEvent, ExecutionFill, ExecutionOrderResponse, OrderStatus, SignalCandidate


def test_signal_candidate_validation() -> None:
    signal = SignalCandidate(
        signal_id="sig-1",
        symbol="AAPL",
        ts=datetime.now(timezone.utc),
        candidate_action="BUY",
        confidence=0.7,
        size_pct=0.02,
        model_version="v1",
    )
    assert signal.symbol == "AAPL"


def test_order_status_enum_serializes() -> None:
    response = ExecutionOrderResponse(
        order_id="ord-1",
        signal_id="sig-1",
        symbol="AAPL",
        side="BUY",
        qty=10,
        order_type="MARKET",
        time_in_force="DAY",
        status=OrderStatus.ACCEPTED,
        created_at=datetime.now(timezone.utc),
    )
    assert response.model_dump()["status"] == OrderStatus.ACCEPTED


def test_signal_candidate_rejects_extra_fields() -> None:
    with pytest.raises(Exception):
        SignalCandidate(
            signal_id="sig-1",
            symbol="AAPL",
            ts=datetime.now(timezone.utc),
            candidate_action="BUY",
            confidence=0.7,
            size_pct=0.02,
            model_version="v1",
            unexpected="boom",
        )


def test_execution_fill_validation() -> None:
    fill = ExecutionFill(
        fill_id="fill-1",
        order_id="order-1",
        external_order_id="broker-1",
        signal_id="sig-1",
        symbol="AAPL",
        side="BUY",
        qty=10,
        price=123.45,
        filled_at=datetime.now(timezone.utc),
    )
    assert fill.price == 123.45


def test_execution_event_validation() -> None:
    event = ExecutionEvent(
        order_id="order-1",
        external_order_id="broker-1",
        signal_id="sig-1",
        symbol="AAPL",
        event_type="order.accepted",
        order_status=OrderStatus.ACCEPTED,
        occurred_at=datetime.now(timezone.utc),
        payload={"source": "paper-broker"},
    )
    assert event.payload["source"] == "paper-broker"
