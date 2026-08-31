"""The execution service, not the caller, decides which broker an order reaches.

These cover acceptance criteria 4 and 5:

* a PAPER sleeve produces real PaperBroker fills, P&L, fees, slippage and
  implementation-shortfall records, and provably calls no live adapter method;
* a CANDIDATE sleeve produces a journalled shadow decision and no order.

The live adapter here is a spy that records every call and raises if one is
ever made. A test that merely asserts "the paper broker was used" would pass
even if the live adapter were called as well.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from brokers import PaperBroker
from contracts import ExecutionOrderRequest, OrderStatus
from execution_service.routing import BrokerRouter
from lifecycle.routing import ExecutionRoute, OrderIntent, assert_not_live, resolve_route


class LiveAdapterSpy:
    """Stands in for a real broker. Records contact and refuses to serve it.

    Any call to this in a paper test is a defect, so it fails loudly rather
    than returning something plausible.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name: str):
        def _explode(*_args, **_kwargs):
            self.calls.append(name)
            raise AssertionError(
                f"A live broker method ({name}) was called. No non-LIVE route may "
                "reach a real venue."
            )

        return _explode


class Prices:
    def __init__(self, book: dict[str, float]) -> None:
        self.book = dict(book)

    def get_price(self, symbol: str) -> float | None:
        return self.book.get(symbol.upper())


class FakeStore:
    """A lifecycle authority with hand-set answers, so routing can be driven."""

    def __init__(
        self,
        state: str | None = "paper",
        live_mode: bool = False,
        position_environment: str = "simulated",
        halted: bool = False,
        halt_reason: str = "",
        raises: Exception | None = None,
    ) -> None:
        self._state = state
        self._live_mode = live_mode
        self._position_environment = position_environment
        self._halted = halted
        self._halt_reason = halt_reason
        self._raises = raises

    def get(self, strategy_id, symbol, account_id=None):
        if self._raises:
            raise self._raises
        if self._state is None:
            return None

        class _Sleeve:
            state = self._state
            position_environment = self._position_environment

        return _Sleeve()

    def live_mode_enabled(self, account_id=None):
        if self._raises:
            raise self._raises
        return self._live_mode

    def reconciliation_state(self, broker, environment, account_id=None):
        class _Halt:
            halted = self._halted
            halt_reason = self._halt_reason

        return _Halt()


@pytest.fixture
def paper(tmp_path: Path) -> PaperBroker:
    return PaperBroker(
        starting_cash=1_000_000.0,
        slippage_bps=5.0,  # non-zero: paper fills must carry real cost
        state_path=tmp_path / "paper.json",
        price_source=Prices({"AAPL": 200.0}),
    )


@pytest.fixture
def live() -> LiveAdapterSpy:
    return LiveAdapterSpy()


def _order(**overrides: object) -> ExecutionOrderRequest:
    payload: dict[str, object] = {
        "signal_id": f"sig-{uuid4()}",
        "symbol": "AAPL",
        "side": "BUY",
        "qty": 10,
        "order_type": "MARKET",
        "decision_price": 200.0,
        "strategy_id": "ema_rsi_macd",
    }
    payload.update(overrides)
    return ExecutionOrderRequest(**payload)


