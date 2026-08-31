"""L0: attribution over the point-in-time archive.

The load-bearing test here is `test_the_decomposition_reconstructs_the_result`.
A decomposition that does not add back up to the realised number is a story
about a trade rather than an account of one, and every conclusion drawn from it
would be unfalsifiable.

The second theme is honesty about gaps. L0 exists to find out whether the
archive can explain outcomes; an attribution that quietly treated a missing
execution price as zero cost would make the archive look richer than it is,
which is the one result that would make the phase worthless.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from attribution import (
    attribute,
    build_report,
    pair_round_trips,
    perfect_exit,
    run_counterfactuals,
    stop_at,
)

NOW = datetime(2025, 6, 2, 14, 0, tzinfo=timezone.utc)


@pytest.fixture
def journal(tmp_path: Path):
    from journal import Journal

    return Journal(path=tmp_path / "journal.db")


def _row(side, fill, decision, minutes, *, qty=10.0, environment="paper", fees=0.0, **kw):
    row = {
        "side": side,
        "qty": qty,
        "filled_qty": qty,
        "fill_price": fill,
        "decision_price": decision,
        "recorded_at": NOW + timedelta(minutes=minutes),
        "filled": True,
        "strategy_id": "ema_rsi_macd",
        "symbol": "AAPL",
        "environment": environment,
        "account_id": "default",
        "fees": fees,
        "outcome": "filled",
    }
    row.update(kw)
    return row


def _trip(entry_fill=100.0, entry_dec=100.0, exit_fill=110.0, exit_dec=110.0, qty=10.0):
    return pair_round_trips(
        [
            _row("BUY", entry_fill, entry_dec, 0, qty=qty),
            _row("SELL", exit_fill, exit_dec, 60, qty=qty),
        ]
    )[0]


class TestPairing:
    def test_a_buy_and_a_sell_make_one_round_trip(self) -> None:
        trips = pair_round_trips([_row("BUY", 100.0, 100.0, 0), _row("SELL", 110.0, 110.0, 60)])
        assert len(trips) == 1
        assert trips[0].realized_per_share == pytest.approx(10.0)

    def test_paper_and_live_fills_are_never_paired(self) -> None:
        """A round trip across two environments never existed, and its result
        would be computed across two different kinds of money."""
        trips = pair_round_trips(
            [
                _row("BUY", 100.0, 100.0, 0, environment="paper"),
                _row("SELL", 110.0, 110.0, 60, environment="live"),
            ]
        )
        assert trips == []

    def test_two_strategies_on_one_symbol_do_not_cross(self) -> None:
        trips = pair_round_trips(
            [
                _row("BUY", 100.0, 100.0, 0),
                _row("SELL", 110.0, 110.0, 60, strategy_id="bollinger_reversion"),
            ]
        )
        assert trips == []

    def test_partial_closes_split_the_entry(self) -> None:
        trips = pair_round_trips(
            [
                _row("BUY", 100.0, 100.0, 0, qty=10),
                _row("SELL", 110.0, 110.0, 30, qty=4),
                _row("SELL", 120.0, 120.0, 60, qty=6),
            ]
        )
        assert [t.qty for t in trips] == [4, 6]

    def test_fifo_matches_the_oldest_entry_first(self) -> None:
        trips = pair_round_trips(
            [
                _row("BUY", 100.0, 100.0, 0, qty=5),
                _row("BUY", 200.0, 200.0, 10, qty=5),
                _row("SELL", 110.0, 110.0, 60, qty=5),
            ]
        )
        assert len(trips) == 1
        assert trips[0].entry.fill_price == 100.0, "FIFO, matching the portfolio ledger"

    def test_an_unfilled_order_is_not_a_position(self) -> None:
        rows = [_row("BUY", None, 100.0, 0), _row("SELL", 110.0, 110.0, 60)]
        rows[0]["filled"] = False
        assert pair_round_trips(rows) == []

    def test_an_open_position_is_not_closed_at_a_guess(self) -> None:
        assert pair_round_trips([_row("BUY", 100.0, 100.0, 0)]) == []

    def test_a_short_round_trip_pairs_sell_then_buy(self) -> None:
        """The first live paper run's only clean trade was a short — SELL to
        open, BUY to flatten. A pairing that hard-coded BUY-opens/SELL-closes
        reported "no closed round trips" over an archive that held one, so L0
        coverage over a real run read as empty. Direction comes from netting,
        not from the side label."""
        trips = pair_round_trips(
            [_row("SELL", 110.0, 110.0, 0), _row("BUY", 100.0, 100.0, 60)]
        )
        assert len(trips) == 1
        trip = trips[0]
        assert trip.direction == -1
        assert trip.realized_per_share == pytest.approx(10.0)
        assert attribute(trip).identity_holds()

    def test_short_lots_close_fifo_like_long_ones(self) -> None:
        trips = pair_round_trips(
            [
                _row("SELL", 110.0, 110.0, 0, qty=5),
                _row("SELL", 120.0, 120.0, 10, qty=5),
                _row("BUY", 100.0, 100.0, 60, qty=5),
            ]
        )
        assert len(trips) == 1
        assert trips[0].entry.fill_price == 110.0

    def test_a_buy_that_overshoots_the_short_flips_to_a_long(self) -> None:
        """Closing a 7-lot short with a 12-lot BUY leaves a 5-lot long open —
        one closed short trip, and the remainder is a position, not an error
        and not a second trip until something sells it."""
        trips = pair_round_trips(
            [
                _row("SELL", 110.0, 110.0, 0, qty=7),
                _row("BUY", 100.0, 100.0, 60, qty=12),
                _row("SELL", 105.0, 105.0, 120, qty=5),
            ]
        )
        assert [(t.direction, t.qty) for t in trips] == [(-1, 7.0), (1, 5.0)]
        assert trips[1].entry.fill_price == 100.0, "the flip remainder is the entry"


class TestTheDecompositionIsExact:
    def test_the_decomposition_reconstructs_the_result(self) -> None:
        """The identity that makes this an attribution rather than a narrative."""
        trip = _trip(entry_fill=100.5, entry_dec=100.0, exit_fill=109.5, exit_dec=110.0)
        result = attribute(trip)

        assert result.complete
        assert result.identity_holds()
        assert result.total == pytest.approx(trip.realized_per_share)

    @pytest.mark.parametrize(
        ("entry_fill", "entry_dec", "exit_fill", "exit_dec"),
        [
            (100.0, 100.0, 110.0, 110.0),
            (100.5, 100.0, 109.5, 110.0),
            (99.0, 100.0, 111.0, 110.0),
            (100.0, 100.0, 90.0, 90.0),
            (105.0, 100.0, 95.0, 100.0),
        ],
    )
    def test_the_identity_holds_across_shapes(
        self, entry_fill, entry_dec, exit_fill, exit_dec
    ) -> None:
        result = attribute(_trip(entry_fill, entry_dec, exit_fill, exit_dec))
        assert result.identity_holds(), result.to_dict()

    def test_a_perfectly_executed_trade_is_all_signal(self) -> None:
        result = attribute(_trip(100.0, 100.0, 110.0, 110.0))
        assert result.signal == pytest.approx(10.0)
        assert result.entry_execution == pytest.approx(0.0)
        assert result.exit_execution == pytest.approx(0.0)

    def test_a_flat_signal_traded_badly_is_all_execution(self) -> None:
        """The case worth separating: the strategy was right that nothing would
        happen, and lost money anyway."""
        result = attribute(_trip(100.5, 100.0, 99.5, 100.0))
        assert result.signal == pytest.approx(0.0)
        assert result.entry_execution == pytest.approx(-0.5)
        assert result.exit_execution == pytest.approx(-0.5)
        assert result.round_trip.realized_per_share == pytest.approx(-1.0)


class TestGapsAreNamedNotZeroed:
    def test_a_missing_decision_price_is_reported(self) -> None:
        result = attribute(_trip(entry_dec=None))
        assert result.complete is False
        assert "entry_decision_price" in result.missing
        assert result.entry_execution is None, (
            "zero would read as 'execution cost nothing', not 'we did not record it'"
        )

    def test_a_partial_attribution_has_no_total(self) -> None:
        result = attribute(_trip(exit_dec=None))
        assert result.total is None

    def test_fees_are_kept_out_of_the_identity(self) -> None:
        trip = pair_round_trips(
            [
                _row("BUY", 100.0, 100.0, 0, fees=1.0),
                _row("SELL", 110.0, 110.0, 60, fees=1.5),
            ]
        )[0]
        result = attribute(trip)
        assert result.fees == pytest.approx(2.5)
        assert result.identity_holds(), "fees are reported alongside, not folded in"


class TestCounterfactualsUseWhatWasKnowable:
    def _bars(self, closes):
        return [
            {
                "bar_ts": NOW + timedelta(minutes=15 * (i + 1)),
                "open": c, "high": c + 1, "low": c - 1, "close": c,
            }
            for i, c in enumerate(closes)
        ]

    def test_perfect_exit_bounds_the_opportunity(self) -> None:
        trip = _trip(100.0, 100.0, 105.0, 105.0)
        result = perfect_exit(trip, self._bars([102, 108, 104]))
        assert result.per_share == pytest.approx(9.0)  # high of 108 + 1
        assert result.difference == pytest.approx(4.0)

    def test_a_stop_that_was_never_touched_changes_nothing(self) -> None:
        trip = _trip(100.0, 100.0, 105.0, 105.0)
        result = stop_at(trip, self._bars([102, 103, 104]), distance=10.0)
        assert result.difference == pytest.approx(0.0)

    def test_a_stop_that_was_touched_fills_at_the_stop(self) -> None:
        """The bar opens above the stop and dips through it intraday, so the
        stop fills at its own price — the gapped case is the next test."""
        trip = _trip(100.0, 100.0, 105.0, 105.0)
        dipped = [
            {"bar_ts": NOW + timedelta(minutes=15), "open": 101.0,
             "high": 101.5, "low": 96.0, "close": 99.0}
        ]
        result = stop_at(trip, dipped, distance=2.0)
        assert result.per_share == pytest.approx(-2.0)

    def test_a_gap_through_the_stop_fills_worse_not_better(self) -> None:
        """Being generous here would make every alternative stop look good."""
        trip = _trip(100.0, 100.0, 105.0, 105.0)
        gapped = [{"bar_ts": NOW + timedelta(minutes=15), "open": 90.0,
                   "high": 91.0, "low": 89.0, "close": 90.0}]
        result = stop_at(trip, gapped, distance=2.0)
        assert result.per_share == pytest.approx(-10.0), "filled at the open, not the stop"

    def test_counterfactuals_without_bars_say_so(self) -> None:
        results = run_counterfactuals(_trip(), bars=[])
        assert all(not c.available for c in results)
        assert all(c.reason for c in results)


class TestTheCoverageReport:
    def _record(self, journal, **kw):
        journal.record_execution(
            symbol="AAPL", strategy_id="ema_rsi_macd", environment="paper", **kw
        )

    def test_an_empty_archive_reports_nothing_to_explain(self, journal) -> None:
        report = build_report(journal)
        assert report["coverage"]["round_trips"] == 0
        assert "nothing to explain" in report["coverage"]["verdict"]

    def test_a_fully_recorded_trade_is_attributable(self, journal) -> None:
        self._record(journal, side="BUY", qty=10, decision_price=100.0, fill_price=100.5)
        self._record(journal, side="SELL", qty=10, decision_price=110.0, fill_price=109.5)

        report = build_report(journal, with_counterfactuals=False)
        assert report["coverage"]["round_trips"] == 1
        assert report["coverage"]["coverage"] == 1.0
        assert report["coverage"]["identity_failures"] == 0

    def test_a_missing_field_lowers_coverage_and_is_named(self, journal) -> None:
        """The L0 finding: not 'the strategy did badly' but 'we cannot tell'."""
        self._record(journal, side="BUY", qty=10, decision_price=None, fill_price=100.5)
        self._record(journal, side="SELL", qty=10, decision_price=110.0, fill_price=109.5)

        coverage = build_report(journal, with_counterfactuals=False)["coverage"]
        assert coverage["coverage"] == 0.0
        assert coverage["missing_counts"]["entry_decision_price"] == 1
        assert "cannot explain" in coverage["verdict"]

    def test_totals_only_count_fully_attributable_trades(self, journal) -> None:
        """Including partial ones would treat unrecorded execution cost as zero,
        which is the direction that flatters."""
        self._record(journal, side="BUY", qty=10, decision_price=100.0, fill_price=100.0)
        self._record(journal, side="SELL", qty=10, decision_price=110.0, fill_price=110.0)
        self._record(journal, side="BUY", qty=10, decision_price=None, fill_price=100.0)
        self._record(journal, side="SELL", qty=10, decision_price=110.0, fill_price=110.0)

        totals = build_report(journal, with_counterfactuals=False)["totals"]
        assert totals["trades"] == 1
        assert totals["identity_matches_realized"] is True

    def test_environments_are_counted_separately(self, journal) -> None:
        for env in ("paper", "live"):
            journal.record_execution(
                symbol="AAPL", side="BUY", qty=10, decision_price=100.0,
                fill_price=100.0, strategy_id="ema_rsi_macd", environment=env,
            )
            journal.record_execution(
                symbol="AAPL", side="SELL", qty=10, decision_price=110.0,
                fill_price=110.0, strategy_id="ema_rsi_macd", environment=env,
            )
        coverage = build_report(journal, with_counterfactuals=False)["coverage"]
        assert coverage["environments"] == {"paper": 1, "live": 1}


class TestRegimeIsClassifiedNotGuessed:
    """The gap L0 shipped with: the decomposition separated signal from
    execution but could not say whether the signal was wrong or merely applied
    in conditions it was not built for.

    The load-bearing property here is that an unmeasurable regime reports
    itself as unmeasurable. `compute_adx` returns 25.0 on a short series, which
    sits above every trend threshold in this codebase — so a classifier that
    trusted it would label thin data "trending" and quietly turn the absence of
    evidence into evidence.
    """

    def _series(self, closes, *, start=NOW, minutes=15, spread=0.5):
        return [
            {
                "bar_ts": start + timedelta(minutes=minutes * i),
                "open": c,
                "high": c + spread,
                "low": c - spread,
                "close": c,
            }
            for i, c in enumerate(closes)
        ]

    def test_a_short_series_is_unknown_not_neutral(self) -> None:
        from attribution import classify

        reading = classify(self._series([100.0 + i for i in range(6)]), NOW + timedelta(hours=3))

        assert reading.available is False
        assert reading.label == "unknown"
        assert reading.adx is None, "the 25.0 sentinel must not be reported as a measurement"
        assert "16 bars" in reading.reason

    def test_a_rising_series_is_trending_up(self) -> None:
        from attribution import classify

        bars = self._series([100.0 + i for i in range(40)])
        reading = classify(bars, bars[-1]["bar_ts"])

        assert reading.available is True
        assert reading.label == "trending_up"
        assert reading.net_move_pct > 0

    def test_a_falling_series_is_trending_down(self) -> None:
        from attribution import classify

        bars = self._series([140.0 - i for i in range(40)])
        reading = classify(bars, bars[-1]["bar_ts"])

        assert reading.label == "trending_down"
        assert reading.net_move_pct < 0

    def test_a_flat_series_is_ranging(self) -> None:
        from attribution import classify

        bars = self._series([100.0] * 40)
        reading = classify(bars, bars[-1]["bar_ts"])

        assert reading.label == "ranging"
        assert reading.adx < 20.0

    def test_classification_never_reads_a_bar_from_the_future(self) -> None:
        """A regime is what was knowable then. Reading past the moment being
        classified is the same hindsight the point-in-time archive exists to
        prevent, and here it would be invisible: the label would simply be
        better than it should be."""
        from attribution import classify

        bars = self._series([100.0] * 20 + [100.0 + 4 * i for i in range(1, 21)])
        cutoff = bars[19]["bar_ts"]

        as_known_then = classify(bars, cutoff)
        with_the_future = classify(bars, bars[-1]["bar_ts"])

        assert as_known_then.bars_used == 20
        assert as_known_then.label == "ranging"
        assert with_the_future.label == "trending_up", "the rally is real, just later"

    def test_a_regime_change_under_a_trade_is_reported(self) -> None:
        from attribution import classify, describe_shift

        # The range oscillates rather than being dead flat: bars with zero
        # directional movement leave Wilder's +DI/-DI ratio frozen, so a
        # perfectly flat tail keeps whatever DX preceded it. A real range
        # moves, and this is what one looks like.
        rally = [100.0 + i for i in range(30)]
        chop = [130.0 + (2.0 if i % 2 else -2.0) for i in range(60)]
        bars = self._series(rally + chop)
        entry = classify(bars, bars[29]["bar_ts"])
        exit_ = classify(bars, bars[-1]["bar_ts"])
        shift = describe_shift(entry, exit_)

        assert entry.label == "trending_up"
        assert exit_.label == "ranging"
        assert shift["changed"] is True
        assert shift["from"] == "trending_up" and shift["to"] == "ranging"

    def test_a_shift_is_none_rather_than_false_when_it_cannot_be_known(self) -> None:
        """False would say "the regime held". The truth is that one end could
        not be classified, and those are different findings."""
        from attribution import classify, describe_shift

        short = classify(self._series([100.0] * 4), NOW + timedelta(hours=1))
        full = classify(self._series([100.0] * 40), NOW + timedelta(days=1))

        assert describe_shift(short, full)["changed"] is None

    def test_volatility_is_relative_to_the_symbol_itself(self) -> None:
        """An absolute ATR threshold is a claim about the universe. A $3 stock
        and a $900 one do not share one."""
        from attribution import classify

        calm = self._series([100.0] * 40, spread=0.05)
        wild = self._series([100.0] * 40, spread=5.0)

        assert classify(calm, calm[-1]["bar_ts"]).atr_pct < classify(
            wild, wild[-1]["bar_ts"]
        ).atr_pct

    def test_an_agitated_tail_reads_as_agitated(self) -> None:
        from attribution import classify

        bars = self._series([100.0] * 30, spread=0.2) + self._series(
            [100.0] * 20,
            start=NOW + timedelta(minutes=15 * 30),
            spread=2.0,
        )
        assert classify(bars, bars[-1]["bar_ts"]).volatility == "agitated"


class TestRegimeReachesTheReport:
    def _bars(self, journal, closes, timeframe="15m"):
        """A series ending at the present moment.

        `record_execution` stamps `recorded_at` itself and there is no override
        — deliberately, since a caller that could backdate an execution could
        rewrite trading history. So the bars are placed around now instead, and
        the trade lands at the end of the series.
        """
        from types import SimpleNamespace

        end = datetime.now(timezone.utc)
        journal.record_bars(
            "AAPL",
            timeframe,
            [
                SimpleNamespace(
                    timestamp=end - timedelta(minutes=15 * (len(closes) - i)),
                    open=c, high=c + 0.5, low=c - 0.5, close=c, volume=1000.0,
                )
                for i, c in enumerate(closes)
            ],
            source="test",
        )

    def test_trades_are_grouped_by_the_regime_they_were_entered_into(self, journal) -> None:
        """The finding L0 could not previously produce: not "the strategy
        loses" but "the strategy loses in one regime", which points at a
        filter rather than at the rule."""
        self._bars(journal, [100.0 + i for i in range(60)])
        journal.record_execution(
            symbol="AAPL", side="BUY", qty=10, decision_price=140.0, fill_price=140.0,
            strategy_id="ema_rsi_macd", environment="paper",
        )
        journal.record_execution(
            symbol="AAPL", side="SELL", qty=10, decision_price=155.0, fill_price=155.0,
            strategy_id="ema_rsi_macd", environment="paper",
        )

        report = build_report(journal, with_counterfactuals=False)
        slices = {s["regime"]: s for s in report["by_regime"]["slices"]}

        assert list(slices) == ["trending_up"]
        assert slices["trending_up"]["trades"] == 1
        assert slices["trending_up"]["realized"] == pytest.approx(150.0)
        assert slices["trending_up"]["win_rate"] == 1.0

    def test_a_trade_with_no_bars_lands_in_its_own_slice(self, journal) -> None:
        """Not a residual bucket that resembles a real regime. "We could not
        tell" has to be visible as its own row or it gets read as a finding."""
        journal.record_execution(
            symbol="AAPL", side="BUY", qty=10, decision_price=100.0, fill_price=100.0,
            strategy_id="ema_rsi_macd", environment="paper",
        )
        journal.record_execution(
            symbol="AAPL", side="SELL", qty=10, decision_price=110.0, fill_price=110.0,
            strategy_id="ema_rsi_macd", environment="paper",
        )

        report = build_report(journal, with_counterfactuals=False)
        slices = {s["regime"]: s for s in report["by_regime"]["slices"]}

        assert list(slices) == ["unknown"]
        assert report["by_regime"]["regime_shift"]["classifiable_trades"] == 0

    def test_the_entry_regime_is_read_from_the_entry_series(self, journal) -> None:
        """Filtering the exit-time series by timestamp is not point-in-time: it
        drops future bars but keeps revisions of past ones that arrived during
        the hold. This asserts the report fetches bars_as_of(entry) rather than
        slicing, which is the difference between classifying the conditions a
        decision was made in and classifying them with data that arrived
        afterwards."""
        asked: list[datetime] = []
        real_bars_as_of = journal.bars_as_of

        def _spy(symbol, timeframe, as_of, *args, **kwargs):
            asked.append(as_of)
            return real_bars_as_of(symbol, timeframe, as_of, *args, **kwargs)

        journal.bars_as_of = _spy  # type: ignore[method-assign]

        journal.record_execution(
            symbol="AAPL", side="BUY", qty=10, decision_price=100.0, fill_price=100.0,
            strategy_id="ema_rsi_macd", environment="paper",
        )
        journal.record_execution(
            symbol="AAPL", side="SELL", qty=10, decision_price=110.0, fill_price=110.0,
            strategy_id="ema_rsi_macd", environment="paper",
        )

        report = build_report(journal, with_counterfactuals=False)
        trade = report["attributions"][0]
        entry_at = datetime.fromisoformat(trade["entry_at"])
        exit_at = datetime.fromisoformat(trade["exit_at"])

        assert entry_at in asked, "the entry regime must be read as of the entry"
        assert exit_at in asked
        assert entry_at != exit_at

    def test_regime_is_a_diagnostic_and_never_enters_the_identity(self, journal) -> None:
        """A label with a threshold in it is an opinion. The three price
        components have to add up whatever anyone thinks about ADX."""
        self._bars(journal, [100.0 + i for i in range(60)])
        journal.record_execution(
            symbol="AAPL", side="BUY", qty=10, decision_price=140.0, fill_price=140.4,
            strategy_id="ema_rsi_macd", environment="paper",
        )
        journal.record_execution(
            symbol="AAPL", side="SELL", qty=10, decision_price=155.0, fill_price=154.6,
            strategy_id="ema_rsi_macd", environment="paper",
        )

        report = build_report(journal, with_counterfactuals=False)

        assert report["coverage"]["identity_failures"] == 0
        assert report["totals"]["identity_matches_realized"] is True
        attribution = report["attributions"][0]
        assert attribution["diagnostics"]["entry_regime"]["available"] is True
        # The regime lives in diagnostics only — nothing about it appears among
        # the summed components.
        assert set(attribution) & {"entry_regime", "regime_shift"} == set()


