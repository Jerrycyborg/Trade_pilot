"""Trade worker: runs the full strategy → policy → execution pipeline loop."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import httpx

from contracts import (
    ExecutionOrderRequest,
    MarketContext,
    PolicyEvaluationRequest,
    PortfolioContext,
    SignalCandidate,
)

from .ai_pipeline import AISignalPipeline, _build_deterministic_signal
from .config import settings

logger = logging.getLogger(__name__)


@dataclass
class WorkerState:
    last_run_at: Optional[datetime] = None
    next_run_at: Optional[datetime] = None
    is_running: bool = False
    last_run_error: Optional[str] = None


# Module-level singleton — read by /v1/worker/status
worker_state = WorkerState()


@dataclass
class WorkerRunResult:
    symbols_processed: int = 0
    signals_generated: int = 0
    orders_submitted: int = 0
    errors: list[str] = field(default_factory=list)


class TradeWorker:
    """Runs one full pipeline cycle: research → signals → policy → execution."""

    async def run_cycle(self) -> dict:
        worker_state.is_running = True
        worker_state.last_run_at = datetime.now(timezone.utc)
        result = WorkerRunResult()

        try:
            # 1. Pre-warm research cache for all watchlist symbols
            await self._warm_research_cache(settings.watchlist)

            # 2. Process each symbol
            for symbol in settings.watchlist:
                try:
                    await self._process_symbol(symbol, result)
                except Exception as exc:
                    logger.error("Worker error for symbol %s: %s", symbol, exc)
                    result.errors.append(f"{symbol}: {exc}")
                result.symbols_processed += 1

        except Exception as exc:
            worker_state.last_run_error = str(exc)
            logger.error("Worker cycle error: %s", exc)
        finally:
            worker_state.is_running = False

        worker_state.last_run_error = ", ".join(result.errors) if result.errors else None
        logger.info(
            "Worker cycle complete: %d symbols, %d signals, %d orders, %d errors",
            result.symbols_processed,
            result.signals_generated,
            result.orders_submitted,
            len(result.errors),
        )
        return {
            "symbols_processed": result.symbols_processed,
            "signals_generated": result.signals_generated,
            "orders_submitted": result.orders_submitted,
            "errors": result.errors,
        }

    async def _warm_research_cache(self, symbols: list[str]) -> None:
        """Ask research-service to pre-fetch reports for all symbols concurrently."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(
                    f"{settings.research_service_url}/v1/research/report",
                    json={"symbols": symbols},
                )
        except Exception as exc:
            logger.debug("Research cache warm failed (non-fatal): %s", exc)

    async def _process_symbol(self, symbol: str, result: WorkerRunResult) -> None:
        # 1. Generate signal
        if settings.use_ai:
            pipeline = AISignalPipeline()
            signal = await pipeline.generate(symbol)
        else:
            signal = _build_deterministic_signal(symbol)

        result.signals_generated += 1

        # Skip HOLD signals
        if signal.candidate_action == "HOLD":
            logger.debug("HOLD signal for %s — skipping", symbol)
            return

        # 2. Get account buying power (best-effort)
        buying_power = await self._get_buying_power()

        # 3. Get current portfolio context
        portfolio_ctx = await self._get_portfolio_context()

        # 4. Evaluate policy
        policy_req = PolicyEvaluationRequest(
            signal_id=signal.signal_id,
            symbol=signal.symbol,
            candidate_action=signal.candidate_action,
            confidence=signal.confidence,
            size_pct=signal.size_pct,
            market_context=MarketContext(
                data_age_seconds=10,
                market_open=True,
                event_blackout_active=False,
                liquidity_score=0.95,
                symbol_allowed=True,
            ),
            portfolio_context=portfolio_ctx,
            risk_score=signal.risk_score,
        )

        policy_decision = await self._call_policy(policy_req)
        if policy_decision.get("decision") != "APPROVE":
            logger.debug(
                "Policy %s for %s: %s",
                policy_decision.get("decision"),
                symbol,
                policy_decision.get("reasons"),
            )
            return

        # 5. Compute order quantity
        approved_size_pct = float(policy_decision.get("approved_size_pct", signal.size_pct))
        qty = _compute_qty(approved_size_pct, buying_power, signal)
        if qty < 1:
            logger.debug("Qty < 1 for %s — skipping order", symbol)
            return

        # 6. Submit order to execution-service
        order_req = ExecutionOrderRequest(
            signal_id=signal.signal_id,
            symbol=signal.symbol,
            side=signal.candidate_action,
            qty=qty,
            order_type="MARKET",
            time_in_force="DAY",
        )
        submitted = await self._submit_order(order_req, idempotency_key=f"worker-{signal.signal_id}")
        if submitted:
            result.orders_submitted += 1
            logger.info("Order submitted for %s: %d shares %s", symbol, qty, signal.candidate_action)

    async def _get_buying_power(self) -> float:
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(f"{settings.execution_service_url}/v1/account")
                if resp.status_code == 200:
                    return float(resp.json().get("buying_power", 100_000.0))
        except Exception as exc:
            logger.debug("Could not fetch buying power: %s", exc)
        return 100_000.0

    async def _get_portfolio_context(self) -> PortfolioContext:
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(f"{settings.portfolio_service_url}/v1/portfolio/positions")
                if resp.status_code == 200:
                    positions = resp.json()
                    total_value = sum(abs(float(p.get("market_value", 0))) for p in positions)
                    # Approximate gross exposure as fraction of $100k baseline
                    gross_exposure = min(total_value / 100_000.0, 1.0)
                    return PortfolioContext(
                        gross_exposure_pct=round(gross_exposure, 4),
                        daily_drawdown_pct=0.0,
                    )
        except Exception as exc:
            logger.debug("Could not fetch portfolio context: %s", exc)
        return PortfolioContext(gross_exposure_pct=0.0, daily_drawdown_pct=0.0)

    async def _call_policy(self, req: PolicyEvaluationRequest) -> dict:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(
                    f"{settings.policy_service_url}/v1/policy/evaluate",
                    json=req.model_dump(mode="json"),
                )
                if resp.status_code == 200:
                    return resp.json()
        except Exception as exc:
            logger.error("Policy call failed: %s", exc)
        return {"decision": "REJECT", "reasons": ["policy_call_failed"], "approved_size_pct": 0.0}

    async def _submit_order(self, req: ExecutionOrderRequest, idempotency_key: str) -> bool:
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    f"{settings.execution_service_url}/v1/orders",
                    json=req.model_dump(mode="json"),
                    headers={"Idempotency-Key": idempotency_key},
                )
                return resp.status_code in (200, 201)
        except Exception as exc:
            logger.error("Order submission failed: %s", exc)
        return False


def _compute_qty(size_pct: float, buying_power: float, signal: SignalCandidate) -> int:
    """Compute integer order quantity from size_pct and buying_power."""
    # Use current price from TA summary if available, else fallback to $100
    current_price: float = 100.0
    if signal.ta_summary and hasattr(signal.ta_summary, "current_price"):
        # TechnicalSummaryContract doesn't carry price — use fallback
        pass

    dollar_amount = size_pct * buying_power
    if current_price <= 0:
        return 1
    qty = int(dollar_amount / current_price)
    return max(1, qty)
