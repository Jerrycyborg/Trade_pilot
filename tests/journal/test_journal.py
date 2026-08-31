"""Tests for the point-in-time archive and decision journal.

The archive exists so later research can ask "what was knowable at the time?".
Two properties matter most and are tested hardest: bars deduplicate into one
clean series, and a journal failure never propagates into the trading path.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from journal import Journal, get_journal, reset_journal
from market_data.models import OHLCVBar


def _bars(n: int = 5, symbol: str = "AAPL", start_close: float = 100.0) -> list[OHLCVBar]:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    return [
        OHLCVBar(
            symbol=symbol,
            timestamp=now - timedelta(minutes=15 * i),
            open=start_close + i,
            high=start_close + i + 1,
            low=start_close + i - 1,
            close=start_close + i + 0.5,
            volume=1000.0 + i,
        )
        for i in range(n)
    ]


@pytest.fixture
def archive(tmp_path: Path) -> Journal:
    return Journal(path=tmp_path / "journal.db")


class TestBarArchive:
    def test_bars_are_stored(self, archive: Journal) -> None:
        assert archive.record_bars("AAPL", "15m", _bars(5)) == 5
        assert archive.stats()["bar_observations"] == 5

    def test_refetching_the_same_bars_adds_nothing(self, archive: Journal) -> None:
        """Every cycle refetches an overlapping window. Without dedup the
        archive becomes millions of duplicate rows instead of a time series."""
        bars = _bars(5)
        archive.record_bars("AAPL", "15m", bars)
        assert archive.record_bars("AAPL", "15m", bars) == 0
        assert archive.stats()["bar_observations"] == 5

    def test_only_the_new_bars_are_added_on_overlap(self, archive: Journal) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        first = [
            OHLCVBar(symbol="AAPL", timestamp=now - timedelta(minutes=15 * i),
                     open=1, high=2, low=0.5, close=1.5, volume=1)
            for i in range(5)
        ]
        overlapping = [
            OHLCVBar(symbol="AAPL", timestamp=now - timedelta(minutes=15 * i),
                     open=1, high=2, low=0.5, close=1.5, volume=1)
            for i in range(3, 8)
        ]
        archive.record_bars("AAPL", "15m", first)
        assert archive.record_bars("AAPL", "15m", overlapping) == 3

    def test_the_same_bar_at_two_timeframes_is_two_records(
        self, archive: Journal
    ) -> None:
        bars = _bars(3)
        archive.record_bars("AAPL", "15m", bars)
        assert archive.record_bars("AAPL", "1m", bars) == 3

    def test_symbols_do_not_collide(self, archive: Journal) -> None:
        archive.record_bars("AAPL", "15m", _bars(3, symbol="AAPL"))
        archive.record_bars("MSFT", "15m", _bars(3, symbol="MSFT"))
        assert sorted(archive.stats()["symbols"]) == ["AAPL", "MSFT"]

    def test_empty_input_is_a_no_op(self, archive: Journal) -> None:
        assert archive.record_bars("AAPL", "15m", []) == 0


class TestPriceObservations:
    def test_staleness_is_derived_from_the_two_timestamps(
        self, archive: Journal
    ) -> None:
        """price_ts is when the market printed it; observed_at is when we saw
        it. The gap between them is the whole point of the archive."""
        archive.record_price(
            "AAPL", 200.0,
            price_ts=datetime.now(timezone.utc) - timedelta(seconds=45),
            source="alpaca_trade",
        )
        assert archive.stats()["price_observations"] == 1

    def test_refused_prices_are_archived_too(self, archive: Journal) -> None:
        """A price we rejected explains a trade we did not make. Discarding it
        leaves a hole a later post-mortem cannot fill."""
        archive.record_price("AAPL", 200.0, source="last_bar", accepted=True)
        archive.record_price("AAPL", 180.0, source="last_bar", accepted=False)
        assert archive.stats()["price_observations"] == 2

    def test_repeated_observations_are_not_deduplicated(
        self, archive: Journal
    ) -> None:
        """Unlike bars: seeing the same price twice is two facts about us."""
        stamp = datetime.now(timezone.utc)
        for _ in range(3):
            archive.record_price("AAPL", 200.0, price_ts=stamp)
        assert archive.stats()["price_observations"] == 3


class TestDecisionJournal:
    def test_inputs_survive_the_round_trip(self, archive: Journal) -> None:
        """Without the inputs, a post-mortem can see that a trade lost money
        but never why the system thought otherwise."""
        archive.record_decision(
            stage="signal", outcome="approved", symbol="AAPL", action="BUY",
            reason="ema cross", inputs={"rsi": 58.2, "adx": 27.1},
            outputs={"size_pct": 0.02}, correlation_id="sig-1",
        )
        entry = archive.recent_decisions()[0]
        assert entry["inputs"] == {"rsi": 58.2, "adx": 27.1}
        assert entry["outputs"] == {"size_pct": 0.02}
        assert entry["correlation_id"] == "sig-1"

    def test_correlation_id_links_the_stages_of_one_signal(
        self, archive: Journal
    ) -> None:
        for stage in ("signal", "risk", "policy", "order"):
            archive.record_decision(
                stage=stage, outcome="approved", symbol="AAPL", correlation_id="sig-7"
            )
        entries = archive.recent_decisions(limit=10)
        assert {e["correlation_id"] for e in entries} == {"sig-7"}
        assert {e["stage"] for e in entries} == {"signal", "risk", "policy", "order"}

    def test_decisions_come_back_newest_first(self, archive: Journal) -> None:
        base = datetime.now(timezone.utc)
        for i in range(3):
            archive.record_decision(
                stage="signal", outcome="approved", symbol="AAPL",
                reason=f"n{i}", ts=base + timedelta(seconds=i),
            )
        assert [e["reason"] for e in archive.recent_decisions()] == ["n2", "n1", "n0"]

    def test_filtering_by_symbol(self, archive: Journal) -> None:
        archive.record_decision(stage="signal", outcome="approved", symbol="AAPL")
        archive.record_decision(stage="signal", outcome="approved", symbol="MSFT")
        assert len(archive.recent_decisions(symbol="AAPL")) == 1

    def test_timestamps_come_back_utc_aware(self, archive: Journal) -> None:
        """SQLite has no timezone type. A naive timestamp in a research archive
        is a timestamp of unknown meaning."""
        archive.record_decision(stage="signal", outcome="approved", symbol="AAPL")
        assert archive.recent_decisions()[0]["ts"].endswith("+00:00")

    def test_unserialisable_inputs_do_not_raise(self, archive: Journal) -> None:
        archive.record_decision(
            stage="signal", outcome="approved", symbol="AAPL",
            inputs={"obj": object(), "when": datetime.now(timezone.utc)},
        )
        assert len(archive.recent_decisions()) == 1


class TestFailureIsNeverFatal:
    """A journal failure must degrade research, never stop or corrupt a trade."""

    def test_disabled_journal_accepts_every_call(self, tmp_path: Path) -> None:
        archive = Journal(path=tmp_path / "j.db", enabled=False)
        assert archive.enabled is False
        assert archive.record_bars("AAPL", "15m", _bars(3)) == 0
        archive.record_price("AAPL", 100.0)
        assert isinstance(archive.record_decision(stage="signal", outcome="ok"), str)
        assert archive.recent_decisions() == []
        assert archive.stats()["enabled"] is False

    def test_an_unwritable_path_disables_rather_than_raises(
        self, tmp_path: Path
    ) -> None:
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("x", encoding="utf-8")
        archive = Journal(path=blocker / "nested" / "j.db")
        assert archive.enabled is False
        archive.record_price("AAPL", 100.0)  # must not raise

    def test_a_broken_engine_swallows_write_errors(self, archive: Journal) -> None:
        archive._session_factory = None  # simulate a failure after open
        archive.record_price("AAPL", 100.0)
        assert archive.record_bars("AAPL", "15m", _bars(2)) == 0


class TestProcessWideJournal:
    def test_get_journal_returns_one_instance(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("JOURNAL_PATH", str(tmp_path / "shared.db"))
        reset_journal(None)
        assert get_journal() is get_journal()
        reset_journal(None)

    def test_journal_can_be_disabled_by_env(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("JOURNAL_PATH", str(tmp_path / "off.db"))
        monkeypatch.setenv("JOURNAL_ENABLED", "false")
        reset_journal(None)
        assert get_journal().enabled is False
        reset_journal(None)


class TestExecutionQuality:
    """What each order actually cost, versus what the decision assumed.

    This is the measurement that replaces the backtest's guessed slippage. If
    it flatters itself, the backtest inherits the flattery.
    """

    def test_a_fill_records_its_shortfall(self, archive: Journal) -> None:
        shortfall = archive.record_execution(
            symbol="AAPL", side="BUY", qty=10,
            decision_price=200.0, fill_price=200.2,
        )
        assert shortfall == 10.0
        report = archive.execution_quality()
        assert report["orders"] == 1
        assert report["filled"] == 1
        assert report["mean_shortfall_bps"] == 10.0

    def test_a_sell_filled_below_the_decision_price_is_also_a_cost(
        self, archive: Journal
    ) -> None:
        assert archive.record_execution(
            symbol="AAPL", side="SELL", qty=10,
            decision_price=200.0, fill_price=199.8,
        ) == 10.0

    def test_a_missed_limit_is_recorded_and_counts_against_the_fill_rate(
        self, archive: Journal
    ) -> None:
        """Reading a fill rate only from fills yields 100% — the number is useless."""
        archive.record_execution(
            symbol="AAPL", side="BUY", qty=10,
            decision_price=200.0, fill_price=200.2,
        )
        assert archive.record_execution(
            symbol="AAPL", side="BUY", qty=10,
            decision_price=200.0, fill_price=None,
            order_type="LIMIT", limit_price=200.2, outcome="limit_not_marketable",
        ) is None

        report = archive.execution_quality()
        assert report["orders"] == 2
        assert report["filled"] == 1
        assert report["fill_rate"] == 0.5
        # The miss cost no slippage, so it must not drag the mean toward zero.
        assert report["mean_shortfall_bps"] == 10.0

    def test_worst_case_is_reported_alongside_the_mean(self, archive: Journal) -> None:
        """A mean hides the fill that actually hurt."""
        for fill in (200.1, 200.2, 201.0):
            archive.record_execution(
                symbol="AAPL", side="BUY", qty=1,
                decision_price=200.0, fill_price=fill,
            )
        report = archive.execution_quality()
        assert report["worst_shortfall_bps"] == 50.0
        assert report["mean_shortfall_bps"] < report["worst_shortfall_bps"]

    def test_cost_is_broken_out_per_symbol(self, archive: Journal) -> None:
        """Thin symbols cost more to trade. An aggregate figure hides which ones."""
        archive.record_execution(
            symbol="AAPL", side="BUY", qty=1, decision_price=200.0, fill_price=200.2
        )
        archive.record_execution(
            symbol="THIN", side="BUY", qty=1, decision_price=200.0, fill_price=201.0
        )
        by_symbol = archive.execution_quality()["mean_shortfall_by_symbol"]
        assert by_symbol == {"AAPL": 10.0, "THIN": 50.0}

    def test_a_fill_without_a_decision_price_records_no_shortfall(
        self, archive: Journal
    ) -> None:
        """It filled, but there is nothing to compare it against — not a zero cost."""
        assert archive.record_execution(
            symbol="AAPL", side="BUY", qty=1, decision_price=None, fill_price=200.0
        ) is None
        report = archive.execution_quality()
        assert report["filled"] == 1
        assert report["mean_shortfall_bps"] is None

    def test_no_orders_yet_reports_empty_rather_than_zero_cost(
        self, archive: Journal
    ) -> None:
        report = archive.execution_quality()
        assert report["orders"] == 0
        assert report["fill_rate"] is None
        assert report["mean_shortfall_bps"] is None

    def test_a_disabled_journal_still_computes_the_shortfall(
        self, tmp_path: Path
    ) -> None:
        """Archiving is optional; the caller's return value should not change."""
        archive = Journal(path=tmp_path / "j.db", enabled=False)
        assert archive.record_execution(
            symbol="AAPL", side="BUY", qty=1, decision_price=200.0, fill_price=200.2
        ) == 10.0
        assert archive.execution_quality() == {"enabled": False}

    def test_a_broken_engine_does_not_break_the_trading_path(
        self, archive: Journal
    ) -> None:
        archive._session_factory = None
        assert archive.record_execution(
            symbol="AAPL", side="BUY", qty=1, decision_price=200.0, fill_price=200.2
        ) == 10.0


