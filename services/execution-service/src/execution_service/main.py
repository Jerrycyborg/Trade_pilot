"""Execution service entrypoint."""

from __future__ import annotations

import hashlib
import json
import logging
from uuid import uuid4

from contracts import (
    AccountInfo,
    ClosePositionRequest,
    ExecutionEvent,
    ExecutionOrderRequest,
    ExecutionOrderResponse,
    FillRecord,
)
from contracts.auth import verify_internal_key
from contracts.sanitize import sanitize_symbol, validate_positive_amount
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .broker import broker, close_position, resolve_instrument_id
from .database import Base, SessionLocal, engine
from .logging_utils import log_event
from .models import ExecutionEventRecord, FillRecord as FillRecordModel, OrderRecord

logging.basicConfig(level=logging.INFO)


Base.metadata.create_all(bind=engine)
app = FastAPI(title="execution-service", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "execution-service"}


@app.post("/v1/orders", response_model=ExecutionOrderResponse)
def create_order(
    request: ExecutionOrderRequest,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    _: None = Depends(verify_internal_key),
) -> ExecutionOrderResponse:
    """Submit an order through the paper broker with idempotency protection."""

    request.symbol = sanitize_symbol(request.symbol)
    payload_hash = _hash_payload(request)
    with SessionLocal() as session:
        existing = session.scalar(
            select(OrderRecord).where(OrderRecord.idempotency_key == idempotency_key)
        )
        if existing:
            if existing.payload_hash != payload_hash:
                raise HTTPException(status_code=409, detail="Idempotency key payload mismatch")
            return _to_response(existing)

        broker_result = broker.place_order(
            request,
            stop_loss_rate=request.stop_loss_rate,
            take_profit_rate=request.take_profit_rate,
        )
        order = OrderRecord(
            order_id=str(uuid4()),
            signal_id=request.signal_id,
            symbol=request.symbol,
            side=request.side,
            qty=request.qty,
            order_type=request.order_type,
            time_in_force=request.time_in_force,
            status=broker_result.status.value,
            external_order_id=broker_result.external_order_id,
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
            rejection_reason=broker_result.rejection_reason,
        )
        session.add(order)
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
            existing = session.scalar(
                select(OrderRecord).where(OrderRecord.idempotency_key == idempotency_key)
            )
            if existing and existing.payload_hash == payload_hash:
                return _to_response(existing)
            raise HTTPException(status_code=409, detail="Idempotency key payload mismatch")
        _persist_event(
            session,
            order=order,
            event_type="order.submitted",
            payload=request.model_dump(mode="json"),
        )
        _persist_event(
            session,
            order=order,
            event_type=f"order.{order.status.lower()}",
            payload={"rejection_reason": broker_result.rejection_reason},
        )
        if broker_result.status.value == "ACCEPTED":
            fill = _persist_fill(session, order=order, fill_price=broker_result.fill_price)
            _persist_event(
                session,
                order=order,
                event_type="fill.recorded",
                payload=fill.model_dump(mode="json"),
            )
        session.commit()
        response = _to_response(order)

    log_event(
        "order_submitted",
        order_id=response.order_id,
        signal_id=response.signal_id,
        status=response.status.value,
    )
    return response


