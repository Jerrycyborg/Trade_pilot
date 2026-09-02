from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from contracts import (
    ApprovalRequest,
    AuditEvent,
    NotificationEvent,
    PolicyEvaluationRequest,
    SignalCandidate,
)
from contracts.auth import verify_admin_key, verify_internal_key
from contracts.cors import cors_origins
from contracts.execution import (
    average_daily_volume,
    marketable_limit_price,
    participation_capped_qty,
)
from contracts.rate_limit import rate_limit_write
from contracts.sanitize import sanitize_symbol
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from lifecycle import DEFAULT_LIVE_STRATEGY
from lifecycle.health import run_health_sweep
from lifecycle.service import LifecycleService, get_lifecycle_service
from lifecycle.store import STATES, LifecycleStoreError, LifecycleUnavailableError
from market_data import (
    LivePriceCache,
    MarketDataSettings,
    RealtimePriceSource,
    StreamManager,
    fetch_bars,
)
from market_data.fetcher import OHLCVFetcherProtocol  # noqa: F401 - kept for type hints
from market_data.indicators import compute_atr
from pydantic import BaseModel, ConfigDict, Field

from .config import settings
from .day_trade_tracker import DayTradeTracker
from .policy_config import is_market_hours, load_policy_config, update_policy_config
from .reconciliation import Reconciler
from .risk_engine import evaluate_risk
from .stop_loss_monitor import StopLossMonitor, StopLossRecord
from .take_profit_monitor import TakeProfitMonitor, TakeProfitRecord

# Without this, records propagate to a handler-less root logger and fall to
# Python's last-resort handler, which prints WARNING and above only — every
# INFO-level operational record (stop-loss registrations, cycle notices,
# recovery messages) silently vanished under uvicorn. The other services
# configure this; the orchestrator, the one that most needs an audit trail
# of what it armed and when, did not.
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())
logger = logging.getLogger(__name__)


def _internal_headers() -> dict[str, str]:
    key = settings.internal_api_key or os.environ.get("INTERNAL_API_KEY", "")
    return {"X-Internal-Key": key} if key else {}


class OrchestratorState:
    running: bool = False
    last_cycle_time: datetime | None = None
    last_cycle_summary: dict[str, object] = {}
    trades_today: int = 0
    weekly_notional_used: float = 0.0
    scheduler: AsyncIOScheduler | None = None
    allowlist_validation_ran: bool = False
    last_validation: dict[str, list[str]] = {"valid": [], "invalid": [], "unknown": []}
    stop_loss_monitor: StopLossMonitor | None = None
    take_profit_monitor: TakeProfitMonitor | None = None
    monthly_realized_loss_usd: float = 0.0
    monthly_realized_profit_usd: float = 0.0
    monthly_reset_month: int = 0
    monthly_reset_year: int = 0
    price_source: RealtimePriceSource | None = None
    reconciler: Reconciler | None = None
    stream_manager: StreamManager | None = None
    market_settings: MarketDataSettings | None = None
    day_trades: DayTradeTracker | None = None
    lifecycle: LifecycleService | None = None
    last_health_sweep: dict[str, object] | None = None
    learning_running: bool = False
    last_learning_summary: dict[str, object] | None = None


state = OrchestratorState()


class KillSwitchRequest(BaseModel):
    active: bool


class LiveModeRequest(BaseModel):
    enable: bool
    confirmation: str = ""


def _cycle_interval_seconds() -> int:
    """Trading cycle cadence. ORCHESTRATOR_INTERVAL_SECONDS wins when set."""
    explicit_seconds = os.getenv("ORCHESTRATOR_INTERVAL_SECONDS")
    if explicit_seconds:
        return max(10, int(explicit_seconds))
    return max(10, settings.orchestrator_interval_minutes * 60)


def _trading_loop_owner() -> str:
    owner = os.getenv("TRADING_LOOP_OWNER", "orchestrator").strip().lower()
    if owner not in {"orchestrator", "strategy"}:
        raise RuntimeError("TRADING_LOOP_OWNER must be 'orchestrator' or 'strategy'")
    return owner


def _risk_check_interval_seconds(env_var: str) -> int:
    """How often to re-check stops/targets.

    An exit check is only as timely as its interval: a 5-minute poll on a
    15-minute intraday strategy means a stop can overshoot by 5 minutes of
    price movement. Intraday runs therefore default to 60 seconds.
    """
    explicit = os.getenv(env_var)
    if explicit:
        return max(10, int(float(explicit) * 60))
    return 60 if _market_settings().is_intraday else 300


