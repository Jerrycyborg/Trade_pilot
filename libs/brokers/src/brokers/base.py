"""Broker interface and shared data classes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol

from contracts import AccountInfo, BrokerPosition, ExecutionOrderRequest, OrderStatus


@dataclass(frozen=True)
class BrokerResult:
    status: OrderStatus
    external_order_id: str
    fill_price: Optional[float] = None
    rejection_reason: Optional[str] = None


class BaseBroker(Protocol):
    def place_order(
        self,
        request: ExecutionOrderRequest,
        stop_loss_rate: float | None = None,
        take_profit_rate: float | None = None,
    ) -> BrokerResult: ...
    def submit(self, request: ExecutionOrderRequest) -> BrokerResult: ...
    def cancel_order(self, order_id: str) -> bool: ...
    def get_positions(self) -> list[BrokerPosition]: ...
    def get_account(self) -> AccountInfo: ...
    def get_order_history(self) -> list[dict[str, object]]: ...
    def close_position(
        self,
        position_id: str,
        instrument_id: int,
        units: float | None = None,
    ) -> bool: ...
