"""Tables for the point-in-time archive and the decision journal.

Three records, answering three different questions after the fact:

* ``bar_observations`` — what the market did. Deduplicated on
  (symbol, timeframe, bar timestamp) so repeated fetches build one clean
  time series rather than millions of duplicate rows.
* ``price_observations`` — what *we saw*, and when. Deliberately append-only
  and never deduplicated: the same bar looked at twice, once fresh and once
  stale, are two different facts about our own behaviour.
* ``decisions`` — what we did about it, with the inputs that produced the
  choice captured alongside it.

The distinction between ``bar_ts``/``price_ts`` and ``recorded_at``/``observed_at``
is the whole point of the archive. One is when the market printed the value; the
other is when this system learned of it. Research that conflates them is
research contaminated by hindsight.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class BarObservation(Base):
    """One OHLCV bar, as delivered by a provider."""

    __tablename__ = "bar_observations"
    __table_args__ = (
        UniqueConstraint("symbol", "timeframe", "bar_ts", name="uq_bar_identity"),
        Index("ix_bar_symbol_ts", "symbol", "bar_ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(16), nullable=False)
    """e.g. "1m", "15m", "1d" — the resolution actually returned, not requested."""

    bar_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    open: Mapped[float] = mapped_column(Float, nullable=False)
    high: Mapped[float] = mapped_column(Float, nullable=False)
    low: Mapped[float] = mapped_column(Float, nullable=False)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    volume: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    """When this system first stored the bar — not when the market printed it."""


class PriceObservation(Base):
    """A single price this system resolved and acted on (or declined to)."""

    __tablename__ = "price_observations"
    __table_args__ = (Index("ix_price_symbol_observed", "symbol", "observed_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    price_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    """When the market produced this price."""

    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    """When this system read it. observed_at - price_ts is the staleness."""

    age_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    accepted: Mapped[bool] = mapped_column(Integer, nullable=False, default=1)
    """False when refused as stale. Rejections are as informative as fills."""


class ExecutionQuality(Base):
    """What one order actually cost, versus what the decision assumed.

    Implementation shortfall is the difference between the price the strategy
    decided on and the price it paid. It is the only honest input to a
    backtest's cost model — everything else is a guess — and it is the number
    that decides whether an intraday strategy survives its own trading.

    Unfilled orders are recorded too, with ``filled = 0``. A limit that missed
    costs nothing in slippage but everything in opportunity, and a fill rate
    read only from fills is a fill rate of 100%.
    """

    __tablename__ = "execution_quality"
    __table_args__ = (
        Index("ix_exec_symbol_ts", "symbol", "decision_ts"),
        Index("ix_exec_order", "order_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    signal_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    qty: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    order_type: Mapped[str] = mapped_column(String(16), nullable=False, default="MARKET")
    limit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    decision_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    fill_price: Mapped[float | None] = mapped_column(Float, nullable=True)

    shortfall_bps: Mapped[float | None] = mapped_column(Float, nullable=True)
    """Signed so positive is always a cost, for buys and sells alike."""

    filled: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False, default="filled")
    decision_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Decision(Base):
    """One decision point in the pipeline, with the inputs behind it."""

    __tablename__ = "decisions"
    __table_args__ = (
        Index("ix_decision_ts", "ts"),
        Index("ix_decision_symbol_stage", "symbol", "stage"),
        Index("ix_decision_correlation", "correlation_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    decision_id: Mapped[str] = mapped_column(String(64), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    """Ties every stage of one signal's journey together — usually the signal id."""

    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    """signal | risk | policy | pdt | order | fill | exit | reconcile"""

    action: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    outcome: Mapped[str] = mapped_column(String(24), nullable=False)
    """approved | rejected | executed | skipped | error"""

    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    inputs_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    """What the system could see when it decided. The research record."""

    outputs_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