def _start_scheduler() -> None:
    if state.scheduler is not None:
        return

    scheduler = AsyncIOScheduler()

    async def run_job() -> None:
        if state.running:
            return
        config = load_policy_config(settings.policy_config_path)
        if not is_market_hours(config):
            return
        await run_cycle()

    if _trading_loop_owner() == "orchestrator":
        scheduler.add_job(
            run_job,
            trigger="interval",
            seconds=_cycle_interval_seconds(),
            id="autonomy_orchestrator",
            max_instances=1,
            coalesce=True,
        )
    else:
        logger.warning(
            "Orchestrator entry scheduler disabled: TRADING_LOOP_OWNER=strategy. "
            "Risk, reconciliation, health and learning jobs remain active."
        )

    # Coroutine functions handed to the scheduler directly, never wrapped in a
    # sync lambda: AsyncIOScheduler runs a sync callable in its thread-pool
    # executor, where asyncio.create_task has no running loop — so every tick
    # of a lambda-wrapped job died with "no running event loop" and the job it
    # wrapped never executed once. The first orchestrator drill found all four
    # risk jobs below dead this way, stop-loss included, while the trading
    # cycle above (a real coroutine job) ran fine.
    scheduler.add_job(
        _run_stop_loss_check,
        "interval",
        seconds=_risk_check_interval_seconds("STOP_LOSS_CHECK_INTERVAL_MINUTES"),
        id="stop_loss_check",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    scheduler.add_job(
        _run_reconciliation,
        "interval",
        seconds=int(os.getenv("RECONCILE_INTERVAL_SECONDS", "300")),
        id="position_reconciliation",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    scheduler.add_job(
        _run_take_profit_check,
        "interval",
        seconds=_risk_check_interval_seconds("TAKE_PROFIT_CHECK_INTERVAL_MINUTES"),
        id="take_profit_check",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    # Demotion triggers and journal completeness. Scheduled rather than left to
    # an operator calling an endpoint: a safety control that only runs when
    # somebody remembers is not a safety control.
    scheduler.add_job(
        _run_health_sweep,
        "interval",
        seconds=int(os.getenv("HEALTH_SWEEP_INTERVAL_SECONDS", "900")),
        id="lifecycle_health_sweep",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    if os.getenv("LEARNING_ENABLED", "false").lower() == "true":
        scheduler.add_job(
            _run_learning_cycles,
            "interval",
            seconds=max(3600, int(os.getenv("LEARNING_INTERVAL_SECONDS", "86400"))),
            next_run_time=datetime.now(timezone.utc) + timedelta(minutes=10),
            id="paper_learning_cycle",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    scheduler.start()
    state.scheduler = scheduler


async def _run_health_sweep() -> dict[str, object]:
    """Check every live sleeve, demote what has stopped working, halt on gaps.

    Runs on a timer. Failures are logged and recorded rather than raised: a
    health check that crashes the scheduler removes the very thing that was
    watching.
    """
    try:
        result = run_health_sweep(_lifecycle(), _journal())
    except Exception as exc:  # pragma: no cover - the sweep must never crash
        logger.exception("Health sweep failed: %s", exc)
        state.last_health_sweep = {"error": str(exc)}
        return state.last_health_sweep

    state.last_health_sweep = {
        **result.to_dict(),
        "at": datetime.now(timezone.utc).isoformat(),
    }
    for demotion in result.demoted:
        await _audit(
            AuditEvent(
                event_type="lifecycle.demoted",
                symbol=demotion.split(":")[0],
                signal_id="health-sweep",
                decision="DEMOTE",
                reasoning=demotion,
                metadata={"source": "health_sweep"},
            )
        )
    return state.last_health_sweep


async def _run_learning_cycles() -> dict[str, object]:
    """Run bounded offline learning; never transition or deploy a sleeve."""
    if state.learning_running:
        return {"status": "already_running"}
    state.learning_running = True
    try:
        # The backtest stack is intentionally lazy: normal trading startup does
        # not import numpy/pandas or pay the learner's initialization cost.
        from .learning_worker import run_paper_learning_cycles

        reports = await asyncio.to_thread(
            run_paper_learning_cycles,
            _lifecycle().store,
            _journal(),
        )
        summary: dict[str, object] = {
            "status": "completed",
            "at": datetime.now(timezone.utc).isoformat(),
            "cycles": len(reports),
            "reports": reports,
        }
    except Exception as exc:
        logger.exception("Paper learning cycle failed: %s", exc)
        summary = {
            "status": "failed",
            "at": datetime.now(timezone.utc).isoformat(),
            "error_type": type(exc).__name__,
        }
    finally:
        state.learning_running = False
    state.last_learning_summary = summary
    return summary


async def _check_dependency(name: str, url: str) -> dict[str, object]:
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{url}/health")
            if resp.status_code == 200:
                return {"name": name, "status": "ok", "url": url}
            return {"name": name, "status": "degraded", "url": url, "code": resp.status_code}
    except Exception as exc:
        return {"name": name, "status": "down", "url": url, "error": str(exc)}


async def _generate_signals(config: dict[str, object]) -> dict[str, object]:
    """Ask strategy-service for one fresh decision per allowed symbol.

    The orchestrator is the default trading-loop owner, so merely polling the
    signal table is insufficient: with the strategy worker disabled there is
    nobody else to populate it. Requests are concurrent but bounded so a large
    allowlist cannot turn one cycle into an outbound connection storm.
    """
    raw = config.get("symbol_allowlist", [])
    candidates = raw if isinstance(raw, list) else []
    symbols: list[str] = []
    for value in candidates[:50]:
        try:
            symbol = sanitize_symbol(str(value))
        except Exception:
            continue
        if symbol not in symbols:
            symbols.append(symbol)

    maximum = max(1, min(10, int(os.getenv("SIGNAL_GENERATION_CONCURRENCY", "4"))))
    semaphore = asyncio.Semaphore(maximum)
    generated: list[str] = []
    errors: list[dict[str, str]] = []

    async with httpx.AsyncClient(timeout=15.0) as client:

        async def one(symbol: str) -> None:
            async with semaphore:
                try:
                    response = await client.post(
                        f"{settings.strategy_service_url}/v1/signals/generate",
                        json={"symbol": symbol},
                        headers=_internal_headers(),
                    )
                    response.raise_for_status()
                    generated.append(symbol)
                except Exception as exc:
                    logger.error("Signal generation failed for %s: %s", symbol, exc)
                    errors.append({"symbol": symbol, "error_type": type(exc).__name__})

        await asyncio.gather(*(one(symbol) for symbol in symbols))

    return {
        "requested": len(symbols),
        "generated": len(generated),
        "failed": len(errors),
        "errors": errors,
    }


async def _startup_health_check() -> None:
    deps = [
        ("strategy-service", settings.strategy_service_url),
        ("policy-service", settings.policy_service_url),
        ("execution-service", settings.execution_service_url),
        ("portfolio-service", settings.portfolio_service_url),
        ("audit-logger", settings.audit_logger_url),
    ]
    results = await asyncio.gather(*[_check_dependency(n, u) for n, u in deps])
    for result in results:
        if result["status"] == "ok":
            logger.info("Dependency %s: OK", result["name"])
        else:
            logger.warning(
                "Dependency %s: %s (%s)",
                result["name"],
                result["status"],
                result.get("error", result.get("code", "")),
            )


def _stream_symbols() -> list[str]:
    """Symbols to stream: the policy allowlist, narrowed by STREAM_SYMBOLS if set."""
    explicit = os.getenv("STREAM_SYMBOLS", "").strip()
    if explicit:
        return [s.strip().upper() for s in explicit.split(",") if s.strip()]
    try:
        config = load_policy_config(settings.policy_config_path)
        allowlist = [str(s).upper() for s in config.get("symbol_allowlist", [])]
    except Exception as exc:
        logger.warning("Could not read allowlist for streaming: %s", exc)
        return []
    # Alpaca's free IEX feed caps concurrent subscriptions; stream the head of the
    # list and let everything else resolve by polling.
    limit = int(os.getenv("STREAM_SYMBOL_LIMIT", "30"))
    return allowlist[:limit]


async def _start_price_stream() -> None:
    market_settings = _market_settings()
    cache: LivePriceCache = _price_source().cache
    state.stream_manager = StreamManager(
        settings=market_settings,
        symbols=_stream_symbols(),
        cache=cache,
    )
    try:
        await state.stream_manager.start()
    except Exception as exc:
        logger.error("Price stream failed to start: %s — falling back to polling", exc)


@asynccontextmanager
async def lifespan(app_: FastAPI):
    # State paths so tracked stops and targets survive a restart. Without
    # them every restart silently orphaned the stops of every open position —
    # the position survived at the broker, the thing watching it did not.
    state.stop_loss_monitor = StopLossMonitor(
        broker_url=settings.broker_url,
        internal_key=settings.internal_api_key,
        state_path=os.getenv("STOP_LOSS_STATE_PATH", "./stop-loss-state.json"),
    )
    state.take_profit_monitor = TakeProfitMonitor(
        broker_url=settings.broker_url,
        internal_key=settings.internal_api_key,
        state_path=os.getenv("TAKE_PROFIT_STATE_PATH", "./take-profit-state.json"),
    )
    now = datetime.now(timezone.utc)
    state.monthly_reset_month = now.month
    state.monthly_reset_year = now.year
    market_settings = _market_settings()
    logger.info(
        "Orchestrator starting: timeframe=%s intraday_minutes=%s cycle=%ss streaming=%s",
        market_settings.timeframe,
        market_settings.intraday_minutes,
        _cycle_interval_seconds(),
        market_settings.can_stream,
    )
    await _startup_health_check()
    await _start_price_stream()
    _start_scheduler()
    try:
        yield
    finally:
        if state.scheduler is not None:
            state.scheduler.shutdown(wait=False)
            state.scheduler = None
        if state.stream_manager is not None:
            await state.stream_manager.stop()
            state.stream_manager = None


app = FastAPI(title="autonomy-orchestrator", version="0.1.0", lifespan=lifespan)
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


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "autonomy-orchestrator"}


@app.get("/v1/orchestrator/realtime")
def realtime_status() -> dict[str, object]:
    """Intraday/streaming state — what resolution the loop is actually running at."""
    market_settings = _market_settings()
    stream_status: dict[str, object] = (
        state.stream_manager.status()
        if state.stream_manager is not None
        else {"enabled": market_settings.can_stream, "running": False}
    )
    source = _price_source()
    return {
        "timeframe": market_settings.timeframe,
        "pattern_day_trader": _day_trades().status(),
        "intraday": market_settings.is_intraday,
        "intraday_minutes": market_settings.intraday_minutes,
        "provider": "alpaca" if market_settings.has_alpaca_credentials else "yahoo",
        "cycle_interval_seconds": _cycle_interval_seconds(),
        "stop_loss_check_seconds": _risk_check_interval_seconds("STOP_LOSS_CHECK_INTERVAL_MINUTES"),
        "take_profit_check_seconds": _risk_check_interval_seconds(
            "TAKE_PROFIT_CHECK_INTERVAL_MINUTES"
        ),
        "max_price_age_seconds": market_settings.max_price_age_seconds,
        "stream": stream_status,
        "cached_prices": {
            symbol: {
                "price": snapshot.price,
                "age_seconds": round(snapshot.age_seconds(), 1),
                "source": snapshot.source,
            }
            for symbol in source.cache.symbols()
            if (snapshot := source.cache.peek(symbol)) is not None
        },
    }


@app.get("/v1/orchestrator/reconciliation")
async def reconciliation_status(refresh: bool = False) -> dict[str, object]:
    """Last ledger-vs-broker comparison. `refresh=true` runs a fresh check."""
    reconciler = _reconciler()
    if refresh or reconciler.last_result is None:
        result = await reconciler.check()
    else:
        result = reconciler.last_result
    return {
        **result.to_dict(),
        "entries_blocked": reconciler.entries_blocked,
        "breaks_before_halt": int(os.getenv("RECONCILE_BREAKS_BEFORE_HALT", "2")),
    }


@app.get("/v1/orchestrator/journal")
def journal_status(limit: int = 25, symbol: str | None = None) -> dict[str, object]:
    """Archive coverage and the most recent decisions, with their inputs."""
    archive = _journal()
    return {
        "archive": archive.stats(),
        "recent_decisions": archive.recent_decisions(limit=limit, symbol=symbol),
    }


@app.get("/v1/orchestrator/lifecycle")
def lifecycle_status() -> dict[str, object]:
    """The strategy roster: what is live, what is on paper, and why."""
    service = _lifecycle()
    if not service.configured:
        return {
            "available": False,
            "reason": "no LIFECYCLE_DATABASE_URL — no shared authority",
            "sleeves": [],
            "trading": [],
        }
    try:
        sleeves = service.all()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"lifecycle_unavailable: {exc}") from exc

    counts: dict[str, int] = {}
    for sleeve in sleeves:
        counts[sleeve.state] = counts.get(sleeve.state, 0) + 1
    return {
        "available": True,
        "counts": counts,
        "trading": [s.key for s in sleeves if s.state == "live"],
        "live_mode_enabled": service.store.live_mode_enabled(),
        "sleeves": [
            {
                "key": s.key,
                "strategy": s.strategy_id,
                "strategy_version": s.strategy_version,
                "symbol": s.symbol,
                "state": s.state,
                "version": s.version,
                "since": s.since.isoformat() if s.since else None,
                "reason": s.reason,
                "probation_count": s.probation_count,
                "position_environment": s.position_environment,
            }
            for s in sleeves
        ],
    }


@app.post("/v1/orchestrator/lifecycle/register")
def lifecycle_register(
    strategy: str,
    symbol: str,
    strategy_version: str = "",
    _: None = Depends(verify_internal_key),
) -> dict[str, object]:
    """Add a sleeve as a candidate. It cannot trade until it earns each step."""
    try:
        record = _lifecycle().register(
            strategy, sanitize_symbol(symbol), strategy_version=strategy_version
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"sleeve": record.key, "state": record.state, "reason": record.reason}


class PromotionRequest(BaseModel):
    """What a promotion may say. Note what is absent: any performance number.

    The caller names the sleeve and the validation runs to read. Every figure
    the gates evaluate is derived by the server from those stored artifacts and
    from the journal. This request cannot assert that a strategy is good.
    """

    model_config = ConfigDict(extra="forbid")

    artifact_ids: list[int] = Field(default_factory=list)
    """Walk-forward artifacts, for candidate -> paper."""
    correlation_artifact_id: int | None = None
    """Portfolio correlation artifact, for paper -> live."""
    paper_window_days: float | None = None


@app.post("/v1/orchestrator/lifecycle/promote")
def lifecycle_promote(
    strategy: str,
    symbol: str,
    request: PromotionRequest | None = None,
    _: None = Depends(verify_admin_key),
) -> dict[str, object]:
    """Move a sleeve up one step, if the stored evidence supports it.

    Admin-gated because the top of this ladder is real money. Every gate fails
    closed: a missing measurement is a refusal, not a pass — and the
    measurements are read from durable records, not from this request.
    """
    body = request or PromotionRequest()
    try:
        record, result = _lifecycle().promote(
            strategy,
            sanitize_symbol(symbol),
            artifact_ids=body.artifact_ids,
            correlation_artifact_id=body.correlation_artifact_id,
            paper_window_days=body.paper_window_days,
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "sleeve": record.key if record else None,
        "state": record.state if record else None,
        "promoted": result.allowed,
        "passed": result.passed,
        "failed": result.failed,
        "reason": result.reason,
    }


@app.post("/v1/orchestrator/lifecycle/demote")
def lifecycle_demote(
    strategy: str,
    symbol: str,
    to: str = "probation",
    reason: str = "manual",
    _: None = Depends(verify_internal_key),
) -> dict[str, object]:
    """Take a sleeve out of live. Never gated — safety must not need approval."""
    if to not in STATES:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown state {to!r}. Available: {', '.join(STATES)}",
        )
    try:
        record = _lifecycle().demote(strategy, sanitize_symbol(symbol), to, reason)
    except LifecycleUnavailableError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"sleeve": record.key, "state": record.state, "reason": record.reason}


@app.post("/v1/orchestrator/lifecycle/live-mode")
def lifecycle_live_mode(
    enabled: bool,
    reason: str = "",
    actor: str = "operator",
    _: None = Depends(verify_admin_key),
) -> dict[str, object]:
    """The operator switch for real-money execution.

    A database row rather than an environment variable, so it is audited, is
    shared by every process at once, and cannot be flipped by a redeploy.
    """
    service = _lifecycle()
    if not service.configured:
        raise HTTPException(status_code=503, detail="no lifecycle authority configured")
    enabled_now = service.store.set_live_mode(enabled, actor=actor, reason=reason)
    return {"live_mode_enabled": enabled_now, "actor": actor, "reason": reason}


@app.get("/v1/orchestrator/health-sweep")
def health_sweep_status() -> dict[str, object]:
    """The last scheduled health sweep, and what it did."""
    return state.last_health_sweep or {"status": "not_run_yet"}


@app.post("/v1/orchestrator/health-sweep")
async def health_sweep_now(_: None = Depends(verify_internal_key)) -> dict[str, object]:
    """Run the sweep immediately. Same code path as the scheduled one."""
    return await _run_health_sweep()


@app.post("/v1/orchestrator/learning/run")
async def learning_cycle_now(
    request: Request,
    _: None = Depends(verify_internal_key),
    _rl: None = Depends(rate_limit_write),
) -> dict[str, object]:
    """Run the same bounded learner used by the scheduler."""
    return await _run_learning_cycles()


@app.get("/v1/orchestrator/learning/status")
def learning_cycle_status() -> dict[str, object]:
    return {
        "running": state.learning_running,
        "last_summary": state.last_learning_summary,
    }


@app.get("/v1/orchestrator/learning/cycles")
def learning_cycles(
    strategy: str | None = None,
    symbol: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    _: None = Depends(verify_internal_key),
) -> dict[str, object]:
    """Immutable learning audits, newest first."""
    service = _lifecycle()
    if not service.configured or service.store is None:
        raise HTTPException(status_code=503, detail="no lifecycle authority configured")
    clean_symbol = sanitize_symbol(symbol) if symbol else None
    return {
        "cycles": service.store.learning_cycles(
            strategy_id=strategy,
            symbol=clean_symbol,
            limit=limit,
        )
    }


@app.get("/v1/orchestrator/learning/curve")
def learning_curve(
    strategy: str | None = None,
    symbol: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    _: None = Depends(verify_internal_key),
) -> dict[str, object]:
    """Chronological evidence and evaluation metrics for each learning cycle."""
    from .learning_view import build_learning_curve

    service = _lifecycle()
    if not service.configured or service.store is None:
        raise HTTPException(status_code=503, detail="no lifecycle authority configured")
    clean_symbol = sanitize_symbol(symbol) if symbol else None
    cycles = service.store.learning_cycles(
        strategy_id=strategy,
        symbol=clean_symbol,
        limit=limit,
    )
    return {"points": build_learning_curve(cycles)}


@app.get("/v1/orchestrator/learning/proposals")
def learning_proposals(
    strategy: str | None = None,
    symbol: str | None = None,
    survived: bool | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    _: None = Depends(verify_internal_key),
) -> dict[str, object]:
    """Bounded challenger proposals for operator review."""
    service = _lifecycle()
    if not service.configured or service.store is None:
        raise HTTPException(status_code=503, detail="no lifecycle authority configured")
    clean_symbol = sanitize_symbol(symbol) if symbol else None
    rows = service.store.challenger_proposals(
        strategy_id=strategy,
        symbol=clean_symbol,
        account_id=settings.account_id,
        limit=limit,
    )
    if survived is not None:
        rows = [row for row in rows if row["survived"] is survived]
    return {"proposals": rows}


class StartPaperChallengerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=500)


