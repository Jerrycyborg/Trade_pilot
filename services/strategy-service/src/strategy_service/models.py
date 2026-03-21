"""SQLAlchemy models for strategy persistence."""

from __future__ import annotations

from datetime import datetime, timezone

from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


class SignalRecord(Base):
    __tablename__ = "signals"

    signal_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    candidate_action: Mapped[str] = mapped_column(String(8))
    confidence: Mapped[float] = mapped_column(Float)
    size_pct: Mapped[float] = mapped_column(Float)
    horizon: Mapped[str] = mapped_column(String(32))
    source: Mapped[str] = mapped_column(String(32))
    model_version: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    # Milestone 2 additions — nullable for backward compat
    risk_score: Mapped[Optional[str]] = mapped_column(String(8), nullable=True, default="MEDIUM")
    ta_summary_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    research_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    acted_on: Mapped[bool] = mapped_column(Boolean, default=False)
