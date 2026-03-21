from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from contracts import ApprovalRequest, AuditEvent, NotificationEvent, PolicyEvaluationRequest, SignalCandidate
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .config import settings
from .policy_config import is_market_hours, load_policy_config, update_policy_config
from .risk_engine import evaluate_risk


class OrchestratorState:
    running: bool = False
    last_cycle_time: datetime | None = None
    last_cycle_summary: dict[str, object] = {}
    trades_today: int = 0
    weekly_notional_used: float = 0.0
    scheduler: AsyncIOScheduler | None = None


state = OrchestratorState()


class KillSwitchRequest(BaseModel):
    active: bool


class LiveModeRequest(BaseModel):
    enable: bool
    confirmation: str = ""


@asynccontextmanager
async def lifespan(_: FastAPI):
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
    scheduler.start()
    state.scheduler = scheduler
    yield
    scheduler.shutdown(wait=False)


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


@app.get("/v1/orchestrator/cycle/last")
def last_cycle() -> dict[str, object]:
    return state.last_cycle_summary


@app.post("/v1/orchestrator/kill-switch")
def toggle_kill_switch(request: KillSwitchRequest) -> dict[str, object]:
    config = update_policy_config(settings.policy_config_path, {"kill_switch": request.active})
    return {"kill_switch": config.get("kill_switch", False)}


@app.post("/v1/orchestrator/live-mode")
async def live_mode(request: LiveModeRequest, raw_request: Request) -> dict[str, object]:
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
    weekly_spend = await _weekly_spend()
    state.weekly_notional_used = weekly_spend
    try:
        await _process_approvals()
        signals = await _pending_signals()
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
        response = await client.get(f"{settings.strategy_service_url}/v1/signals", params={"limit": 50, "acted_on": "false"})
        response.raise_for_status()
    return [SignalCandidate.model_validate(item) for item in response.json()]


async def _portfolio_state() -> dict[str, object]:
    async with httpx.AsyncClient(timeout=5.0) as client:
        positions_resp = await client.get(f"{settings.portfolio_service_url}/v1/portfolio/positions")
        account_resp = await client.get(f"{settings.execution_service_url}/v1/account")
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
    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.post(
            f"{settings.policy_service_url}/v1/policy/evaluate",
            json=request.model_dump(mode="json"),
        )
        response.raise_for_status()
        return response.json()


async def _submit_order(signal: SignalCandidate, risk, config: dict[str, object], portfolio_state: dict[str, object]) -> dict[str, object]:
    buying_power = float(portfolio_state.get("buying_power", 100_000.0))
    amount_usd = round(buying_power * risk.adjusted_size_pct, 2)
    qty = max(1, int(amount_usd / 100.0))
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
            },
            headers={"Idempotency-Key": f"orchestrator-{signal.signal_id}"},
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
    async with httpx.AsyncClient(timeout=5.0) as client:
        await client.post(f"{settings.approval_gateway_url}/v1/approvals", json=approval.model_dump(mode="json"))


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
        await client.post(f"{settings.notification_service_url}/v1/notify", json=event.model_dump(mode="json"))


async def _process_approvals() -> None:
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(f"{settings.approval_gateway_url}/v1/approvals/pending", params={"status": "APPROVED"})
        if response.status_code != 200:
            return
        approved_rows = response.json()
        for row in approved_rows:
            signal = SignalCandidate.model_validate(row["metadata"]["signal"])
            risk = type("RiskShim", (), {"adjusted_size_pct": min(signal.size_pct, 0.05), "tier": row["tier"]})
            order = await _submit_order(signal, risk, load_policy_config(settings.policy_config_path), {"buying_power": 100_000.0})
            await _audit(
                AuditEvent(
                    event_type="trade.executed.approval",
                    symbol=signal.symbol,
                    signal_id=signal.signal_id,
                    decision="APPROVED",
                    reasoning="approval executed",
                    metadata=order,
                )
            )
            await _mark_signal_acted(signal.signal_id)


async def _mark_signal_acted(signal_id: str) -> None:
    async with httpx.AsyncClient(timeout=5.0) as client:
        await client.post(f"{settings.strategy_service_url}/v1/signals/{signal_id}/act")


async def _weekly_spend() -> float:
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(
            f"{settings.audit_logger_url}/v1/audit/logs",
            params={"event_type": "trade.executed", "since": since, "limit": 1000},
        )
        if response.status_code != 200:
            return 0.0
        rows = response.json()
    return round(sum(float(row.get("metadata", {}).get("amount_usd", 0.0)) for row in rows), 2)


async def _audit(event: AuditEvent) -> None:
    async with httpx.AsyncClient(timeout=5.0) as client:
        await client.post(f"{settings.audit_logger_url}/v1/audit/log", json=event.model_dump(mode="json"))
