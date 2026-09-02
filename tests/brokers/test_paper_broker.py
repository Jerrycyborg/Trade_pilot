"""Tests for the PaperBroker fill simulator.

These cover the accounting that makes paper mode measurable: fills priced from
the market, cash movement, mark-to-market and realised P&L on close.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from brokers import PaperBroker, PaperStateCorruptError, PaperStatePersistenceError
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

    def test_unpriceable_order_leaves_the_ledger_untouched(self, broker: PaperBroker) -> None:
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

    def test_averaging_up_blends_the_entry_price(self, broker: PaperBroker, prices: Prices) -> None:
        broker.place_order(_order(qty=10))
        prices.book["AAPL"] = 300.0
        second = _order(qty=10)
        second.signal_id = "sig-AAPL-BUY-2"
        broker.place_order(second)

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
            max_qty=5,
            starting_cash=10_000_000.0,
            state_path=tmp_path / "s.json",
            price_source=prices,
        )
        result = broker.place_order(_order(qty=10))
        assert result.status == OrderStatus.REJECTED
        assert result.rejection_reason == "qty_limit_exceeded"


class TestClosePosition:
    def test_close_flattens_and_realises_pnl(self, broker: PaperBroker, prices: Prices) -> None:
        """Regression: close_position() previously always returned False."""
        broker.place_order(_order(qty=10))
        prices.book["AAPL"] = 220.0

        assert broker.close_position("AAPL")  # truthy: the close fill details
        assert broker.get_positions() == []
        assert broker.realized_pnl() == 200.0
        assert broker.get_account().cash == 10_200.0

    def test_partial_close_leaves_the_remainder_open(
        self, broker: PaperBroker, prices: Prices
    ) -> None:
        broker.place_order(_order(qty=10))
        assert broker.close_position("AAPL", units=4)  # truthy: the close fill details
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
            starting_cash=10_000.0,
            slippage_bps=0.0,
            state_path=state_path,
            price_source=prices,
        )
        first.place_order(_order(qty=10))

        second = PaperBroker(
            starting_cash=10_000.0,
            slippage_bps=0.0,
            state_path=state_path,
            price_source=prices,
        )
        assert second.get_account().cash == 8_000.0
        assert second.get_positions()[0].qty == 10

    def test_corrupt_state_refuses_to_start(self, tmp_path: Path, prices: Prices) -> None:
        state_path = tmp_path / "state.json"
        state_path.write_text("{not json", encoding="utf-8")
        with pytest.raises(PaperStateCorruptError, match="unreadable"):
            PaperBroker(starting_cash=10_000.0, state_path=state_path, price_source=prices)

    def test_malformed_replay_ledger_refuses_to_start(
        self,
        tmp_path: Path,
        prices: Prices,
    ) -> None:
        state_path = tmp_path / "bad-replays.json"
        state_path.write_text(
            json.dumps(
                {
                    "schema_version": 3,
                    "cash": 10_000.0,
                    "starting_cash": 10_000.0,
                    "realized_pnl": 0.0,
                    "positions": {},
                    "orders": [],
                    "replays": {"not-a-digest": {}},
                }
            ),
            encoding="utf-8",
        )

        with pytest.raises(PaperStateCorruptError, match="unreadable"):
            PaperBroker(
                starting_cash=10_000.0,
                state_path=state_path,
                price_source=prices,
            )

    def test_v2_state_restores_position_and_replay_identity(
        self,
        tmp_path: Path,
        prices: Prices,
    ) -> None:
        state_path = tmp_path / "legacy-v2.json"
        first = PaperBroker(
            starting_cash=10_000.0,
            slippage_bps=0.0,
            state_path=state_path,
            price_source=prices,
        )
        request = _order(qty=2)
        fill = first.place_order(request)
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        payload["schema_version"] = 2
        payload.pop("replays")
        for position in payload["positions"].values():
            position.pop("position_id")
            position.pop("order_ids")
        state_path.write_text(json.dumps(payload), encoding="utf-8")

        migrated = PaperBroker(
            starting_cash=10_000.0,
            slippage_bps=0.0,
            state_path=state_path,
            price_source=prices,
        )

        assert migrated.place_order(request).external_order_id == fill.external_order_id
        assert migrated.get_positions()[0].qty == 2

    def test_reset_returns_to_starting_cash(self, broker: PaperBroker) -> None:
        broker.place_order(_order(qty=10))
        broker.reset()
        assert broker.get_account().cash == 10_000.0
        assert broker.get_positions() == []

    def test_failed_save_rejects_order_and_rolls_back_memory(
        self,
        broker: PaperBroker,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def fail() -> None:
            raise PaperStatePersistenceError("disk unavailable")

        monkeypatch.setattr(broker, "_save_state", fail)
        result = broker.place_order(_order(qty=2))

        assert result.status == OrderStatus.REJECTED
        assert result.rejection_reason == "paper_state_persistence_failed"
        assert broker.get_positions() == []
        assert broker.get_account().cash == 10_000.0
        assert broker.get_order_history() == []


class TestSleeveIsolation:
    def test_opposite_strategies_do_not_close_each_others_virtual_position(
        self,
        broker: PaperBroker,
    ) -> None:
        champion = _order(side="BUY", qty=5)
        challenger = _order(side="SELL", qty=5)
        challenger.strategy_id = "ema_rsi_macd@chal-0123456789ab"

        champion_fill = broker.place_order(champion)
        assert champion_fill.status == OrderStatus.ACCEPTED
        assert broker.place_order(challenger).status == OrderStatus.ACCEPTED

        sleeves = broker.get_sleeve_positions()
        assert {(row.strategy_id, row.qty) for row in sleeves} == {
            ("ema_rsi_macd", 5),
            ("ema_rsi_macd@chal-0123456789ab", -5),
        }
        assert broker.get_positions() == [], "the broker-level account is net flat"

        closed = broker.close_position(
            champion_fill.external_order_id,
            symbol="AAPL",
            strategy_id="ema_rsi_macd",
            account_id="default",
        )
        assert closed
        remaining = broker.get_sleeve_positions()
        assert [(row.strategy_id, row.qty) for row in remaining] == [
            ("ema_rsi_macd@chal-0123456789ab", -5)
        ]

    def test_close_refuses_an_ambiguous_symbol(
        self,
        broker: PaperBroker,
    ) -> None:
        broker.place_order(_order(side="BUY", qty=5))
        challenger = _order(side="SELL", qty=5)
        challenger.strategy_id = "ema_rsi_macd@chal-0123456789ab"
        broker.place_order(challenger)

        assert broker.close_position("AAPL", symbol="AAPL") is False


def test_order_history_records_each_fill(broker: PaperBroker) -> None:
    broker.place_order(_order(qty=2))
    broker.place_order(_order(side="SELL", qty=1))

    history = broker.get_order_history()
    assert [row["side"] for row in history] == ["BUY", "SELL"]
    assert history[0]["fill_price"] == 200.0


def test_duplicate_signal_is_replayed_without_a_second_fill(
    broker: PaperBroker,
    prices: Prices,
) -> None:
    request = _order(qty=2)
    first = broker.place_order(request)
    del prices.book["AAPL"]
    second = broker.place_order(request)

    assert second.external_order_id == first.external_order_id
    assert second.fill_price == first.fill_price
    assert broker.get_positions()[0].qty == 2
    assert len(broker.get_order_history()) == 1


def test_replay_survives_order_history_eviction_and_restart(
    tmp_path: Path,
    prices: Prices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import brokers.paper_broker as paper_module

    monkeypatch.setattr(paper_module, "_MAX_ORDER_HISTORY", 3)
    state_path = tmp_path / "bounded-history.json"
    first_broker = PaperBroker(
        starting_cash=1_000_000.0,
        slippage_bps=0.0,
        state_path=state_path,
        price_source=prices,
    )
    original = _order(qty=1)
    original.signal_id = "oldest-signal"
    first = first_broker.place_order(original)
    for index in range(4):
        request = _order(qty=1)
        request.signal_id = f"newer-signal-{index}"
        assert first_broker.place_order(request).status == OrderStatus.ACCEPTED

    assert len(first_broker.get_order_history()) == 3
    assert first_broker.place_order(original).external_order_id == first.external_order_id
    assert first_broker.get_positions()[0].qty == 5

    restarted = PaperBroker(
        starting_cash=1_000_000.0,
        slippage_bps=0.0,
        state_path=state_path,
        price_source=prices,
    )
    assert restarted.place_order(original).external_order_id == first.external_order_id
    assert restarted.get_positions()[0].qty == 5


def test_any_entry_from_the_current_generation_can_identify_the_sleeve(
    broker: PaperBroker,
) -> None:
    first = broker.place_order(_order(qty=2))
    second_request = _order(qty=3)
    second_request.signal_id = "scale-in"
    second = broker.place_order(second_request)

    assert broker.close_position(
        second.external_order_id,
        symbol="AAPL",
        strategy_id="ema_rsi_macd",
        signal_id="close-scaled-position",
    )
    assert first.external_order_id != second.external_order_id
    assert broker.get_positions() == []


def test_entry_capacity_is_reserved_before_exits_are_stranded(
    broker: PaperBroker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import brokers.paper_broker as paper_module

    monkeypatch.setattr(paper_module, "_MAX_REPLAY_RECORDS", 2)
    monkeypatch.setattr(paper_module, "_REPLAY_EXIT_RESERVE", 1)
    entry = broker.place_order(_order(qty=2))

    another = _order(qty=1)
    another.signal_id = "entry-after-cap"
    refused = broker.place_order(another)
    assert refused.status == OrderStatus.REJECTED
    assert refused.rejection_reason == "paper_replay_ledger_full"

    assert broker.close_position(
        entry.external_order_id,
        symbol="AAPL",
        strategy_id="ema_rsi_macd",
        signal_id="reserved-exit",
    )
    assert broker.get_positions() == []


def test_stale_position_generation_cannot_close_a_replacement(
    broker: PaperBroker,
) -> None:
    first = broker.place_order(_order(qty=2))
    assert broker.close_position(
        first.external_order_id,
        symbol="AAPL",
        strategy_id="ema_rsi_macd",
        signal_id="close-first-generation",
    )

    replacement_request = _order(qty=3)
    replacement_request.signal_id = "replacement-entry"
    replacement = broker.place_order(replacement_request)

    assert (
        broker.close_position(
            first.external_order_id,
            symbol="AAPL",
            strategy_id="ema_rsi_macd",
            signal_id="stale-close",
        )
        is False
    )
    sleeve = broker.get_sleeve_positions()[0]
    assert sleeve.qty == 3
    assert sleeve.position_id == replacement.external_order_id


def test_close_signal_is_idempotent_and_rejects_payload_changes(
    broker: PaperBroker,
) -> None:
    entry = broker.place_order(_order(qty=10))
    first = broker.close_position(
        entry.external_order_id,
        units=2,
        symbol="AAPL",
        strategy_id="ema_rsi_macd",
        signal_id="partial-close",
    )
    replay = broker.close_position(
        entry.external_order_id,
        units=2,
        symbol="AAPL",
        strategy_id="ema_rsi_macd",
        signal_id="partial-close",
    )
    mismatch = broker.close_position(
        entry.external_order_id,
        units=3,
        symbol="AAPL",
        strategy_id="ema_rsi_macd",
        signal_id="partial-close",
    )

    assert isinstance(first, dict)
    assert replay == first
    assert mismatch is False
    assert broker.get_sleeve_positions()[0].qty == 8


def test_full_close_replay_returns_the_original_fill(
    broker: PaperBroker,
) -> None:
    entry = broker.place_order(_order(qty=2))
    first = broker.close_position(
        entry.external_order_id,
        symbol="AAPL",
        strategy_id="ema_rsi_macd",
        signal_id="full-close",
    )
    replay = broker.close_position(
        entry.external_order_id,
        symbol="AAPL",
        strategy_id="ema_rsi_macd",
        signal_id="full-close",
    )

    assert isinstance(first, dict)
    assert replay == first
    assert broker.get_positions() == []


def test_reused_signal_id_with_different_payload_is_rejected(
    broker: PaperBroker,
) -> None:
    request = _order(qty=2)
    broker.place_order(request)
    changed = request.model_copy(update={"qty": 3})

    result = broker.place_order(changed)

    assert result.status == OrderStatus.REJECTED
    assert result.rejection_reason == "signal_id_payload_mismatch"
    assert broker.get_positions()[0].qty == 2


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
    def test_supported_sides_are_case_insensitive(self, broker: PaperBroker, side: str) -> None:
        result = broker.place_order(
            ExecutionOrderRequest(
                signal_id="s", symbol="AAPL", side=side, qty=1, order_type="MARKET"
            )
        )
        assert result.status == OrderStatus.ACCEPTED


def _limit_order(
    limit_price: float,
    symbol: str = "AAPL",
    side: str = "BUY",
    qty: int = 10,
) -> ExecutionOrderRequest:
    return ExecutionOrderRequest(
        signal_id=f"sig-{symbol}-{side}",
        symbol=symbol,
        side=side,
        qty=qty,
        order_type="LIMIT",
        limit_price=limit_price,
        decision_price=200.0,
    )


class TestLimitOrders:
    """A paper run that only ever fills cannot show what limit pricing costs.

    The simulator has to be able to miss, otherwise the fill rate reported by
    paper mode is a fiction and the backtest inherits it.
    """

    def test_marketable_buy_limit_fills(self, broker: PaperBroker) -> None:
        result = broker.place_order(_limit_order(200.2))
        assert result.status == OrderStatus.ACCEPTED
        assert result.fill_price == 200.0

    def test_buy_limit_fills_at_the_market_when_the_market_is_better(
        self, broker: PaperBroker, prices: Prices
    ) -> None:
        """The limit is a cap, not a price you volunteer to pay."""
        prices.book["AAPL"] = 199.5
        result = broker.place_order(_limit_order(200.2))
        assert result.fill_price == 199.5

    def test_buy_limit_beyond_the_market_is_cancelled(
        self, broker: PaperBroker, prices: Prices
    ) -> None:
        prices.book["AAPL"] = 201.0
        result = broker.place_order(_limit_order(200.2))
        assert result.status == OrderStatus.CANCELLED
        assert result.rejection_reason == "limit_not_marketable"
        assert result.fill_price is None

    def test_marketable_sell_limit_fills(self, broker: PaperBroker) -> None:
        broker.place_order(_order(side="BUY", qty=10))
        result = broker.place_order(_limit_order(199.8, side="SELL"))
        assert result.status == OrderStatus.ACCEPTED
        assert result.fill_price == 200.0

    def test_sell_limit_below_the_market_is_cancelled(
        self, broker: PaperBroker, prices: Prices
    ) -> None:
        broker.place_order(_order(side="BUY", qty=10))
        prices.book["AAPL"] = 199.0
        result = broker.place_order(_limit_order(199.8, side="SELL"))
        assert result.status == OrderStatus.CANCELLED
        assert result.rejection_reason == "limit_not_marketable"

    def test_a_missed_limit_leaves_cash_and_positions_untouched(
        self, broker: PaperBroker, prices: Prices
    ) -> None:
        """A cancel that still moved the ledger would be worse than no simulation."""
        cash_before = broker.get_account().cash
        positions_before = len(broker.get_positions())

        prices.book["AAPL"] = 201.0
        assert broker.place_order(_limit_order(200.2)).status == OrderStatus.CANCELLED

        assert broker.get_account().cash == cash_before
        assert len(broker.get_positions()) == positions_before

    def test_a_limit_order_without_a_limit_price_behaves_as_a_market_order(
        self, broker: PaperBroker, prices: Prices
    ) -> None:
        """Nothing to enforce, so it must not silently cancel every order."""
        prices.book["AAPL"] = 201.0
        request = _limit_order(200.2)
        request.limit_price = None
        result = broker.place_order(request)
        assert result.status == OrderStatus.ACCEPTED
        assert result.fill_price == 201.0
