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

    def __init__(
        self,
        permitted: bool = True,
        reason: str = "live",
        challengers: list | None = None,
        parameters: dict | None = None,
    ) -> None:
        self._permitted = permitted
        self._reason = reason
        self._challengers = challengers or []
        self._parameters = parameters or {}
        self.gate_calls: list[str] = []

    def may_open(self, strategy_id: str, symbol: str, account_id: str | None = None):
        from lifecycle.service import GateAnswer

        self.gate_calls.append(strategy_id)
        return GateAnswer(self._permitted, self._reason)

    def paper_challengers(self, symbol: str):
        return list(self._challengers)

    def challenger_parameters(self, challenger_id: str):
        value = self._parameters.get(challenger_id)
        if isinstance(value, Exception):
            raise value
        return value


def _make_live(
    monkeypatch,
    permitted: bool = True,
    reason: str = "live",
    challengers: list | None = None,
    parameters: dict | None = None,
) -> "_AlwaysLive":
    from lifecycle.service import reset_lifecycle_service

    stub = _AlwaysLive(permitted, reason, challengers, parameters)
    reset_lifecycle_service(stub)
    return stub


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
        challengers: list | None = None,
        parameters: dict | None = None,
        bars_override: list | None = None,
        signal_builder=None,
    ) -> ExecutionOrderRequest | None:
        captured.clear()
        if challengers is not None or parameters is not None:
            # Only replace the service when the test wires challengers in;
            # several tests install their own refused/unavailable authority
            # before calling run(), and that setup must stand.
            run.service = _make_live(
                monkeypatch, challengers=challengers, parameters=parameters
            )
        worker = TradeWorker()
        bars = bars_override if bars_override is not None else _bars(
            volume, n=bar_count, flat=flat
        )

        monkeypatch.setattr(
            "strategy_service.worker._build_deterministic_signal",
            signal_builder or (lambda _s, **_kw: _signal(action)),
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
        run.orders = list(captured)
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


def _challenger_sleeve(challenger_id: str = "chal-abc123"):
    """A roster row as the worker sees it: paper, challenger-origin, derived id."""
    from types import SimpleNamespace

    return SimpleNamespace(
        strategy_id=f"ema_rsi_macd@{challenger_id}",
        strategy_version=challenger_id,
        symbol="AAPL",
        state="paper",
        origin="challenger",
        account_id="default",
    )


def _tradeable_bars(n: int = 80) -> list[OHLCVBar]:
    """Rising with dips, ending in a small up-leg: RSI ~69, MACD histogram
    positive, ADX ~27. A monotonic rise pins RSI at 100, which no challenger
    with rsi_buy_max <= 85 may trade — and a fixture no subject can pass tests
    the fixture, not the subject."""
    now = datetime.now(timezone.utc)
    closes, price = [], 190.0
    for i in range(n):
        if i >= n - 3:
            price += 0.25  # a small up-leg so MACD momentum is positive now
        else:
            price += -0.6 if i % 3 == 2 else 0.5
        closes.append(price)
    return [
        OHLCVBar(
            symbol="AAPL",
            timestamp=now - timedelta(minutes=5 * (n - 1 - i)),
            open=c, high=c + 0.1, low=c - 0.1, close=c, volume=50_000_000.0,
        )
        for i, c in enumerate(closes)
    ]


class TestChallengersTradeTheirOwnParameters:
    """L4's missing half. Challenger sleeves could sit on the roster while the
    worker traded one global parameter set, so the comparison had no divergent
    fills to read. The challenger pass runs each paper challenger's *recorded
    proposal* through the identical pipeline, tagged with its derived id.
    """

    WIDE_BAND = {
        "ema_fast": 20.0, "ema_slow": 50.0,
        "rsi_buy_min": 45.0, "rsi_buy_max": 85.0, "macd_hist_min": 0.0,
    }

    @pytest.mark.asyncio
    async def test_champion_and_challenger_both_submit_under_their_own_ids(
        self, worker_run
    ) -> None:
        sleeve = _challenger_sleeve()
        await worker_run(
            bars_override=_tradeable_bars(),
            challengers=[sleeve],
            parameters={"chal-abc123": dict(self.WIDE_BAND)},
        )

        ids = [order.strategy_id for order in worker_run.orders]
        assert ids == ["ema_rsi_macd", "ema_rsi_macd@chal-abc123"]

    @pytest.mark.asyncio
    async def test_the_challenger_trades_its_parameters_not_the_champions(
        self, worker_run
    ) -> None:
        """The same bars, a band that excludes them: the challenger holds while
        the champion trades. If both always agreed, the comparison would be a
        mirror, not a comparison."""
        sleeve = _challenger_sleeve()
        narrow = {**self.WIDE_BAND, "rsi_buy_min": 45.0, "rsi_buy_max": 55.0}
        await worker_run(
            bars_override=_tradeable_bars(),
            challengers=[sleeve],
            parameters={"chal-abc123": narrow},
        )

        ids = [order.strategy_id for order in worker_run.orders]
        assert ids == ["ema_rsi_macd"], "the narrow band excludes these bars"

    @pytest.mark.asyncio
    async def test_a_challenger_with_no_recorded_proposal_trades_nothing(
        self, worker_run
    ) -> None:
        """Parameters come from lifecycle.challenger_proposal and nowhere else.
        Guessing them would record evidence for a strategy nobody proposed."""
        await worker_run(
            bars_override=_tradeable_bars(),
            challengers=[_challenger_sleeve()],
            parameters={},
        )

        ids = [order.strategy_id for order in worker_run.orders]
        assert ids == ["ema_rsi_macd"]

    @pytest.mark.asyncio
    async def test_a_broken_challenger_costs_nobody_else_anything(
        self, worker_run
    ) -> None:
        """One challenger's failure is contained: the champion has already run,
        and the remaining challengers still get their turn."""
        broken = _challenger_sleeve("chal-broken")
        healthy = _challenger_sleeve("chal-healthy")
        await worker_run(
            bars_override=_tradeable_bars(),
            challengers=[broken, healthy],
            parameters={
                "chal-broken": RuntimeError("proposal store on fire"),
                "chal-healthy": dict(self.WIDE_BAND),
            },
        )

        ids = [order.strategy_id for order in worker_run.orders]
        assert ids == ["ema_rsi_macd", "ema_rsi_macd@chal-healthy"]

    @pytest.mark.asyncio
    async def test_the_gate_is_asked_about_the_challengers_own_sleeve(
        self, worker_run
    ) -> None:
        """A challenger is gated against its own roster row, not smuggled
        through under the champion's."""
        await worker_run(
            bars_override=_tradeable_bars(),
            challengers=[_challenger_sleeve()],
            parameters={"chal-abc123": dict(self.WIDE_BAND)},
        )

        assert worker_run.service.gate_calls == [
            "ema_rsi_macd",
            "ema_rsi_macd@chal-abc123",
        ]

    @pytest.mark.asyncio
    async def test_the_entry_gates_bind_challengers_too(self, worker_run) -> None:
        """A challenger proposes thresholds inside the rule; it does not get to
        skip the regime filter that sits in front of the rule. Six bars is not
        a measurable regime for anyone."""
        await worker_run(
            bar_count=6,
            challengers=[_challenger_sleeve()],
            parameters={"chal-abc123": dict(self.WIDE_BAND)},
        )

        assert worker_run.orders == []


class TestTheParameterisedRule:
    def _ta(self, bars):
        return build_ta_summary("AAPL", bars, data_source="intraday")

    def test_champion_defaults_reproduce_the_original_rule_exactly(self) -> None:
        """One code path serves champion and challenger, so the champion's
        behaviour under it must be bit-for-bit what it was. Asserted across
        rising, falling and flat series rather than believed."""
        from strategy_service.rule_engine import CHAMPION_PARAMETERS, evaluate_rules

        for kind, bars in (
            ("tradeable", _tradeable_bars()),
            ("rising", _bars(1_000_000.0, n=80)),
            ("flat", _bars(1_000_000.0, n=80, flat=True)),
        ):
            ta = self._ta(bars)
            plain = evaluate_rules(ta, bars=bars)
            explicit = evaluate_rules(ta, bars=bars, parameters=dict(CHAMPION_PARAMETERS))
            assert (plain.action, plain.confidence, plain.risk_score) == (
                explicit.action, explicit.confidence, explicit.risk_score,
            ), f"defaults diverged on the {kind} series"

    def test_non_default_periods_are_computed_from_the_bars(self) -> None:
        from strategy_service.rule_engine import evaluate_rules

        bars = _tradeable_bars()
        result = evaluate_rules(
            self._ta(bars), bars=bars,
            parameters={"ema_fast": 10.0, "ema_slow": 30.0, "rsi_buy_max": 85.0},
        )
        assert "EMA10" in result.reasoning and "EMA30" in result.reasoning

    def test_too_little_history_for_the_slow_average_holds(self) -> None:
        """Trading a challenger on the champion's averages would record
        evidence for a strategy nobody proposed. The answer is 'not
        evaluated', never a quiet substitution."""
        from strategy_service.rule_engine import evaluate_rules

        bars = _tradeable_bars(20)
        result = evaluate_rules(
            self._ta(bars), bars=bars,
            parameters={"ema_fast": 10.0, "ema_slow": 60.0},
        )
        assert result.action == "HOLD"
        assert result.size_pct == 0.0
        assert "not evaluated" in result.reasoning


class TestTheChampionSignalReadsTheMarket:
    @pytest.mark.asyncio
    async def test_the_deterministic_signal_gets_the_snapshot(
        self, worker_run, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The deterministic path used to build its signal before the snapshot
        existed, so it fell through to the no-TA fallback and traded a hash of
        the symbol name — the rule engine was wired, tested, and never
        consulted on the one path that runs when AI is off."""
        received: dict = {}

        def spy(symbol, ta_summary=None, sentiment_score=None, bars=None):
            received["ta"] = ta_summary
            received["bars"] = bars
            return _signal(CandidateAction.BUY)

        await worker_run(signal_builder=spy)

        assert received["ta"] is not None, "the signal must see the TA summary"
        assert received["bars"], "and the bars it was computed from"
        assert received["ta"].bars_count == len(received["bars"])
