"""Deterministic paper broker — migrated from execution-service."""

from __future__ import annotations

from uuid import uuid4

from contracts import AccountInfo, BrokerPosition, ExecutionOrderRequest, OrderStatus

from .base import BrokerResult

_DEFAULT_MAX_QTY = 1000
_DEFAULT_FILL_PRICE = 100.0


class PaperBroker:
    """Small deterministic broker for local development and testing."""

    def __init__(self, max_qty: int = _DEFAULT_MAX_QTY) -> None:
        self._max_qty = max_qty

    def submit(self, request: ExecutionOrderRequest) -> BrokerResult:
        return self.place_order(request)

    def place_order(
        self,
        request: ExecutionOrderRequest,
        stop_loss_rate: float | None = None,
        take_profit_rate: float | None = None,
    ) -> BrokerResult:
        if request.symbol.upper() == "REJECT":
            return BrokerResult(
                status=OrderStatus.REJECTED,
                external_order_id=str(uuid4()),
                fill_price=None,
                rejection_reason="symbol_rejected",
            )
        if request.qty > self._max_qty:
            return BrokerResult(
                status=OrderStatus.REJECTED,
                external_order_id=str(uuid4()),
                fill_price=None,
                rejection_reason="qty_limit_exceeded",
            )
        return BrokerResult(
            status=OrderStatus.ACCEPTED,
            external_order_id=str(uuid4()),
            fill_price=_DEFAULT_FILL_PRICE,
        )

    def cancel_order(self, order_id: str) -> bool:
        return True

    def get_positions(self) -> list[BrokerPosition]:
        return []

    def get_account(self) -> AccountInfo:
        return AccountInfo(
            buying_power=100_000.0,
            equity=100_000.0,
            cash=100_000.0,
            mode="paper",
        )

    def get_order_history(self) -> list[dict[str, object]]:
        return []

    def close_position(
        self,
        position_id: str,
        instrument_id: int,
        units: float | None = None,
    ) -> bool:
        return False