@app.post("/v1/orchestrator/learning/proposals/{proposal_id}/paper")
def start_paper_challenger(
    proposal_id: int,
    body: StartPaperChallengerRequest,
    _: None = Depends(verify_admin_key),
    _rl: None = Depends(rate_limit_write),
) -> dict[str, object]:
    """Start a qualified proposal in paper; never adopts or promotes it."""
    try:
        sleeve = _lifecycle().start_paper_challenger(
            proposal_id,
            actor=body.actor,
            reason=body.reason,
            account_id=settings.account_id,
        )
    except LifecycleUnavailableError as exc:
        raise HTTPException(
            status_code=503,
            detail="lifecycle authority unavailable",
        ) from exc
    except LifecycleStoreError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("Paper challenger activation failed: lifecycle unavailable")
        raise HTTPException(
            status_code=503,
            detail="lifecycle authority unavailable",
        ) from exc
    return {
        "proposal_id": proposal_id,
        "sleeve": sleeve.key,
        "strategy_version": sleeve.strategy_version,
        "state": sleeve.state,
        "origin": sleeve.origin,
        "live_eligible": False,
    }


@app.get("/v1/orchestrator/learning/proposals/{proposal_id}/comparison")
def paper_challenger_comparison(
    proposal_id: int,
    window_days: float = Query(default=30.0, gt=0.0, le=3650.0),
    _: None = Depends(verify_internal_key),
) -> dict[str, object]:
    """Champion and challenger paper outcomes over the same time window."""
    from challengers import compare, derived_strategy_id

    service = _lifecycle()
    if not service.configured or service.store is None:
        raise HTTPException(status_code=503, detail="no lifecycle authority configured")
    proposals = service.store.challenger_proposals(
        proposal_id=proposal_id,
        account_id=settings.account_id,
        limit=1,
    )
    if not proposals:
        raise HTTPException(status_code=404, detail="challenger proposal not found")
    proposal = proposals[0]
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=window_days)
    return compare(
        _journal(),
        symbol=str(proposal["symbol"]),
        champion_strategy_id=str(proposal["strategy_id"]),
        challenger_strategy_id=derived_strategy_id(
            str(proposal["strategy_id"]),
            str(proposal["challenger_id"]),
        ),
        account_id=settings.account_id,
        window_start=start,
        window_end=end,
        environment="paper",
    ).to_dict()


@app.get("/v1/orchestrator/day-trades")
def day_trade_status() -> dict[str, object]:
    """Day-trade budget under the US pattern-day-trader rule."""
    return _day_trades().status()


@app.get("/v1/orchestrator/status")
def status() -> dict[str, object]:
    config = load_policy_config(settings.policy_config_path)
    return {
        "running": state.running,
        "last_cycle_time": state.last_cycle_time,
        "trades_today": state.trades_today,
        "weekly_notional_used": state.weekly_notional_used,
        "weekly_notional_cap_usd": config.get("weekly_notional_cap_usd", 0.0),
        "kill_switch": config.get("kill_switch", False),
        "trading_mode": config.get("trading_mode", "demo"),
    }


@app.get("/v1/orchestrator/client-config")
def client_config() -> dict[str, str]:
    """Return only non-secret dashboard capability metadata."""
    return {
        "browserReceivesSecrets": "false",
        "mutationsRequireAuthenticatedOperator": "true",
    }


@app.get("/v1/orchestrator/cycle/last")
def last_cycle() -> dict[str, object]:
    return state.last_cycle_summary


@app.post("/v1/orchestrator/cycle/trigger")
async def trigger_cycle(
    _: None = Depends(verify_internal_key),
) -> dict[str, object]:
    """Manually trigger a cycle (bypasses market hours gate — for testing)."""
    from .policy_config import load_policy_config

    config = load_policy_config(settings.policy_config_path)
    if config.get("kill_switch"):
        return {"status": "halted", "reason": "kill_switch_active"}
    if state.running:
        return {"status": "busy", "reason": "cycle_already_running"}
    return await run_cycle()


@app.get("/v1/orchestrator/health/deps")
async def health_deps() -> list[dict[str, object]]:
    """Returns per-dependency health status."""
    deps = [
        ("strategy-service", settings.strategy_service_url),
        ("policy-service", settings.policy_service_url),
        ("execution-service", settings.execution_service_url),
        ("portfolio-service", settings.portfolio_service_url),
        ("audit-logger", settings.audit_logger_url),
    ]
    results = await asyncio.gather(*[_check_dependency(n, u) for n, u in deps])
    return list(results)


