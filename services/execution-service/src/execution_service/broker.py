"""Deterministic paper broker adapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from uuid import uuid4

from contracts import ExecutionOrderRequest, OrderStatus

from .config import settings


@dataclass(frozen=True)
class BrokerResult:
    status: OrderStatus
    external_order_id: str
    rejection_reason: Optional[str] = None


class PaperBroker:
    """Small deterministic broker used for Milestone 1."""

    def submit(self, request: ExecutionOrderRequest) -> BrokerResult:
        if request.symbol.upper() == "REJECT":
            return BrokerResult(
                status=OrderStatus.REJECTED,
                external_order_id=str(uuid4()),
                rejection_reason="symbol_rejected",
            )
        if request.qty > settings.max_qty:
            return BrokerResult(
                status=OrderStatus.REJECTED,
                external_order_id=str(uuid4()),
                rejection_reason="qty_limit_exceeded",
            )
        return BrokerResult(status=OrderStatus.ACCEPTED, external_order_id=str(uuid4()))
