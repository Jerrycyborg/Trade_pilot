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

from datetime import datetime, timezone

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
    """One OHLCV bar **as observed**, not one bar per timestamp.

    Providers revise bars. The original identity was
    ``(symbol, timeframe, bar_ts)`` with ``ON CONFLICT DO NOTHING``, so the
    first version seen won permanently and a correction was discarded without
    trace. That makes the archive able to answer "what did we see first?" and
    unable to answer "what did we know at 14:35?" — which is the only question
    it exists for.

    Identity now includes the payload hash, so a revision is a new row rather
    than a dropped one. A re-fetch of an unchanged bar still deduplicates,
    because the hash is unchanged. Point-in-time reconstruction is then "the
    latest observation of each bar_ts whose observed_at is at or before the
    decision time" — see ``Journal.bars_as_of``.
    """

    __tablename__ = "bar_observations"
    __table_args__ = (
        UniqueConstraint(
            "symbol", "timeframe", "bar_ts", "payload_hash", name="uq_bar_observation"
        ),
        Index("ix_bar_symbol_ts", "symbol", "bar_ts"),
        Index("ix_bar_observed", "symbol", "timeframe", "bar_ts", "observed_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(16), nullable=False)
    """e.g. "1m", "15m", "1d" — the resolution actually returned, not requested."""

    bar_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    """When the market printed this bar. Never substituted — a bar without a
    usable market timestamp is rejected, not stamped with the fetch time."""

    open: Mapped[float] = mapped_column(Float, nullable=False)
    high: Mapped[float] = mapped_column(Float, nullable=False)
    low: Mapped[float] = mapped_column(Float, nullable=False)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    volume: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    """When this system first stored the bar — not when the market printed it."""

    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    """When this system learned of *this version*. A revision observed later
    has a later observed_at, which is what makes point-in-time queries work."""

    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    """sha256 over the OHLCV values. Two observations of the same bar_ts with
    different hashes are a revision; with the same hash, a re-fetch."""

    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    """0 for the first observation of a bar_ts, incrementing per revision."""

    provider_meta: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    """Provider request identity and any feed metadata, as JSON. Without it two
    observations of the same bar cannot be attributed or compared."""


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

    # --- scope -----------------------------------------------------------
    # Without these, paper, live and test fills are indistinguishable rows and
    # any performance figure computed over them mixes environments silently.
    strategy_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    strategy_version: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    environment: Mapped[str] = mapped_column(String(16), nullable=False, default="paper")
    """backtest | paper | live. The single most important column here."""
    account_id: Mapped[str] = mapped_column(String(64), nullable=False, default="default")
    portfolio_id: Mapped[str] = mapped_column(String(64), nullable=False, default="default")
    broker: Mapped[str] = mapped_column(String(32), nullable=False, default="")

    # --- traceability ------------------------------------------------------
    decision_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    order_intent_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    """Links the order back to the decision that asked for it, so a post-trade
    attribution can ask why rather than only what."""

    requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    filled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    """Three timestamps, not one: the gap between requested and submitted is
    the system's own latency, and between submitted and filled is the venue's."""

    # --- cost breakdown ----------------------------------------------------
    fees: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    spread_bps: Mapped[float | None] = mapped_column(Float, nullable=True)
    filled_qty: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    """Partial fills are the normal case at size; a boolean filled flag cannot
    express one."""
    cancelled: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rejected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

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
