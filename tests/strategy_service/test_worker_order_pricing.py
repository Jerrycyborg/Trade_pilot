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
from market_data import build_ta_summary
from market_data.models import OHLCVBar
from strategy_service.config import settings as worker_settings
from strategy_service.worker import TradeWorker, WorkerRunResult

_UNSET = object()


def _bars(volume: float, n: int = 30, flat: bool = False) -> list[OHLCVBar]:
    """A gently rising series, long enough for ADX to be a measurement.

    Three flat bars used to be enough, because `compute_adx` returns its
    neutral sentinel of 25.0 on a short series and 25.0 clears the worker's
    trend gate. That made these sizing tests run down a path where the regime
    filter was disabled by a fabricated number — which is the defect the worker
    now refuses. The series rises so the regime is genuinely trending; volumes
    stay uniform so the ADV arithmetic below is unchanged.
    """
    now = datetime.now(timezone.utc)
    step = 0.0 if flat else 0.05
    prices = [200.0 - step * (n - 1 - i) for i in range(n)]
    return [
        OHLCVBar(
            symbol="AAPL",
            timestamp=now - timedelta(minutes=5 * (n - 1 - i)),
            open=price, high=price, low=price, close=price, volume=volume,
        )
        for i, price in enumerate(prices)
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


class _AlwaysLive:
    """Stands in for the shared lifecycle authority, answering "yes".

    These tests are about sizing and limit pricing, not about the roster, so
    the gate is stubbed rather than driven through a real promotion. The tests
    that exercise the gate itself are in tests/hardening.
    """

    configured = True

    def __init__(self, permitted: bool = True, reason: str = "live") -> None:
        self._permitted = permitted
        self._reason = reason

    def may_open(self, strategy_id: str, symbol: str, account_id: str | None = None):
        from lifecycle.service import GateAnswer

        return GateAnswer(self._permitted, self._reason)


def _make_live(monkeypatch, permitted: bool = True, reason: str = "live") -> None:
    from lifecycle.service import reset_lifecycle_service

    reset_lifecycle_service(_AlwaysLive(permitted, reason))


@pytest.fixture
def worker_run(monkeypatch: pytest.MonkeyPatch, stub_prices):
    """Drive _process_symbol with every collaborator stubbed, capturing the order."""
    stub_prices.set("AAPL", 200.0)
    _make_live(monkeypatch)
    captured: list[ExecutionOrderRequest] = []

    async def run(
        *,
        volume: float = 50_000_000.0,
        action: CandidateAction = CandidateAction.BUY,
        bar_count: int = 30,
        flat: bool = False,
        ta: object = _UNSET,
    ) -> ExecutionOrderRequest | None:
        worker = TradeWorker()
        bars = _bars(volume, n=bar_count, flat=flat)

        monkeypatch.setattr(
            "strategy_service.worker._build_deterministic_signal", lambda _s: _signal(action)
        )
        summary = (
            build_ta_summary("AAPL", bars, data_source="intraday") if ta is _UNSET else ta
        )
        monkeypatch.setattr(worker, "_get_market_snapshot", lambda _s: (summary, bars))

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
async def test_a_refused_sleeve_places_no_order(worker_run, monkeypatch) -> None:
    """The worker posts to execution-service directly, so it asks the shared
    authority before doing so — otherwise a refusal is only discovered
    downstream, stripped of the context this worker has."""
    _make_live(monkeypatch, permitted=False, reason="sleeve_paper")
    assert await worker_run() is None


@pytest.mark.asyncio
async def test_an_unavailable_authority_places_no_order(worker_run, monkeypatch) -> None:
    """Losing the roster blocks entries. It used to be a per-process JSON file,
    so an outage was invisible and the worker carried on from a stale copy."""
    from lifecycle.service import LifecycleService, reset_lifecycle_service

    reset_lifecycle_service(LifecycleService(store=None))
    assert await worker_run() is None


class TestTheRegimeGateFailsClosed:
    """`compute_adx` returns 25.0 when the series is too short to measure one,
    and 25.0 is *above* the worker's own trend threshold of 20. So the filter
    that exists to keep trend entries out of a range used to pass on thin or
    absent data — the one condition where nothing is known about the regime at
    all. An unmeasurable regime is not a trending one.
    """

    @pytest.mark.asyncio
    async def test_a_short_series_does_not_reach_the_broker(self, worker_run) -> None:
        assert await worker_run(bar_count=6) is None

    @pytest.mark.asyncio
    async def test_no_market_snapshot_at_all_does_not_reach_the_broker(
        self, worker_run
    ) -> None:
        assert await worker_run(ta=None) is None

    @pytest.mark.asyncio
    async def test_a_measurable_trend_still_trades(self, worker_run) -> None:
        """The gate must refuse the unknown without refusing everything: a
        filter that blocks every entry is not fail-closed, it is broken."""
        assert await worker_run() is not None

    @pytest.mark.asyncio
    async def test_a_measurable_range_is_still_refused(self, worker_run) -> None:
        assert await worker_run(flat=True) is None
