"""SQLAlchemy models for portfolio persistence."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


class PositionRecordModel(Base):
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    net_qty: Mapped[int] = mapped_column(Integer)
    average_cost: Mapped[float] = mapped_column(Float)
    realized_pnl: Mapped[float] = mapped_column(Float)
    unrealized_pnl: Mapped[float] = mapped_column(Float)
    market_price: Mapped[float] = mapped_column(Float)
    market_value: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    reconcile_key: Mapped[str] = mapped_column(String(128), index=True)


class PortfolioSnapshotRecord(Base):
    __tablename__ = "portfolio_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    reconcile_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    positions_json: Mapped[str] = mapped_column(Text)
    realized_pnl: Mapped[float] = mapped_column(Float)
    unrealized_pnl: Mapped[float] = mapped_column(Float)
    gross_exposure: Mapped[float] = mapped_column(Float)
    processed_fill_count: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class PnLHistoryRecord(Base):
    __tablename__ = "pnl_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    reconcile_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    realized_pnl: Mapped[float] = mapped_column(Float)
    unrealized_pnl: Mapped[float] = mapped_column(Float)
    gross_exposure: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