@app.post("/v1/orchestrator/validate")
async def validate_allowlist(_: None = Depends(verify_internal_key)) -> dict[str, list[str]]:
    result = await _validate_allowlist_symbols()
    await _audit(
        AuditEvent(
            event_type="orchestrator.validation",
            decision="CHECKED",
            reasoning="allowlist validation",
            metadata=result,
        )
    )
    return result


@app.post("/v1/orchestrator/kill-switch")
def toggle_kill_switch(
    request: KillSwitchRequest,
    _: None = Depends(verify_admin_key),
    _rl: None = Depends(rate_limit_write),
) -> dict[str, object]:
    config = update_policy_config(settings.policy_config_path, {"kill_switch": request.active})
    return {"kill_switch": config.get("kill_switch", False)}


@app.post("/v1/orchestrator/live-mode")
async def live_mode(
    request: LiveModeRequest,
    raw_request: Request,
    _: None = Depends(verify_admin_key),
    _rl: None = Depends(rate_limit_write),
) -> dict[str, object]:
    config = load_policy_config(settings.policy_config_path)
    if request.enable:
        if request.confirmation != "I CONFIRM LIVE TRADING":
            raise HTTPException(status_code=400, detail="Confirmation string mismatch")
        if config.get("kill_switch"):
            raise HTTPException(
                status_code=400, detail="Kill switch must be off before enabling live mode"
            )
        if not config.get("weekly_notional_cap_usd"):
            raise HTTPException(
                status_code=400, detail="Weekly cap must be set before enabling live mode"
            )
        if not config.get("symbol_allowlist"):
            raise HTTPException(
                status_code=400,
                detail="Symbol allowlist must be non-empty before enabling live mode",
            )
        update_policy_config(settings.policy_config_path, {"trading_mode": "live"})
    else:
        update_policy_config(settings.policy_config_path, {"trading_mode": "demo"})
    await _audit(
        AuditEvent(
            event_type="orchestrator.live_mode",
            decision="LIVE" if request.enable else "DEMO",
            reasoning="live mode toggle",
            metadata={"ip": getattr(raw_request.client, "host", None), "enabled": request.enable},
        )
    )
    return {"trading_mode": "live" if request.enable else "demo"}


async def run_cycle() -> dict[str, object]:
    if _trading_loop_owner() != "orchestrator":
        return {
            "status": "disabled",
            "reason": "trading_loop_owned_by_strategy",
        }
    config = load_policy_config(settings.policy_config_path)
    if config.get("kill_switch"):
        state.last_cycle_summary = {"status": "halted", "reason": "kill_switch_active"}
        return state.last_cycle_summary

    state.running = True
    state.last_cycle_time = datetime.now(timezone.utc)
    summary: dict[str, Any] = {
        "status": "completed",
        "started_at": state.last_cycle_time,
        "approved": 0,
        "rejected": 0,
        "review": 0,
        "executed": 0,
        "signals": 0,
    }
    try:
        # Inside the try, not before it: everything between `state.running =
        # True` and this block runs outside the finally that resets the flag,
        # so any exception there wedges every future cycle as "busy".
        weekly_spend = await _weekly_spend_safe()
        state.weekly_notional_used = weekly_spend
        if not state.allowlist_validation_ran:
            validation = await _validate_allowlist_symbols()
            state.allowlist_validation_ran = True
            if validation["invalid"] or validation["unknown"]:
                for bucket in ("invalid", "unknown"):
                    for symbol in validation[bucket]:
                        logger.warning("Allowlist validation %s: %s", bucket, symbol)
        await _process_approvals()
        if not _monthly_limits_ok():
            return summary
        signals = await _pending_signals()
        if signals is None:
            summary["generation"] = {
                "status": "skipped_signal_queue_unavailable",
            }
            signals = []
        elif signals:
            summary["generation"] = {
                "status": "skipped_pending_backlog",
                "pending": len(signals),
            }
        else:
            summary["generation"] = await _generate_signals(config)
            signals = await _pending_signals() or []
        portfolio_state = await _portfolio_state()

        # Pattern-day-trader budget. The equity lookup is a network call so it
        # is done once, but the decision is re-taken per signal: every accepted
        # entry increments open_today, which the budget is gated on.
        account_equity = await _account_equity()
        # A ledger that disagrees with the broker is not a safe basis for a new
        # position. Exits stay enabled: refusing to close something we cannot
        # account for is worse than closing it.
        reconciliation = await _reconciler().check()
        summary["reconciliation"] = {
            "ok": reconciliation.ok,
            "breaks": len(reconciliation.breaks),
            "halted": reconciliation.halted,
        }
        if reconciliation.halted:
            _journal().record_decision(
                stage="reconcile",
                outcome="rejected",
                reason="position break persisted — new entries blocked",
                inputs=reconciliation.to_dict(),
            )
            await _notify_smart(
                "reconciliation_break",
                "\u26d4 Broker and ledger disagree — new entries paused. Exits unaffected.",
                tier=3,
            )

        pdt = _day_trades().check_entry(account_equity)
        summary["pdt"] = {
            "allowed": pdt.allowed,
            "reason": pdt.reason,
            "day_trades_used": pdt.day_trades_used,
            "day_trades_remaining": pdt.day_trades_remaining,
        }
        if not pdt.allowed:
            logger.warning("New entries blocked: %s", pdt.reason)
            await _notify_smart(
                "pdt_limit_reached",
                f"\u26d4 New entries paused — {pdt.reason}. Exits are unaffected.",
                tier=2,
            )

        for signal in signals:
            summary["signals"] += 1
            if signal.candidate_action == "HOLD":
                summary["held"] = summary.get("held", 0) + 1
                await _mark_signal_acted(signal.signal_id)
                continue
            if reconciliation.halted:
                summary["rejected"] += 1
                await _mark_signal_acted(signal.signal_id)
                continue
            pdt = _day_trades().check_entry(account_equity)
            if not pdt.allowed:
                summary["rejected"] += 1
                _journal().record_decision(
                    stage="pdt",
                    outcome="rejected",
                    symbol=signal.symbol,
                    action=str(signal.candidate_action),
                    reason=pdt.reason,
                    inputs={
                        "day_trades_used": pdt.day_trades_used,
                        "open_today": pdt.open_today,
                        "equity": pdt.equity,
                    },
                    correlation_id=signal.signal_id,
                )
                await _audit(
                    AuditEvent(
                        event_type="signal.rejected",
                        symbol=signal.symbol,
                        signal_id=signal.signal_id,
                        decision="REJECT",
                        reasoning=pdt.reason,
                        metadata={"guard": "pattern_day_trader"},
                    )
                )
                await _mark_signal_acted(signal.signal_id)
                continue
            price_bars = _fetch_price_bars(signal.symbol)
            risk = evaluate_risk(
                signal, portfolio_state, weekly_spend, config, price_bars=price_bars
            )
            if not risk.approved:
                summary["rejected"] += 1
                _journal().record_decision(
                    stage="risk",
                    outcome="rejected",
                    symbol=signal.symbol,
                    action=str(signal.candidate_action),
                    reason=risk.reason,
                    inputs={
                        "size_pct": signal.size_pct,
                        "confidence": signal.confidence,
                        "risk_score": signal.risk_score,
                        "bars": len(price_bars or []),
                    },
                    outputs={"tier": risk.tier},
                    correlation_id=signal.signal_id,
                )
                await _audit(
                    AuditEvent(
                        event_type="signal.rejected",
                        symbol=signal.symbol,
                        signal_id=signal.signal_id,
                        decision="REJECT",
                        reasoning=risk.reason,
                        metadata={"tier": risk.tier},
                    )
                )
                await _mark_signal_acted(signal.signal_id)
                continue
            policy = await _policy_evaluate(signal, risk, config, portfolio_state)
            decision = policy.get("decision", "REJECT")
            if risk.tier >= 1:
                await _notify(signal, risk, policy)
            if decision == "APPROVE" and risk.tier < 3:
                # The roster decides what may reach the broker. A sleeve that
                # is validated but still on paper runs the whole pipeline and
                # records its decision — that recorded history is exactly the
                # evidence its promotion to live is gated on.
                strategy_name = _strategy_of(signal)
                gate = _lifecycle_gate(signal)
                if gate is not None:
                    summary["paper_only"] = summary.get("paper_only", 0) + 1
                    _journal().record_decision(
                        stage="lifecycle_gate",
                        outcome="not_traded",
                        symbol=signal.symbol,
                        action=_side_of(signal.candidate_action),
                        reason=gate,
                        inputs={
                            "strategy": strategy_name,
                            "confidence": signal.confidence,
                            "adjusted_size_pct": risk.adjusted_size_pct,
                        },
                        outputs={"would_have_traded": True},
                        correlation_id=signal.signal_id,
                    )
                    await _audit(
                        AuditEvent(
                            event_type="signal.paper_only",
                            symbol=signal.symbol,
                            signal_id=signal.signal_id,
                            decision="PAPER",
                            reasoning=gate,
                            metadata={"strategy": strategy_name},
                        )
                    )
                    await _mark_signal_acted(signal.signal_id)
                    continue

                order = await _submit_order(signal, risk, config, portfolio_state, price_bars)
                if not _order_accepted(order):
                    # execution-service answers 200 with status REJECTED (no cash,
                    # qty limit). Recording an open here would burn a day-trade
                    # reservation on a position that does not exist.
                    summary["rejected"] += 1
                    await _audit(
                        AuditEvent(
                            event_type="signal.rejected",
                            symbol=signal.symbol,
                            signal_id=signal.signal_id,
                            decision="REJECT",
                            reasoning=str(order.get("rejection_reason") or "broker_rejected"),
                            metadata={"order": order},
                        )
                    )
                    await _mark_signal_acted(signal.signal_id)
                    continue
                _day_trades().record_open(signal.symbol)
                _journal().record_decision(
                    stage="order",
                    outcome="executed",
                    symbol=signal.symbol,
                    action=_side_of(signal.candidate_action),
                    reason="order_submitted",
                    inputs={
                        "confidence": signal.confidence,
                        "risk_score": signal.risk_score,
                        "adjusted_size_pct": risk.adjusted_size_pct,
                        "policy_decision": policy.get("decision"),
                        "research": (signal.research_summary or "")[:400],
                    },
                    outputs={
                        "qty": order.get("qty"),
                        "entry_price": order.get("entry_price"),
                        "amount_usd": order.get("amount_usd"),
                        "order_id": order.get("order_id"),
                    },
                    correlation_id=signal.signal_id,
                )
                _register_stop_loss(
                    signal.symbol,
                    order,
                    price_bars,
                    side=_side_of(signal.candidate_action),
                    strategy_id=_strategy_of(signal),
                )
                _register_take_profit(signal, order)
                weekly_spend += float(order.get("amount_usd", 0.0))
                state.weekly_notional_used = weekly_spend
                state.trades_today += 1
                summary["approved"] += 1
                summary["executed"] += 1
                await _audit(
                    AuditEvent(
                        event_type="trade.executed",
                        symbol=signal.symbol,
                        signal_id=signal.signal_id,
                        decision="APPROVE",
                        reasoning="order_submitted",
                        metadata=order,
                    )
                )
                await _notify_trade_executed(signal, order)
                await _mark_signal_acted(signal.signal_id)
            elif decision == "REVIEW" or risk.tier >= 3:
                summary["review"] += 1
                await _approval(signal, risk, policy)
                await _audit(
                    AuditEvent(
                        event_type="signal.review",
                        symbol=signal.symbol,
                        signal_id=signal.signal_id,
                        decision="PENDING",
                        reasoning="approval_required",
                        metadata={"tier": risk.tier, "policy": policy},
                    )
                )
                await _mark_signal_acted(signal.signal_id)
            else:
                summary["rejected"] += 1
                await _audit(
                    AuditEvent(
                        event_type="signal.rejected",
                        symbol=signal.symbol,
                        signal_id=signal.signal_id,
                        decision=decision,
                        reasoning="; ".join(policy.get("reasons", [])),
                        metadata={"tier": risk.tier},
                    )
                )
                await _mark_signal_acted(signal.signal_id)
        closed = await _process_exit_signals()
        summary["closed"] = closed
        summary["executed"] += closed
        if config.get("close_positions_eod", False):
            now_utc = datetime.now(timezone.utc)
            # 19:30-19:45 UTC = approx 15:30-15:45 ET (during EDT)
            if now_utc.hour == 19 and 30 <= now_utc.minute <= 45:
                logger.warning("EOD position closure triggered")
                summary["executed"] += await _process_exit_signals()
    except RuntimeError as exc:
        summary["status"] = "halted"
        summary["reason"] = str(exc)
        state.last_cycle_summary = summary
        state.running = False
        return summary
    finally:
        summary["completed_at"] = datetime.now(timezone.utc)
        state.last_cycle_summary = summary
        state.running = False
        await _audit(
            AuditEvent(
                event_type="orchestrator.cycle",
                decision="SUMMARY",
                reasoning="cycle completed",
                metadata=summary,
            )
        )
    return summary


