"""Tests for the pure execution helpers.

These are the arithmetic the whole execution-quality story rests on: what a
marketable limit is priced at, what it fills at, what that fill cost, and how
big an order is allowed to be relative to what actually trades.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from contracts.execution import (
    average_daily_volume,
    implementation_shortfall_bps,
    limit_fill_price,
    limit_is_marketable,
    marketable_limit_price,
    participation_capped_qty,
)


# ---------------------------------------------------------------------------
# Marketable limit pricing
# ---------------------------------------------------------------------------
def test_buy_limit_is_priced_above_the_reference() -> None:
    """A buy pays up to fill: 10bps through 200.00 is 200.20."""
    assert marketable_limit_price(200.0, "BUY", 10.0) == 200.2


def test_sell_limit_is_priced_below_the_reference() -> None:
    """A sell accepts down to fill — the mirror of the buy."""
    assert marketable_limit_price(200.0, "SELL", 10.0) == 199.8


def test_zero_tolerance_prices_at_the_reference() -> None:
    assert marketable_limit_price(200.0, "BUY", 0.0) == 200.0


def test_lowercase_side_is_accepted() -> None:
    assert marketable_limit_price(200.0, "buy", 10.0) == 200.2


@pytest.mark.parametrize("reference", [0.0, -1.0, None])
def test_unusable_reference_price_returns_none(reference) -> None:
    """No price, no order. Guessing a reference is how a fat-finger fill happens."""
    assert marketable_limit_price(reference, "BUY", 10.0) is None


def test_negative_tolerance_is_rejected() -> None:
    """A negative tolerance would price a buy *below* the market — never marketable."""
    with pytest.raises(ValueError):
        marketable_limit_price(200.0, "BUY", -5.0)


# ---------------------------------------------------------------------------
# Fill behaviour
# ---------------------------------------------------------------------------
def test_buy_limit_fills_when_market_is_at_or_below_it() -> None:
    assert limit_is_marketable(200.2, 200.0, "BUY") is True
    assert limit_is_marketable(200.2, 200.2, "BUY") is True
    assert limit_is_marketable(200.2, 200.3, "BUY") is False


def test_sell_limit_fills_when_market_is_at_or_above_it() -> None:
    assert limit_is_marketable(199.8, 200.0, "SELL") is True
    assert limit_is_marketable(199.8, 199.8, "SELL") is True
    assert limit_is_marketable(199.8, 199.7, "SELL") is False


def test_buy_gets_the_market_price_when_it_is_better_than_the_limit() -> None:
    """A limit caps what you pay; it does not oblige you to pay it."""
    assert limit_fill_price(200.2, 199.9, "BUY") == 199.9


def test_sell_gets_the_market_price_when_it_is_better_than_the_limit() -> None:
    assert limit_fill_price(199.8, 200.4, "SELL") == 200.4


def test_buy_fill_is_capped_at_the_limit() -> None:
    assert limit_fill_price(200.2, 200.2, "BUY") == 200.2


def test_limit_beyond_the_market_does_not_fill() -> None:
    """This is the whole point of a limit: the bad fill is refused."""
    assert limit_fill_price(200.2, 201.0, "BUY") is None
    assert limit_fill_price(199.8, 199.0, "SELL") is None


# ---------------------------------------------------------------------------
# Implementation shortfall
# ---------------------------------------------------------------------------
def test_buying_above_the_decision_price_is_a_cost() -> None:
    assert implementation_shortfall_bps(200.0, 200.2, "BUY") == 10.0


def test_selling_below_the_decision_price_is_also_a_cost() -> None:
    """The sign flips for sells so costs do not cancel out when averaged."""
    assert implementation_shortfall_bps(200.0, 199.8, "SELL") == 10.0


def test_a_favourable_fill_is_negative() -> None:
    assert implementation_shortfall_bps(200.0, 199.8, "BUY") == -10.0
    assert implementation_shortfall_bps(200.0, 200.2, "SELL") == -10.0


def test_mixing_buys_and_sells_does_not_cancel_real_costs() -> None:
    """Without the sign convention this average would read 0bps — free trading."""
    costs = [
        implementation_shortfall_bps(200.0, 200.2, "BUY"),
        implementation_shortfall_bps(200.0, 199.8, "SELL"),
    ]
    assert sum(costs) / len(costs) == 10.0


@pytest.mark.parametrize(
    ("decision", "fill"),
    [(0.0, 200.0), (200.0, 0.0), (None, 200.0), (200.0, None), (-1.0, 200.0)],
)
def test_shortfall_is_none_without_two_usable_prices(decision, fill) -> None:
    assert implementation_shortfall_bps(decision, fill, "BUY") is None


# ---------------------------------------------------------------------------
# Participation cap
# ---------------------------------------------------------------------------
def test_order_is_trimmed_to_the_participation_ceiling() -> None:
    """1% of a 100k-share ADV is 1,000 shares, whatever the strategy wanted."""
    assert participation_capped_qty(5_000, 100_000.0, 0.01) == 1_000


def test_order_within_the_ceiling_is_untouched() -> None:
    assert participation_capped_qty(100, 100_000.0, 0.01) == 100


def test_unknown_volume_does_not_trim() -> None:
    """No volume estimate is not evidence of a small symbol — leave the size alone."""
    assert participation_capped_qty(5_000, None, 0.01) == 5_000
    assert participation_capped_qty(5_000, 0.0, 0.01) == 5_000


def test_cap_disabled_by_zero_participation() -> None:
    assert participation_capped_qty(5_000, 100_000.0, 0.0) == 5_000


def test_thin_symbol_can_trim_to_zero() -> None:
    """1% of 50 shares is nothing tradeable — the caller must skip, not round up."""
    assert participation_capped_qty(10, 50.0, 0.01) == 0


def test_non_positive_qty_stays_zero() -> None:
    assert participation_capped_qty(0, 100_000.0, 0.01) == 0
    assert participation_capped_qty(-5, 100_000.0, 0.01) == 0


# ---------------------------------------------------------------------------
# ADV inference from bars
# ---------------------------------------------------------------------------
@dataclass
class Bar:
    volume: float


def test_daily_bars_average_directly() -> None:
    assert average_daily_volume([Bar(1_000), Bar(2_000), Bar(3_000)], bars_per_day=1.0) == 2_000.0


def test_intraday_bars_are_scaled_to_a_session() -> None:
    """A 5-minute bar is 1/78th of a session, so the mean is scaled back up."""
    assert average_daily_volume([Bar(1_000), Bar(2_000)], bars_per_day=78.0) == 117_000.0


def test_zero_volume_bars_are_ignored() -> None:
    """Halted or padded bars would otherwise drag the estimate toward zero."""
    assert average_daily_volume([Bar(0), Bar(2_000), Bar(0)], bars_per_day=1.0) == 2_000.0


def test_no_usable_bars_returns_none() -> None:
    assert average_daily_volume([], bars_per_day=78.0) is None
    assert average_daily_volume([Bar(0)], bars_per_day=78.0) is None
    assert average_daily_volume(None, bars_per_day=78.0) is None


def test_missing_volume_attribute_is_treated_as_no_data() -> None:
    class NoVolume:
        pass

    assert average_daily_volume([NoVolume()], bars_per_day=1.0) is None
