"""The earnings gate: what it says, and what it admits it cannot say.

The first live paper run found the old gate absent twice over — the calendar
lookup failed under an egress policy that blocks yfinance and was logged at
debug, and the worker never consulted it anyway. The properties tested here
are the ones that failure hid: an unanswerable calendar is visible, the
failure posture is explicit configuration, and "no blackout" is never the
same verdict as "could not ask".

Every test carries `real_earnings_calendar`: the suite-wide conftest stub
would otherwise answer for the code under test. yfinance is patched directly,
so nothing here reaches the network either.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from strategy_service.earnings_calendar import (
    check_earnings_blackout,
    is_earnings_blackout,
)

pytestmark = pytest.mark.real_earnings_calendar


def _mock_ticker(days_delta: int | None):
    """Return a mock yf.Ticker whose .calendar matches real yfinance dict shape."""
    mock = MagicMock()
    if days_delta is None:
        mock.calendar = {}
    else:
        target = datetime.now(timezone.utc).date() + timedelta(days=days_delta)
        mock.calendar = {"Earnings Date": [target]}
    return mock


class TestTheVerdict:
    def test_blackout_active_when_near(self) -> None:
        with patch("yfinance.Ticker", return_value=_mock_ticker(1)):
            check = check_earnings_blackout("AAPL", blackout_days=2)
        assert check.active is True
        assert check.checked is True

    def test_blackout_on_exact_day(self) -> None:
        with patch("yfinance.Ticker", return_value=_mock_ticker(0)):
            assert check_earnings_blackout("AAPL", blackout_days=2).active is True

    def test_blackout_inactive_when_far(self) -> None:
        with patch("yfinance.Ticker", return_value=_mock_ticker(10)):
            check = check_earnings_blackout("AAPL", blackout_days=2)
        assert check.active is False
        assert check.checked is True

    def test_no_earnings_date_is_a_checked_no(self) -> None:
        with patch("yfinance.Ticker", return_value=_mock_ticker(None)):
            check = check_earnings_blackout("AAPL")
        assert check.active is False
        assert check.checked is True

    def test_the_bool_wrapper_reports_active(self) -> None:
        with patch("yfinance.Ticker", return_value=_mock_ticker(1)):
            assert is_earnings_blackout("AAPL", blackout_days=2) is True


class TestAnUnansweredCalendarIsNotSilent:
    def test_failing_open_is_a_distinct_verdict(self) -> None:
        """`checked=False` is the whole point of the verdict type: the caller
        can tell "no blackout" from "nobody could ask"."""
        with patch("yfinance.Ticker", side_effect=Exception("egress blocked")):
            check = check_earnings_blackout("AAPL")
        assert check.active is False
        assert check.checked is False
        assert "failing open" in check.reason

    def test_failing_open_warns_the_operator(self, caplog) -> None:
        """The run's finding verbatim: a fail-open guard should at least
        report that it is open. Debug level is not a report."""
        with caplog.at_level(logging.WARNING, logger="strategy_service.earnings_calendar"):
            with patch("yfinance.Ticker", side_effect=Exception("egress blocked")):
                check_earnings_blackout("AAPL")
        assert any(
            "earnings gate is OPEN" in record.getMessage() for record in caplog.records
        )

    def test_the_warning_does_not_repeat_every_cycle(self, caplog) -> None:
        """One WARNING per outage per symbol; repeats drop to debug. A guard
        that cries once per cycle for the life of an outage gets silenced by
        the operator, which is the failure mode with extra steps."""
        import strategy_service.earnings_calendar as gate

        with caplog.at_level(logging.WARNING, logger="strategy_service.earnings_calendar"):
            with patch("yfinance.Ticker", side_effect=Exception("egress blocked")):
                check_earnings_blackout("AAPL")
                gate._cache.clear()  # expire the failure verdict: force a re-consult
                check_earnings_blackout("AAPL")
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(warnings) == 1

    def test_fail_closed_treats_unknown_as_blackout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("EARNINGS_GATE_FAIL_CLOSED", "true")
        with patch("yfinance.Ticker", side_effect=Exception("egress blocked")):
            check = check_earnings_blackout("AAPL")
        assert check.active is True
        assert check.checked is False

    def test_a_garbage_failure_mode_is_refused_up_front(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Refused on the first call, not on the first outage — a guard running
        on a default the operator believes they replaced is a guard nobody
        configured."""
        monkeypatch.setenv("EARNINGS_GATE_FAIL_CLOSED", "yes please")
        with patch("yfinance.Ticker", return_value=_mock_ticker(10)):
            with pytest.raises(ValueError, match="EARNINGS_GATE_FAIL_CLOSED"):
                check_earnings_blackout("AAPL")


class TestTheCalendarIsConsultedNotHammered:
    def test_a_verdict_is_cached(self) -> None:
        ticker = MagicMock(return_value=_mock_ticker(10))
        with patch("yfinance.Ticker", ticker):
            check_earnings_blackout("AAPL", blackout_days=2)
            check_earnings_blackout("AAPL", blackout_days=2)
        assert ticker.call_count == 1

    def test_symbols_do_not_share_a_verdict(self) -> None:
        ticker = MagicMock(return_value=_mock_ticker(10))
        with patch("yfinance.Ticker", ticker):
            check_earnings_blackout("AAPL")
            check_earnings_blackout("MSFT")
        assert ticker.call_count == 2


class TestTheWorkerActuallyAsks:
    """The other half of the run's finding: the worker hard-coded
    event_blackout_active=False into every policy request, so the policy
    service's hard event_blackout rejection could never fire on the only path
    that was trading."""

    def test_the_policy_request_carries_the_gates_verdict(
        self, monkeypatch: pytest.MonkeyPatch, stub_prices
    ) -> None:
        from strategy_service.earnings_calendar import BlackoutCheck
        from strategy_service.worker import TradeWorker

        stub_prices.set("AAPL", 200.0)
        monkeypatch.setattr(
            "strategy_service.worker.check_earnings_blackout",
            lambda _symbol, blackout_days: BlackoutCheck(
                active=True, checked=True, reason="earnings tomorrow"
            ),
        )
        context = TradeWorker()._market_context("AAPL", bars=[])
        assert context.event_blackout_active is True