async def _pending_signals() -> list[SignalCandidate] | None:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                f"{settings.strategy_service_url}/v1/signals",
                params={
                    "limit": 100,
                    "acted_on": "false",
                    "entry_only": "true",
                    "oldest_first": "true",
                },
                headers=_internal_headers(),
            )
            response.raise_for_status()
    except Exception as exc:
        # A signal source that is down means no entries this cycle — it must
        # not also mean no exit processing, which runs later in the same
        # cycle and used to be skipped when this raised.
        logger.error("Strategy service unreachable for pending signals: %s", exc)
        return None
    signals: list[SignalCandidate] = []
    for item in response.json():
        try:
            item["symbol"] = sanitize_symbol(str(item.get("symbol", "")))
        except Exception:
            logger.warning("Skipping malformed signal symbol: %r", item.get("symbol"))
            continue
        signals.append(SignalCandidate.model_validate(item))
    return signals


async def _pending_exit_signals() -> list[SignalCandidate]:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                f"{settings.strategy_service_url}/v1/signals",
                params={
                    "limit": 100,
                    "acted_on": "false",
                    "candidate_action": "EXIT",
                    "oldest_first": "true",
                },
                headers=_internal_headers(),
            )
            response.raise_for_status()
    except Exception as exc:
        logger.error("Strategy service unreachable for exit signals: %s", exc)
        return []
    signals: list[SignalCandidate] = []
    for item in response.json():
        try:
            item["symbol"] = sanitize_symbol(str(item.get("symbol", "")))
        except Exception:
            logger.warning("Skipping malformed signal symbol: %r", item.get("symbol"))
            continue
        signals.append(SignalCandidate.model_validate(item))
    return signals


async def _portfolio_state() -> dict[str, object]:
    positions: list = []
    account: dict = {"buying_power": 100_000.0}
    # Unreachable falls back exactly as a non-200 does. When the execution
    # service itself is down, the defaulted buying power cannot buy anything
    # anyway — order submission fails closed against the same dead service.
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            positions_resp = await client.get(
                f"{settings.portfolio_service_url}/v1/portfolio/positions",
                headers=_internal_headers(),
            )
            account_resp = await client.get(
                f"{settings.execution_service_url}/v1/account",
                headers=_internal_headers(),
            )
        if positions_resp.status_code == 200:
            positions = positions_resp.json()
        if account_resp.status_code == 200:
            account = account_resp.json()
    except Exception as exc:
        logger.error("Portfolio state unreachable: %s", exc)
    return {
        "positions": positions,
        "buying_power": float(account.get("buying_power", 100_000.0)),
        "daily_drawdown_pct": 0.0,
    }


async def _policy_evaluate(
    signal: SignalCandidate, risk, config: dict[str, object], portfolio_state: dict[str, object]
) -> dict[str, object]:
    # The gate reaches yfinance synchronously; on a thread so a slow calendar
    # cannot stall the cycle's event loop (and with it every risk job).
    event_blackout = await asyncio.to_thread(_earnings_blackout_for, signal.symbol)

    request = PolicyEvaluationRequest(
        signal_id=signal.signal_id,
        symbol=signal.symbol,
        candidate_action=signal.candidate_action,
        confidence=signal.confidence,
        size_pct=risk.adjusted_size_pct or signal.size_pct,
        market_context={
            "data_age_seconds": (
                max(
                    0,
                    int(
                        (
                            datetime.now(timezone.utc)
                            - (
                                signal.ta_summary.as_of
                                if signal.ta_summary is not None
                                else datetime.fromtimestamp(0, tz=timezone.utc)
                            )
                        ).total_seconds()
                    ),
                )
            ),
            "market_open": is_market_hours(config),
            "event_blackout_active": event_blackout,
            "liquidity_score": 0.95,
            "symbol_allowed": signal.symbol.upper()
            in {str(sym).upper() for sym in config.get("symbol_allowlist", [])},
        },
        portfolio_context={
            "gross_exposure_pct": min(len(portfolio_state.get("positions", [])) / 10.0, 1.0),
            "daily_drawdown_pct": float(portfolio_state.get("daily_drawdown_pct", 0.0)),
        },
        risk_score=signal.risk_score,
    )
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(
                f"{settings.policy_service_url}/v1/policy/evaluate",
                json=request.model_dump(mode="json"),
                headers=_internal_headers(),
            )
            response.raise_for_status()
            return response.json()
    except Exception as exc:
        logger.critical("Policy service unreachable: %s — halting cycle (fail closed)", exc)
        raise RuntimeError(f"Policy service unreachable: {exc}") from exc


