"""SQLAlchemy models for policy persistence."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


class PolicyEvaluationRecord(Base):
    __tablename__ = "policy_evaluations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    signal_id: Mapped[str] = mapped_column(String(64), index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    decision: Mapped[str] = mapped_column(String(16))
    approved_size_pct: Mapped[float] = mapped_column(Float)
    policy_version: Mapped[str] = mapped_column(String(32))
    reasons_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    rule_hits: Mapped[list["PolicyRuleHitRecord"]] = relationship(back_populates="evaluation")


class PolicyRuleHitRecord(Base):
    __tablename__ = "policy_rule_hits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    evaluation_id: Mapped[int] = mapped_column(ForeignKey("policy_evaluations.id"))
    rule_name: Mapped[str] = mapped_column(String(64))

    evaluation: Mapped[PolicyEvaluationRecord] = relationship(back_populates="rule_hits")
