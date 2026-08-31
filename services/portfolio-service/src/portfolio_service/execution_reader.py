"""Read-only access to execution fills."""

from __future__ import annotations

from sqlalchemy import DateTime, Float, Integer, String, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from .config import settings


class ExecutionBase(DeclarativeBase):
    pass


class ExecutionFillRecord(ExecutionBase):
    __tablename__ = "fills"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fill_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    order_id: Mapped[str] = mapped_column(String(64), index=True)
    external_order_id: Mapped[str] = mapped_column(String(64), index=True)
    signal_id: Mapped[str] = mapped_column(String(64), index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    side: Mapped[str] = mapped_column(String(8))
    qty: Mapped[int] = mapped_column(Integer)
    price: Mapped[float] = mapped_column(Float)
    filled_at: Mapped[object] = mapped_column(DateTime(timezone=True))


execution_connect_args = (
    {"check_same_thread": False} if settings.execution_database_url.startswith("sqlite") else {}
)
execution_engine = create_engine(
    settings.execution_database_url, future=True, connect_args=execution_connect_args
)
ExecutionSessionLocal = sessionmaker(
    bind=execution_engine, autoflush=False, autocommit=False, future=True
)


def list_execution_fills() -> list[ExecutionFillRecord]:
    with ExecutionSessionLocal() as session:
        return session.scalars(
            select(ExecutionFillRecord).order_by(
                ExecutionFillRecord.filled_at, ExecutionFillRecord.fill_id
            )
        ).all()
