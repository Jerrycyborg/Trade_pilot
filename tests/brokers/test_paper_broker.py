"""Tests for the PaperBroker fill simulator.

These cover the accounting that makes paper mode measurable: fills priced from
the market, cash movement, mark-to-market and realised P&L on close.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from brokers import PaperBroker
from contracts import ExecutionOrderRequest, OrderStatus


class Prices:
    def __init__(self, book: dict[str, float] | None = None) -> None:
        self.book = dict(book or {})

    def get_price(self, symbol: str) -> float | None:
        return self.book.get(symbol.upper())


@pytest.fixture
def prices() -> Prices:
    return Prices({"AAPL": 200.0})


@pytest.fixture
def broker(tmp_path: Path, prices: Prices) -> PaperBroker:
    return PaperBroker(
        starting_cash=10_000.0,
        slippage_bps=0.0,
        state_path=tmp_path / "state.json",
        price_source=prices,
    )


def _order(symbol: str = "AAPL", side: str = "BUY", qty: int = 10) -> ExecutionOrderRequest:
    return ExecutionOrderRequest(
        signal_id=f"sig-{symbol}-{side}",
        symbol=symbol,
        side=side,
        qty=qty,
        order_type="MARKET",
    )


class TestFillPricing:
    def test_fills_at_the_market_price(self, broker: PaperBroker) -> None:
        """Regression: every fill used to be booked at a flat $100."""
        result = broker.place_order(_order())
        assert result.status == OrderStatus.ACCEPTED
        assert result.fill_price == 200.0

    def test_slippage_works_against_the_trader(self, tmp_path: Path, prices: Prices) -> None:
        broker = PaperBroker(
            starting_cash=10_000.0,
            slippage_bps=50.0,  # 0.5%
            state_path=tmp_path / "state.json",
            price_source=prices,
        )
        buy = broker.place_order(_order(side="BUY", qty=1))
        sell = broker.place_order(_order(side="SELL", qty=1))
        assert buy.fill_price > 200.0
        assert sell.fill_price < 200.0

    def test_unpriceable_symbol_is_rejected_not_filled_at_a_placeholder(
        self, broker: PaperBroker
    ) -> None:
        """Filling at an invented price writes fictitious cash, exposure and P&L
        into the ledger — the exact thing this simulator exists to stop."""
        result = broker.place_order(_order(symbol="NOPRICE", qty=1))
        assert result.status == OrderStatus.REJECTED
        assert result.rejection_reason == "no_market_price"

    def test_unpriceable_order_leaves_the_ledger_untouched(
        self, broker: PaperBroker
    ) -> None:
        broker.place_order(_order(symbol="NOPRICE", qty=1))
        assert broker.get_positions() == []
        assert broker.get_account().cash == 10_000.0

    def test_close_is_refused_when_the_symbol_cannot_be_priced(
        self, broker: PaperBroker, prices: Prices
    ) -> None:
        """Better to leave the position open and visible than to book an exit at
        a made-up price."""
        broker.place_order(_order(qty=5))
        del prices.book["AAPL"]

        assert broker.close_position("AAPL") is False
        assert broker.get_positions()[0].qty == 5


class TestPositionAccounting:
    def test_buy_creates_a_position_and_spends_cash(self, broker: PaperBroker) -> None:
        broker.place_order(_order(qty=10))

        positions = broker.get_positions()
        assert len(positions) == 1
        assert positions[0].symbol == "AAPL"
        assert positions[0].qty == 10
        assert positions[0].average_price == 200.0
        assert broker.get_account().cash == 8_000.0

    def test_position_marks_to_market(self, broker: PaperBroker, prices: Prices) -> None:
        broker.place_order(_order(qty=10))
        prices.book["AAPL"] = 220.0

        position = broker.get_positions()[0]
        assert position.market_value == 2_200.0
        assert position.unrealized_pnl == 200.0
        assert broker.get_account().equity == 10_200.0

    def test_averaging_up_blends_the_entry_price(
        self, broker: PaperBroker, prices: Prices
    ) -> None:
        broker.place_order(_order(qty=10))
        prices.book["AAPL"] = 300.0
        broker.place_order(_order(qty=10))

        position = broker.get_positions()[0]
        assert position.qty == 20
        assert position.average_price == 250.0

    def test_partial_sell_realises_pnl_on_the_closed_portion(
        self, broker: PaperBroker, prices: Prices
    ) -> None:
        broker.place_order(_order(qty=10))
        prices.book["AAPL"] = 220.0
        broker.place_order(_order(side="SELL", qty=4))

        assert broker.realized_pnl() == 80.0  # 4 shares x $20
        assert broker.get_positions()[0].qty == 6

    def test_buy_is_rejected_when_cash_is_insufficient(self, broker: PaperBroker) -> None:
        result = broker.place_order(_order(qty=100))  # $20k against $10k cash
        assert result.status == OrderStatus.REJECTED
        assert "insufficient_cash" in result.rejection_reason
        assert broker.get_positions() == []

    def test_qty_limit_is_enforced(self, tmp_path: Path, prices: Prices) -> None:
        broker = PaperBroker(
            max_qty=5, starting_cash=10_000_000.0,
            state_path=tmp_path / "s.json", price_source=prices,
        )
        result = broker.place_order(_order(qty=10))
        assert result.status == OrderStatus.REJECTED
        assert result.rejection_reason == "qty_limit_exceeded"


class TestClosePosition:
    def test_close_flattens_and_realises_pnl(
        self, broker: PaperBroker, prices: Prices
    ) -> None:
        """Regression: close_position() previously always returned False."""
        broker.place_order(_order(qty=10))
        prices.book["AAPL"] = 220.0

        assert broker.close_position("AAPL") is True
        assert broker.get_positions() == []
        assert broker.realized_pnl() == 200.0
        assert broker.get_account().cash == 10_200.0

    def test_partial_close_leaves_the_remainder_open(
        self, broker: PaperBroker, prices: Prices
    ) -> None:
        broker.place_order(_order(qty=10))
        assert broker.close_position("AAPL", units=4) is True
        assert broker.get_positions()[0].qty == 6

    def test_closing_an_absent_position_reports_failure(self, broker: PaperBroker) -> None:
        assert broker.close_position("AAPL") is False


class TestShorts:
    def test_sell_without_a_position_opens_a_short(self, broker: PaperBroker) -> None:
        broker.place_order(_order(side="SELL", qty=5))
        position = broker.get_positions()[0]
        assert position.qty == -5
        assert broker.get_account().cash == 11_000.0

    def test_covering_a_short_lower_realises_a_gain(
        self, broker: PaperBroker, prices: Prices
    ) -> None:
        broker.place_order(_order(side="SELL", qty=5))
        prices.book["AAPL"] = 180.0
        broker.place_order(_order(side="BUY", qty=5))

        assert broker.realized_pnl() == 100.0  # 5 shares x $20
        assert broker.get_positions() == []


class TestPersistence:
    def test_ledger_survives_a_restart(self, tmp_path: Path, prices: Prices) -> None:
        """The monitors depend on positions outliving a service restart."""
        state_path = tmp_path / "state.json"
        first = PaperBroker(
            starting_cash=10_000.0, slippage_bps=0.0,
            state_path=state_path, price_source=prices,
        )
        first.place_order(_order(qty=10))

        second = PaperBroker(
            starting_cash=10_000.0, slippage_bps=0.0,
            state_path=state_path, price_source=prices,
        )
        assert second.get_account().cash == 8_000.0
        assert second.get_positions()[0].qty == 10

    def test_corrupt_state_starts_clean_rather_than_crashing(
        self, tmp_path: Path, prices: Prices
    ) -> None:
        state_path = tmp_path / "state.json"
        state_path.write_text("{not json", encoding="utf-8")
        broker = PaperBroker(
            starting_cash=10_000.0, state_path=state_path, price_source=prices
        )
        assert broker.get_account().cash == 10_000.0

    def test_reset_returns_to_starting_cash(self, broker: PaperBroker) -> None:
        broker.place_order(_order(qty=10))
        broker.reset()
        assert broker.get_account().cash == 10_000.0
        assert broker.get_positions() == []


def test_order_history_records_each_fill(broker: PaperBroker) -> None:
    broker.place_order(_order(qty=2))
    broker.place_order(_order(side="SELL", qty=1))

    history = broker.get_order_history()
    assert [row["side"] for row in history] == ["BUY", "SELL"]
    assert history[0]["fill_price"] == 200.0


class TestSideValidation:
    """Anything not exactly BUY used to fall through to the SELL branch, so a
    typo opened a simulated short and reported an accepted fill."""

    @pytest.mark.parametrize("side", ["BUYY", "SEL", "LONG", "", "HOLD", "EXIT"])
    def test_unsupported_side_is_rejected(self, broker: PaperBroker, side: str) -> None:
        result = broker.place_order(
            ExecutionOrderRequest(
                signal_id="s", symbol="AAPL", side=side, qty=1, order_type="MARKET"
            )
        )
        assert result.status == OrderStatus.REJECTED
        assert "unsupported_side" in result.rejection_reason

    def test_rejected_side_does_not_touch_the_ledger(self, broker: PaperBroker) -> None:
        broker.place_order(
            ExecutionOrderRequest(
                signal_id="s", symbol="AAPL", side="BUYY", qty=5, order_type="MARKET"
            )
        )
        assert broker.get_positions() == []
        assert broker.get_account().cash == 10_000.0
        assert broker.get_order_history() == []

    @pytest.mark.parametrize("side", ["buy", "Buy", "sell", "SELL"])
    def test_supported_sides_are_case_insensitive(
        self, broker: PaperBroker, side: str
    ) -> None:
        result = broker.place_order(
            ExecutionOrderRequest(
                signal_id="s", symbol="AAPL", side=side, qty=1, order_type="MARKET"
            )
        )
        assert result.status == OrderStatus.ACCEPTED