async def _submit_order(
    signal: SignalCandidate,
    risk,
    config: dict[str, object],
    portfolio_state: dict[str, object],
    price_bars: list[Any] | None = None,
) -> dict[str, object]:
    buying_power = float(portfolio_state.get("buying_power", 100_000.0))
    amount_usd = round(buying_power * risk.adjusted_size_pct, 2)
    current_price = await _get_quote_price(signal.symbol)
    # Sizing must divide by the real price. The hardcoded 100.0 that used to be
    # here was only ever consistent with the old paper broker's flat $100 fill;
    # against a real quote a $5,000 target in a $500 stock became a $25,000
    # position. The same bug was fixed in strategy-service's _compute_qty.
    if current_price is None or current_price <= 0:
        logger.warning("No quote for %s — refusing to size an order", signal.symbol)
        return {"status": "REJECTED", "rejection_reason": "no_quote_for_sizing"}
    qty = int(amount_usd / current_price)

    # Trim to a share of the symbol's volume. Being a large fraction of what
    # trades moves the price against you, so the cost of the order becomes a
    # function of its own size — the effect that punishes thin small caps.
    max_participation = float(os.getenv("MAX_ADV_PARTICIPATION", "0.01"))
    adv = average_daily_volume(price_bars or [], bars_per_day=_bars_per_day())
    capped = participation_capped_qty(qty, adv, max_participation)
    if capped < qty:
        logger.info(
            "Order for %s trimmed %d -> %d shares (%.1f%% of ~%.0f ADV)",
            signal.symbol,
            qty,
            capped,
            max_participation * 100,
            adv or 0,
        )
        qty = capped

    if qty < 1:
        logger.info(
            "Order for %s sizes to 0 shares ($%.2f at %.4f) — skipping",
            signal.symbol,
            amount_usd,
            current_price,
        )
        return {"status": "REJECTED", "rejection_reason": "qty_below_one_share"}

    # A marketable limit with IOC: fills now at or inside the limit, or not at
    # all. A market order would accept whatever the book offers, which on a
    # spike is exactly the fill the strategy did not assume.
    order_type, time_in_force, limit_price = "MARKET", "DAY", None
    if os.getenv("USE_LIMIT_ORDERS", "true").lower() == "true":
        tolerance = float(os.getenv("LIMIT_TOLERANCE_BPS", "10"))
        limit_price = marketable_limit_price(
            current_price, _side_of(signal.candidate_action), tolerance
        )
        if limit_price is not None:
            order_type, time_in_force = "LIMIT", "IOC"

    stop_loss_pct = float(config.get("stop_loss_pct", 0.03))
    take_profit_pct = float(config.get("take_profit_pct", 0.06))
    stop_loss_rate = current_price * (1 - stop_loss_pct) if current_price is not None else None
    take_profit_rate = current_price * (1 + take_profit_pct) if current_price is not None else None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{settings.execution_service_url}/v1/orders",
                json={
                    "signal_id": signal.signal_id,
                    "symbol": signal.symbol,
                    "side": signal.candidate_action,
                    "qty": qty,
                    "order_type": order_type,
                    "time_in_force": time_in_force,
                    "limit_price": limit_price,
                    "decision_price": current_price,
                    "stop_loss_rate": stop_loss_rate,
                    "take_profit_rate": take_profit_rate,
                    "strategy_id": _strategy_of(signal),
                    "account_id": settings.account_id,
                },
                headers={
                    "Idempotency-Key": f"orchestrator-{signal.signal_id}",
                    **_internal_headers(),
                },
            )
            response.raise_for_status()
    except Exception as exc:
        # The caller already has a path for a broker that says no; a broker
        # that cannot be reached takes the same one. Raising here killed the
        # rest of the cycle — the remaining signals and the exit pass. The
        # idempotency key is derived from the signal id, so a retry on a
        # later cycle replays rather than double-fills.
        logger.error("Execution service unreachable for %s: %s", signal.symbol, exc)
        return {"status": "REJECTED", "rejection_reason": f"execution_unreachable: {exc}"}
    body = response.json()
    body["amount_usd"] = amount_usd
    body["entry_price"] = current_price
    body["qty"] = qty
    body["trading_mode"] = config.get("trading_mode", "demo")
    return body


def _strategy_of(signal: SignalCandidate) -> str:
    """Which rule produced this signal.

    Older producers do not set the field; they are all the momentum rule, which
    is what the default names. Guessing the wrong strategy here would gate a
    sleeve against another sleeve's roster entry.
    """
    return getattr(signal, "strategy", None) or DEFAULT_LIVE_STRATEGY


def _earnings_blackout_for(symbol: str) -> bool:
    """The earnings gate's verdict, honouring the operator's failure posture.

    The gate itself handles an unanswerable calendar per
    EARNINGS_GATE_FAIL_CLOSED. This wrapper covers the failures *outside* it —
    the cross-service import missing from this container, or the module
    raising before any posture applies. A blanket `except: False` here
    silently failed OPEN for an operator who had configured the gate to fail
    CLOSED, and swallowed the gate's own refusal of a garbage config value.
    """
    try:
        from strategy_service.earnings_calendar import check_earnings_blackout

        return check_earnings_blackout(symbol).active
    except ValueError:
        # A garbage EARNINGS_GATE_FAIL_CLOSED is refused, not defaulted around
        # — the gate's documented contract.
        raise
    except Exception as exc:
        fail_closed = os.getenv("EARNINGS_GATE_FAIL_CLOSED", "false").strip().lower() == "true"
        logger.error(
            "Earnings gate unavailable for %s (%s) — failing %s per EARNINGS_GATE_FAIL_CLOSED",
            symbol,
            exc,
            "CLOSED" if fail_closed else "open",
        )
        return fail_closed


def _lifecycle_gate(signal: SignalCandidate) -> str | None:
    """Why this signal may not reach the broker, or None if it may.

    Advisory: execution-service resolves and enforces the route regardless.
    Asking here means a refused signal gets journalled with the context the
    orchestrator has — confidence, sizing, the risk tier — instead of arriving
    downstream stripped of it.

    This used to consult a per-process JSON registry loaded once at boot, so
    the orchestrator could believe a sleeve was live minutes after it had been
    demoted elsewhere. It now reads the shared authority, and an unreachable
    authority is a refusal rather than a pass.
    """
    answer = _lifecycle().may_open(_strategy_of(signal), signal.symbol)
    if answer.permitted:
        return None
    if not answer.available:
        logger.error(
            "Lifecycle authority unavailable (%s) — refusing to open %s",
            answer.reason,
            signal.symbol,
        )
    return answer.reason


def _lifecycle() -> LifecycleService:
    """The shared authority. Reconnects on demand rather than caching a failure."""
    if state.lifecycle is None or not state.lifecycle.configured:
        state.lifecycle = get_lifecycle_service()
    return state.lifecycle


def _day_trades() -> DayTradeTracker:
    if state.day_trades is None:
        state.day_trades = DayTradeTracker()
    return state.day_trades


async def _account_equity() -> float | None:
    """Account equity, or None if it cannot be read.

    None is not treated as "fine": the PDT guard assumes an unknown balance is
    below the threshold, because approving on a guess is how an account gets
    flagged.
    """
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(
                f"{settings.execution_service_url}/v1/account",
                headers=_internal_headers(),
            )
            if response.status_code == 200:
                return float(response.json().get("equity"))
    except Exception as exc:
        logger.warning("Could not read account equity for the PDT check: %s", exc)
    return None


def _side_of(action: object) -> str:
    """The order side as a plain string.

    CandidateAction is a (str, Enum), and in Python 3.11 str() on such a member
    yields "CandidateAction.SELL" rather than "SELL". Storing that made every
    downstream direction check compare against a value that could never match,
    so shorts silently kept using long-position logic.
    """
    return str(getattr(action, "value", action)).upper()


def _reconciler() -> Reconciler:
    if state.reconciler is None:
        state.reconciler = Reconciler(
            execution_url=settings.execution_service_url,
            portfolio_url=settings.portfolio_service_url,
            internal_key=settings.internal_api_key,
        )
    return state.reconciler


def _journal():
    """Process-wide decision journal; a stub if journalling is unavailable."""
    from journal import get_journal

    return get_journal()


def _bars_per_day() -> float:
    """Bars in a session at the configured resolution, for scaling volume."""
    market_settings = _market_settings()
    if not market_settings.is_intraday:
        return 1.0
    return max(1.0, 390.0 / max(1, market_settings.intraday_minutes))


def _order_accepted(order: dict[str, object]) -> bool:
    """Whether the broker actually took the order.

    execution-service returns HTTP 200 for a rejected order with the reason in
    the body, so the status has to be read rather than inferred from the
    response code.
    """
    status = str(order.get("status", "")).upper()
    return status not in ("REJECTED", "CANCELLED")


def _market_settings() -> MarketDataSettings:
    if state.market_settings is None:
        state.market_settings = MarketDataSettings()
    return state.market_settings


def _price_source() -> RealtimePriceSource:
    """Shared price resolver — reads the stream cache first, then polls."""
    if state.price_source is None:
        state.price_source = RealtimePriceSource(_market_settings())
    return state.price_source


def _fetch_price_bars(symbol: str) -> list[Any] | None:
    """Bars at the configured timeframe. ATR-based stops depend on this matching
    the trading horizon: daily ATR on an intraday strategy sets stops far too wide."""
    try:
        return fetch_bars(symbol.upper(), _market_settings())
    except Exception as exc:
        logger.warning("Unable to fetch price bars for %s: %s", symbol, exc)
        return None


def _register_stop_loss(
    symbol: str,
    order: dict[str, object],
    price_bars: list[Any] | None,
    side: str = "BUY",
    strategy_id: str = "",
) -> None:
    if state.stop_loss_monitor is None:
        return

    entry_price = float(order.get("entry_price", 0.0))
    if entry_price <= 0.0:
        logger.warning("Skipping stop registration for %s: missing entry price", symbol)
        return

    # A short is stopped out by a RISE, so its stop sits above the entry. Placing
    # it below (as the long formula does) puts the stop on the wrong side of the
    # market: the direction-aware monitor would fire it on the first check,
    # closing every short the moment it opened.
    is_short = side.upper() == "SELL"
    distance = entry_price * 0.02
    if price_bars:
        highs = [float(bar.high) for bar in price_bars]
        lows = [float(bar.low) for bar in price_bars]
        closes = [float(bar.close) for bar in price_bars]
        atr = compute_atr(highs, lows, closes)
        if atr > 0.0:
            distance = atr * 2.0
    stop_price = entry_price + distance if is_short else entry_price - distance

    state.stop_loss_monitor.register(
        StopLossRecord(
            symbol=symbol,
            entry_price=entry_price,
            stop_price=stop_price,
            position_id=str(order.get("external_order_id") or order.get("order_id") or symbol),
            qty=float(order.get("qty", 0.0)),
            side=side,
            strategy_id=strategy_id,
            account_id=settings.account_id,
            created_at=datetime.now(timezone.utc),
        )
    )


