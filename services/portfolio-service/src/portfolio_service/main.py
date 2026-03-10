"""Portfolio service entrypoint."""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from contracts import PortfolioReconcileRequest, PortfolioReconcileResponse, PortfolioSnapshot, PositionRecord
from fastapi import FastAPI
from sqlalchemy import delete, select

from .database import Base, SessionLocal, engine
from .execution_reader import list_execution_fills
from .logging_utils import log_event
from .models import PnLHistoryRecord, PortfolioSnapshotRecord, PositionRecordModel
from .reconcile import fill_from_execution, reconcile_portfolio

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(title="portfolio-service", version="0.1.0", lifespan=lifespan)


@app.get("/v1/portfolio/positions", response_model=list[PositionRecord])
def list_positions() -> list[PositionRecord]:
    """Return current derived positions."""

    with SessionLocal() as session:
        rows = session.scalars(select(PositionRecordModel).order_by(PositionRecordModel.symbol)).all()
        return [
            PositionRecord(
                symbol=row.symbol,
                net_qty=row.net_qty,
                average_cost=row.average_cost,
                realized_pnl=row.realized_pnl,
                unrealized_pnl=row.unrealized_pnl,
                market_price=row.market_price,
                market_value=row.market_value,
                updated_at=row.updated_at,
            )
            for row in rows
        ]


@app.get("/v1/portfolio/snapshot", response_model=PortfolioSnapshot)
def get_snapshot() -> PortfolioSnapshot:
    """Return the latest reconciled snapshot."""

    with SessionLocal() as session:
        snapshot = session.scalar(
            select(PortfolioSnapshotRecord).order_by(PortfolioSnapshotRecord.created_at.desc())
        )
        if not snapshot:
            fills = list_execution_fills()
            return PortfolioSnapshot(
                as_of=fills[-1].filled_at if fills else datetime.now(timezone.utc),
                positions=[],
                realized_pnl=0.0,
                unrealized_pnl=0.0,
                gross_exposure=0.0,
            )
        return PortfolioSnapshot.model_validate(
            {
                "as_of": snapshot.as_of,
                "positions": json.loads(snapshot.positions_json),
                "realized_pnl": snapshot.realized_pnl,
                "unrealized_pnl": snapshot.unrealized_pnl,
                "gross_exposure": snapshot.gross_exposure,
            }
        )


@app.post("/v1/portfolio/reconcile", response_model=PortfolioReconcileResponse)
def reconcile(request: PortfolioReconcileRequest) -> PortfolioReconcileResponse:
    """Derive portfolio state from execution fills only."""

    fills = [fill_from_execution(record) for record in list_execution_fills()]
    response = reconcile_portfolio(fills, request)
    with SessionLocal() as session:
        existing = session.scalar(
            select(PortfolioSnapshotRecord).where(
                PortfolioSnapshotRecord.reconcile_key == response.reconcile_key
            )
        )
        if existing:
            return PortfolioReconcileResponse(
                snapshot=PortfolioSnapshot.model_validate(
                    {
                        "as_of": existing.as_of,
                        "positions": json.loads(existing.positions_json),
                        "realized_pnl": existing.realized_pnl,
                        "unrealized_pnl": existing.unrealized_pnl,
                        "gross_exposure": existing.gross_exposure,
                    }
                ),
                processed_fill_count=existing.processed_fill_count,
                idempotent=True,
                reconcile_key=existing.reconcile_key,
            )

        session.execute(delete(PositionRecordModel))
        for position in response.snapshot.positions:
            session.add(
                PositionRecordModel(
                    symbol=position.symbol,
                    net_qty=position.net_qty,
                    average_cost=position.average_cost,
                    realized_pnl=position.realized_pnl,
                    unrealized_pnl=position.unrealized_pnl,
                    market_price=position.market_price,
                    market_value=position.market_value,
                    updated_at=position.updated_at,
                    reconcile_key=response.reconcile_key,
                )
            )
        session.add(
            PortfolioSnapshotRecord(
                reconcile_key=response.reconcile_key,
                as_of=response.snapshot.as_of,
                positions_json=json.dumps(
                    [position.model_dump(mode="json") for position in response.snapshot.positions]
                ),
                realized_pnl=response.snapshot.realized_pnl,
                unrealized_pnl=response.snapshot.unrealized_pnl,
                gross_exposure=response.snapshot.gross_exposure,
                processed_fill_count=response.processed_fill_count,
            )
        )
        session.add(
            PnLHistoryRecord(
                reconcile_key=response.reconcile_key,
                as_of=response.snapshot.as_of,
                realized_pnl=response.snapshot.realized_pnl,
                unrealized_pnl=response.snapshot.unrealized_pnl,
                gross_exposure=response.snapshot.gross_exposure,
            )
        )
        session.commit()

    log_event(
        "portfolio_reconciled",
        reconcile_key=response.reconcile_key,
        fill_count=response.processed_fill_count,
        position_count=len(response.snapshot.positions),
    )
    return response
