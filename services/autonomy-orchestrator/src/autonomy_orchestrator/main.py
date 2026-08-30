from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import asyncio
import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from contracts import ApprovalRequest, AuditEvent, NotificationEvent, PolicyEvaluationRequest, SignalCandidate
from contracts.auth import verify_admin_key, verify_internal_key
from contracts.rate_limit import rate_limit_write
from contracts.sanitize import sanitize_symbol
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from market_data import (
    LivePriceCache,
    MarketDataSettings,
    RealtimePriceSource,
    StreamManager,
    fetch_bars,
)
from market_data.fetcher import OHLCVFetcherProtocol  # noqa: F401 - kept for type hints
from market_data.indicators import compute_atr
from pydantic import BaseModel

from .config import settings
from .day_trade_tracker import DayTradeTracker
from .stop_loss_monitor import StopLossMonitor, StopLossRecord
from .take_profit_monitor import TakeProfitMonitor, TakeProfitRecord
from .policy_config import is_market_hours, load_policy_config, update_policy_config
from .risk_engine import evaluate_risk

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
    stream_manager: StreamManager | None = None
    market_settings: MarketDataSettings | None = None
    day_trades: DayTradeTracker | None = None


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

    scheduler.add_job(
        run_job,
        trigger="interval",
        seconds=_cycle_interval_seconds(),
        id="autonomy_orchestrator",
        max_instances=1,
        coalesce=True,
    )

    scheduler.add_job(
        lambda: asyncio.create_task(_run_stop_loss_check()),
        "interval",
        seconds=_risk_check_interval_seconds("STOP_LOSS_CHECK_INTERVAL_MINUTES"),
        id="stop_loss_check",
        replace_existing=True,
        coalesce=True,
    )
    scheduler.add_job(
        lambda: asyncio.create_task(_run_take_profit_check()),
        "interval",
        seconds=_risk_check_interval_seconds("TAKE_PROFIT_CHECK_INTERVAL_MINUTES"),
        id="take_profit_check",
        replace_existing=True,
        coalesce=True,
    )
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    scheduler.start()
    state.scheduler = scheduler


