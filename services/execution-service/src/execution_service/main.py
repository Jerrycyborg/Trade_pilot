"""Execution service entrypoint."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from uuid import uuid4

from contracts import (
    AccountInfo,
    BrokerPosition,
    ClosePositionRequest,
    ExecutionEvent,
    ExecutionOrderRequest,
    ExecutionOrderResponse,
    FillRecord,
    OrderStatus,
)
from contracts.auth import verify_internal_key
from contracts.cors import cors_origins
from contracts.sanitize import sanitize_symbol
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from lifecycle.routing import assert_not_live
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .broker import broker, resolve_instrument_id  # noqa: F401
from .config import settings as config_settings
from .database import Base, SessionLocal, engine
from .logging_utils import log_event
from .models import ExecutionEventRecord, OrderRecord
from .models import FillRecord as FillRecordModel
from .routing import build_router

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# One router per process. Reads shared lifecycle state on every order; the
# route is never cached, so a demotion takes effect on the next order.
# The router simulates on THIS process's paper adapter when the configured
# broker is one — a second instance over the same state file gave the process
# two books: fills landed on the router's, reads answered from this one.
from brokers import PaperBroker as _PaperBroker  # noqa: E402

router = build_router(
    max_qty=config_settings.max_qty,
    simulated=broker if isinstance(broker, _PaperBroker) else None,
)


Base.metadata.create_all(bind=engine)
app = FastAPI(title="execution-service", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins(),
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=[
        "Content-Type",
        "X-Internal-Key",
        "X-Admin-Key",
        "Idempotency-Key",
    ],
)


@app.get("/health")
def health() -> dict[str, object]:
    return {
        "status": "ok",
        "service": "execution-service",
        "operating_state": router.operating_state(os.getenv("TRADING_ACCOUNT_ID", "default")),
    }


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
                raise HTTPException(
                    status_code=409, detail="Idempotency key payload mismatch"
                ) from None
            return _to_response(existing)

        # The route is decided here, from shared lifecycle state — not by the
        # caller, and not by whichever broker the environment happens to
        # configure. A CANDIDATE sleeve journals a shadow decision and places
        # nothing; a PAPER sleeve reaches the simulator and never a live venue.
        routed = router.route(
            strategy_id=request.strategy_id,
            symbol=request.symbol,
            account_id=request.account_id,
            reduce_only=request.reduce_only,
        )
        if not routed.places_order:
            return _record_unplaced(session, request, routed, idempotency_key, payload_hash)

        if routed.decision.is_live and not {"strategy_id", "account_id"}.issubset(
            request.model_fields_set
        ):
            return _record_unplaced(
                session,
                request,
                routed,
                idempotency_key,
                payload_hash,
                reason="live_order_requires_explicit_strategy_and_account",
            )

        # Position cap, enforced here and not in the caller: the strategy
        # worker re-signals every cycle, and a persistent signal that re-enters
        # every cycle stacks one sleeve's position without bound (the first
        # live paper run went 6 → 12 → 19 shares short in three cycles). The
        # worker now checks before submitting, but a control enforced only by
        # the thing it constrains is not a control. Reduce-only orders are
        # never refused here — risk-reducing exits stay possible.
        refusal = _position_cap_refusal(request, routed)
        if refusal is not None:
            return _record_unplaced(
                session, request, routed, idempotency_key, payload_hash, reason=refusal
            )

        if routed.decision.is_live:
            # Second, independent check at the boundary. resolve_route already
            # decided this; a routing bug that got past it would place a real
            # order, so it takes two mistakes rather than one.
            assert_not_live(routed.decision.route, routed.adapter_name)

        broker_result = routed.adapter.place_order(
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
            raise HTTPException(
                status_code=409, detail="Idempotency key payload mismatch"
            ) from None
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
        # A fill is recorded whenever the broker gave us a price and did not
        # reject the order. Keying only off ACCEPTED loses the fill for brokers
        # that report an immediate FILLED (the paper simulator, and Alpaca when
        # a market order fills inside the status poll).
        # Measure what the order cost against the price the decision was based
        # on. This is the only honest input to a cost model; everything else is
        # an assumption. Misses are recorded too, or fill rate reads as 100%.
        _record_execution_quality(request, order, broker_result, routed)

        if broker_result.fill_price is not None and broker_result.status in (
            OrderStatus.ACCEPTED,
            OrderStatus.FILLED,
            OrderStatus.PARTIALLY_FILLED,
        ):
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


def _position_cap_refusal(request, routed) -> str | None:
    """Why this order may not open or extend a position, or None if it may.

    The sleeve's current book is read from the journal's own fill record — the
    same scope attribution pairs by — not from anything the caller asserts.
    Unknowable is not flat: a journal that is disabled or unreadable refuses
    entries rather than treating the missing book as empty. Exits are exempt
    either way, so this can never trap a position behind its own guard.
    """
    from journal import get_journal

    environment = "live" if routed.decision.is_live else "paper"
    net = get_journal().net_position(
        strategy_id=request.strategy_id,
        symbol=request.symbol,
        environment=environment,
        account_id=request.account_id,
    )
    if net is None:
        return (
            "position_unknowable: the journal cannot say what this sleeve "
            "already holds, so the order is refused"
        )
    side = str(request.side).upper()
    signed = float(request.qty) if side == "BUY" else -float(request.qty)
    new_net = net + signed

    if request.reduce_only:
        if abs(net) <= 1e-9:
            return "reduce_only_no_position"
        if net * signed >= 0:
            return (
                f"reduce_only_wrong_side: holding {net:+g}, order {signed:+g} "
                "would increase exposure"
            )
        if abs(signed) > abs(net) + 1e-9:
            return (
                f"reduce_only_exceeds_position: holding {net:+g}, order "
                f"{signed:+g} could reverse the position"
            )
        if abs(new_net) > abs(net) + 1e-9:
            return "reduce_only_invariant_failed"
        return None

    cap = float(config_settings.max_position_qty)
    if abs(new_net) > cap and abs(new_net) > abs(net):
        return (
            f"position_cap: {net:+g} {signed:+g} would hold {new_net:+g} "
            f"against a cap of {cap:g} for {request.strategy_id}/{request.symbol}"
        )
    return None


def _record_unplaced(
    session,
    request,
    routed,
    idempotency_key: str,
    payload_hash: str,
    reason: str | None = None,
):
    """Persist a decision that reached no broker, and say why.

    A shadow or blocked order is still a decision the system made. Dropping it
    would leave a CANDIDATE sleeve with no record of what it would have traded
    — which is exactly the evidence its promotion is supposed to rest on — and
    would make a halt invisible after the fact.
    """
    reason = reason or routed.decision.reason
    order = OrderRecord(
        order_id=str(uuid4()),
        signal_id=request.signal_id,
        symbol=request.symbol,
        side=request.side,
        qty=request.qty,
        order_type=request.order_type,
        time_in_force=request.time_in_force,
        idempotency_key=idempotency_key,
        payload_hash=payload_hash,
        status=OrderStatus.REJECTED.value,
        # No broker was contacted, so there is no broker id — but the column is
        # NOT NULL and UNIQUE, and a rejection from the broker gets a uuid too.
        # A prefixed uuid keeps the convention and makes it obvious in the table
        # that nothing was ever sent. Passing None here failed on PostgreSQL
        # (NotNullViolation) while passing on the simulated-only routers the
        # tests used, which is how it reached CI.
        external_order_id=f"unplaced-{uuid4()}",
        rejection_reason=f"{routed.decision.route.value}: {reason}",
    )
    session.add(order)
    session.flush()
    _persist_event(
        session,
        order=order,
        event_type="order.not_placed",
        payload={
            "route": routed.decision.route.value,
            "reason": reason,
            "strategy_id": request.strategy_id,
            "account_id": request.account_id,
            "reduce_only": request.reduce_only,
        },
    )
    session.commit()
    log_event(
        "order_not_placed",
        order_id=order.order_id,
        signal_id=request.signal_id,
        route=routed.decision.route.value,
        reason=reason,
    )
    return _to_response(order)


@app.post("/v1/orders/close")
def close_order(
    request: ClosePositionRequest, _: None = Depends(verify_internal_key)
) -> dict[str, object]:
    """Close through the server-side route for the named sleeve.

    This endpoint used a process-global broker and hardcoded every journal row
    to paper. A stop for one sleeve could therefore close a different venue,
    while a real close was recorded as simulated.
    """
    request.symbol = sanitize_symbol(request.symbol)
    routed = router.route(
        strategy_id=request.strategy_id,
        symbol=request.symbol,
        account_id=request.account_id,
        reduce_only=True,
    )
    if not routed.places_order or routed.adapter is None:
        raise HTTPException(
            status_code=409,
            detail=f"close_not_routable: {routed.decision.reason}",
        )
    if routed.decision.is_live and not {"strategy_id", "account_id"}.issubset(
        request.model_fields_set
    ):
        raise HTTPException(
            status_code=422,
            detail="live_close_requires_explicit_strategy_and_account",
        )

    closer = getattr(routed.adapter, "close_position", None)
    if closer is None:
        raise HTTPException(status_code=501, detail="broker_does_not_support_position_close")

    units = request.units if request.units is not None else request.qty
    if bool(getattr(routed.adapter, "requires_position_id_match", False)):
        position_reader = getattr(routed.adapter, "get_positions", None)
        if not callable(position_reader):
            raise HTTPException(
                status_code=501,
                detail="broker_cannot_verify_position_identity",
            )
        candidates = [
            position
            for position in position_reader()
            if position.symbol.upper() == request.symbol
            and position.position_id == request.position_id
        ]
        if not candidates:
            raise HTTPException(
                status_code=409,
                detail="position_id_does_not_match_symbol",
            )
        if units is not None and float(units) > abs(float(candidates[0].qty)):
            raise HTTPException(
                status_code=422,
                detail="close_units_exceed_broker_position",
            )

    instrument_id = 0
    resolver = getattr(routed.adapter, "resolve_instrument_id", None) or getattr(
        routed.adapter, "_resolve_instrument_id", None
    )
    if callable(resolver):
        try:
            instrument_id = int(resolver(request.symbol))
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    try:
        close_kwargs = {
            "position_id": request.position_id,
            "instrument_id": instrument_id,
            "units": units,
            "symbol": request.symbol,
        }
        if bool(getattr(routed.adapter, "supports_sleeve_positions", False)):
            close_kwargs.update(
                strategy_id=request.strategy_id,
                account_id=request.account_id,
                signal_id=request.signal_id,
            )
        closed = closer(**close_kwargs)
    except NotImplementedError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    if not closed:
        raise HTTPException(status_code=502, detail="position_close_failed")

    environment = "live" if routed.decision.is_live else "paper"
    fill_confirmed = bool(
        isinstance(closed, dict)
        and closed.get("fill_price")
        and closed.get("side")
        and float(closed.get("qty") or units or 0.0) > 0
    )
    if fill_confirmed:
        try:
            from journal import get_journal

            get_journal().record_execution(
                symbol=request.symbol,
                side=str(closed["side"]),
                qty=float(closed.get("qty") or units or 0.0),
                decision_price=None,
                fill_price=float(closed["fill_price"]),
                order_id=str(closed.get("order_id") or request.position_id),
                signal_id=request.signal_id,
                outcome="closed",
                strategy_id=request.strategy_id,
                strategy_version=routed.strategy_version,
                account_id=request.account_id,
                environment=environment,
                broker=routed.adapter_name,
                deduplicate=True,
            )
        except Exception as exc:
            logger.error("Close fill not journalled for %s: %s", request.symbol, exc)
    return {
        "status": "closed" if fill_confirmed else "close_submitted",
        "symbol": request.symbol,
        "position_id": request.position_id,
        "signal_id": request.signal_id,
        "environment": environment,
        "broker": routed.adapter_name,
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
        orders = session.scalars(
            statement.order_by(OrderRecord.created_at.desc()).limit(limit)
        ).all()
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


def _record_execution_quality(request, order, broker_result, routed) -> None:
    """Archive execution cost for this order, scoped to the sleeve it belongs to.

    Never raises. The scoping fields are not decoration: attribution pairs
    round trips within (strategy, symbol, environment, account), promotion
    evidence is derived from the same scope, and the champion/challenger
    comparison separates its sides by strategy id. This function used to route
    an order *by* request.strategy_id and then record the fill without it, so
    every fill in the archive was unscoped — attribution for a named strategy
    found nothing, and the evidence a paper sleeve exists to accumulate was
    being written where no gate would ever read it. Found by the first live
    paper run, whose first real fill came back with strategy_id="".

    The environment comes from the resolved route, not from the request: the
    router decided which kind of money this was, and the archive must say what
    actually happened rather than what the caller intended.
    """
    try:
        from journal import get_journal

        environment = "live" if routed.decision.is_live else "paper"
        get_journal().record_execution(
            symbol=request.symbol,
            side=request.side,
            qty=request.qty,
            decision_price=request.decision_price,
            fill_price=broker_result.fill_price,
            order_type=request.order_type,
            limit_price=request.limit_price,
            order_id=str(order.order_id),
            signal_id=request.signal_id,
            outcome=(
                broker_result.rejection_reason
                or str(getattr(broker_result.status, "value", broker_result.status)).lower()
            ),
            strategy_id=request.strategy_id,
            strategy_version=routed.strategy_version,
            account_id=request.account_id,
            environment=environment,
            broker=routed.adapter_name,
        )
    except Exception as exc:  # pragma: no cover - measurement is best effort
        logger.debug("Execution quality not recorded: %s", exc)


@app.get("/v1/execution/quality")
def execution_quality(limit: int = 200) -> dict[str, object]:
    """Measured execution cost: fill rate and implementation shortfall.

    Feed mean_shortfall_bps back into the backtest's slippage assumption to
    replace a guess with a measurement.
    """
    try:
        from journal import get_journal

        return get_journal().execution_quality(limit=limit)
    except Exception as exc:
        return {"enabled": False, "error": str(exc)}


@app.get("/v1/positions", response_model=list[BrokerPosition])
def list_broker_positions() -> list[BrokerPosition]:
    """Positions as the BROKER reports them.

    Distinct from portfolio-service's /v1/portfolio/positions, which derives
    holdings from our own fill history. The broker is authoritative; the derived
    view is a cache. Reconciliation compares the two, and cannot run without
    this endpoint.
    """
    try:
        return broker.get_positions()
    except Exception as exc:
        logger.error("Broker position lookup failed: %s", exc)
        raise HTTPException(status_code=503, detail=f"broker_unavailable: {exc}") from exc


@app.get("/v1/positions/exposure")
def sleeve_exposure(
    symbol: str, strategy_id: str, account_id: str = "default"
) -> dict[str, object]:
    """Net filled quantity one sleeve holds, per environment, from the journal.

    The broker's /v1/positions is keyed by symbol alone and cannot say which
    sleeve holds what. This is the per-sleeve view the position cap enforces
    against, exposed so a caller (the strategy worker) can decline to submit
    an entry it already knows would stack. 503 when the journal cannot answer:
    unknowable is not flat, and a caller that treats it as flat re-creates the
    stacking this exists to prevent.
    """
    from journal import get_journal

    clean = sanitize_symbol(symbol)
    journal = get_journal()
    by_environment: dict[str, float] = {}
    for environment in ("paper", "live"):
        net = journal.net_position(
            strategy_id=strategy_id,
            symbol=clean,
            environment=environment,
            account_id=account_id,
        )
        if net is None:
            raise HTTPException(status_code=503, detail="position_unknowable")
        by_environment[environment] = net
    return {
        "symbol": clean,
        "strategy_id": strategy_id,
        "account_id": account_id,
        "net_by_environment": by_environment,
    }


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
        external_order_id=order.external_order_id,
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