def _register_take_profit(signal: SignalCandidate, order: dict[str, object]) -> None:
    entry_price = float(order.get("entry_price", 0.0))
    qty = float(order.get("qty", 0.0))
    if entry_price <= 0.0 or state.take_profit_monitor is None:
        return

    # Mirror image: a short profits as price FALLS, so its target sits below the
    # entry. An above-entry target is already satisfied at the moment of entry.
    is_short = _side_of(signal.candidate_action) == "SELL"
    gain_per_share = settings.take_profit_target_usd / qty if qty > 0 else entry_price * 0.06
    target_price = entry_price - gain_per_share if is_short else entry_price + gain_per_share
    state.take_profit_monitor.register(
        TakeProfitRecord(
            strategy_id=_strategy_of(signal),
            account_id=settings.account_id,
            symbol=signal.symbol,
            entry_price=entry_price,
            target_price=target_price,
            position_id=str(
                order.get("external_order_id") or order.get("order_id") or signal.symbol
            ),
            qty=qty,
            side=_side_of(signal.candidate_action),
            target_gain_usd=settings.take_profit_target_usd,
            created_at=datetime.now(timezone.utc),
        )
    )


def _realized_pnl(record, exit_price: float | None) -> float | None:
    """P&L on a closed position, or None when it cannot be determined.

    Direction matters. A short profits when price falls, so the long-only
    formula inverts its sign: a losing short would be booked as monthly profit
    and could carry the account straight past the loss limit.

    A record registered with qty=0 means "close whatever is open at the broker",
    so the size is unknown here and no P&L can be attributed.
    """
    if record is None or exit_price is None:
        return None
    qty = float(getattr(record, "qty", 0.0) or 0.0)
    entry = float(getattr(record, "entry_price", 0.0) or 0.0)
    if qty <= 0.0 or entry <= 0.0:
        return None
    direction = -1.0 if str(getattr(record, "side", "BUY")).upper() == "SELL" else 1.0
    return (exit_price - entry) * qty * direction


async def _run_reconciliation() -> None:
    """Persist the broker-vs-ledger latch in the shared lifecycle authority."""
    result = await _reconciler().check()
    store = _lifecycle().store
    if store is not None:
        try:
            live = store.live_mode_enabled(settings.account_id)
            environment = "live" if live else "paper"
            durable = store.record_reconciliation(
                broker=environment,
                environment=environment,
                ok=result.ok,
                breaks=len(result.breaks),
                error=result.error or "",
                dependency_available=result.error is None,
                account_id=settings.account_id,
                now=result.checked_at,
            )
            result.consecutive_breaks = durable.consecutive_breaks
            result.halted = durable.halted
        except Exception as exc:
            logger.exception("Could not persist reconciliation state: %s", exc)

    if result.breaks or result.error:
        await _audit(
            AuditEvent(
                event_type="reconciliation.break",
                decision="ALERT",
                reasoning=(
                    "; ".join(b.describe() for b in result.breaks[:5])
                    or f"dependency_unavailable: {result.error}"
                ),
                metadata=result.to_dict(),
            )
        )
        _journal().record_decision(
            stage="reconcile",
            outcome="rejected" if result.halted else "skipped",
            reason=(
                f"{len(result.breaks)} position break(s)"
                if result.breaks
                else f"dependency unavailable: {result.error}"
            ),
            inputs=result.to_dict(),
        )


def _clear_risk_records(record: object) -> None:
    """Remove only the sibling protection for the position that closed."""
    for monitor in (state.stop_loss_monitor, state.take_profit_monitor):
        if monitor is not None:
            monitor.remove(
                str(getattr(record, "symbol", "")),
                strategy_id=str(getattr(record, "strategy_id", "") or ""),
                account_id=str(getattr(record, "account_id", "default") or "default"),
                position_id=str(getattr(record, "position_id", "") or ""),
            )


async def _run_stop_loss_check() -> None:
    if state.stop_loss_monitor is None:
        return
    prices = _price_source()
    # check_all() drops each record as it fires, so snapshot before calling it.
    tracked = state.stop_loss_monitor.records()
    triggered = await state.stop_loss_monitor.check_all(prices)
    if not triggered:
        return
    logger.info("StopLossMonitor triggered exits for: %s", triggered)
    for key in triggered:
        record = tracked.get(key)
        if record is None:
            logger.error("Triggered stop record %s disappeared before attribution", key)
            continue
        symbol = record.symbol.upper()
        _clear_risk_records(record)
        # Attribute the actual loss. Adding a flat constant per stop meant the
        # monthly limit tripped after a fixed number of stops rather than at a
        # real drawdown — and intraday fires stops far more often.
        _day_trades().record_close(symbol)
        realized = _realized_pnl(record, prices.get_price(symbol))
        if realized is None:
            logger.warning(
                "Stop-loss on %s: position size unknown, loss not attributed to the monthly limit",
                symbol,
            )
        else:
            state.monthly_realized_loss_usd += max(0.0, -realized)
        await _notify_smart("stop_loss_triggered", f"⛔ Stop-loss fired: {symbol}", tier=2)
        if state.monthly_realized_loss_usd >= settings.monthly_loss_limit_usd * 0.7:
            await _notify_smart(
                "loss_warning",
                f"⚠️ Monthly loss ${state.monthly_realized_loss_usd:.2f} approaching "
                f"${settings.monthly_loss_limit_usd:.2f} limit",
                tier=2,
            )
        if state.monthly_realized_loss_usd >= settings.monthly_loss_limit_usd:
            await _notify_smart(
                "loss_limit_hit",
                f"🛑 Monthly ${settings.monthly_loss_limit_usd:.2f} loss limit reached "
                "— trading paused",
                tier=3,
            )


async def _run_take_profit_check() -> None:
    if state.take_profit_monitor is None:
        return
    prices = _price_source()
    tracked = state.take_profit_monitor.records()
    triggered = await state.take_profit_monitor.check_all(prices)
    for key in triggered:
        record = tracked.get(key)
        if record is None:
            logger.error("Triggered target record %s disappeared before attribution", key)
            continue
        symbol = record.symbol.upper()
        _clear_risk_records(record)
        # Book the gain actually achieved, not the target that was aimed at.
        _day_trades().record_close(symbol)
        realized = _realized_pnl(record, prices.get_price(symbol))
        if realized is None:
            logger.warning(
                "Take-profit on %s: position size unknown, gain not attributed to the "
                "monthly target",
                symbol,
            )
        else:
            state.monthly_realized_profit_usd += max(0.0, realized)
        await _notify_smart("take_profit", f"✅ Take-profit hit: {symbol}", tier=1)
        if state.monthly_realized_profit_usd >= settings.monthly_profit_target_usd:
            await _notify_smart(
                "profit_target_hit",
                f"🎯 Monthly ${settings.monthly_profit_target_usd:.2f} profit target "
                "reached — coasting",
                tier=1,
            )


async def _approval(signal: SignalCandidate, risk, policy: dict[str, object]) -> None:
    amount_usd = round(100_000.0 * (risk.adjusted_size_pct or signal.size_pct), 2)
    approval = ApprovalRequest(
        signal_id=signal.signal_id,
        symbol=signal.symbol,
        action=signal.candidate_action,
        amount_usd=amount_usd,
        tier=risk.tier,
        reason="; ".join(policy.get("reasons", []) or [risk.reason]),
        metadata={"policy": policy, "signal": signal.model_dump(mode="json")},
    )
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(
                f"{settings.approval_gateway_url}/v1/approvals",
                json=approval.model_dump(mode="json"),
                headers=_internal_headers(),
            )
    except Exception as exc:
        logger.error("Approval gateway unreachable: %s — signal rejected (fail safe)", exc)


async def _notify(signal: SignalCandidate, risk, policy: dict[str, object]) -> None:
    event = NotificationEvent(
        tier=risk.tier,
        symbol=signal.symbol,
        action=signal.candidate_action,
        amount_usd=round(100_000.0 * (risk.adjusted_size_pct or signal.size_pct), 2),
        reason="; ".join(policy.get("reasons", []) or [risk.reason]),
        signal_id=signal.signal_id,
    )
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(
                f"{settings.notification_service_url}/v1/notify",
                json=event.model_dump(mode="json"),
                headers=_internal_headers(),
            )
    except Exception as exc:
        # A notification is telemetry about a decision, not part of it. This
        # call sits between the policy verdict and the order placement, and
        # unguarded it turned a down notification service into "no trades and
        # no stop registration for any tier>=1 signal" — the drill's approved
        # entry aborted here before it reached the broker.
        logger.error("Notification service unreachable: %s — decision proceeds", exc)


async def _notify_smart(event_type: str, message: str, tier: int = 1) -> None:
    """Queue notification delivery without blocking the caller."""
    try:
        asyncio.get_running_loop().create_task(_send_smart_notification(event_type, message, tier))
    except RuntimeError as exc:
        logger.debug("_notify_smart scheduling failed: %s", exc)


async def _send_smart_notification(event_type: str, message: str, tier: int = 1) -> None:
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            await client.post(
                f"{settings.notification_service_url}/v1/notify",
                json={
                    "event_type": event_type,
                    "message": message,
                    "tier": tier,
                    "ts": datetime.now(timezone.utc).isoformat(),
                },
                headers=_internal_headers(),
            )
    except Exception as exc:
        logger.debug("_send_smart_notification failed: %s", exc)


async def _notify_trade_executed(signal: SignalCandidate, order: dict[str, object]) -> None:
    entry_price = float(order.get("entry_price", 0.0))
    qty = float(order.get("qty", 0.0))
    await _notify_smart(
        "trade_executed",
        f"📈 Trade: BUY {qty:g}x {signal.symbol} @ ${entry_price:.2f}",
        tier=1,
    )


