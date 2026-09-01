"""Pydantic contracts shared across services."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Risk classification
# ---------------------------------------------------------------------------


class RiskScore(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


# ---------------------------------------------------------------------------
# Market data / technical analysis contracts
# ---------------------------------------------------------------------------


class TechnicalSummaryContract(BaseModel):
    """Stripped-down TA summary for inter-service transport."""

    model_config = ConfigDict(extra="ignore")

    symbol: str
    trend_direction: str  # "bullish" | "bearish" | "neutral"
    signal_tags: list[str] = Field(default_factory=list)
    rsi_14: float
    macd_histogram: float
    bb_position: float  # 0-1 position within Bollinger Bands
    data_source: str
    as_of: datetime


# ---------------------------------------------------------------------------
# Research contracts
# ---------------------------------------------------------------------------


class ResearchReport(BaseModel):
    """Structured output from research-service per symbol."""

    model_config = ConfigDict(extra="ignore")

    symbol: str
    generated_at: datetime
    sentiment: str  # "bullish" | "bearish" | "neutral"
    headline_summary: str
    risk_factors: list[str] = Field(default_factory=list)
    macro_context: str = ""
    confidence_modifier: float = Field(default=0.0, ge=-0.2, le=0.2)
    cached: bool = False


# ---------------------------------------------------------------------------
# Broker / account contracts
# ---------------------------------------------------------------------------


class AccountInfo(BaseModel):
    """Broker account state."""

    model_config = ConfigDict(extra="ignore")

    buying_power: float
    equity: float
    cash: float
    mode: str  # "paper" | "live"


class BrokerPosition(BaseModel):
    """Broker-native position snapshot used by execution integrations."""

    model_config = ConfigDict(extra="ignore")

    symbol: str
    qty: float
    market_value: float = 0.0
    average_price: float = 0.0
    unrealized_pnl: float = 0.0


# ---------------------------------------------------------------------------
# Worker / scheduler contracts
# ---------------------------------------------------------------------------


class WorkerStatus(BaseModel):
    """Trade worker / scheduler state."""

    model_config = ConfigDict(extra="ignore")

    last_run_at: Optional[datetime] = None
    next_run_at: Optional[datetime] = None
    symbols_watched: list[str] = Field(default_factory=list)
    is_running: bool = False
    last_run_error: Optional[str] = None


class AuditEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    event_id: Optional[str] = None
    event_type: str
    symbol: Optional[str] = None
    signal_id: Optional[str] = None
    decision: Optional[str] = None
    reasoning: str = ""
    metadata: dict[str, object] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class AuditLogResponse(AuditEvent):
    event_id: str


class RiskAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approved: bool
    reason: str
    adjusted_size_pct: float = Field(ge=0.0, le=1.0, default=0.0)
    tier: int = Field(ge=1, le=3, default=1)


class SentimentScore(BaseModel):
    model_config = ConfigDict(extra="ignore")

    symbol: str
    score: float = Field(ge=-1.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    sources_used: list[str] = Field(default_factory=list)
    cached_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class NotificationEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    tier: int = Field(ge=1, le=3)
    symbol: Optional[str] = None
    action: str
    amount_usd: float = Field(ge=0.0)
    reason: str = ""
    signal_id: Optional[str] = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    signal_id: str
    symbol: str
    action: str
    amount_usd: float = Field(ge=0.0)
    tier: int = Field(ge=1, le=3)
    reason: str = ""
    metadata: dict[str, object] = Field(default_factory=dict)


class ApprovalResponse(ApprovalRequest):
    approval_id: str
    status: str
    created_at: datetime
    expires_at: Optional[datetime] = None
    reviewed_at: Optional[datetime] = None


class OrderStatus(str, Enum):
    NEW = "NEW"
    ACCEPTED = "ACCEPTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class CandidateAction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"
    EXIT = "EXIT"


class SignalCandidate(BaseModel):
    # Relaxed to "ignore" to allow forward-compatible field additions
    model_config = ConfigDict(extra="ignore")

    signal_id: str
    symbol: str
    ts: datetime
    candidate_action: CandidateAction
    confidence: float = Field(ge=0.0, le=1.0)
    size_pct: float = Field(gt=0.0, le=1.0)
    horizon: str = "intraday"
    source: str = "strategy-service"
    model_version: str
    # Milestone 2 additions
    risk_score: str = "MEDIUM"
    strategy: str = "ema_rsi_macd"
    """Which rule produced this signal. The strategy lifecycle keys its roster
    on (strategy, symbol), so a signal that does not name its rule would be
    gated against another rule's entry. Defaults to the momentum rule, which is
    what every producer predating this field was running."""
    ta_summary: Optional[TechnicalSummaryContract] = None
    research_summary: Optional[str] = None
    acted_on: bool = False


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
    model_config = ConfigDict(extra="ignore")

    signal_id: str
    symbol: str
    candidate_action: CandidateAction
    confidence: float = Field(ge=0.0, le=1.0)
    size_pct: float = Field(gt=0.0, le=1.0)
    market_context: MarketContext
    portfolio_context: PortfolioContext
    # Milestone 2 addition
    risk_score: str = "MEDIUM"


class PolicyDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    signal_id: str
    decision: str
    reasons: list[str] = Field(default_factory=list)
    approved_size_pct: float = Field(ge=0.0, le=1.0, default=0.0)
    policy_version: str = "risk_policy_v1"
    tier: int = Field(ge=1, le=3, default=1)


class PolicyEvaluationRecordResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    signal_id: str
    symbol: str
    decision: str
    reasons: list[str] = Field(default_factory=list)
    approved_size_pct: float = Field(ge=0.0, le=1.0, default=0.0)
    policy_version: str
    created_at: datetime


class ExecutionOrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    signal_id: str = Field(min_length=1, max_length=128)
    symbol: str = Field(min_length=1, max_length=32)
    side: Literal["BUY", "SELL"]
    qty: int = Field(gt=0, le=1_000_000)
    order_type: Literal["MARKET", "LIMIT"]
    time_in_force: Literal["DAY", "GTC", "IOC"] = "DAY"
    stop_loss_rate: float | None = Field(default=None, gt=0.0)
    take_profit_rate: float | None = Field(default=None, gt=0.0)
    limit_price: float | None = Field(default=None, gt=0.0)
    """Set for LIMIT orders. With time_in_force=IOC this caps the price paid
    without leaving a working order behind."""

    decision_price: float | None = Field(default=None, gt=0.0)
    """The price the decision was based on. Carried through to the fill so
    execution cost can be measured rather than assumed."""

    strategy_id: str = Field(default="ema_rsi_macd", min_length=1, max_length=128)
    """Required for live routing; the paper default keeps legacy simulations
    readable while execution rejects an implicit identity on a live route.
    Execution routes on (strategy, symbol, account); silently defaulting this field can route an order against a
    different sleeve than the one policy evaluated."""

    account_id: str = Field(default="default", min_length=1, max_length=128)

    reduce_only: bool = False
    """A claim that the order only reduces the named sleeve. Execution verifies
    the claim against its durable fill ledger before contacting any adapter."""


class ClosePositionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=1, max_length=32)
    position_id: str = Field(min_length=1, max_length=128)
    signal_id: str = Field(min_length=1, max_length=128)
    units: float | None = Field(default=None, gt=0.0)
    qty: float | None = Field(default=None, gt=0.0)
    strategy_id: str = Field(default="ema_rsi_macd", min_length=1, max_length=128)
    account_id: str = Field(default="default", min_length=1, max_length=128)


class ExecutionOrderResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_id: str
    external_order_id: Optional[str] = None
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
