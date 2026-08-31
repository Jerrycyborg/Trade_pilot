"""Trade worker: runs the full strategy → policy → execution pipeline loop."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import uuid4

import httpx
from contracts import (
    CandidateAction,
    ExecutionOrderRequest,
    MarketContext,
    PolicyEvaluationRequest,
    PortfolioContext,
    SignalCandidate,
    TechnicalSummaryContract,
)
from contracts.execution import (
    average_daily_volume,
    marketable_limit_price,
    participation_capped_qty,
)
from lifecycle import DEFAULT_LIVE_STRATEGY
from lifecycle.service import get_lifecycle_service, reset_lifecycle_service
from market_data import (
    ADX_NEUTRAL,
    MarketDataSettings,
    RealtimePriceSource,
    adx_is_computable,
    build_ta_summary,
    fetch_bars,
    market_session,
)

from .ai_pipeline import AISignalPipeline, _build_deterministic_signal
from .config import settings

logger = logging.getLogger(__name__)

def _lifecycle():
    """The shared roster. Not a per-process copy: this worker used to hold a
    JSON registry loaded once at boot, so it could believe a sleeve was live
    long after another process demoted it."""
    return get_lifecycle_service()


def reset_lifecycle() -> None:
    """Drop the cached authority — for tests, and after an out-of-band change."""
    reset_lifecycle_service(None)


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
    orders_gated: int = 0
    """Signals the strategy roster did not permit to trade. Recorded, not
    dropped — a paper sleeve's decisions are the evidence it is promoted on."""
    errors: list[str] = field(default_factory=list)