async def _check_dependency(name: str, url: str) -> dict[str, object]:
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{url}/health")
            if resp.status_code == 200:
                return {"name": name, "status": "ok", "url": url}
            return {"name": name, "status": "degraded", "url": url, "code": resp.status_code}
    except Exception as exc:
        return {"name": name, "status": "down", "url": url, "error": str(exc)}


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
    state.stop_loss_monitor = StopLossMonitor(
        broker_url=settings.broker_url,
        internal_key=settings.internal_api_key,
    )
    state.take_profit_monitor = TakeProfitMonitor(
        broker_url=settings.broker_url,
        internal_key=settings.internal_api_key,
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
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
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
        "stop_loss_check_seconds": _risk_check_interval_seconds(
            "STOP_LOSS_CHECK_INTERVAL_MINUTES"
        ),
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
async def client_config(request: Request) -> dict[str, str]:
    """Dashboard client configuration.

    This used to return INTERNAL_API_KEY and ADMIN_API_KEY to any caller whose
    source address looked local. Behind a reverse proxy — nginx, or
    scripts/serve_dashboard.py — every request appears to come from localhost,
    so anyone who could reach the dashboard could read the admin key and toggle
    the kill switch or live mode.

    Keys are now injected by the proxy on the server side and are not returned
    here. Set EXPOSE_CLIENT_KEYS=true only if you run an older proxy that
    cannot inject them, and understand that it hands the admin key to every
    visitor of the dashboard.
    """
    import os

    if os.getenv("EXPOSE_CLIENT_KEYS", "false").lower() != "true":
        return {"keysInjectedByProxy": "true"}

    client_host = getattr(request.client, "host", "")
    allowed = (
        client_host in ("127.0.0.1", "::1", "localhost")
        or client_host.startswith("192.168.")
        or client_host.startswith("10.")
        or client_host.startswith("172.")
    )
    if not allowed:
        raise HTTPException(status_code=403, detail="not allowed")
    logger.warning(
        "EXPOSE_CLIENT_KEYS=true — serving API keys to %s. Anyone who can reach "
        "the dashboard can read the admin key.", client_host,
    )
    return {
        "internalKey": os.getenv("INTERNAL_API_KEY", ""),
        "adminKey": os.getenv("ADMIN_API_KEY", ""),
    }




@app.get("/v1/orchestrator/cycle/last")
def last_cycle() -> dict[str, object]:
    return state.last_cycle_summary

@app.post("/v1/orchestrator/cycle/trigger")
async def trigger_cycle(x_internal_key: str = Header(...)) -> dict[str, object]:
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
            raise HTTPException(status_code=400, detail="Kill switch must be off before enabling live mode")
        if not config.get("weekly_notional_cap_usd"):
            raise HTTPException(status_code=400, detail="Weekly cap must be set before enabling live mode")
        if not config.get("symbol_allowlist"):
            raise HTTPException(status_code=400, detail="Symbol allowlist must be non-empty before enabling live mode")
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
    weekly_spend = await _weekly_spend_safe()
    state.weekly_notional_used = weekly_spend
    try:
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
        signals = [signal for signal in await _pending_signals() if signal.candidate_action != "EXIT"]
        portfolio_state = await _portfolio_state()

        # Pattern-day-trader budget. Checked once per cycle rather than per
        # signal: the equity lookup is a network call and the budget cannot
        # change mid-cycle, since it only moves when a position is closed.
        pdt = _day_trades().check_entry(await _account_equity())
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
            if not pdt.allowed:
                summary["rejected"] += 1
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
                continue
            price_bars = _fetch_price_bars(signal.symbol)
            risk = evaluate_risk(signal, portfolio_state, weekly_spend, config, price_bars=price_bars)
            if not risk.approved:
                summary["rejected"] += 1
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
                continue
            policy = await _policy_evaluate(signal, risk, config, portfolio_state)
            decision = policy.get("decision", "REJECT")
            if risk.tier >= 1:
                await _notify(signal, risk, policy)
            if decision == "APPROVE" and risk.tier < 3:
                order = await _submit_order(signal, risk, config, portfolio_state)
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
                    continue
                _day_trades().record_open(signal.symbol)
                _register_stop_loss(signal.symbol, order, price_bars)
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


async def _pending_signals() -> list[SignalCandidate]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(
            f"{settings.strategy_service_url}/v1/signals",
            params={"limit": 50, "acted_on": "false"},
            headers=_internal_headers(),
        )
        response.raise_for_status()
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
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(
            f"{settings.strategy_service_url}/v1/signals",
            params={"limit": 50, "acted_on": "false", "candidate_action": "EXIT"},
            headers=_internal_headers(),
        )
        response.raise_for_status()
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
    async with httpx.AsyncClient(timeout=5.0) as client:
        positions_resp = await client.get(
            f"{settings.portfolio_service_url}/v1/portfolio/positions",
            headers=_internal_headers(),
        )
        account_resp = await client.get(
            f"{settings.execution_service_url}/v1/account",
            headers=_internal_headers(),
        )
    positions = positions_resp.json() if positions_resp.status_code == 200 else []
    account = account_resp.json() if account_resp.status_code == 200 else {"buying_power": 100_000.0}
    return {
        "positions": positions,
        "buying_power": float(account.get("buying_power", 100_000.0)),
        "daily_drawdown_pct": 0.0,
    }


async def _policy_evaluate(signal: SignalCandidate, risk, config: dict[str, object], portfolio_state: dict[str, object]) -> dict[str, object]:
    try:
        from strategy_service.earnings_calendar import is_earnings_blackout as _iec

        event_blackout = _iec(signal.symbol)
    except Exception:
        event_blackout = False

    request = PolicyEvaluationRequest(
        signal_id=signal.signal_id,
        symbol=signal.symbol,
        candidate_action=signal.candidate_action,
        confidence=signal.confidence,
        size_pct=risk.adjusted_size_pct or signal.size_pct,
        market_context={
            "data_age_seconds": 10,
            "market_open": is_market_hours(config),
            "event_blackout_active": event_blackout,
            "liquidity_score": 0.95,
            "symbol_allowed": signal.symbol.upper() in {str(sym).upper() for sym in config.get("symbol_allowlist", [])},
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


async def _submit_order(signal: SignalCandidate, risk, config: dict[str, object], portfolio_state: dict[str, object]) -> dict[str, object]:
    buying_power = float(portfolio_state.get("buying_power", 100_000.0))
    amount_usd = round(buying_power * risk.adjusted_size_pct, 2)
    qty = max(1, int(amount_usd / 100.0))
    current_price = await _get_quote_price(signal.symbol)
    stop_loss_pct = float(config.get("stop_loss_pct", 0.03))
    take_profit_pct = float(config.get("take_profit_pct", 0.06))
    stop_loss_rate = current_price * (1 - stop_loss_pct) if current_price is not None else None
    take_profit_rate = current_price * (1 + take_profit_pct) if current_price is not None else None
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            f"{settings.execution_service_url}/v1/orders",
            json={
                "signal_id": signal.signal_id,
                "symbol": signal.symbol,
                "side": signal.candidate_action,
                "qty": qty,
                "order_type": "MARKET",
                "time_in_force": "DAY",
                "stop_loss_rate": stop_loss_rate,
                "take_profit_rate": take_profit_rate,
            },
            headers={
                "Idempotency-Key": f"orchestrator-{signal.signal_id}",
                **_internal_headers(),
            },
        )
        response.raise_for_status()
    body = response.json()
    body["amount_usd"] = amount_usd
    body["entry_price"] = current_price if current_price is not None else round(amount_usd / qty, 4)
    body["qty"] = qty
    body["trading_mode"] = config.get("trading_mode", "demo")
    return body


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


def _register_stop_loss(symbol: str, order: dict[str, object], price_bars: list[Any] | None) -> None:
    if state.stop_loss_monitor is None:
        return

    entry_price = float(order.get("entry_price", 0.0))
    if entry_price <= 0.0:
        logger.warning("Skipping stop registration for %s: missing entry price", symbol)
        return

    stop_price = entry_price * 0.98
    if price_bars:
        highs = [float(bar.high) for bar in price_bars]
        lows = [float(bar.low) for bar in price_bars]
        closes = [float(bar.close) for bar in price_bars]
        atr = compute_atr(highs, lows, closes)
        if atr > 0.0:
            stop_price = entry_price - atr * 2.0

    state.stop_loss_monitor.register(
        StopLossRecord(
            symbol=symbol,
            entry_price=entry_price,
            stop_price=stop_price,
            position_id=str(order.get("order_id", symbol)),
            qty=float(order.get("qty", 0.0)),
            created_at=datetime.now(timezone.utc),
        )
    )


def _register_take_profit(signal: SignalCandidate, order: dict[str, object]) -> None:
    entry_price = float(order.get("entry_price", 0.0))
    qty = float(order.get("qty", 0.0))
    if entry_price <= 0.0 or state.take_profit_monitor is None:
        return

    target_price = entry_price + (settings.take_profit_target_usd / qty) if qty > 0 else entry_price * 1.06
    state.take_profit_monitor.register(
        TakeProfitRecord(
            symbol=signal.symbol,
            entry_price=entry_price,
            target_price=target_price,
            position_id=str(order.get("order_id", signal.symbol)),
            qty=qty,
            target_gain_usd=settings.take_profit_target_usd,
            created_at=datetime.now(timezone.utc),
        )
    )


def _realized_pnl(record, exit_price: float | None) -> float | None:
    """P&L on a closed position, or None when it cannot be determined.

    A record registered with qty=0 means "close whatever is open at the broker",
    so the size is unknown here and no P&L can be attributed.
    """
    if record is None or exit_price is None:
        return None
    qty = float(getattr(record, "qty", 0.0) or 0.0)
    entry = float(getattr(record, "entry_price", 0.0) or 0.0)
    if qty <= 0.0 or entry <= 0.0:
        return None
    return (exit_price - entry) * qty


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
    for symbol in triggered:
        # Attribute the actual loss. Adding a flat constant per stop meant the
        # monthly limit tripped after a fixed number of stops rather than at a
        # real drawdown — and intraday fires stops far more often.
        _day_trades().record_close(symbol)
        realized = _realized_pnl(tracked.get(symbol), prices.get_price(symbol))
        if realized is None:
            logger.warning(
                "Stop-loss on %s: position size unknown, loss not attributed to the "
                "monthly limit", symbol,
            )
        else:
            state.monthly_realized_loss_usd += max(0.0, -realized)
        await _notify_smart("stop_loss_triggered", f"⛔ Stop-loss fired: {symbol}", tier=2)
        if state.monthly_realized_loss_usd >= settings.monthly_loss_limit_usd * 0.7:
            await _notify_smart(
                "loss_warning",
                f"⚠️ Monthly loss ${state.monthly_realized_loss_usd:.2f} approaching ${settings.monthly_loss_limit_usd:.2f} limit",
                tier=2,
            )
        if state.monthly_realized_loss_usd >= settings.monthly_loss_limit_usd:
            await _notify_smart(
                "loss_limit_hit",
                f"🛑 Monthly ${settings.monthly_loss_limit_usd:.2f} loss limit reached — trading paused",
                tier=3,
            )


async def _run_take_profit_check() -> None:
    if state.take_profit_monitor is None:
        return
    prices = _price_source()
    tracked = state.take_profit_monitor.records()
    triggered = await state.take_profit_monitor.check_all(prices)
    for symbol in triggered:
        # Book the gain actually achieved, not the target that was aimed at.
        _day_trades().record_close(symbol)
        realized = _realized_pnl(tracked.get(symbol), prices.get_price(symbol))
        if realized is None:
            logger.warning(
                "Take-profit on %s: position size unknown, gain not attributed to the "
                "monthly target", symbol,
            )
        else:
            state.monthly_realized_profit_usd += max(0.0, realized)
        await _notify_smart("take_profit", f"✅ Take-profit hit: {symbol}", tier=1)
        if state.monthly_realized_profit_usd >= settings.monthly_profit_target_usd:
            await _notify_smart(
                "profit_target_hit",
                f"🎯 Monthly ${settings.monthly_profit_target_usd:.2f} profit target reached — coasting",
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
    async with httpx.AsyncClient(timeout=5.0) as client:
        await client.post(
            f"{settings.notification_service_url}/v1/notify",
            json=event.model_dump(mode="json"),
            headers=_internal_headers(),
        )


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
            "Approval gateway unreachable: %s — treating all pending approvals as REJECTED (fail safe)",
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
    pdt = _day_trades().check_entry(await _account_equity())
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
        price_bars = _fetch_price_bars(signal.symbol)
        # Re-run risk + policy with current state before executing
        risk = evaluate_risk(signal, portfolio_state, weekly_spend, config, price_bars=price_bars)
        if not risk.approved:
            await _audit(AuditEvent(
                event_type="approval.stale_rejected",
                symbol=signal.symbol,
                signal_id=signal.signal_id,
                decision="REJECT",
                reasoning=f"deferred approval failed re-check: {risk.reason}",
                metadata={"tier": risk.tier},
            ))
            continue
        policy = await _policy_evaluate(signal, risk, config, portfolio_state)
        if policy.get("decision") != "APPROVE":
            await _audit(AuditEvent(
                event_type="approval.stale_rejected",
                symbol=signal.symbol,
                signal_id=signal.signal_id,
                decision="REJECT",
                reasoning="deferred approval failed policy re-check",
                metadata={"policy": policy},
            ))
            continue
        order = await _submit_order(signal, risk, config, portfolio_state)
        if not _order_accepted(order):
            logger.warning(
                "Approved order for %s rejected by the broker: %s",
                signal.symbol,
                order.get("rejection_reason"),
            )
            continue
        _day_trades().record_open(signal.symbol)
        _register_stop_loss(signal.symbol, order, price_bars)
        _register_take_profit(signal, order)
        weekly_spend += float(order.get("amount_usd", 0.0))
        state.weekly_notional_used = weekly_spend
        await _notify_trade_executed(signal, order)
        await _audit(AuditEvent(
            event_type="trade.executed.approval",
            symbol=signal.symbol,
            signal_id=signal.signal_id,
            decision="APPROVED",
            reasoning="approval executed after re-check",
            metadata=order,
        ))
        await _mark_signal_acted(signal.signal_id)


async def _mark_signal_acted(signal_id: str) -> None:
    async with httpx.AsyncClient(timeout=5.0) as client:
        await client.post(
            f"{settings.strategy_service_url}/v1/signals/{signal_id}/act",
            headers=_internal_headers(),
        )


async def _weekly_spend() -> float:
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(
            f"{settings.audit_logger_url}/v1/audit/logs",
            params={"event_type": "trade.executed", "since": since, "limit": 1000},
            headers=_internal_headers(),
        )
        if response.status_code != 200:
            return 0.0
        rows = response.json()
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
        position_id = str(position.get("position_id") or position.get("positionId") or signal.symbol)
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{settings.execution_service_url}/v1/orders/close",
                json={
                    "symbol": signal.symbol,
                    "position_id": position_id,
                    "signal_id": signal.signal_id,
                },
                headers=_internal_headers(),
            )
        if response.status_code not in (200, 201):
            continue
        closed += 1
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
    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.get(
            f"{settings.portfolio_service_url}/v1/portfolio/positions",
            headers=_internal_headers(),
        )
        if response.status_code != 200:
            return []
        return response.json()


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
    if (
        current_month != state.monthly_reset_month
        or current_year != state.monthly_reset_year
    ):
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
        logger.info("Monthly profit target $%.2f reached — coasting", settings.monthly_profit_target_usd)
        return False
    return True
