from __future__ import annotations

import logging
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
from pydantic import BaseModel

from .config import settings
from .policy_config import is_market_hours, load_policy_config, update_policy_config
from .risk_engine import evaluate_risk

logger = logging.getLogger(__name__)


def _internal_headers() -> dict[str, str]:
    import os

    key = os.environ.get("INTERNAL_API_KEY", "")
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


state = OrchestratorState()


class KillSwitchRequest(BaseModel):
    active: bool


class LiveModeRequest(BaseModel):
    enable: bool
    confirmation: str = ""


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
        minutes=settings.orchestrator_interval_minutes,
        id="autonomy_orchestrator",
        max_instances=1,
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


@asynccontextmanager
async def lifespan(app_: FastAPI):
    await _startup_health_check()
    _start_scheduler()
    yield


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
    """Return dashboard client keys. Only served to requests from localhost or LAN."""
    import os
    client_host = getattr(request.client, "host", "")
    # Allow localhost and RFC-1918 private ranges only
    allowed = (
        client_host in ("127.0.0.1", "::1", "localhost") or
        client_host.startswith("192.168.") or
        client_host.startswith("10.") or
        client_host.startswith("172.")
    )
    if not allowed:
        raise HTTPException(status_code=403, detail="not allowed")
    return {
        "internalKey": os.getenv("INTERNAL_API_KEY", ""),
        "adminKey":    os.getenv("ADMIN_API_KEY", ""),
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
        signals = [signal for signal in await _pending_signals() if signal.candidate_action != "EXIT"]
        portfolio_state = await _portfolio_state()
        for signal in signals:
            summary["signals"] += 1
            risk = evaluate_risk(signal, portfolio_state, weekly_spend, config)
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
    request = PolicyEvaluationRequest(
        signal_id=signal.signal_id,
        symbol=signal.symbol,
        candidate_action=signal.candidate_action,
        confidence=signal.confidence,
        size_pct=risk.adjusted_size_pct or signal.size_pct,
        market_context={
            "data_age_seconds": 10,
            "market_open": is_market_hours(config),
            "event_blackout_active": False,
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
    body["trading_mode"] = config.get("trading_mode", "demo")
    return body


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
    for row in approved_rows:
        signal = SignalCandidate.model_validate(row["metadata"]["signal"])
        # Re-run risk + policy with current state before executing
        risk = evaluate_risk(signal, portfolio_state, weekly_spend, config)
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
        weekly_spend += float(order.get("amount_usd", 0.0))
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
    except Exception:
        return None
    return None


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