async def _process_approvals() -> None:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                f"{settings.approval_gateway_url}/v1/approvals/pending",
                params={"status": "APPROVED"},
                headers=_internal_headers(),
            )
            if response.status_code != 200:
                return
            approved_rows = response.json()
    except Exception as exc:
        logger.error(
            "Approval gateway unreachable: %s — treating all pending approvals as "
            "REJECTED (fail safe)",
            exc,
        )
        return
    config = load_policy_config(settings.policy_config_path)
    # Re-check kill switch before executing any deferred approvals
    if config.get("kill_switch"):
        return
    portfolio_state = await _portfolio_state()
    weekly_spend = await _weekly_spend()
    if not _monthly_limits_ok():
        return

    # Deferred approvals open positions just like the direct path, so they are
    # subject to the same day-trade budget. Skipping this let an account at its
    # limit keep opening through the approval route.
    account_equity = await _account_equity()
    pdt = _day_trades().check_entry(account_equity)
    if not pdt.allowed:
        logger.warning("Approved orders held back: %s", pdt.reason)
        for row in approved_rows:
            await _audit(
                AuditEvent(
                    event_type="signal.rejected",
                    symbol=str(row.get("symbol", "")),
                    signal_id=str(row.get("signal_id", "")),
                    decision="REJECT",
                    reasoning=pdt.reason,
                    metadata={"guard": "pattern_day_trader", "path": "approval"},
                )
            )
        return

    for row in approved_rows:
        signal = SignalCandidate.model_validate(row["metadata"]["signal"])
        # Re-checked per order: each accepted entry consumes budget.
        if not _day_trades().check_entry(account_equity).allowed:
            logger.warning("Remaining approved orders held back: day-trade budget spent")
            break
        price_bars = _fetch_price_bars(signal.symbol)
        # Re-run risk + policy with current state before executing
        risk = evaluate_risk(signal, portfolio_state, weekly_spend, config, price_bars=price_bars)
        if not risk.approved:
            await _audit(
                AuditEvent(
                    event_type="approval.stale_rejected",
                    symbol=signal.symbol,
                    signal_id=signal.signal_id,
                    decision="REJECT",
                    reasoning=f"deferred approval failed re-check: {risk.reason}",
                    metadata={"tier": risk.tier},
                )
            )
            continue
        policy = await _policy_evaluate(signal, risk, config, portfolio_state)
        if policy.get("decision") != "APPROVE":
            await _audit(
                AuditEvent(
                    event_type="approval.stale_rejected",
                    symbol=signal.symbol,
                    signal_id=signal.signal_id,
                    decision="REJECT",
                    reasoning="deferred approval failed policy re-check",
                    metadata={"policy": policy},
                )
            )
            continue
        order = await _submit_order(signal, risk, config, portfolio_state, price_bars)
        if not _order_accepted(order):
            logger.warning(
                "Approved order for %s rejected by the broker: %s",
                signal.symbol,
                order.get("rejection_reason"),
            )
            continue
        _day_trades().record_open(signal.symbol)
        _register_stop_loss(
            signal.symbol,
            order,
            price_bars,
            side=_side_of(signal.candidate_action),
            strategy_id=_strategy_of(signal),
        )
        _register_take_profit(signal, order)
        weekly_spend += float(order.get("amount_usd", 0.0))
        state.weekly_notional_used = weekly_spend
        await _notify_trade_executed(signal, order)
        await _audit(
            AuditEvent(
                event_type="trade.executed.approval",
                symbol=signal.symbol,
                signal_id=signal.signal_id,
                decision="APPROVED",
                reasoning="approval executed after re-check",
                metadata=order,
            )
        )
        await _mark_signal_acted(signal.signal_id)


async def _mark_signal_acted(signal_id: str) -> None:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(
                f"{settings.strategy_service_url}/v1/signals/{signal_id}/act",
                headers=_internal_headers(),
            )
    except Exception as exc:
        # The signal stays pending and is retried next cycle; the
        # signal-derived idempotency key at execution makes that retry a
        # replay, not a second fill. Raising here aborted the cycle after
        # the trade had already been placed.
        logger.error("Could not mark signal %s acted: %s", signal_id, exc)


async def _weekly_spend() -> float:
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                f"{settings.audit_logger_url}/v1/audit/logs",
                params={"event_type": "trade.executed", "since": since, "limit": 1000},
                headers=_internal_headers(),
            )
            if response.status_code != 200:
                return 0.0
            rows = response.json()
    except Exception as exc:
        # A non-200 already fell back to 0.0; an unreachable audit logger must
        # not be harder failure than a broken one. This call used to raise —
        # and, sitting between `state.running = True` and the cycle's try
        # block, one refused connection wedged the orchestrator as "busy"
        # until restart. _weekly_spend_safe then prefers the cached figure.
        logger.error("Audit logger unreachable for weekly spend: %s", exc)
        return 0.0
    return round(sum(float(row.get("metadata", {}).get("amount_usd", 0.0)) for row in rows), 2)


async def _weekly_spend_safe() -> float:
    """Return weekly spend, falling back to cached state value if audit unavailable."""
    spend = await _weekly_spend()
    # If audit logger unreachable (returned 0.0), prefer cached state to avoid resetting cap
    if spend == 0.0 and state.weekly_notional_used > 0.0:
        return state.weekly_notional_used
    return spend


async def _audit(event: AuditEvent) -> None:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(
                f"{settings.audit_logger_url}/v1/audit/log",
                json=event.model_dump(mode="json"),
                headers=_internal_headers(),
            )
    except Exception as exc:
        logger.error(
            "Audit logger unreachable, logging locally: %s | event=%s",
            exc,
            event.event_type,
        )


async def _process_exit_signals() -> int:
    closed = 0
    positions = await _portfolio_positions()
    position_map = {str(row.get("symbol", "")).upper(): row for row in positions}
    for signal in await _pending_exit_signals():
        position = position_map.get(signal.symbol.upper())
        if not position:
            continue
        position_id = str(
            position.get("position_id") or position.get("positionId") or signal.symbol
        )
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    f"{settings.execution_service_url}/v1/orders/close",
                    json={
                        "symbol": signal.symbol,
                        "position_id": position_id,
                        "signal_id": signal.signal_id,
                        "strategy_id": _strategy_of(signal),
                    },
                    headers=_internal_headers(),
                )
        except Exception as exc:
            # One exit that cannot reach the broker must not abandon the
            # remaining exits in the same pass.
            logger.error("Exit close unreachable for %s: %s", signal.symbol, exc)
            continue
        if response.status_code not in (200, 201):
            continue
        closed += 1
        _clear_risk_records(signal.symbol)
        _day_trades().record_close(signal.symbol)
        await _audit(
            AuditEvent(
                event_type="trade.closed",
                symbol=signal.symbol,
                signal_id=signal.signal_id,
                decision="EXIT",
                reasoning=signal.research_summary or "exit_signal_executed",
                metadata={"position_id": position_id, "response": response.json()},
            )
        )
        await _mark_signal_acted(signal.signal_id)
    return closed


async def _portfolio_positions() -> list[dict[str, object]]:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(
                f"{settings.portfolio_service_url}/v1/portfolio/positions",
                headers=_internal_headers(),
            )
            if response.status_code != 200:
                return []
            return response.json()
    except Exception as exc:
        # A non-200 already read as "no positions to exit"; an unreachable
        # portfolio service must not be a harder failure than a broken one.
        logger.error("Portfolio service unreachable for positions: %s", exc)
        return []


async def _get_quote_price(symbol: str) -> float | None:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(
                f"{settings.strategy_service_url}/v1/market/quote/{symbol}",
                headers=_internal_headers(),
            )
            if response.status_code == 200:
                return float(response.json().get("price"))
    except Exception as exc:
        logger.debug("Quote lookup via strategy-service failed for %s: %s", symbol, exc)
    # Fall back to our own price source rather than giving up on a price.
    return _price_source().get_price(symbol)


async def _validate_allowlist_symbols() -> dict[str, list[str]]:
    config = load_policy_config(settings.policy_config_path)
    symbols: list[str] = []
    for raw_symbol in config.get("symbol_allowlist", []):
        try:
            symbols.append(sanitize_symbol(str(raw_symbol)))
        except Exception:
            logger.warning("Skipping malformed allowlist symbol: %r", raw_symbol)
    result: dict[str, list[str]] = {"valid": [], "invalid": [], "unknown": []}
    async with httpx.AsyncClient(timeout=5.0) as client:
        for symbol in symbols:
            try:
                response = await client.get(
                    f"{settings.execution_service_url}/v1/instruments/{symbol}/validate",
                    headers=_internal_headers(),
                )
            except Exception:
                result["unknown"].append(symbol)
                continue
            if response.status_code != 200:
                result["unknown"].append(symbol)
                continue
            status = str(response.json().get("status", "unknown")).lower()
            if status == "valid":
                result["valid"].append(symbol)
            elif status == "invalid":
                result["invalid"].append(symbol)
            else:
                result["unknown"].append(symbol)
    state.last_validation = result
    return result


def _check_monthly_reset() -> None:
    """Reset monthly counters on new calendar month."""
    now = datetime.now(timezone.utc)
    current_month = now.month
    current_year = now.year
    if current_month != state.monthly_reset_month or current_year != state.monthly_reset_year:
        state.monthly_realized_loss_usd = 0.0
        state.monthly_realized_profit_usd = 0.0
        state.monthly_reset_month = current_month
        state.monthly_reset_year = current_year
        logger.info("Monthly P&L counters reset for month %d", current_month)


def _monthly_limits_ok() -> bool:
    """Return False if monthly loss limit or profit target reached."""
    _check_monthly_reset()
    if state.monthly_realized_loss_usd >= settings.monthly_loss_limit_usd:
        logger.warning("Monthly loss limit $%.2f reached", settings.monthly_loss_limit_usd)
        return False
    if state.monthly_realized_profit_usd >= settings.monthly_profit_target_usd:
        logger.info(
            "Monthly profit target $%.2f reached — coasting", settings.monthly_profit_target_usd
        )
        return False
    return True
