from datetime import datetime, timezone

import pytest
from contracts import (
    CandidateAction,
    ExecutionEvent,
    ExecutionFill,
    ExecutionOrderRequest,
    ExecutionOrderResponse,
    OrderStatus,
    SignalCandidate,
)


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


def test_signal_candidate_supports_exit_action() -> None:
    signal = SignalCandidate(
        signal_id="sig-exit",
        symbol="AAPL",
        ts=datetime.now(timezone.utc),
        candidate_action=CandidateAction.EXIT,
        confidence=0.95,
        size_pct=1.0,
        model_version="exit-v1",
    )
    assert signal.candidate_action == CandidateAction.EXIT


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


def test_signal_candidate_ignores_extra_fields() -> None:
    # SignalCandidate uses extra="ignore" for forward compatibility.
    # Extra fields should be silently ignored rather than raising an error.
    signal = SignalCandidate(
        signal_id="sig-1",
        symbol="AAPL",
        ts=datetime.now(timezone.utc),
        candidate_action="BUY",
        confidence=0.7,
        size_pct=0.02,
        model_version="v1",
        unexpected_future_field="ignored",
    )
    assert signal.symbol == "AAPL"
    assert not hasattr(signal, "unexpected_future_field")


def test_signal_candidate_new_risk_fields() -> None:
    from contracts import TechnicalSummaryContract
    from datetime import datetime, timezone

    signal = SignalCandidate(
        signal_id="sig-2",
        symbol="MSFT",
        ts=datetime.now(timezone.utc),
        candidate_action="BUY",
        confidence=0.75,
        size_pct=0.015,
        model_version="ai-v1",
        risk_score="LOW",
        research_summary="Strong earnings beat.",
    )
    assert signal.risk_score == "LOW"
    assert signal.research_summary == "Strong earnings beat."
    assert signal.ta_summary is None


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


def test_execution_order_request_accepts_stop_loss_take_profit() -> None:
    request = ExecutionOrderRequest(
        signal_id="sig-1",
        symbol="AAPL",
        side="BUY",
        qty=10,
        order_type="MARKET",
        time_in_force="DAY",
        stop_loss_rate=97.0,
        take_profit_rate=106.0,
    )
    assert request.stop_loss_rate == 97.0
    assert request.take_profit_rate == 106.0
