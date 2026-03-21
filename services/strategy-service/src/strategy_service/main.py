"""Strategy service entrypoint."""

from __future__ import annotations

import logging
from typing import Optional

from contracts import CandidateAction, SignalCandidate, TechnicalSummaryContract, WorkerStatus
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from market_data import MarketDataSettings, build_ta_summary, get_fetcher
from market_data.fetcher import DataUnavailableError
from pydantic import BaseModel
from sqlalchemy import select

from .ai_pipeline import AISignalPipeline, _build_deterministic_signal
from .config import settings
from .database import Base, SessionLocal, engine
from .models import SignalRecord

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class SignalGenerationRequest(BaseModel):
    symbol: str | None = "AAPL"
    symbols: list[str] | None = None
    use_ai: Optional[bool] = None  # None = auto-detect from ANTHROPIC_API_KEY


Base.metadata.create_all(bind=engine)
app = FastAPI(title="strategy-service", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "strategy-service"}


if settings.worker_enabled:
    from .scheduler import start_scheduler

    start_scheduler(app)


@app.post("/v1/signals/generate", response_model=SignalCandidate)
async def generate_signal(request: SignalGenerationRequest) -> SignalCandidate:
    """Generate a trading signal. Uses AI pipeline when ANTHROPIC_API_KEY is set."""
    symbols = [symbol.strip().upper() for symbol in (request.symbols or []) if symbol and symbol.strip()]
    target_symbol = symbols[0] if symbols else (request.symbol or "AAPL").strip().upper()
    should_use_ai = request.use_ai if request.use_ai is not None else settings.use_ai

    if should_use_ai:
        pipeline = AISignalPipeline()
        signal = await pipeline.generate(target_symbol)
    else:
        signal = _build_deterministic_signal(target_symbol)

    _persist_signal(signal)
    return signal


@app.get("/v1/signals", response_model=list[SignalCandidate])
def list_signals(
    limit: int = Query(default=20, ge=1, le=100),
    symbol: str | None = None,
    acted_on: bool | None = None,
    candidate_action: CandidateAction | None = None,
) -> list[SignalCandidate]:
    """Return persisted signals ordered newest-first."""
    with SessionLocal() as session:
        statement = select(SignalRecord)
        if symbol:
            statement = statement.where(SignalRecord.symbol == symbol.upper())
        if acted_on is not None:
            statement = statement.where(SignalRecord.acted_on == acted_on)
        if candidate_action is not None:
            statement = statement.where(SignalRecord.candidate_action == candidate_action.value)
        rows = session.scalars(statement.order_by(SignalRecord.ts.desc()).limit(limit)).all()
    return [_to_candidate(row) for row in rows]


@app.post("/v1/signals/{signal_id}/act")
def mark_signal_acted(signal_id: str) -> dict[str, object]:
    with SessionLocal() as session:
        row = session.scalar(select(SignalRecord).where(SignalRecord.signal_id == signal_id))
        if not row:
            raise HTTPException(status_code=404, detail="Signal not found")
        row.acted_on = True
        session.commit()
    return {"signal_id": signal_id, "acted_on": True}


@app.get("/v1/strategy/watchlist")
def get_watchlist() -> dict:
    """Return the configured symbol watchlist."""
    return {"symbols": settings.watchlist, "count": len(settings.watchlist)}



