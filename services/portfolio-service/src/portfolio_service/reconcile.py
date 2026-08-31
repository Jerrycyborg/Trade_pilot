"""Portfolio reconciliation logic."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone

from contracts import (
    FillRecord,
    PortfolioReconcileRequest,
    PortfolioReconcileResponse,
    PortfolioSnapshot,
    PositionRecord,
)

from .execution_reader import ExecutionFillRecord


@dataclass
class PositionState:
    symbol: str
    net_qty: int = 0
    average_cost: float = 0.0
    realized_pnl: float = 0.0
    last_fill_price: float = 0.0


def build_reconcile_key(fills: list[FillRecord], request: PortfolioReconcileRequest) -> str:
    payload = {
        "fills": [
            {
                "fill_id": fill.fill_id,
                "symbol": fill.symbol,
                "side": fill.side,
                "qty": fill.qty,
                "price": fill.price,
                "filled_at": fill.filled_at.isoformat(),
            }
            for fill in fills
        ],
        "latest_quotes": request.latest_quotes,
        "as_of": request.as_of.isoformat() if request.as_of else None,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def fill_from_execution(record: ExecutionFillRecord) -> FillRecord:
    return FillRecord(
        fill_id=record.fill_id,
        order_id=record.order_id,
        external_order_id=record.external_order_id,
        signal_id=record.signal_id,
        symbol=record.symbol,
        side=record.side,
        qty=record.qty,
        price=record.price,
        filled_at=record.filled_at,
    )


def reconcile_portfolio(
    fills: list[FillRecord], request: PortfolioReconcileRequest
) -> PortfolioReconcileResponse:
    positions: dict[str, PositionState] = {}
    for fill in sorted(fills, key=lambda item: (item.filled_at, item.fill_id)):
        state = positions.setdefault(fill.symbol, PositionState(symbol=fill.symbol))
        _apply_fill(state, fill)

    as_of = request.as_of or _default_as_of(fills)
    position_records: list[PositionRecord] = []
    realized_total = 0.0
    unrealized_total = 0.0
    gross_exposure = 0.0

    for symbol in sorted(positions):
        state = positions[symbol]
        if state.net_qty == 0:
            continue
        market_price = request.latest_quotes.get(
            symbol, state.last_fill_price or state.average_cost
        )
        market_value = state.net_qty * market_price
        unrealized_pnl = _compute_unrealized(state.net_qty, state.average_cost, market_price)
        gross_exposure += abs(state.net_qty * market_price)
        realized_total += state.realized_pnl
        unrealized_total += unrealized_pnl
        position_records.append(
            PositionRecord(
                symbol=symbol,
                net_qty=state.net_qty,
                average_cost=round(state.average_cost, 6),
                realized_pnl=round(state.realized_pnl, 6),
                unrealized_pnl=round(unrealized_pnl, 6),
                market_price=round(market_price, 6),
                market_value=round(market_value, 6),
                updated_at=as_of,
            )
        )

    snapshot = PortfolioSnapshot(
        as_of=as_of,
        positions=position_records,
        realized_pnl=round(realized_total, 6),
        unrealized_pnl=round(unrealized_total, 6),
        gross_exposure=round(gross_exposure, 6),
    )
    return PortfolioReconcileResponse(
        snapshot=snapshot,
        processed_fill_count=len(fills),
        idempotent=False,
        reconcile_key=build_reconcile_key(fills, request),
    )


def _default_as_of(fills: list[FillRecord]) -> datetime:
    if fills:
        return max(fill.filled_at for fill in fills)
    return datetime.now(timezone.utc)


def _apply_fill(state: PositionState, fill: FillRecord) -> None:
    signed_qty = fill.qty if fill.side.upper() == "BUY" else -fill.qty
    state.last_fill_price = fill.price

    if (
        state.net_qty == 0
        or (state.net_qty > 0 and signed_qty > 0)
        or (state.net_qty < 0 and signed_qty < 0)
    ):
        new_qty = state.net_qty + signed_qty
        total_cost = abs(state.net_qty) * state.average_cost + abs(signed_qty) * fill.price
        state.net_qty = new_qty
        state.average_cost = 0.0 if new_qty == 0 else total_cost / abs(new_qty)
        return

    closing_qty = min(abs(state.net_qty), abs(signed_qty))
    if state.net_qty > 0:
        state.realized_pnl += closing_qty * (fill.price - state.average_cost)
    else:
        state.realized_pnl += closing_qty * (state.average_cost - fill.price)

    remaining_qty = state.net_qty + signed_qty
    if remaining_qty == 0:
        state.net_qty = 0
        state.average_cost = 0.0
    elif (state.net_qty > 0 and remaining_qty > 0) or (state.net_qty < 0 and remaining_qty < 0):
        state.net_qty = remaining_qty
    else:
        state.net_qty = remaining_qty
        state.average_cost = fill.price


def _compute_unrealized(net_qty: int, average_cost: float, market_price: float) -> float:
    if net_qty > 0:
        return net_qty * (market_price - average_cost)
    if net_qty < 0:
        return abs(net_qty) * (average_cost - market_price)
    return 0.0
