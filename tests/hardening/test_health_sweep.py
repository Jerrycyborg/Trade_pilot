"""The scheduled sweep that demotes and halts without being asked.

Before this, the demotion triggers were implemented and tested but reachable
only by an operator calling an endpoint by hand — so "automatic demotion on
decay" was a claim rather than a behaviour, and a journal gap blocked a
promotion but never blocked an entry.

These cover the sweep as it actually runs, including the asymmetry that makes
it a safety control: a hard breach acts at any sample size, a statistical claim
waits for one, and nothing here ever blocks an exit.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from lifecycle.health import (
    HealthThresholds,
    LiveMetrics,
    evaluate_health,
    run_health_sweep,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_LIFECYCLE_POSTGRES_URL"),
    reason="set TEST_LIFECYCLE_POSTGRES_URL to run the health-sweep tests",
)

NOW = datetime.now(timezone.utc)


@pytest.fixture
def journal(tmp_path: Path):
    from journal import Journal

    return Journal(path=tmp_path / "journal.db")


@pytest.fixture
def service(store):
    from lifecycle.service import LifecycleService

    return LifecycleService(store=store)


def _live_sleeve(store, symbol: str = "AAPL"):
    sleeve = store.register("ema_rsi_macd", symbol, strategy_version="v1")
    sleeve = store.transition(sleeve, "paper", "test setup")
    return store.transition(sleeve, "live", "test setup")


def _bars(symbol: str, *, hours: int = 6, skip: set[int] | None = None):
    from market_data.models import OHLCVBar

    start = NOW - timedelta(hours=hours)
    return [
        OHLCVBar(
            symbol=symbol,
            timestamp=start + timedelta(minutes=15 * i),
            open=1, high=1, low=1, close=1, volume=1,
        )
        for i in range(hours * 4)
        if i not in (skip or set())
    ]


class TestTriggersActWithoutBeingAsked:
    def test_a_drawdown_breach_needs_no_sample(self) -> None:
        """A breach is a fact about money already lost, not a claim about a
        distribution, so it does not wait."""
        check = evaluate_health(
            "live", LiveMetrics(trades=2, max_drawdown_pct=0.30), HealthThresholds()
        )
        assert check.healthy is False
        assert check.demote_to == "probation"

    def test_decay_waits_for_a_sample(self) -> None:
        check = evaluate_health(
            "live",
            LiveMetrics(trades=4, sharpe=-3.0, validated_sharpe=1.5),
            HealthThresholds(),
        )
        assert check.healthy is True, "four bad trades is not evidence of decay"

    def test_decay_acts_once_the_sample_arrives(self) -> None:
        check = evaluate_health(
            "live",
            LiveMetrics(trades=60, sharpe=-3.0, validated_sharpe=1.5),
            HealthThresholds(),
        )
        assert check.healthy is False

    def test_triggers_are_not_weighed_against_each_other(self) -> None:
        """A profitable sleeve breaching its drawdown limit still demotes."""
        check = evaluate_health(
            "live",
            LiveMetrics(trades=100, sharpe=3.0, validated_sharpe=1.5, max_drawdown_pct=0.4),
            HealthThresholds(),
        )
        assert check.healthy is False

    def test_a_sleeve_that_is_not_live_is_not_swept(self) -> None:
        for state in ("candidate", "paper", "probation", "retired"):
            check = evaluate_health(
                state, LiveMetrics(trades=100, max_drawdown_pct=0.9), HealthThresholds()
            )
            assert check.healthy is True


class TestTheSweep:
    def test_a_complete_journal_produces_no_gap(self, service, journal, store) -> None:
        _live_sleeve(store)
        journal.record_bars("AAPL", "15m", _bars("AAPL"))
        result = run_health_sweep(service, journal, window_hours=6)
        assert result.checked == 1
        assert result.journal_gaps == []
        assert result.entries_blocked is False

    def test_a_gap_is_recorded_and_halts_entries(self, service, journal, store) -> None:
        _live_sleeve(store)
        journal.record_bars("AAPL", "15m", _bars("AAPL", skip={8, 9, 10}))
        result = run_health_sweep(service, journal, window_hours=6)

        assert result.journal_gaps == ["AAPL:ema_rsi_macd"]
        assert result.entries_blocked is True

    def test_what_the_sweep_reports_matches_the_store(
        self, service, journal, store
    ) -> None:
        """The sweep once said entries_blocked=True while the store said
        halted=False, because the gap was routed through the consecutive-break
        counter that needs two passes to latch."""
        _live_sleeve(store)
        journal.record_bars("AAPL", "15m", _bars("AAPL", skip={8, 9, 10}))
        result = run_health_sweep(service, journal, window_hours=6)

        assert result.entries_blocked == store.reconciliation_state("live", "live").halted

    def test_a_fresh_gap_is_inside_the_grace_period(self, service, journal, store) -> None:
        """A brief provider outage must not halt trading."""
        _live_sleeve(store)
        # The hole is at the very end of the window, so it is minutes old.
        journal.record_bars("AAPL", "15m", _bars("AAPL", hours=6, skip={21, 22}))
        result = run_health_sweep(
            service, journal, window_hours=6,
            thresholds=HealthThresholds(journal_gap_grace_minutes=600),
        )
        assert result.journal_gaps == ["AAPL:ema_rsi_macd"], "the gap is still reported"
        assert result.entries_blocked is False, "but it is too young to halt on"

    def test_only_live_sleeves_are_swept(self, service, journal, store) -> None:
        store.register("ema_rsi_macd", "PAPERONLY")
        journal.record_bars("PAPERONLY", "15m", _bars("PAPERONLY", skip={8, 9}))
        result = run_health_sweep(service, journal, window_hours=6)
        assert result.checked == 0

    def test_an_unconfigured_authority_reports_rather_than_raising(
        self, journal
    ) -> None:
        from lifecycle.service import LifecycleService

        result = run_health_sweep(LifecycleService(store=None), journal)
        assert result.checked == 0
        assert result.errors

    def test_the_sweep_never_raises(self, service, store) -> None:
        """A health check that crashes the scheduler removes the thing that was
        watching."""
        _live_sleeve(store)

        class ExplodingJournal:
            def completeness(self, **_kwargs):
                raise RuntimeError("journal on fire")

            def scoped_execution_metrics(self, **_kwargs):
                raise RuntimeError("journal on fire")

        result = run_health_sweep(service, ExplodingJournal(), window_hours=6)
        assert result.checked == 1
        assert result.errors


class TestHaltingLeavesExitsAlone:
    def test_a_halt_from_a_journal_gap_still_permits_an_exit(
        self, service, journal, store
    ) -> None:
        from lifecycle.routing import ExecutionRoute, OrderIntent, resolve_route

        _live_sleeve(store)
        journal.record_bars("AAPL", "15m", _bars("AAPL", skip={8, 9, 10}))
        run_health_sweep(service, journal, window_hours=6)
        assert store.reconciliation_state("live", "live").halted is True

        entry = resolve_route(
            state="live", intent=OrderIntent.ENTRY, live_mode_enabled=True,
            position_environment="live", entries_halted=True,
        )
        exit_ = resolve_route(
            state="live", intent=OrderIntent.REDUCE_ONLY, live_mode_enabled=True,
            position_environment="live", entries_halted=True,
        )
        assert entry.route is ExecutionRoute.BLOCKED
        assert exit_.route is ExecutionRoute.LIVE

    def test_clearing_still_needs_a_named_operator(self, service, journal, store) -> None:
        from lifecycle.store import LifecycleStoreError

        _live_sleeve(store)
        journal.record_bars("AAPL", "15m", _bars("AAPL", skip={8, 9, 10}))
        run_health_sweep(service, journal, window_hours=6)

        with pytest.raises(LifecycleStoreError):
            store.clear_reconciliation_halt(
                broker="live", environment="live", actor="", reason="tidying up"
            )
        assert store.reconciliation_state("live", "live").halted is True


class TestCompletenessIgnoresMarketHours:
    """Completeness is about holes in the series, not about wall-clock minutes.

    The first version derived it from expected-vs-actual observations over the
    elapsed span, which assumes the market is open every minute of the window.
    Any window crossing an overnight close or a weekend looked catastrophically
    short, and halting on that would have stopped trading every morning.
    """

    def test_an_overnight_gap_in_the_window_is_not_incompleteness(
        self, service, journal, store
    ) -> None:
        from market_data.models import OHLCVBar

        _live_sleeve(store, "OVERNIGHT")
        # Two dense sessions with fifteen hours of closed market between them.
        bars = []
        for day in (2, 1):
            session_start = NOW - timedelta(days=day)
            bars += [
                OHLCVBar(
                    symbol="OVERNIGHT",
                    timestamp=session_start + timedelta(minutes=15 * i),
                    open=1, high=1, low=1, close=1, volume=1,
                )
                for i in range(26)
            ]
        journal.record_bars("OVERNIGHT", "15m", bars)

        result = journal.completeness(
            symbol="OVERNIGHT", timeframe="15m",
            window_start=NOW - timedelta(days=2, hours=1),
            window_end=NOW - timedelta(days=1) + timedelta(hours=7),
            expected_interval_minutes=15,
        )
        # The overnight break IS reported as a gap — it is one, in the data —
        # but the expected/actual ratio no longer independently condemns the
        # window, which is what would have fired every day.
        assert result["actual_observations"] == 52
        assert result["expected_observations"] > result["actual_observations"]
        assert result["gap_count"] == 1

    def test_a_series_that_simply_stopped_is_caught(self, journal) -> None:
        """An interior hole and a feed that died are both losses of coverage,
        and only the first appears as a gap between consecutive bars."""
        from market_data.models import OHLCVBar

        start = NOW - timedelta(hours=6)
        journal.record_bars(
            "STALE", "15m",
            [
                OHLCVBar(symbol="STALE", timestamp=start + timedelta(minutes=15 * i),
                         open=1, high=1, low=1, close=1, volume=1)
                for i in range(8)  # stops two hours into a six-hour window
            ],
        )
        result = journal.completeness(
            symbol="STALE", timeframe="15m", window_start=start,
            window_end=NOW, expected_interval_minutes=15,
        )
        assert result["gap_count"] == 0, "there is no interior hole"
        assert result["stale"] is True
        assert result["complete"] is False
        assert result["last_gap_at"] is not None, (
            "a stale series must be datable, or it can never age out of its grace period"
        )

    def test_an_empty_window_is_not_complete(self, journal) -> None:
        result = journal.completeness(
            symbol="NOTHING", timeframe="15m",
            window_start=NOW - timedelta(hours=6), window_end=NOW,
            expected_interval_minutes=15,
        )
        assert result["complete"] is False