class TradeWorker:
    """Runs one full pipeline cycle: research → signals → policy → execution."""

    def __init__(self, price_source: RealtimePriceSource | None = None) -> None:
        self._market_settings = MarketDataSettings()
        self._prices = price_source or RealtimePriceSource(self._market_settings)

    async def run_cycle(self) -> dict:
        worker_state.is_running = True
        worker_state.last_run_at = datetime.now(timezone.utc)
        result = WorkerRunResult()

        try:
            # 1. Pre-warm research cache for all watchlist symbols
            await self._warm_research_cache(settings.watchlist)

            # 2. Emit any exit signals for current open positions
            result.signals_generated += await self._generate_exit_signals()

            # 3. Process each symbol
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

        ta, bars = self._get_market_snapshot(symbol)
        if signal.candidate_action == "BUY":
            # compute_adx returns ADX_NEUTRAL (25.0) when the series is too
            # short, and 25.0 sits *above* this filter's threshold — so on thin
            # or missing data the regime gate used to pass on a fabricated
            # number rather than refuse. An unmeasurable regime is not a
            # trending one.
            bars_count = getattr(ta, "bars_count", 0) if ta is not None else 0
            adx = getattr(ta, "adx", ADX_NEUTRAL) if ta is not None else ADX_NEUTRAL
            if not adx_is_computable(bars_count):
                logger.debug(
                    "regime: not measurable (%s bars), suppressing trend signal", bars_count
                )
                signal.candidate_action = CandidateAction.HOLD
            elif adx < 20.0:
                logger.debug("regime: ranging (adx=%s), suppressing trend signal", round(adx, 4))
                signal.candidate_action = CandidateAction.HOLD
            elif settings.volume_confirm_enabled and bars:
                volumes = [float(getattr(bar, "volume", 0.0) or 0.0) for bar in bars]
                current_volume = volumes[-1] if volumes else None
                avg_volume = sum(volumes[-20:]) / min(len(volumes), 20) if volumes else None
                if (
                    current_volume is not None
                    and avg_volume is not None
                    and current_volume <= avg_volume
                ):
                    logger.debug("volume_confirm: below avg, suppressing BUY")
                    signal.candidate_action = CandidateAction.HOLD

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
            market_context=self._market_context(symbol, bars),
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
        reference_price = self._prices.get_price(symbol)
        if reference_price is None and bars:
            reference_price = float(bars[-1].close)
        qty = _compute_qty(approved_size_pct, buying_power, reference_price)

        # Trim to a share of the symbol's volume — a large order in a thin name
        # pays for its own market impact.
        bars_per_day = (
            max(1.0, 390.0 / max(1, self._market_settings.intraday_minutes))
            if self._market_settings.is_intraday
            else 1.0
        )
        qty = participation_capped_qty(
            qty,
            average_daily_volume(bars, bars_per_day=bars_per_day),
            float(os.getenv("MAX_ADV_PARTICIPATION", "0.01")),
        )

        if qty < 1:
            logger.debug(
                "Qty < 1 for %s (size_pct=%.4f, buying_power=%.2f, price=%s) — skipping order",
                symbol,
                approved_size_pct,
                buying_power,
                reference_price,
            )
            return

        # 6. The strategy roster decides whether this may reach the broker.
        # This worker posts to execution-service directly, so without the check
        # here it would walk around the orchestrator's gate entirely — and a
        # safety control one code path can bypass is worse than none, because
        # it creates confidence that is not warranted. Both paths read the same
        # roster from the same state file.
        gate = self._lifecycle_gate(signal)
        if gate is not None:
            logger.info(
                "Not trading %s %s: %s (signal recorded, no order placed)",
                signal.candidate_action,
                symbol,
                gate,
            )
            self._record_gated_decision(signal, symbol, qty, reference_price, gate)
            result.orders_gated += 1
            return

        # 7. Submit order to execution-service
        # Marketable limit + IOC: fills now at or inside the limit, or not at
        # all. Nothing is left working that would need managing.
        order_type, time_in_force, limit_price = "MARKET", "DAY", None
        if os.getenv("USE_LIMIT_ORDERS", "true").lower() == "true":
            limit_price = marketable_limit_price(
                reference_price,
                str(getattr(signal.candidate_action, "value", signal.candidate_action)),
                float(os.getenv("LIMIT_TOLERANCE_BPS", "10")),
            )
            if limit_price is not None:
                order_type, time_in_force = "LIMIT", "IOC"

        order_req = ExecutionOrderRequest(
            signal_id=signal.signal_id,
            symbol=signal.symbol,
            side=signal.candidate_action,
            qty=qty,
            order_type=order_type,
            time_in_force=time_in_force,
            limit_price=limit_price,
            decision_price=reference_price,
        )
        submitted = await self._submit_order(
            order_req, idempotency_key=f"worker-{signal.signal_id}"
        )
        if submitted:
            result.orders_submitted += 1
            logger.info(
                "Order submitted for %s: %d shares %s", symbol, qty, signal.candidate_action
            )

    def _lifecycle_gate(self, signal: SignalCandidate) -> str | None:
        """Why this signal may not reach the broker, or None if it may.

        Advisory — execution-service resolves and enforces the route whatever
        this says. Asking here is what lets the refusal be journalled with the
        context this worker has, instead of arriving downstream without it.
        """
        strategy = getattr(signal, "strategy", None) or DEFAULT_LIVE_STRATEGY
        answer = _lifecycle().may_open(strategy, signal.symbol)
        if answer.permitted:
            return None
        if not answer.available:
            logger.error(
                "Lifecycle authority unavailable (%s) — refusing to open %s",
                answer.reason, signal.symbol,
            )
        return answer.reason

    def _record_gated_decision(
        self,
        signal: SignalCandidate,
        symbol: str,
        qty: int,
        reference_price: float | None,
        gate: str,
    ) -> None:
        """Archive the trade that would have happened.

        A sleeve on paper is not idle — this recorded history is exactly the
        evidence its promotion to live is gated on, so dropping the signal
        silently would make it unpromotable.
        """
        try:
            from journal import get_journal

            get_journal().record_decision(
                stage="lifecycle_gate",
                outcome="not_traded",
                symbol=symbol,
                action=str(getattr(signal.candidate_action, "value", signal.candidate_action)),
                reason=gate,
                inputs={
                    "strategy": getattr(signal, "strategy", DEFAULT_LIVE_STRATEGY),
                    "confidence": signal.confidence,
                    "reference_price": reference_price,
                },
                outputs={"would_have_traded": True, "qty": qty},
                correlation_id=signal.signal_id,
            )
        except Exception as exc:  # pragma: no cover - journalling is best effort
            logger.debug("Gated decision not journalled: %s", exc)

    def _market_context(self, symbol: str, bars: list) -> MarketContext:
        """Build the policy's market context from observed data, not assumptions.

        The policy service rejects stale data, so reporting a made-up age would
        disable that guard entirely. Age comes from our freshest actual price;
        when no price can be resolved we report an age that trips the staleness
        rule rather than one that passes it.
        """
        session = market_session(self._market_settings)
        snapshot = self._prices.get_snapshot(symbol)
        if snapshot is not None:
            age_seconds = int(snapshot.age_seconds())
        elif bars:
            age_seconds = int(
                (datetime.now(timezone.utc) - _as_utc(bars[-1].timestamp)).total_seconds()
            )
        else:
            # No observable price: fail closed.
            age_seconds = 10_000
            logger.warning("No price available for %s — reporting data as stale", symbol)

        return MarketContext(
            data_age_seconds=age_seconds,
            market_open=session.is_open,
            event_blackout_active=False,
            liquidity_score=0.95,
            symbol_allowed=True,
        )

    async def _generate_exit_signals(self) -> int:
        positions = await self._get_open_positions()
        emitted = 0
        for position in positions:
            exit_signal = self._build_exit_signal(position)
            if exit_signal is None:
                continue
            if await self._signal_exists(exit_signal.symbol, exit_signal.candidate_action):
                continue
            await self._persist_signal(exit_signal)
            emitted += 1
        return emitted

    async def _get_open_positions(self) -> list[dict[str, object]]:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{settings.portfolio_service_url}/v1/portfolio/positions")
                if resp.status_code == 200:
                    return [row for row in resp.json() if int(row.get("net_qty", 0)) != 0]
        except Exception as exc:
            logger.debug("Could not fetch open positions for exit checks: %s", exc)
        return []

    def _build_exit_signal(self, position: dict[str, object]) -> SignalCandidate | None:
        symbol = str(position.get("symbol", "")).upper()
        if not symbol:
            return None

        ta = self._get_ta_snapshot(symbol)
        if ta is None:
            return None

        entry_price = float(position.get("average_cost", 0.0))
        # Prefer the live price over the last bar close: at intraday resolution a
        # stop that reacts only on bar boundaries is a stop that fires late.
        live_price = self._prices.get_price(symbol)
        current_price = float(live_price if live_price is not None else ta.current_price)
        qty = int(position.get("net_qty", 0))
        opened_at = _parse_datetime(position.get("opened_at")) or _parse_datetime(
            position.get("updated_at")
        )
        max_hold = timedelta(hours=settings.max_hold_hours)

        reasons: list[str] = []
        if entry_price > 0 and current_price < entry_price * (1 - settings.stop_loss_pct):
            reasons.append("stop_loss_hit")
        if entry_price > 0 and current_price > entry_price * (1 + settings.take_profit_pct):
            reasons.append("take_profit_hit")
        if qty > 0 and ta.indicators.rsi_14 > 70:
            reasons.append("signal_reversal")
        if qty < 0 and ta.indicators.rsi_14 < 30:
            reasons.append("signal_reversal")
        if opened_at and datetime.now(timezone.utc) - opened_at > max_hold:
            reasons.append("max_hold_time")

        if not reasons:
            return None

        return SignalCandidate(
            signal_id=str(uuid4()),
            symbol=symbol,
            ts=datetime.now(timezone.utc),
            candidate_action=CandidateAction.EXIT,
            confidence=0.95,
            size_pct=1.0,
            horizon="swing",
            source="strategy-service",
            model_version="strategy-exit-v1",
            risk_score="LOW",
            ta_summary=TechnicalSummaryContract(
                symbol=symbol,
                trend_direction=ta.trend_direction,
                signal_tags=ta.signal_tags,
                rsi_14=ta.indicators.rsi_14,
                macd_histogram=ta.indicators.macd_histogram,
                bb_position=ta.indicators.bb_position,
                data_source=ta.data_source,
                as_of=ta.as_of,
            ),
            research_summary=", ".join(reasons),
        )

    def _get_market_snapshot(self, symbol: str):
        """Bars at the configured timeframe — intraday when MARKET_DATA_TIMEFRAME=intraday."""
        try:
            bars = fetch_bars(symbol, self._market_settings)
        except Exception as exc:
            logger.debug("Bar fetch failed for %s: %s", symbol, exc)
            return None, []
        if not bars:
            return None, []
        source = "intraday" if self._market_settings.is_intraday else "daily"
        return build_ta_summary(symbol, bars, data_source=source), bars

    def _get_ta_snapshot(self, symbol: str):
        ta, _ = self._get_market_snapshot(symbol)
        return ta

    async def _signal_exists(self, symbol: str, action: CandidateAction) -> bool:
        from sqlalchemy import select

        from .database import SessionLocal
        from .models import SignalRecord

        with SessionLocal() as session:
            existing = session.scalar(
                select(SignalRecord).where(
                    SignalRecord.symbol == symbol,
                    SignalRecord.candidate_action == action.value,
                    SignalRecord.acted_on.is_(False),
                )
            )
        return existing is not None

    async def _persist_signal(self, signal: SignalCandidate) -> None:
        from .database import SessionLocal
        from .models import SignalRecord

        ta_json = signal.ta_summary.model_dump_json() if signal.ta_summary else None
        with SessionLocal() as session:
            session.add(
                SignalRecord(
                    signal_id=signal.signal_id,
                    symbol=signal.symbol,
                    ts=signal.ts,
                    candidate_action=signal.candidate_action.value,
                    confidence=signal.confidence,
                    size_pct=signal.size_pct,
                    horizon=signal.horizon,
                    source=signal.source,
                    model_version=signal.model_version,
                    risk_score=signal.risk_score,
                    ta_summary_json=ta_json,
                    research_summary=signal.research_summary,
                    acted_on=False,
                )
            )
            session.commit()

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


def _compute_qty(
    size_pct: float,
    buying_power: float,
    reference_price: float | None,
) -> int:
    """Shares to buy for a target dollar exposure at the current market price.

    Sizing must divide by the real price. Using a fixed placeholder silently
    scales every order by price/placeholder — a $500 share gets 5x the intended
    exposure and a $10 share a fifth of it.
    """
    if reference_price is None or reference_price <= 0:
        logger.warning("No reference price available — cannot size order")
        return 0
    dollar_amount = size_pct * buying_power
    return int(dollar_amount / reference_price)


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _parse_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
