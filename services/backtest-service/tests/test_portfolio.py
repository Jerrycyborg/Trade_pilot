"""Tests for combining sleeves into a portfolio.

The claim a portfolio makes is that its parts fail at different times. These
tests check that the code measures that claim rather than assuming it: that two
identical sleeves are reported as identical, that returns are aligned by
timestamp and not by list position, and that a combination which does not help
says so.

Synthetic bars throughout — nothing here says the strategies are any good.
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone

import pytest
from backtest_service.models import BacktestRequest, StrategyParams
from backtest_service.portfolio import (
    HIGH_CORRELATION,
    Sleeve,
    _correlation,
    _timestamped_returns,
    _weights,
    build_sleeves,
    run_portfolio,
)
from market_data.models import OHLCVBar

START = datetime(2025, 1, 2, 14, 30, tzinfo=timezone.utc)


def _bars(n: int, seed: int, symbol: str = "SYN", start: datetime = START) -> list[OHLCVBar]:
    random.seed(seed)
    price = 100.0
    out: list[OHLCVBar] = []
    for i in range(n):
        price *= math.exp(random.gauss(0.0, 0.004))
        out.append(
            OHLCVBar(
                symbol=symbol,
                timestamp=start + timedelta(minutes=15 * i),
                open=price,
                high=price * 1.003,
                low=price * 0.997,
                close=price,
                volume=100_000.0,
            )
        )
    return out


def _request() -> BacktestRequest:
    return BacktestRequest(symbol="PORTFOLIO", timeframe="intraday", intraday_minutes=15)


# ---------------------------------------------------------------------------
# The pieces
# ---------------------------------------------------------------------------
class TestCorrelation:
    def test_a_series_correlates_perfectly_with_itself(self) -> None:
        series = [0.01, -0.02, 0.005, 0.03, -0.01]
        assert _correlation(series, series) == pytest.approx(1.0)

    def test_an_inverted_series_correlates_at_minus_one(self) -> None:
        series = [0.01, -0.02, 0.005, 0.03, -0.01]
        assert _correlation(series, [-x for x in series]) == pytest.approx(-1.0)

    def test_a_flat_series_has_no_correlation_rather_than_zero(self) -> None:
        """0.0 would read as 'usefully uncorrelated'. An idle sleeve is not
        diversification — it is a sleeve that did nothing."""
        assert _correlation([0.0, 0.0, 0.0, 0.0], [0.01, -0.02, 0.03, 0.0]) is None

    def test_too_short_a_series_returns_none(self) -> None:
        assert _correlation([0.01], [0.02]) is None


class TestAlignment:
    def test_returns_are_paired_with_the_bar_they_happened_on(self) -> None:
        bars = _bars(5, seed=1)
        curve = [100.0, 110.0, 99.0, 99.0, 108.9]
        returns = _timestamped_returns(bars, 0, curve)
        assert list(returns) == [b.timestamp for b in bars[1:5]]
        assert returns[bars[1].timestamp] == pytest.approx(0.1)
        assert returns[bars[2].timestamp] == pytest.approx(-0.1)

    def test_the_start_index_offsets_the_timestamps(self) -> None:
        bars = _bars(10, seed=1)
        returns = _timestamped_returns(bars, 4, [100.0, 101.0, 102.0])
        assert list(returns) == [bars[5].timestamp, bars[6].timestamp]

    def test_the_extra_closing_point_folds_into_the_last_bar(self) -> None:
        """The simulation appends one extra value when it closes a position at
        the end. It must not be hung on a bar that does not exist."""
        bars = _bars(4, seed=1)
        returns = _timestamped_returns(bars, 0, [100.0, 101.0, 102.0, 103.0, 104.0])
        assert len(returns) <= len(bars) - 1
        assert max(returns) == bars[-1].timestamp

    def test_sleeves_on_offset_calendars_align_by_time_not_position(self) -> None:
        """Two symbols whose bars start a day apart must not have Tuesday
        compared against Wednesday."""
        early = _bars(400, seed=1, symbol="EARLY")
        late = _bars(400, seed=2, symbol="LATE", start=START + timedelta(days=1))
        result = run_portfolio(
            _request(),
            [Sleeve("ema_rsi_macd", "EARLY"), Sleeve("ema_rsi_macd", "LATE")],
            {"EARLY": early, "LATE": late},
        )
        # The union of two barely-overlapping calendars is longer than either.
        assert result.aligned_bars > len(early) - 51


class TestWeights:
    def test_equal_allocation_splits_evenly(self) -> None:
        assert _weights("equal", [0.01, 0.02, 0.04]) == pytest.approx([1 / 3] * 3)

    def test_inverse_volatility_favours_the_calmer_sleeve(self) -> None:
        weights = _weights("inverse_volatility", [0.01, 0.02])
        assert weights[0] > weights[1]
        assert sum(weights) == pytest.approx(1.0)

    def test_inverse_volatility_falls_back_when_nothing_moved(self) -> None:
        assert _weights("inverse_volatility", [0.0, 0.0]) == pytest.approx([0.5, 0.5])


# ---------------------------------------------------------------------------
# The whole thing
# ---------------------------------------------------------------------------
class TestPortfolio:
    def test_identical_sleeves_are_reported_as_identical(self) -> None:
        """The clearest possible failure of diversification, and the clearest
        test that the machinery detects it."""
        bars = _bars(1_000, seed=1)
        result = run_portfolio(
            _request(),
            [Sleeve("ema_rsi_macd", "AAA"), Sleeve("ema_rsi_macd", "CLONE")],
            {"AAA": bars, "CLONE": list(bars)},
        )
        assert result.correlations[0].correlation == pytest.approx(1.0)
        assert result.diversification_ratio == pytest.approx(1.0)
        assert any("correlate at 1.00" in w for w in result.warnings)

    def test_an_equal_combination_is_not_reported_as_a_loss(self) -> None:
        """Rounded sleeve figures used to make an exact tie look like a defeat."""
        bars = _bars(1_000, seed=1)
        result = run_portfolio(
            _request(),
            [Sleeve("ema_rsi_macd", "AAA"), Sleeve("ema_rsi_macd", "CLONE")],
            {"AAA": bars, "CLONE": list(bars)},
        )
        assert not any("did worse than its best single sleeve" in w for w in result.warnings)

    def test_uncorrelated_sleeves_diversify(self) -> None:
        result = run_portfolio(
            _request(),
            build_sleeves(["AAA", "BBB"], ["ema_rsi_macd", "bollinger_reversion"]),
            {"AAA": _bars(1_200, seed=1), "BBB": _bars(1_200, seed=2)},
        )
        assert result.max_correlation < HIGH_CORRELATION
        assert result.diversification_ratio > 1.0

    def test_diversification_reduces_variance_it_does_not_create_return(self) -> None:
        """The most common misreading of a good diversification ratio.

        Combining uncorrelated sleeves smooths the ride; it cannot lift the
        return above every part. The combined return has to sit inside the
        range of its sleeves, however well they diversify.
        """
        result = run_portfolio(
            _request(),
            build_sleeves(["AAA", "BBB"], ["ema_rsi_macd", "bollinger_reversion"]),
            {"AAA": _bars(1_200, seed=1), "BBB": _bars(1_200, seed=2)},
        )
        returns = [s.total_return_pct for s in result.sleeves]
        assert result.diversification_ratio > 1.0
        assert min(returns) <= result.total_return_pct <= max(returns)

    def test_the_combination_is_calmer_than_its_average_sleeve(self) -> None:
        """What diversification does buy: a smaller drawdown than the mean of
        the parts, when the parts are genuinely uncorrelated."""
        result = run_portfolio(
            _request(),
            build_sleeves(["AAA", "BBB"], ["ema_rsi_macd", "bollinger_reversion"]),
            {"AAA": _bars(1_200, seed=1), "BBB": _bars(1_200, seed=2)},
        )
        mean_drawdown = sum(s.max_drawdown_pct for s in result.sleeves) / len(result.sleeves)
        assert result.max_drawdown_pct < mean_drawdown

    def test_a_losing_combination_says_so(self) -> None:
        result = run_portfolio(
            _request(),
            build_sleeves(["AAA", "BBB"], ["ema_rsi_macd", "bollinger_reversion"]),
            {"AAA": _bars(1_200, seed=1), "BBB": _bars(1_200, seed=2)},
        )
        if result.sharpe_ratio < result.best_sleeve_sharpe:
            assert any("best single sleeve" in w for w in result.warnings)

    def test_weights_sum_to_one_and_match_the_sleeves(self) -> None:
        result = run_portfolio(
            _request(),
            build_sleeves(["AAA", "BBB"], ["ema_rsi_macd"]),
            {"AAA": _bars(1_000, seed=1), "BBB": _bars(1_000, seed=2)},
        )
        assert len(result.weights) == len(result.sleeves)
        assert sum(result.weights) == pytest.approx(1.0, abs=1e-3)

    def test_inverse_volatility_changes_the_weights(self) -> None:
        data = {"AAA": _bars(1_000, seed=1), "BBB": _bars(1_000, seed=2)}
        sleeves = build_sleeves(["AAA", "BBB"], ["ema_rsi_macd"])
        equal = run_portfolio(_request(), sleeves, data, allocation="equal")
        inverse = run_portfolio(_request(), sleeves, data, allocation="inverse_volatility")
        assert equal.weights != inverse.weights

    def test_an_unknown_allocation_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="Unknown allocation"):
            run_portfolio(
                _request(), [Sleeve("ema_rsi_macd", "AAA")], {"AAA": _bars(500, seed=1)},
                allocation="magic",
            )

    def test_a_sleeve_with_no_data_is_dropped_and_disclosed(self) -> None:
        """Silently running fewer sleeves would overstate the diversification."""
        result = run_portfolio(
            _request(),
            [Sleeve("ema_rsi_macd", "AAA"), Sleeve("ema_rsi_macd", "MISSING")],
            {"AAA": _bars(1_000, seed=1)},
        )
        assert len(result.sleeves) == 1
        assert any("no bars supplied" in w for w in result.warnings)

    def test_a_sleeve_without_enough_warm_up_is_dropped_and_disclosed(self) -> None:
        result = run_portfolio(
            _request(),
            [Sleeve("ema_rsi_macd", "AAA"), Sleeve("ema_rsi_macd", "SHORT")],
            {"AAA": _bars(1_000, seed=1), "SHORT": _bars(20, seed=2)},
        )
        assert len(result.sleeves) == 1
        assert any("warm-up" in w for w in result.warnings)

    def test_no_usable_sleeve_is_an_error(self) -> None:
        with pytest.raises(ValueError, match="No sleeve"):
            run_portfolio(_request(), [Sleeve("ema_rsi_macd", "AAA")], {"AAA": _bars(20, seed=1)})

    def test_an_empty_portfolio_is_an_error(self) -> None:
        with pytest.raises(ValueError, match="at least one sleeve"):
            run_portfolio(_request(), [], {"AAA": _bars(500, seed=1)})

    def test_a_larger_search_deflates_harder(self) -> None:
        """Screening fifty symbols and running three is a fifty-trial search."""
        data = {"AAA": _bars(1_200, seed=1), "BBB": _bars(1_200, seed=2)}
        sleeves = build_sleeves(["AAA", "BBB"], ["ema_rsi_macd", "bollinger_reversion"])
        honest = run_portfolio(_request(), sleeves, data, n_trials=200)
        flattering = run_portfolio(_request(), sleeves, data, n_trials=len(sleeves))
        assert honest.n_trials == 200
        if honest.deflated_sharpe_ratio is not None:
            assert honest.deflated_sharpe_ratio <= flattering.deflated_sharpe_ratio


class TestBuildSleeves:
    def test_it_is_the_cross_product(self) -> None:
        sleeves = build_sleeves(["aaa", "bbb"], ["ema_rsi_macd", "bollinger_reversion"])
        assert len(sleeves) == 4
        assert {s.symbol for s in sleeves} == {"AAA", "BBB"}

    def test_a_typo_in_a_strategy_name_fails_immediately(self) -> None:
        """Not three minutes into a run, after the data has been fetched."""
        with pytest.raises(ValueError, match="Unknown strategy"):
            build_sleeves(["AAA"], ["ema_rsi_macd", "nope"])

    def test_params_are_carried_onto_every_sleeve(self) -> None:
        params = StrategyParams(bb_period=30)
        sleeves = build_sleeves(["AAA"], ["bollinger_reversion"], params)
        assert sleeves[0].params.bb_period == 30
