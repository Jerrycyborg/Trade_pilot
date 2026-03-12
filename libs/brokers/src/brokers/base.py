"""Broker interface and shared data classes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from contracts import AccountInfo, ExecutionOrderRequest, OrderStatus


@dataclass(frozen=True)
class BrokerResult:
    status: OrderStatus
    external_order_id: str
    fill_price: Optional[float] = None
    rejection_reason: Optional[str] = None