@app.get("/v1/strategy/watchlist/etoro")
async def etoro_watchlist() -> dict:
    """Fetch and return the live eToro watchlist for this account."""
    import os, uuid
    import httpx
    api_key = os.getenv("ETORO_API_KEY", "")
    user_key = os.getenv("ETORO_USER_KEY", "")
    if not api_key or not user_key:
        return {"error": "eToro credentials not configured", "symbols": []}
    headers = {
        "x-api-key": api_key,
        "x-user-key": user_key,
        "x-request-id": str(uuid.uuid4()),
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://public-api.etoro.com/api/v1/watchlists",
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()
        watchlists = []
        for wl in data.get("watchlists", []):
            items = [
                {"symbol": item["market"]["symbolName"], "name": item["market"]["displayName"]}
                for item in wl.get("items", [])
                if "market" in item
            ]
            watchlists.append({"name": wl["name"], "id": wl["watchlistId"], "items": items})
        return {"watchlists": watchlists, "total": len(watchlists)}
    except Exception as exc:
        return {"error": str(exc), "symbols": []}

@app.get("/v1/worker/status", response_model=WorkerStatus)
def get_worker_status() -> WorkerStatus:
    """Return trade worker / scheduler state."""
    from .worker import worker_state
    return WorkerStatus(
        last_run_at=worker_state.last_run_at,
        next_run_at=worker_state.next_run_at,
        symbols_watched=settings.watchlist,
        is_running=worker_state.is_running,
        last_run_error=worker_state.last_run_error,
    )


@app.post("/v1/worker/run")
async def trigger_worker_run() -> dict:
    """Manually trigger one worker cycle (useful for operator testing)."""
    from .worker import TradeWorker, worker_state
    if worker_state.is_running:
        return {"status": "already_running", "message": "A cycle is already in progress."}
    worker = TradeWorker()
    result = await worker.run_cycle()
    return {"status": "completed", "result": result}


# ---------------------------------------------------------------------------
# Market data endpoints
# ---------------------------------------------------------------------------

_md_settings = MarketDataSettings()


@app.get("/v1/market/quote/{symbol}")
def get_quote(symbol: str) -> dict:
    """Return latest price + key indicators for a symbol."""
    try:
        fetcher = get_fetcher(_md_settings)
        bars = fetcher.fetch(symbol.upper(), period_days=30)
        if not bars:
            raise HTTPException(status_code=404, detail=f"No market data for {symbol}")
        ta = build_ta_summary(symbol.upper(), bars)
        return {
            "symbol": ta.symbol,
            "price": ta.current_price,
            "trend": ta.trend_direction,
            "rsi": round(ta.indicators.rsi_14, 2),
            "macd_histogram": round(ta.indicators.macd_histogram, 6),
            "ema_20": round(ta.indicators.ema_20, 4),
            "ema_50": round(ta.indicators.ema_50, 4),
            "bb_position": round(ta.indicators.bb_position, 4),
            "signal_tags": ta.signal_tags,
            "as_of": ta.as_of.isoformat(),
            "data_source": ta.data_source,
        }
    except DataUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@app.get("/v1/market/chart/{symbol}")
def get_chart(
    symbol: str,
    days: int = Query(default=60, ge=7, le=365),
) -> dict:
    """Return OHLCV bars for charting + technical indicator overlays."""
    try:
        fetcher = get_fetcher(_md_settings)
        bars = fetcher.fetch(symbol.upper(), period_days=days)
        if not bars:
            raise HTTPException(status_code=404, detail=f"No chart data for {symbol}")
        ta = build_ta_summary(symbol.upper(), bars)

        # Build EMA series for overlay
        from market_data.indicators import compute_ema
        closes = [b.close for b in bars]
        ema20_series: list[dict] = []
        ema50_series: list[dict] = []
        for i in range(len(bars)):
            slice_closes = closes[: i + 1]
            ema20_series.append({
                "time": bars[i].timestamp.strftime("%Y-%m-%d"),
                "value": round(compute_ema(slice_closes, 20), 4),
            })
            ema50_series.append({
                "time": bars[i].timestamp.strftime("%Y-%m-%d"),
                "value": round(compute_ema(slice_closes, 50), 4),
            })

        return {
            "symbol": symbol.upper(),
            "bars": [
                {
                    "time": b.timestamp.strftime("%Y-%m-%d"),
                    "open": b.open,
                    "high": b.high,
                    "low": b.low,
                    "close": b.close,
                    "volume": b.volume,
                }
                for b in bars
            ],
            "ema_20": ema20_series,
            "ema_50": ema50_series,
            "indicators": {
                "rsi": round(ta.indicators.rsi_14, 2),
                "macd_line": round(ta.indicators.macd_line, 6),
                "macd_signal": round(ta.indicators.macd_signal, 6),
                "macd_histogram": round(ta.indicators.macd_histogram, 6),
                "bb_upper": round(ta.indicators.bb_upper, 4),
                "bb_middle": round(ta.indicators.bb_middle, 4),
                "bb_lower": round(ta.indicators.bb_lower, 4),
            },
            "trend": ta.trend_direction,
            "signal_tags": ta.signal_tags,
            "data_source": ta.data_source,
        }
    except DataUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@app.get("/v1/market/quotes")
def get_quotes(symbols: str = Query(description="Comma-separated symbols")) -> list[dict]:
    """Return latest quotes for multiple symbols (for ticker bar)."""
    symbol_list = [s.strip().upper() for s in symbols.split(",") if s.strip()][:10]
    results = []
    fetcher = get_fetcher(_md_settings)
    for sym in symbol_list:
        try:
            bars = fetcher.fetch(sym, period_days=5)
            if bars and len(bars) >= 2:
                latest = bars[-1].close
                prev = bars[-2].close
                change_pct = (latest - prev) / prev * 100
                results.append({
                    "symbol": sym,
                    "price": round(latest, 4),
                    "change_pct": round(change_pct, 2),
                    "direction": "up" if change_pct >= 0 else "down",
                })
            elif bars:
                results.append({
                    "symbol": sym,
                    "price": round(bars[-1].close, 4),
                    "change_pct": 0.0,
                    "direction": "neutral",
                })
        except DataUnavailableError:
            results.append({"symbol": sym, "price": None, "change_pct": None, "direction": "neutral"})
    return results


@app.get("/v1/market/events/{symbol}")
def get_market_events(symbol: str) -> dict:
    """Stub: returns no events. Wire in real data source when available."""
    return {"symbol": symbol.upper(), "has_event": False, "event_date": None}


# ---------------------------------------------------------------------------
# Manual trade endpoint (operator-triggered single trade)
# ---------------------------------------------------------------------------

class ManualTradeRequest(BaseModel):
    symbol: str
    side: str  # BUY | SELL
    qty: int
    order_type: str = "MARKET"


@app.post("/v1/trade/manual")
async def manual_trade(request: ManualTradeRequest) -> dict:
    """Submit a manual trade directly through policy → execution pipeline."""
    import httpx
    from uuid import uuid4

    signal_id = f"manual-{uuid4()}"

    # 1. Policy check
    policy_req = {
        "signal_id": signal_id,
        "symbol": request.symbol.upper(),
        "candidate_action": request.side.upper(),
        "confidence": 0.80,
        "size_pct": 0.01,
        "risk_score": "MEDIUM",
        "market_context": {
            "data_age_seconds": 5,
            "market_open": True,
            "liquidity_score": 0.95,
        },
        "portfolio_context": {
            "gross_exposure_pct": 0.0,
            "daily_drawdown_pct": 0.0,
        },
    }
    async with httpx.AsyncClient(timeout=5.0) as client:
        policy_resp = await client.post(
            f"{settings.policy_service_url}/v1/policy/evaluate", json=policy_req
        )

    policy = policy_resp.json()
    if policy.get("decision") not in ("APPROVE", "REVIEW"):
        return {
            "status": "rejected_by_policy",
            "decision": policy.get("decision"),
            "reasons": policy.get("reasons", []),
        }

    # 2. Place order
    order_req = {
        "signal_id": signal_id,
        "symbol": request.symbol.upper(),
        "side": request.side.upper(),
        "qty": request.qty,
        "order_type": request.order_type.upper(),
        "time_in_force": "DAY",
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        order_resp = await client.post(
            f"{settings.execution_service_url}/v1/orders",
            json=order_req,
            headers={"Idempotency-Key": signal_id},
        )

    order = order_resp.json()
    return {
        "status": "submitted",
        "order_id": order.get("order_id"),
        "order_status": order.get("status"),
        "symbol": request.symbol.upper(),
        "side": request.side.upper(),
        "qty": request.qty,
        "policy_decision": policy.get("decision"),
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _persist_signal(signal: SignalCandidate) -> None:
    ta_json = signal.ta_summary.model_dump_json() if signal.ta_summary else None
    with SessionLocal() as session:
        session.add(
            SignalRecord(
                signal_id=signal.signal_id,
                symbol=signal.symbol,
                ts=signal.ts,
                candidate_action=signal.candidate_action,
                confidence=signal.confidence,
                size_pct=signal.size_pct,
                horizon=signal.horizon,
                source=signal.source,
                model_version=signal.model_version,
                risk_score=signal.risk_score,
                ta_summary_json=ta_json,
                research_summary=signal.research_summary,
                acted_on=signal.acted_on,
            )
        )
        session.commit()


def _to_candidate(row: SignalRecord) -> SignalCandidate:
    ta_summary = None
    if row.ta_summary_json:
        try:
            ta_summary = TechnicalSummaryContract.model_validate_json(row.ta_summary_json)
        except Exception:
            pass

    return SignalCandidate(
        signal_id=row.signal_id,
        symbol=row.symbol,
        ts=row.ts,
        candidate_action=row.candidate_action,
        confidence=row.confidence,
        size_pct=row.size_pct,
        horizon=row.horizon,
        source=row.source,
        model_version=row.model_version,
        risk_score=row.risk_score or "MEDIUM",
        ta_summary=ta_summary,
        research_summary=row.research_summary,
        acted_on=row.acted_on,
    )
