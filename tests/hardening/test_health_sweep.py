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


def _decay(
    *,
    trades: int,
    annualised: float,
    validated: float,
    std_error: float = 0.4,
    **kw,
) -> LiveMetrics:
    """LiveMetrics for the decay trigger, on the *annualised* footing.

    The comparison is annualised-against-annualised: `sharpe` alone is a
    per-trade ratio and deliberately no longer feeds it. A default standard
    error is supplied so these tests state a band rather than depending on one
    computed elsewhere.
    """
    return LiveMetrics(
        trades=trades,
        sharpe_annualised=annualised,
        sharpe_annualised_std_error=std_error,
        validated_sharpe=validated,
        **kw,
    )


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
            _decay(trades=4, annualised=-3.0, validated=1.5),
            HealthThresholds(),
        )
        assert check.healthy is True, "four bad trades is not evidence of decay"

    def test_decay_acts_once_the_sample_arrives(self) -> None:
        check = evaluate_health(
            "live",
            _decay(trades=60, annualised=-3.0, validated=1.5),
            HealthThresholds(),
        )
        assert check.healthy is False

    def test_triggers_are_not_weighed_against_each_other(self) -> None:
        """A profitable sleeve breaching its drawdown limit still demotes."""
        check = evaluate_health(
            "live",
            _decay(trades=100, annualised=3.0, validated=1.5, max_drawdown_pct=0.4),
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


class TestLiveMetricsComeFromRealisedTrades:
    """The decay trigger was implemented and could not fire.

    _live_metrics populated only a trade count, so live Sharpe and drawdown
    were always None and the decay branch was unreachable on real data. It now
    pairs realised round trips through libs/attribution — which is also what
    keeps a healthy paper record from hiding a failing live one, since pairing
    never crosses environments.
    """

    def _round_trip(self, journal, entry: float, exit_: float, *, environment="live"):
        for side, price in (("BUY", entry), ("SELL", exit_)):
            journal.record_execution(
                symbol="AAPL", side=side, qty=10,
                decision_price=price, fill_price=price,
                strategy_id="ema_rsi_macd", environment=environment,
            )

    def test_sharpe_and_drawdown_are_computed(self, service, journal, store) -> None:
        from lifecycle.health import _live_metrics

        sleeve = _live_sleeve(store)
        for entry, exit_ in ((100, 105), (105, 103), (103, 110), (110, 104)):
            self._round_trip(journal, entry, exit_)

        # A live window, not the module-level NOW: record_execution stamps
        # recorded_at with the real clock, which is already past it.
        now = datetime.now(timezone.utc)
        metrics = _live_metrics(service, journal, sleeve, now - timedelta(days=1), now)
        assert metrics.trades == 4
        assert metrics.sharpe is not None
        assert metrics.max_drawdown_amount is not None
        assert metrics.max_drawdown_pct is None, (
            "a percentage needs a capital base, and none was declared"
        )

    def test_a_drawdown_percentage_needs_a_declared_capital_base(
        self, service, journal, store
    ) -> None:
        """It used to divide by the running peak of cumulative P&L, so a sleeve
        that won $20 early and bled to -$168 reported a 940% drawdown and
        breached a limit labelled 15%. The denominator has to be the money at
        risk or the number is not a drawdown percentage."""
        from lifecycle.health import _live_metrics

        sleeve = _live_sleeve(store)
        for entry, exit_ in ((100, 105), (105, 103), (103, 110), (110, 104)):
            self._round_trip(journal, entry, exit_)

        now = datetime.now(timezone.utc)
        metrics = _live_metrics(
            service, journal, sleeve, now - timedelta(days=1), now,
            capital_base=100_000.0,
        )
        assert metrics.max_drawdown_pct is not None
        assert metrics.max_drawdown_pct < 0.01, (
            f"{metrics.max_drawdown_amount} against $100,000 is well under 1%"
        )

    def test_paper_trades_do_not_count_toward_live_health(
        self, service, journal, store
    ) -> None:
        from lifecycle.health import _live_metrics

        sleeve = _live_sleeve(store)
        for _ in range(5):
            self._round_trip(journal, 100, 120, environment="paper")

        now = datetime.now(timezone.utc)
        metrics = _live_metrics(service, journal, sleeve, now - timedelta(days=1), now)
        assert metrics.trades == 0, "a healthy simulator must not mask a live sleeve"

    def test_the_validated_figure_comes_from_the_promotion_snapshot(
        self, service, journal, store
    ) -> None:
        """Compared against what was actually claimed at promotion, not a
        number someone remembers."""
        from lifecycle.health import _live_metrics

        sleeve = store.register("ema_rsi_macd", "MSFT", strategy_version="v1")
        sleeve = store.transition(sleeve, "paper", "setup")
        snapshot = store.record_evidence(
            strategy_id="ema_rsi_macd", strategy_version="v1", symbol="MSFT",
            asset_class="equity", environment="backtest", broker="none",
            account_id="default", portfolio_id="none",
            window_start=NOW - timedelta(days=60), window_end=NOW,
            metrics={"out_of_sample_sharpe": 1.75}, source_artifacts=[],
        )
        sleeve = store.transition(
            sleeve, "live", "promoted", evidence_snapshot_id=snapshot
        )

        now = datetime.now(timezone.utc)
        metrics = _live_metrics(service, journal, sleeve, now - timedelta(days=1), now)
        assert metrics.validated_sharpe == 1.75

    def test_a_decayed_live_sleeve_is_demoted_by_the_sweep(
        self, service, journal, store
    ) -> None:
        """The end of the loop: promoted on a claim, measured against it, and
        taken off live without anyone asking."""
        sleeve = store.register("ema_rsi_macd", "DECAY", strategy_version="v1")
        sleeve = store.transition(sleeve, "paper", "setup")
        snapshot = store.record_evidence(
            strategy_id="ema_rsi_macd", strategy_version="v1", symbol="DECAY",
            asset_class="equity", environment="backtest", broker="none",
            account_id="default", portfolio_id="none",
            window_start=NOW - timedelta(days=60), window_end=NOW,
            metrics={"out_of_sample_sharpe": 3.0}, source_artifacts=[],
        )
        store.transition(sleeve, "live", "promoted", evidence_snapshot_id=snapshot)

        # Twenty-five losing trades with some spread in them: enough to clear
        # the sample gate, and a per-trade Sharpe far below the 3.0 it was
        # promoted on. Identical losses would have zero variance and therefore
        # no computable Sharpe at all — which is itself worth knowing.
        import random

        random.seed(4)
        for _ in range(25):
            exit_price = 100.0 - abs(random.gauss(1.0, 0.4))
            for side, price in (("BUY", 100.0), ("SELL", exit_price)):
                journal.record_execution(
                    symbol="DECAY", side=side, qty=10,
                    decision_price=price, fill_price=price,
                    strategy_id="ema_rsi_macd", environment="live",
                )

        result = run_health_sweep(service, journal, window_hours=24)
        assert any("DECAY" in d for d in result.demoted), result.to_dict()
        assert store.get("ema_rsi_macd", "DECAY").state == "probation"


class TestTheWorstCaseIsNotSilent:
    """Both of the original triggers can go quiet on the worst possible record.

    A sleeve that only ever loses never establishes a positive peak, so its
    drawdown is not measurable as a percentage — reported as None rather than
    0.0, which would have read as "no drawdown". And a sleeve whose losses are
    identical has zero variance, so no Sharpe is computable either. Between
    them, the two triggers that exist to catch a failing sleeve could both stay
    silent on the clearest failure there is.
    """

    def test_an_only_losing_record_has_no_measurable_drawdown(self) -> None:
        from attribution.models import Leg, RoundTrip
        from attribution.report import performance_from_trades

        base = datetime(2025, 6, 1, tzinfo=timezone.utc)
        trips = [
            RoundTrip(
                "s", "A", "live", "default",
                Leg("BUY", 10, 100.0, 100.0, base + timedelta(hours=i)),
                Leg("SELL", 10, 99.0, 99.0, base + timedelta(hours=i, minutes=30)),
                10,
            )
            for i in range(25)
        ]
        performance = performance_from_trades(trips)
        assert performance["max_drawdown_pct"] is None, (
            "0.0 would read as 'no drawdown' for the worst possible record"
        )
        assert performance["max_cumulative_loss"] < 0, "the fact is carried here instead"

    def test_identical_losses_have_no_computable_sharpe(self) -> None:
        from attribution.models import Leg, RoundTrip
        from attribution.report import performance_from_trades

        base = datetime(2025, 6, 1, tzinfo=timezone.utc)
        trips = [
            RoundTrip(
                "s", "A", "live", "default",
                Leg("BUY", 10, 100.0, 100.0, base + timedelta(hours=i)),
                Leg("SELL", 10, 99.0, 99.0, base + timedelta(hours=i, minutes=30)),
                10,
            )
            for i in range(25)
        ]
        assert performance_from_trades(trips)["sharpe"] is None

    def test_losing_outright_is_caught_when_the_others_cannot_be(self) -> None:
        check = evaluate_health(
            "live",
            LiveMetrics(
                trades=25, sharpe=None, max_drawdown_pct=None,
                realized_total=-250.0, win_rate=0.0,
            ),
            HealthThresholds(),
        )
        assert check.healthy is False
        assert "losing outright" in check.reasons[0]

    def test_it_still_waits_for_a_sample(self) -> None:
        """Four bad trades is not a broken strategy."""
        check = evaluate_health(
            "live",
            LiveMetrics(trades=4, realized_total=-100.0, win_rate=0.0),
            HealthThresholds(),
        )
        assert check.healthy is True

    def test_a_profitable_sleeve_is_untouched(self) -> None:
        check = evaluate_health(
            "live",
            LiveMetrics(
                trades=50, sharpe=1.0, max_drawdown_pct=0.05,
                realized_total=500.0, win_rate=0.6,
            ),
            HealthThresholds(),
        )
        assert check.healthy is True

    def test_a_losing_sleeve_that_still_wins_often_is_not_caught_here(self) -> None:
        """The trigger is for a broken sleeve, not an unprofitable week. A 50%
        win rate losing money is a sizing or exit problem, which the decay and
        drawdown triggers are the right instruments for."""
        check = evaluate_health(
            "live",
            LiveMetrics(trades=50, realized_total=-100.0, win_rate=0.5),
            HealthThresholds(),
        )
        assert check.healthy is True


class TestTheDecayComparisonIsBetweenLikeAndLike:
    """The trigger used to read a raw per-trade Sharpe against a validated
    figure that is annualised and bar-based. Those are not the same kind of
    number, and the error is not small: a per-trade 0.20 at roughly 250 trades
    a year is an annualised 3.16, so a sleeve comfortably beating a validated
    2.50 read as 2.30 *below* it — and got demoted for outperforming.
    """

    def test_a_sleeve_beating_its_validation_is_not_demoted(self) -> None:
        """The original defect, stated as the outcome rather than the units."""
        import math

        per_trade, trades_per_year = 0.20, 250.0
        check = evaluate_health(
            "live",
            LiveMetrics(
                trades=40,
                sharpe=per_trade,
                sharpe_annualised=round(per_trade * math.sqrt(trades_per_year), 4),
                sharpe_annualised_std_error=2.5,
                trades_per_year=trades_per_year,
                validated_sharpe=2.5,
                realized_total=900.0,
                win_rate=0.6,
                max_drawdown_pct=0.04,
            ),
            HealthThresholds(),
        )
        assert check.healthy is True, "annualised 3.16 beats a validated 2.50"

    def test_a_genuinely_decayed_sleeve_still_demotes(self) -> None:
        """The band must not be so wide that nothing trips it. A sleeve running
        at an annualised -2.37 against a validated +2.50 is broken by any
        reading, and a health check that passes it is not one."""
        check = evaluate_health(
            "live",
            _decay(trades=40, annualised=-2.37, validated=2.5, std_error=2.51),
            HealthThresholds(),
        )
        assert check.healthy is False
        assert "annualised" in check.reasons[0]

    def test_the_band_widens_when_the_sample_is_small(self) -> None:
        """The same shortfall is evidence over 500 trades and noise over 20.
        A fixed absolute gap could not express that; a standard error can."""
        gap_is_1_5 = dict(annualised=0.0, validated=1.5)
        noisy = evaluate_health(
            "live", _decay(trades=25, std_error=3.0, **gap_is_1_5), HealthThresholds()
        )
        settled = evaluate_health(
            "live", _decay(trades=500, std_error=0.3, **gap_is_1_5), HealthThresholds()
        )
        assert noisy.healthy is True
        assert settled.healthy is False

    def test_a_trivial_shortfall_does_not_churn_a_working_sleeve(self) -> None:
        """With enough trades any gap becomes significant. The absolute floor
        stops the trigger acting on a difference nobody would act on if shown
        it directly."""
        check = evaluate_health(
            "live",
            _decay(trades=5000, annualised=1.45, validated=1.5, std_error=0.01),
            HealthThresholds(),
        )
        assert check.healthy is True

    def test_an_unscalable_record_stays_quiet_rather_than_guessing(self) -> None:
        """When the live span is too short to measure a trade frequency, the
        annualised figure is absent. Falling back to the per-trade ratio would
        reinstate the original defect; the drawdown and losing-outright
        triggers still cover the worst case without needing a scaling."""
        check = evaluate_health(
            "live",
            LiveMetrics(
                trades=40,
                sharpe=-0.15,
                sharpe_annualised=None,
                validated_sharpe=2.5,
                realized_total=-400.0,
                win_rate=0.35,
                max_drawdown_pct=0.04,
            ),
            HealthThresholds(),
        )
        assert check.healthy is True
        assert check.reasons == []


class TestThresholdsHaveOneSourceOfTruth:
    def test_an_unset_variable_leaves_the_field_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("LIFECYCLE_SHARPE_DECAY_SIGMAS", raising=False)
        assert HealthThresholds.from_env().sharpe_decay_sigmas == (
            HealthThresholds().sharpe_decay_sigmas
        ), "from_env must not carry its own copy of a default"

    def test_a_set_variable_overrides_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LIFECYCLE_SHARPE_DECAY_SIGMAS", "3")
        assert HealthThresholds.from_env().sharpe_decay_sigmas == 3.0

    def test_an_unparseable_value_is_refused_not_ignored(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Silently falling back would leave a trading system running on a
        default the operator believes they replaced."""
        monkeypatch.setenv("LIFECYCLE_MAX_LIVE_DRAWDOWN", "fifteen percent")
        with pytest.raises(ValueError, match="LIFECYCLE_MAX_LIVE_DRAWDOWN"):
            HealthThresholds.from_env()

    def test_the_retired_variable_no_longer_does_anything(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """LIFECYCLE_MAX_SHARPE_DECAY named an absolute gap the check no longer
        uses. Rebinding it to the sigma band would silently change what an
        operator's existing setting means."""
        monkeypatch.setenv("LIFECYCLE_MAX_SHARPE_DECAY", "99")
        assert HealthThresholds.from_env().sharpe_decay_sigmas == 1.0


class TestTheDrawdownTriggerKnowsWhatItIsAFractionOf:
    """A percentage with no denominator is not a percentage. This trigger used
    to divide the peak-to-trough fall of cumulative P&L by the running peak of
    that same curve — a number that is tiny early in a sleeve's life — so it
    breached its 15% limit on almost any losing sleeve that had one good trade
    first, at magnitudes like 940%.
    """

    def test_a_percentage_limit_fires_against_the_capital_base(self) -> None:
        check = evaluate_health(
            "live",
            LiveMetrics(trades=2, max_drawdown_pct=0.30, max_drawdown_amount=30_000.0),
            HealthThresholds(capital_base=100_000.0),
        )
        assert check.healthy is False
        assert "capital base" in check.reasons[0]

    def test_a_small_real_loss_is_not_a_breach(self) -> None:
        """$188 lost on a $100,000 account is 0.19%, not 940%."""
        check = evaluate_health(
            "live",
            LiveMetrics(trades=30, max_drawdown_pct=0.0019, max_drawdown_amount=188.0),
            HealthThresholds(capital_base=100_000.0),
        )
        assert check.healthy is True

    def test_an_absolute_limit_works_without_a_capital_base(self) -> None:
        check = evaluate_health(
            "live",
            LiveMetrics(trades=30, max_drawdown_amount=5_000.0),
            HealthThresholds(max_live_drawdown_amount=2_500.0),
        )
        assert check.healthy is False
        assert "5,000.00" in check.reasons[0]

    def test_neither_configured_warns_rather_than_passing_quietly(self) -> None:
        """"We did not check" reported as "healthy" is how a safety control
        becomes decoration."""
        check = evaluate_health(
            "live",
            LiveMetrics(trades=30, max_drawdown_amount=50_000.0),
            HealthThresholds(),
        )
        assert check.healthy is True, "it cannot demote on a check it did not run"
        assert check.warnings, "but it must not be silent about not running it"
        assert "LIFECYCLE_CAPITAL_BASE_USD" in check.warnings[0]

    def test_the_sweep_surfaces_the_warning_once(self, service, journal, store) -> None:
        _live_sleeve(store, "AAPL")
        _live_sleeve(store, "MSFT")
        for symbol in ("AAPL", "MSFT"):
            journal.record_bars(symbol, "15m", _bars(symbol))
            for _ in range(3):
                for side, price in (("BUY", 100), ("SELL", 96)):
                    journal.record_execution(
                        symbol=symbol, side=side, qty=10, decision_price=price,
                        fill_price=price, strategy_id="ema_rsi_macd",
                        environment="live",
                    )

        result = run_health_sweep(service, journal, window_hours=6)

        assert result.warnings, "an undeclared capital base is a finding"
        assert len(result.warnings) == 1, "one configuration problem, not one per sleeve"
