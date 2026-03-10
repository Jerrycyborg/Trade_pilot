"""Pydantic contracts shared across services."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class OrderStatus(str, Enum):
    NEW = "NEW"
    ACCEPTED = "ACCEPTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class SignalCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    signal_id: str
    symbol: str
    ts: datetime
    candidate_action: str
    confidence: float = Field(ge=0.0, le=1.0)
    size_pct: float = Field(gt=0.0, le=1.0)
    horizon: str = "intraday"
    source: str = "strategy-service"
    model_version: str


class MarketContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data_age_seconds: int = Field(ge=0)
    market_open: bool
    event_blackout_active: bool = False
    liquidity_score: float = Field(ge=0.0, le=1.0, default=1.0)
    symbol_allowed: bool = True


class PortfolioContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gross_exposure_pct: float = Field(ge=0.0, le=1.0)
    daily_drawdown_pct: float = Field(ge=0.0, le=1.0)


class PolicyEvaluationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    signal_id: str
    symbol: str
    candidate_action: str
    confidence: float = Field(ge=0.0, le=1.0)
    size_pct: float = Field(gt=0.0, le=1.0)
    market_context: MarketContext
    portfolio_context: PortfolioContext


class PolicyDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    signal_id: str
    decision: str
    reasons: list[str] = Field(default_factory=list)
    approved_size_pct: float = Field(ge=0.0, le=1.0, default=0.0)
    policy_version: str = "risk_policy_v1"


class ExecutionOrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    signal_id: str
    symbol: str
    side: str
    qty: int = Field(gt=0)
    order_type: str
    time_in_force: str = "DAY"


class ExecutionOrderResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_id: str
    signal_id: str
    symbol: str
    side: str
    qty: int
    order_type: str
    time_in_force: str
    status: OrderStatus
    created_at: datetime
    rejection_reason: Optional[str] = None


class FillRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fill_id: str
    order_id: str
    external_order_id: str
    signal_id: str
    symbol: str
    side: str
    qty: int = Field(gt=0)
    price: float = Field(gt=0.0)
    filled_at: datetime


ExecutionFill = FillRecord


class ExecutionEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_id: str
    external_order_id: str
    signal_id: str
    symbol: str
    event_type: str
    order_status: OrderStatus
    occurred_at: datetime
    payload: dict[str, object] = Field(default_factory=dict)


class PositionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    net_qty: int
    average_cost: float = Field(ge=0.0)
    realized_pnl: float
    unrealized_pnl: float
    market_price: float = Field(ge=0.0)
    market_value: float
    updated_at: datetime


class PortfolioSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    as_of: datetime
    positions: list[PositionRecord] = Field(default_factory=list)
    realized_pnl: float
    unrealized_pnl: float
    gross_exposure: float = Field(ge=0.0)


class PortfolioReconcileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    latest_quotes: dict[str, float] = Field(default_factory=dict)
    as_of: datetime | None = None


class PortfolioReconcileResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot: PortfolioSnapshot
    processed_fill_count: int = Field(ge=0)
    idempotent: bool
    reconcile_key: str


class FeatureSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    features: dict[str, float]