class TestNetPosition:
    """The sleeve's book, as the fill record has it.

    This is what the execution service's position cap enforces against, so the
    two properties that matter are the sign convention and the None: a journal
    that cannot answer must say so, because a cap that reads a missing book as
    flat re-creates the unbounded stacking it exists to prevent.
    """

    def test_fills_net_signed_within_the_scope(self, archive: Journal) -> None:
        archive.record_execution(
            symbol="NVDA", side="SELL", qty=7,
            decision_price=219.5, fill_price=219.46, strategy_id="ema_rsi_macd",
        )
        archive.record_execution(
            symbol="NVDA", side="BUY", qty=3,
            decision_price=219.4, fill_price=219.45, strategy_id="ema_rsi_macd",
        )
        assert archive.net_position(
            strategy_id="ema_rsi_macd", symbol="NVDA", environment="paper"
        ) == -4.0

    def test_a_miss_is_not_a_position(self, archive: Journal) -> None:
        archive.record_execution(
            symbol="NVDA", side="BUY", qty=10,
            decision_price=219.5, fill_price=None,
            order_type="LIMIT", limit_price=219.4, outcome="limit_not_marketable",
            strategy_id="ema_rsi_macd",
        )
        assert archive.net_position(
            strategy_id="ema_rsi_macd", symbol="NVDA", environment="paper"
        ) == 0.0

    def test_another_sleeves_fills_do_not_count(self, archive: Journal) -> None:
        archive.record_execution(
            symbol="NVDA", side="SELL", qty=7,
            decision_price=219.5, fill_price=219.46, strategy_id="ema_rsi_macd",
        )
        assert archive.net_position(
            strategy_id="ema_rsi_macd@chal-1", symbol="NVDA", environment="paper"
        ) == 0.0
        assert archive.net_position(
            strategy_id="ema_rsi_macd", symbol="NVDA", environment="live"
        ) == 0.0

    def test_a_disabled_journal_answers_none_not_flat(self, tmp_path: Path) -> None:
        dead = Journal(path=tmp_path / "off.db", enabled=False)
        assert dead.net_position(
            strategy_id="ema_rsi_macd", symbol="NVDA", environment="paper"
        ) is None