@app.post("/v1/orders/close")
def close_order(request: ClosePositionRequest, _: None = Depends(verify_internal_key)) -> dict[str, object]:
    request.symbol = sanitize_symbol(request.symbol)
    units = None if request.qty == 0 else request.qty
    try:
        closed = close_position(
            position_id=request.position_id,
            symbol=request.symbol,
            units=request.units if request.units is not None else units,
        )
    except NotImplementedError as exc:
        raise HTTPException(status_code=501, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    if not closed:
        raise HTTPException(status_code=502, detail="position_close_failed")
    return {
        "status": "closed",
        "symbol": request.symbol,
        "position_id": request.position_id,
        "signal_id": request.signal_id,
    }


@app.get("/v1/instruments/{symbol}/validate")
def validate_instrument(symbol: str) -> dict[str, object]:
    symbol = sanitize_symbol(symbol)
    try:
        instrument_id = resolve_instrument_id(symbol)
    except NotImplementedError:
        return {"symbol": symbol.upper(), "status": "unknown"}
    except ValueError:
        return {"symbol": symbol.upper(), "status": "invalid"}
    return {"symbol": symbol.upper(), "status": "valid", "instrument_id": instrument_id}


@app.get("/v1/orders/{order_id}", response_model=ExecutionOrderResponse)
def get_order(order_id: str) -> ExecutionOrderResponse:
    """Fetch an order by ID."""

    with SessionLocal() as session:
        order = session.scalar(select(OrderRecord).where(OrderRecord.order_id == order_id))
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")
        return _to_response(order)


@app.get("/v1/orders", response_model=list[ExecutionOrderResponse])
def list_orders(
    limit: int = Query(default=20, ge=1, le=100),
    symbol: str | None = None,
    status: str | None = None,
) -> list[ExecutionOrderResponse]:
    """Return persisted orders ordered newest-first."""

    with SessionLocal() as session:
        statement = select(OrderRecord)
        if symbol:
            symbol = sanitize_symbol(symbol)
            statement = statement.where(OrderRecord.symbol == symbol)
        if status:
            statement = statement.where(OrderRecord.status == status.upper())
        orders = session.scalars(statement.order_by(OrderRecord.created_at.desc()).limit(limit)).all()
        return [_to_response(order) for order in orders]


@app.get("/v1/orders/{order_id}/fills", response_model=list[FillRecord])
def get_order_fills(order_id: str) -> list[FillRecord]:
    """Return fills for a single order."""

    with SessionLocal() as session:
        order = session.scalar(select(OrderRecord).where(OrderRecord.order_id == order_id))
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")
        fills = session.scalars(
            select(FillRecordModel).where(FillRecordModel.order_id == order_id)
        ).all()
        return [_to_fill_response(fill) for fill in fills]


@app.get("/v1/fills", response_model=list[FillRecord])
def list_fills() -> list[FillRecord]:
    """Return all persisted fills."""

    with SessionLocal() as session:
        fills = session.scalars(select(FillRecordModel)).all()
        return [_to_fill_response(fill) for fill in fills]


@app.get("/v1/execution/events", response_model=list[ExecutionEvent])
def list_execution_events() -> list[ExecutionEvent]:
    """Return all persisted execution events."""

    with SessionLocal() as session:
        events = session.scalars(select(ExecutionEventRecord)).all()
        return [_to_event_response(event) for event in events]


@app.get("/v1/account", response_model=AccountInfo)
def get_account() -> AccountInfo:
    """Return broker account balance and mode (paper/live)."""
    return broker.get_account()


def _hash_payload(request: ExecutionOrderRequest) -> str:
    body = json.dumps(request.model_dump(mode="json"), sort_keys=True)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _to_response(order: OrderRecord) -> ExecutionOrderResponse:
    return ExecutionOrderResponse(
        order_id=order.order_id,
        signal_id=order.signal_id,
        symbol=order.symbol,
        side=order.side,
        qty=order.qty,
        order_type=order.order_type,
        time_in_force=order.time_in_force,
        status=order.status,
        created_at=order.created_at,
        rejection_reason=order.rejection_reason,
    )


def _to_fill_response(fill: FillRecordModel) -> FillRecord:
    return FillRecord(
        fill_id=fill.fill_id,
        order_id=fill.order_id,
        external_order_id=fill.external_order_id,
        signal_id=fill.signal_id,
        symbol=fill.symbol,
        side=fill.side,
        qty=fill.qty,
        price=fill.price,
        filled_at=fill.filled_at,
    )


def _to_event_response(event: ExecutionEventRecord) -> ExecutionEvent:
    payload = json.loads(event.payload_json)
    if isinstance(payload, dict) and "payload" in payload:
        payload = payload["payload"]
    return ExecutionEvent(
        order_id=event.order_id,
        external_order_id=event.external_order_id,
        signal_id=event.signal_id,
        symbol=event.symbol,
        event_type=event.event_type,
        order_status=event.order_status,
        occurred_at=event.created_at,
        payload=payload,
    )


def _persist_event(
    session,
    *,
    order: OrderRecord,
    event_type: str,
    payload: dict[str, object],
) -> None:
    event = ExecutionEvent(
        order_id=order.order_id,
        external_order_id=order.external_order_id,
        signal_id=order.signal_id,
        symbol=order.symbol,
        event_type=event_type,
        order_status=order.status,
        occurred_at=order.created_at,
        payload=payload,
    )
    session.add(
        ExecutionEventRecord(
            order_id=event.order_id,
            external_order_id=event.external_order_id,
            signal_id=event.signal_id,
            symbol=event.symbol,
            event_type=event.event_type,
            order_status=event.order_status.value,
            payload_json=event.model_dump_json(),
            created_at=event.occurred_at,
        )
    )


def _persist_fill(session, *, order: OrderRecord, fill_price: float | None = None) -> FillRecord:
    price = fill_price if fill_price is not None else 100.0
    fill = FillRecord(
        fill_id=str(uuid4()),
        order_id=order.order_id,
        external_order_id=order.external_order_id,
        signal_id=order.signal_id,
        symbol=order.symbol,
        side=order.side,
        qty=order.qty,
        price=price,
        filled_at=order.created_at,
    )
    session.add(
        FillRecordModel(
            fill_id=fill.fill_id,
            order_id=fill.order_id,
            external_order_id=fill.external_order_id,
            signal_id=fill.signal_id,
            symbol=fill.symbol,
            side=fill.side,
            qty=fill.qty,
            price=fill.price,
            filled_at=fill.filled_at,
        )
    )
    return fill