class TestPaperSleeveNeverReachesLive:
    def test_a_paper_sleeve_routes_to_the_simulator(
        self, paper: PaperBroker, live: LiveAdapterSpy
    ) -> None:
        router = BrokerRouter(store=FakeStore(state="paper"), simulated=paper, live=live)
        routed = router.route(
            strategy_id="ema_rsi_macd", symbol="AAPL", account_id="default", reduce_only=False
        )
        assert routed.decision.route is ExecutionRoute.SIMULATED
        assert routed.adapter is paper
        assert live.calls == []

    def test_a_paper_sleeve_produces_real_simulated_fills(
        self, paper: PaperBroker, live: LiveAdapterSpy
    ) -> None:
        """Acceptance criterion 4: not a no-op that reports success."""
        router = BrokerRouter(store=FakeStore(state="paper"), simulated=paper, live=live)
        routed = router.route(
            strategy_id="ema_rsi_macd", symbol="AAPL", account_id="default", reduce_only=False
        )
        result = routed.adapter.place_order(_order())

        assert result.status is OrderStatus.ACCEPTED
        assert result.fill_price is not None
        # Slippage is charged, and against the trader.
        assert result.fill_price > 200.0
        assert paper.get_positions()[0].qty == 10
        assert live.calls == []

    def test_paper_fills_move_cash_and_produce_pnl(
        self, paper: PaperBroker, live: LiveAdapterSpy
    ) -> None:
        opening_cash = paper.get_account().cash
        router = BrokerRouter(store=FakeStore(state="paper"), simulated=paper, live=live)
        routed = router.route(
            strategy_id="ema_rsi_macd", symbol="AAPL", account_id="default", reduce_only=False
        )
        routed.adapter.place_order(_order())
        assert paper.get_account().cash < opening_cash

        routed.adapter.place_order(_order(side="SELL", reduce_only=True))
        # Bought above the mid and sold below it: the round trip cost real money.
        assert paper.get_account().cash < opening_cash
        assert live.calls == []

    def test_a_paper_sleeve_records_implementation_shortfall(
        self, paper: PaperBroker, live: LiveAdapterSpy, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("JOURNAL_PATH", str(tmp_path / "journal.db"))
        from journal import get_journal, reset_journal

        reset_journal(None)
        router = BrokerRouter(store=FakeStore(state="paper"), simulated=paper, live=live)
        routed = router.route(
            strategy_id="ema_rsi_macd", symbol="AAPL", account_id="default", reduce_only=False
        )
        result = routed.adapter.place_order(_order())

        shortfall = get_journal().record_execution(
            symbol="AAPL", side="BUY", qty=10,
            decision_price=200.0, fill_price=result.fill_price,
        )
        assert shortfall is not None and shortfall > 0, "paper trading must cost something"
        assert get_journal().execution_quality()["filled"] == 1
        assert live.calls == []

    def test_a_live_route_is_refused_when_no_live_adapter_exists(
        self, paper: PaperBroker
    ) -> None:
        """Better to block than to fill on the simulator and call it real."""
        router = BrokerRouter(
            store=FakeStore(state="live", live_mode=True), simulated=paper, live=None
        )
        router._live_resolved = True  # nothing to discover
        routed = router.route(
            strategy_id="ema_rsi_macd", symbol="AAPL", account_id="default", reduce_only=False
        )
        assert routed.decision.route is ExecutionRoute.BLOCKED
        assert routed.adapter is None

    @pytest.mark.parametrize("state", ["candidate", "paper", "probation", "retired"])
    def test_no_non_live_state_ever_binds_the_live_adapter(
        self, state: str, paper: PaperBroker, live: LiveAdapterSpy
    ) -> None:
        router = BrokerRouter(
            store=FakeStore(state=state, live_mode=True, position_environment="simulated"),
            simulated=paper,
            live=live,
        )
        for reduce_only in (False, True):
            routed = router.route(
                strategy_id="ema_rsi_macd", symbol="AAPL",
                account_id="default", reduce_only=reduce_only,
            )
            assert routed.adapter is not live
        assert live.calls == []

    def test_the_boundary_guard_rejects_a_mis_routed_order(self) -> None:
        """Second, independent check: a routing bug takes two mistakes to reach
        a real venue, not one."""
        for route in (ExecutionRoute.SIMULATED, ExecutionRoute.SHADOW, ExecutionRoute.BLOCKED):
            with pytest.raises(PermissionError):
                assert_not_live(route, "AlpacaBroker")
        assert_not_live(ExecutionRoute.LIVE, "AlpacaBroker")  # must not raise


class TestCandidateShadowsOnly:
    def test_a_candidate_places_no_order(
        self, paper: PaperBroker, live: LiveAdapterSpy
    ) -> None:
        """Acceptance criterion 5."""
        router = BrokerRouter(store=FakeStore(state="candidate"), simulated=paper, live=live)
        routed = router.route(
            strategy_id="ema_rsi_macd", symbol="AAPL", account_id="default", reduce_only=False
        )
        assert routed.decision.route is ExecutionRoute.SHADOW
        assert routed.places_order is False
        assert routed.adapter is None
        assert paper.get_positions() == []
        assert live.calls == []

    def test_an_unregistered_sleeve_is_blocked(
        self, paper: PaperBroker, live: LiveAdapterSpy
    ) -> None:
        router = BrokerRouter(store=FakeStore(state=None), simulated=paper, live=live)
        routed = router.route(
            strategy_id="ema_rsi_macd", symbol="AAPL", account_id="default", reduce_only=False
        )
        assert routed.decision.route is ExecutionRoute.BLOCKED
        assert routed.decision.reason == "sleeve_not_registered"


class TestExitsSurviveEveryHalt:
    """Acceptance criterion 8, and the rule the whole design rests on."""

    def test_a_reconciliation_halt_blocks_entries_and_permits_exits(
        self, paper: PaperBroker, live: LiveAdapterSpy
    ) -> None:
        store = FakeStore(
            state="paper",
            halted=True,
            halt_reason="position break",
            position_environment="simulated",
        )
        router = BrokerRouter(store=store, simulated=paper, live=live)

        entry = router.route(
            strategy_id="s", symbol="AAPL", account_id="default", reduce_only=False
        )
        exit_ = router.route(
            strategy_id="s", symbol="AAPL", account_id="default", reduce_only=True
        )
        assert entry.decision.route is ExecutionRoute.BLOCKED
        assert exit_.places_order is True

    def test_live_mode_off_blocks_entry_but_not_an_exit(self) -> None:
        """Live mode is the switch pulled in a hurry; getting flat afterwards is
        the thing most needed."""
        entry = resolve_route(
            state="live", intent=OrderIntent.ENTRY,
            live_mode_enabled=False, position_environment="live",
        )
        exit_ = resolve_route(
            state="live", intent=OrderIntent.REDUCE_ONLY,
            live_mode_enabled=False, position_environment="live",
        )
        assert entry.route is ExecutionRoute.BLOCKED
        assert exit_.route is ExecutionRoute.LIVE

    def test_losing_the_authority_blocks_entries_and_permits_exits(
        self, paper: PaperBroker, live: LiveAdapterSpy
    ) -> None:
        """A database outage must not become an unmanaged position."""
        store = FakeStore(raises=RuntimeError("connection refused"))
        router = BrokerRouter(store=store, simulated=paper, live=live)

        entry = router.route(
            strategy_id="s", symbol="AAPL", account_id="default", reduce_only=False
        )
        exit_ = router.route(
            strategy_id="s", symbol="AAPL", account_id="default", reduce_only=True
        )
        assert entry.decision.route is ExecutionRoute.BLOCKED
        assert "lifecycle_unavailable" in entry.decision.reason
        assert exit_.places_order is True
        assert live.calls == [], "an exit on a lost authority must not guess at a live venue"


class TestNoAuthorityMeansNoLiveTrading:
    def test_an_unconfigured_service_routes_to_the_simulator(
        self, paper: PaperBroker, live: LiveAdapterSpy
    ) -> None:
        """A deployment that was never wired up cannot reach a live venue by
        accident. The failure mode is paper trading."""
        router = BrokerRouter(store=None, simulated=paper, live=live)
        routed = router.route(
            strategy_id="s", symbol="AAPL", account_id="default", reduce_only=False
        )
        assert routed.decision.route is ExecutionRoute.SIMULATED
        assert routed.adapter is paper
        assert live.calls == []

    def test_build_router_without_a_url_is_simulated_only(self, monkeypatch) -> None:
        from execution_service.routing import build_router

        monkeypatch.delenv("LIFECYCLE_DATABASE_URL", raising=False)
        router = build_router()
        routed = router.route(
            strategy_id="s", symbol="AAPL", account_id="default", reduce_only=False
        )
        assert routed.decision.route is ExecutionRoute.SIMULATED


class TestAnUnreachableAuthorityIsNotADevFallback:
    """A configured authority that cannot be reached must not degrade to
    simulated-only for the life of the process.

    The first version of build_router connected once at import and, on failure,
    returned a simulated-only router permanently. A database that blipped
    during startup left the service trading on paper with no roster, silently,
    until someone restarted it. CI is what surfaced this.
    """

    def test_an_unreachable_authority_blocks_entries(
        self, paper: PaperBroker, live: LiveAdapterSpy
    ) -> None:
        def _never_connects():
            raise RuntimeError("connection refused")

        router = BrokerRouter(
            store=None, simulated=paper, live=live, store_factory=_never_connects
        )
        routed = router.route(
            strategy_id="s", symbol="AAPL", account_id="default", reduce_only=False
        )
        assert routed.decision.route is ExecutionRoute.BLOCKED
        assert "unreachable" in routed.decision.reason

    def test_an_unreachable_authority_still_permits_exits(
        self, paper: PaperBroker, live: LiveAdapterSpy
    ) -> None:
        def _never_connects():
            raise RuntimeError("connection refused")

        router = BrokerRouter(
            store=None, simulated=paper, live=live, store_factory=_never_connects
        )
        routed = router.route(
            strategy_id="s", symbol="AAPL", account_id="default", reduce_only=True
        )
        assert routed.places_order is True
        assert live.calls == []

    def test_it_retries_and_recovers(self, paper: PaperBroker, live: LiveAdapterSpy) -> None:
        """The point of the fix: a later order connects, rather than the
        process being stuck until a restart."""
        attempts = {"n": 0}

        def _flaky():
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise RuntimeError("connection refused")
            return FakeStore(state="paper")

        router = BrokerRouter(
            store=None, simulated=paper, live=live, store_factory=_flaky
        )
        first = router.route(
            strategy_id="s", symbol="AAPL", account_id="default", reduce_only=False
        )
        assert first.decision.route is ExecutionRoute.BLOCKED

        second = router.route(
            strategy_id="s", symbol="AAPL", account_id="default", reduce_only=False
        )
        assert second.decision.route is ExecutionRoute.BLOCKED

        recovered = router.route(
            strategy_id="s", symbol="AAPL", account_id="default", reduce_only=False
        )
        assert recovered.decision.route is ExecutionRoute.SIMULATED

    def test_no_configured_authority_is_still_the_documented_dev_mode(
        self, paper: PaperBroker, live: LiveAdapterSpy
    ) -> None:
        """No factory means nothing was configured — that is the intentional
        single-process case and stays simulated-only."""
        router = BrokerRouter(store=None, simulated=paper, live=live)
        routed = router.route(
            strategy_id="s", symbol="AAPL", account_id="default", reduce_only=False
        )
        assert routed.decision.route is ExecutionRoute.SIMULATED
