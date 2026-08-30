"""Tests for the pattern-day-trader guard.

The rule being implemented: four or more day trades in five rolling business
days, with account equity under $25,000, gets an account designated a pattern
day trader and restricted. The guard therefore allows three and blocks the
fourth.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from autonomy_orchestrator.day_trade_tracker import (
    DayTradeTracker,
    PDTSettings,
    business_window,
    session_date,
)


@pytest.fixture
def settings(tmp_path: Path) -> PDTSettings:
    return PDTSettings(
        enabled=True,
        equity_threshold_usd=25_000.0,
        max_day_trades=3,
        state_path=tmp_path / "day-trades.json",
    )


@pytest.fixture
def tracker(settings: PDTSettings) -> DayTradeTracker:
    return DayTradeTracker(settings)


def _at(hour_utc: int, day: int = 5, month: int = 3) -> datetime:
    return datetime(2024, month, day, hour_utc, 0, tzinfo=timezone.utc)


class TestSessionAttribution:
    def test_intraday_moments_share_one_session(self) -> None:
        assert session_date(_at(15)) == session_date(_at(20))

    def test_after_hours_utc_rollover_stays_on_the_us_session(self) -> None:
        """19:30 ET is 00:30 UTC the next calendar day, but the same session."""
        assert session_date(datetime(2024, 3, 6, 0, 30, tzinfo=timezone.utc)) == "2024-03-05"

    def test_separate_days_are_separate_sessions(self) -> None:
        assert session_date(_at(15, day=5)) != session_date(_at(15, day=6))


class TestDayTradeDetection:
    def test_open_and_close_same_session_is_a_day_trade(
        self, tracker: DayTradeTracker
    ) -> None:
        tracker.record_open("AAPL", _at(15))
        assert tracker.record_close("AAPL", _at(19)) is True
        assert tracker.day_trades_used(now=_at(20)) == 1

    def test_overnight_hold_is_not_a_day_trade(self, tracker: DayTradeTracker) -> None:
        tracker.record_open("AAPL", _at(19, day=5))
        assert tracker.record_close("AAPL", _at(15, day=6)) is False
        assert tracker.day_trades_used(now=_at(20, day=6)) == 0

    def test_closing_an_untracked_symbol_is_not_a_day_trade(
        self, tracker: DayTradeTracker
    ) -> None:
        assert tracker.record_close("TSLA", _at(19)) is False

    def test_symbols_are_tracked_independently(self, tracker: DayTradeTracker) -> None:
        tracker.record_open("AAPL", _at(15))
        tracker.record_open("MSFT", _at(15))
        tracker.record_close("AAPL", _at(19))
        assert tracker.day_trades_used(now=_at(20)) == 1
        tracker.record_close("MSFT", _at(20))
        assert tracker.day_trades_used(now=_at(20)) == 2

    def test_reopening_the_same_symbol_keeps_the_original_session(
        self, tracker: DayTradeTracker
    ) -> None:
        """A second open before a close must not reset the entry session and
        turn a day trade into an overnight hold."""
        tracker.record_open("AAPL", _at(15, day=5))
        tracker.record_open("AAPL", _at(15, day=6))
        assert tracker.record_close("AAPL", _at(19, day=5)) is True


class TestEntryGating:
    def _use(self, tracker: DayTradeTracker, count: int, day: int = 5) -> None:
        for i in range(count):
            tracker.record_open(f"SYM{i}", _at(15, day=day))
            tracker.record_close(f"SYM{i}", _at(19, day=day))

    def test_entry_allowed_within_budget(self, tracker: DayTradeTracker) -> None:
        self._use(tracker, 2)
        decision = tracker.check_entry(equity=10_000.0, now=_at(20))
        assert decision.allowed is True
        assert decision.day_trades_remaining == 1

    def test_fourth_day_trade_is_blocked(self, tracker: DayTradeTracker) -> None:
        self._use(tracker, 3)
        decision = tracker.check_entry(equity=10_000.0, now=_at(20))
        assert decision.allowed is False
        assert "pdt_day_trade_limit" in decision.reason

    def test_large_account_is_exempt(self, tracker: DayTradeTracker) -> None:
        """Above $25k the rule does not bite, however many day trades."""
        self._use(tracker, 3)
        decision = tracker.check_entry(equity=100_000.0, now=_at(20))
        assert decision.allowed is True
        assert decision.reason == "above_pdt_equity_threshold"

    def test_unknown_equity_fails_closed(self, tracker: DayTradeTracker) -> None:
        """Approving on an unknown balance is how an account gets flagged."""
        self._use(tracker, 3)
        assert tracker.check_entry(equity=None, now=_at(20)).allowed is False

    def test_unknown_equity_still_allows_entries_within_budget(
        self, tracker: DayTradeTracker
    ) -> None:
        self._use(tracker, 1)
        assert tracker.check_entry(equity=None, now=_at(20)).allowed is True

    def test_disabled_guard_always_allows(self, settings: PDTSettings) -> None:
        """Non-US brokers are outside this rule; the guard can be turned off."""
        tracker = DayTradeTracker(
            PDTSettings(
                enabled=False,
                equity_threshold_usd=settings.equity_threshold_usd,
                max_day_trades=settings.max_day_trades,
                state_path=settings.state_path,
            )
        )
        self._use(tracker, 10)
        decision = tracker.check_entry(equity=1_000.0, now=_at(20))
        assert decision.allowed is True
        assert decision.reason == "pdt_disabled"


class TestBusinessWindow:
    def test_window_is_five_business_days_ending_today(self) -> None:
        # Fri 8 Mar 2024 back through Mon 4 Mar.
        assert business_window(date(2024, 3, 8)) == {
            "2024-03-04", "2024-03-05", "2024-03-06", "2024-03-07", "2024-03-08",
        }

    def test_window_skips_the_weekend(self) -> None:
        """From Monday the window reaches back into the previous week, not to
        Saturday and Sunday."""
        window = business_window(date(2024, 3, 11))  # Monday
        assert "2024-03-09" not in window  # Saturday
        assert "2024-03-10" not in window  # Sunday
        assert "2024-03-05" in window      # the fifth business day back

    def test_weekend_today_is_included_but_prior_days_are_weekdays(self) -> None:
        """Crypto trades on weekends, so today always counts. The rest of the
        window is business days."""
        window = business_window(date(2024, 3, 9))  # Saturday
        assert "2024-03-09" in window
        others = window - {"2024-03-09"}
        assert all(date.fromisoformat(d).weekday() < 5 for d in others)


class TestRollingWindow:
    def _day_trade(self, tracker: DayTradeTracker, day: int) -> None:
        tracker.record_open(f"SYM{day}", _at(15, day=day))
        tracker.record_close(f"SYM{day}", _at(19, day=day))

    def test_five_consecutive_business_days_all_count(
        self, tracker: DayTradeTracker
    ) -> None:
        for day in (4, 5, 6, 7, 8):  # Mon-Fri
            self._day_trade(tracker, day)
        assert tracker.day_trades_used(now=_at(20, day=8)) == 5

    def test_a_weekend_gap_does_not_expire_the_window(
        self, tracker: DayTradeTracker
    ) -> None:
        """Fri 1 Mar is still inside the window on Thu 7 Mar: the weekend does
        not count as two of the five business days."""
        for day in (1, 4, 5, 6, 7):
            self._day_trade(tracker, day)
        assert tracker.day_trades_used(now=_at(20, day=7)) == 5

    def test_older_day_trades_drop_out(self, tracker: DayTradeTracker) -> None:
        for day in (4, 5, 6):
            self._day_trade(tracker, day)
        assert tracker.day_trades_used(now=_at(20, day=6)) == 3
        # Five business days later nothing from that run remains.
        assert tracker.day_trades_used(now=_at(20, day=15)) == 0

    def test_budget_frees_up_as_the_window_rolls(
        self, tracker: DayTradeTracker
    ) -> None:
        for day in (4, 5, 6):
            self._day_trade(tracker, day)
        assert tracker.check_entry(10_000.0, now=_at(20, day=6)).allowed is False
        # Mon 11 Mar: Mon 4 Mar has rolled off, freeing one.
        assert tracker.check_entry(10_000.0, now=_at(20, day=11)).allowed is True


def _today_at(hour_utc: int) -> datetime:
    """A moment in the current session.

    Persistence tests must use current dates: loading prunes history well
    outside the window, so a 2024 fixture would correctly be discarded and the
    test would be asserting the wrong thing.
    """
    return datetime.now(timezone.utc).replace(hour=hour_utc, minute=0, microsecond=0)


class TestPersistence:
    def test_state_survives_a_restart(self, settings: PDTSettings) -> None:
        """The window spans five days — losing it on restart would reset the
        count and let the account breach the limit."""
        first = DayTradeTracker(settings)
        first.record_open("AAPL", _today_at(15))
        first.record_close("AAPL", _today_at(19))

        second = DayTradeTracker(settings)
        assert second.day_trades_used() == 1

    def test_open_positions_survive_a_restart(self, settings: PDTSettings) -> None:
        first = DayTradeTracker(settings)
        first.record_open("AAPL", _today_at(15))

        second = DayTradeTracker(settings)
        assert second.record_close("AAPL", _today_at(19)) is True

    def test_corrupt_state_starts_empty_without_crashing(
        self, settings: PDTSettings
    ) -> None:
        settings.state_path.write_text("{not json", encoding="utf-8")
        assert DayTradeTracker(settings).day_trades_used() == 0

    def test_reset_clears_the_ledger(self, tracker: DayTradeTracker) -> None:
        tracker.record_open("AAPL", _today_at(15))
        tracker.record_close("AAPL", _today_at(19))
        tracker.reset()
        assert tracker.day_trades_used() == 0


class TestStatus:
    def test_status_reports_the_budget(self, tracker: DayTradeTracker) -> None:
        tracker.record_open("AAPL", _at(15))
        tracker.record_close("AAPL", _at(19))
        status = tracker.status(now=_at(20))

        assert status["enabled"] is True
        assert status["day_trades_used"] == 1
        assert status["day_trades_remaining"] == 2
        assert status["rolling_sessions"] == 5
        assert "PDT_ENABLED=false" in str(status["rule"])

    def test_status_lists_open_positions(self, tracker: DayTradeTracker) -> None:
        tracker.record_open("NVDA", _at(15))
        assert "NVDA" in dict(tracker.status()["open_positions"])


class TestSettings:
    def test_defaults_match_the_finra_rule(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PDT_ENABLED", raising=False)
        monkeypatch.delenv("PDT_MAX_DAY_TRADES", raising=False)
        monkeypatch.delenv("PDT_EQUITY_THRESHOLD_USD", raising=False)
        loaded = PDTSettings.from_env()

        assert loaded.enabled is True
        assert loaded.equity_threshold_usd == 25_000.0
        assert loaded.max_day_trades == 3  # the 4th is what triggers designation

    def test_env_overrides_apply(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PDT_ENABLED", "false")
        monkeypatch.setenv("PDT_MAX_DAY_TRADES", "10")
        monkeypatch.setenv("PDT_EQUITY_THRESHOLD_USD", "0")
        loaded = PDTSettings.from_env()

        assert loaded.enabled is False
        assert loaded.max_day_trades == 10
        assert loaded.equity_threshold_usd == 0.0
