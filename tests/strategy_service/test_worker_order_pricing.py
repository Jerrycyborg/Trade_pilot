"""Order pricing and volume-aware sizing on the strategy worker's path.

The worker submits orders independently of the orchestrator, so the same two
guarantees have to hold here: a marketable limit carrying its decision price,
and a size trimmed to a share of what the symbol trades.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from contracts import CandidateAction, ExecutionOrderRequest, PortfolioContext, SignalCandidate
from lifecycle import DEFAULT_LIVE_STRATEGY, Evidence, LifecycleRegistry, State
from market_data.models import OHLCVBar
from strategy_service.config import settings as worker_settings
from strategy_service.worker import TradeWorker, WorkerRunResult


def _bars(volume: float, n: int = 3) -> list[OHLCVBar]:
    now = datetime.now(timezone.utc)
    return [
        OHLCVBar(
            symbol="AAPL",
            timestamp=now - timedelta(minutes=5 * i),
            open=200.0, high=200.0, low=200.0, close=200.0, volume=volume,
        )
        for i in range(n)
    ]


def _signal(action: CandidateAction = CandidateAction.BUY) -> SignalCandidate:
    return SignalCandidate(
        signal_id="sig-1",
        symbol="AAPL",
        ts=datetime.now(timezone.utc),
        candidate_action=action,
        confidence=0.8,
        size_pct=0.05,
        model_version="test",
    )


def _make_live(strategy: str = DEFAULT_LIVE_STRATEGY, symbol: str = "AAPL") -> None:
    """Put a sleeve on the roster as live, so orders are permitted."""
    registry = LifecycleRegistry()
    registry.register(strategy, symbol)
    registry.promote(
        strategy,
        symbol,
        Evidence(
            deflated_sharpe_ratio=0.97,
            out_of_sample_sharpe=1.4,
            out_of_sample_return_pct=0.08,
            out_of_sample_trades=45,
        ),
    )
    registry.promote(
        strategy,
        symbol,
        Evidence(
            paper_started_at=datetime.now(timezone.utc) - timedelta(days=30),
            paper_decisions=40,
            measured_shortfall_bps=2.5,
            max_correlation_with_live=0.2,
        ),
    )
    assert registry.get(strategy, symbol).state is State.LIVE


@pytest.fixture
def worker_run(monkeypatch: pytest.MonkeyPatch, stub_prices):
    """Drive _process_symbol with every collaborator stubbed, capturing the order."""
    stub_prices.set("AAPL", 200.0)
    _make_live()
    captured: list[ExecutionOrderRequest] = []

    async def run(
        *,
        volume: float = 50_000_000.0,
        action: CandidateAction = CandidateAction.BUY,
    ) -> ExecutionOrderRequest | None:
        worker = TradeWorker()
        bars = _bars(volume)

        monkeypatch.setattr(
            "strategy_service.worker._build_deterministic_signal", lambda _s: _signal(action)
        )
        monkeypatch.setattr(worker, "_get_market_snapshot", lambda _s: (None, bars))

        async def buying_power() -> float:
            return 100_000.0

        async def portfolio_context() -> PortfolioContext:
            return PortfolioContext(gross_exposure_pct=0.0, daily_drawdown_pct=0.0)

        async def call_policy(_req) -> dict:
            return {"decision": "APPROVE", "approved_size_pct": 0.05}

        async def submit(req: ExecutionOrderRequest, idempotency_key: str) -> bool:
            captured.append(req)
            return True

        monkeypatch.setattr(worker, "_get_buying_power", buying_power)
        monkeypatch.setattr(worker, "_get_portfolio_context", portfolio_context)
        monkeypatch.setattr(worker, "_call_policy", call_policy)
        monkeypatch.setattr(worker, "_submit_order", submit)
        # Daily bars, so a bar's volume is a day's volume and the arithmetic
        # below stays readable.
        monkeypatch.setattr(
            worker, "_market_settings", replace(worker._market_settings, timeframe="daily")
        )
        # Uniform bar volumes would otherwise trip the volume-confirmation
        # gate and turn every BUY into a HOLD before sizing is reached.
        monkeypatch.setattr(
            "strategy_service.worker.settings",
            replace(worker_settings, volume_confirm_enabled=False),
        )

        await worker._process_symbol("AAPL", WorkerRunResult())
        return captured[-1] if captured else None

    return run


@pytest.mark.asyncio
async def test_the_worker_sends_a_marketable_limit(
    worker_run, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("USE_LIMIT_ORDERS", "true")
    monkeypatch.setenv("LIMIT_TOLERANCE_BPS", "10")
    order = await worker_run()

    assert order is not None
    assert order.order_type == "LIMIT"
    assert order.time_in_force == "IOC"
    assert order.limit_price == 200.2
    assert order.decision_price == 200.0


@pytest.mark.asyncio
async def test_a_sell_is_priced_on_the_other_side(
    worker_run, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("USE_LIMIT_ORDERS", "true")
    monkeypatch.setenv("LIMIT_TOLERANCE_BPS", "10")
    order = await worker_run(action=CandidateAction.SELL)

    assert order is not None
    assert order.limit_price == 199.8


@pytest.mark.asyncio
async def test_limit_orders_can_be_turned_off(
    worker_run, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("USE_LIMIT_ORDERS", "false")
    order = await worker_run()

    assert order is not None
    assert order.order_type == "MARKET"
    assert order.limit_price is None
    # The decision price is still needed — market orders have a shortfall too.
    assert order.decision_price == 200.0


@pytest.mark.asyncio
async def test_a_thin_symbol_trims_the_order(
    worker_run, monkeypatch: pytest.MonkeyPatch
) -> None:
    """$5,000 at $200 is 25 shares; 1% of a 1,000-share day is 10."""
    monkeypatch.setenv("MAX_ADV_PARTICIPATION", "0.01")
    order = await worker_run(volume=1_000.0)

    assert order is not None
    assert order.qty == 10


@pytest.mark.asyncio
async def test_a_liquid_symbol_is_untouched(
    worker_run, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAX_ADV_PARTICIPATION", "0.01")
    order = await worker_run(volume=50_000_000.0)

    assert order is not None
    assert order.qty == 25


@pytest.mark.asyncio
async def test_a_symbol_too_thin_to_trade_submits_nothing(
    worker_run, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAX_ADV_PARTICIPATION", "0.01")
    assert await worker_run(volume=50.0) is None


# ---------------------------------------------------------------------------
# The roster gates this path too
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_an_unregistered_sleeve_places_no_order(worker_run, monkeypatch) -> None:
    """The worker posts to execution-service directly, so it has to enforce the
    roster itself — otherwise it walks around the orchestrator's gate."""
    from strategy_service.worker import reset_lifecycle

    monkeypatch.setenv("LIFECYCLE_STATE_PATH", "/nonexistent/roster.json")
    reset_lifecycle()
    assert await worker_run() is None


@pytest.mark.asyncio
async def test_a_paper_sleeve_places_no_order(worker_run, monkeypatch, tmp_path) -> None:
    from strategy_service.worker import reset_lifecycle

    monkeypatch.setenv("LIFECYCLE_STATE_PATH", str(tmp_path / "paper.json"))
    reset_lifecycle()
    registry = LifecycleRegistry()
    registry.register(DEFAULT_LIVE_STRATEGY, "AAPL")
    registry.promote(
        DEFAULT_LIVE_STRATEGY,
        "AAPL",
        Evidence(
            deflated_sharpe_ratio=0.97,
            out_of_sample_return_pct=0.08,
            out_of_sample_trades=45,
        ),
    )
    assert registry.get(DEFAULT_LIVE_STRATEGY, "AAPL").state is State.PAPER
    reset_lifecycle()
    assert await worker_run() is None