class TestAnnualisationMakesTheComparisonPossible:
    """`performance_from_trades` reports a per-trade Sharpe, and the lifecycle
    health check compares it against a validated figure that is annualised and
    bar-based. Those are different units, and the health check was making that
    comparison — so the annualised figure is computed here, from the trade
    frequency this sleeve actually ran at rather than an assumed one.
    """

    def _trips(self, results, *, spacing_days=1.0):
        from attribution import Leg, RoundTrip

        trips = []
        for i, value in enumerate(results):
            at = NOW + timedelta(days=spacing_days * i)
            trips.append(
                RoundTrip(
                    strategy_id="ema_rsi_macd", symbol="AAPL", environment="live",
                    account_id="default", qty=1.0,
                    entry=Leg("BUY", 1.0, 100.0, 100.0, at),
                    exit=Leg("SELL", 1.0, 100.0 + value, 100.0 + value,
                             at + timedelta(minutes=30)),
                )
            )
        return trips

    def test_the_annualised_figure_scales_the_per_trade_one(self) -> None:
        from attribution import performance_from_trades

        # 21 trades one day apart: 20 intervals over 20 days.
        result = performance_from_trades(self._trips([1.0, -0.5] * 10 + [1.0]))

        assert result["span_days"] == pytest.approx(20.0)
        assert result["trades_per_year"] == pytest.approx(365.25, rel=1e-3)
        assert result["sharpe_annualised"] == pytest.approx(
            result["sharpe"] * 365.25**0.5, rel=1e-3
        )

    def test_frequency_is_measured_not_assumed(self) -> None:
        """The same per-trade edge at twice the rate is a different annual
        Sharpe. A strategy firing twice a day and one firing twice a month
        produce identical per-trade ratios from very different edges."""
        from attribution import performance_from_trades

        results = [1.0, -0.5] * 10 + [1.0]
        slow = performance_from_trades(self._trips(results, spacing_days=4.0))
        fast = performance_from_trades(self._trips(results, spacing_days=1.0))

        assert slow["sharpe"] == pytest.approx(fast["sharpe"])
        assert fast["sharpe_annualised"] > slow["sharpe_annualised"]
        assert fast["sharpe_annualised"] == pytest.approx(
            slow["sharpe_annualised"] * 2.0, rel=1e-3
        )

    def test_a_short_record_reports_no_annualised_figure(self) -> None:
        """Annualising three days of intraday activity is extrapolation, and a
        wrong scaling is worse than an absent one: it produces a number that
        looks comparable and is not."""
        from attribution import performance_from_trades

        result = performance_from_trades(self._trips([1.0, -0.5, 1.0], spacing_days=0.5))

        assert result["sharpe"] is not None, "the per-trade ratio is still reported"
        assert result["sharpe_annualised"] is None
        assert result["trades_per_year"] is None

    def test_the_standard_error_shrinks_with_the_sample(self) -> None:
        """It is what turns a fixed demotion gap into a sample-aware band."""
        from attribution import performance_from_trades

        few = performance_from_trades(self._trips([1.0, -0.5] * 10 + [1.0]))
        many = performance_from_trades(self._trips([1.0, -0.5] * 100 + [1.0]))

        assert many["sharpe_annualised_std_error"] < few["sharpe_annualised_std_error"]

    def test_the_span_covers_intervals_not_trades(self) -> None:
        """n exits span n-1 intervals. Dividing by n overstates the rate mildly
        at 200 trades and by a third at four."""
        from attribution import performance_from_trades

        result = performance_from_trades(self._trips([1.0, -0.5, 1.0, -0.5, 1.0] * 2,
                                                     spacing_days=2.0))
        n = result["trades"]
        assert result["trades_per_year"] == pytest.approx(
            (n - 1) * 365.25 / result["span_days"], rel=1e-3
        )  # the reported value is rounded to two places
