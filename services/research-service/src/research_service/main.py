"""Research service entrypoint."""

from __future__ import annotations

import asyncio
import logging

from contracts import ResearchReport
from contracts.auth import verify_internal_key
from contracts.sanitize import sanitize_symbol
from contracts.cors import cors_origins
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .cache import ResearchCache, _to_report
from .config import settings
from .database import Base, SessionLocal, engine
from .models import ResearchReportRecord
from .researcher import AIResearcher

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


Base.metadata.create_all(bind=engine)
if not settings.anthropic_api_key:
    logger.warning("ANTHROPIC_API_KEY is not set. Research calls will return neutral stubs.")
app = FastAPI(title="research-service", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins(),
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=[
        "Content-Type",
        "X-Internal-Key",
        "X-Admin-Key",
        "Idempotency-Key",
    ],
)

_cache = ResearchCache()
_semaphore = asyncio.Semaphore(3)  # limit concurrent Anthropic calls


class ResearchRequest(BaseModel):
    symbols: list[str]


@app.post("/v1/research/report", response_model=list[ResearchReport])
async def get_research_reports(
    request: ResearchRequest,
    _: None = Depends(verify_internal_key),
) -> list[ResearchReport]:
    """Research one or more symbols. Returns cached results when available."""
    symbols = [
        sanitize_symbol(symbol)
        for symbol in request.symbols[: settings.max_symbols_per_request]
    ]
    results: list[ResearchReport] = []

    async def research_one(symbol: str) -> ResearchReport:
        with SessionLocal() as session:
            cached = _cache.get(symbol, session)
            if cached:
                return cached

        # Cache miss — call AI researcher
        if not settings.anthropic_api_key:
            from .researcher import _build_stub
            return _build_stub(symbol)

        async with _semaphore:
            researcher = AIResearcher()
            report = await researcher.research(symbol)

        with SessionLocal() as session:
            _cache.set(report, session)
            session.commit()
        return report

    tasks = [research_one(s.upper()) for s in symbols]
    results = await asyncio.gather(*tasks)
    return list(results)


@app.get("/v1/research/report/{symbol}", response_model=ResearchReport)
def get_symbol_report(symbol: str) -> ResearchReport:
    """Return the latest cached research report for a symbol."""
    with SessionLocal() as session:
        cached = _cache.get(symbol.upper(), session)
        if cached:
            return cached
        raise HTTPException(status_code=404, detail=f"No cached report for {symbol.upper()}")


@app.get("/v1/research/reports", response_model=list[ResearchReport])
def list_reports(limit: int = Query(default=20, ge=1, le=100)) -> list[ResearchReport]:
    """Return the latest report per symbol (most recent first)."""
    with SessionLocal() as session:
        # Get latest record per symbol
        from sqlalchemy import func

        subq = (
            session.query(
                ResearchReportRecord.symbol,
                func.max(ResearchReportRecord.generated_at).label("max_ts"),
            )
            .group_by(ResearchReportRecord.symbol)
            .subquery()
        )
        records = (
            session.query(ResearchReportRecord)
            .join(
                subq,
                (ResearchReportRecord.symbol == subq.c.symbol)
                & (ResearchReportRecord.generated_at == subq.c.max_ts),
            )
            .order_by(ResearchReportRecord.generated_at.desc())
            .limit(limit)
            .all()
        )
        return [_to_report(r) for r in records]


@app.get("/v1/research/status")
def get_status() -> dict:
    """Return research service configuration."""
    return {
        "anthropic_configured": bool(settings.anthropic_api_key),
        "cache_ttl_seconds": settings.cache_ttl_seconds,
        "model": settings.claude_model,
        "max_symbols_per_request": settings.max_symbols_per_request,
    }
