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
