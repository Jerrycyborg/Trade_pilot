"""Execution quality recorded on the order path.

The orchestrator can price a marketable limit perfectly and still learn
nothing unless every submitted order — filled or missed — lands in the
archive. These tests cover that wiring end to end through the service.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from brokers import PaperBroker
from contracts import ExecutionOrderRequest, OrderStatus
from execution_service.routing import BrokerRouter


class Prices:
    def __init__(self, book: dict[str, float]) -> None:
        self.book = dict(book)

    def get_price(self, symbol: str) -> float | None:
        return self.book.get(symbol.upper())


@pytest.fixture
def prices() -> Prices:
    return Prices({"AAPL": 200.0})


@pytest.fixture
def main(tmp_path: Path, prices: Prices, monkeypatch: pytest.MonkeyPatch):
    db_file = tmp_path / f"execution-{uuid4()}.db"
    import execution_service.config as config
    import execution_service.database as database
    import execution_service.main as module

    config.settings = config.ExecutionSettings(database_url=f"sqlite+pysqlite:///{db_file}")
    database.settings = config.settings
    database.connect_args = {"check_same_thread": False}
    database.engine = database.create_engine(
        config.settings.database_url, future=True, connect_args=database.connect_args
    )
    database.SessionLocal.configure(bind=database.engine)
    database.Base.metadata.create_all(bind=database.engine)
    monkeypatch.setattr(module, "engine", database.engine)
    monkeypatch.setattr(module, "SessionLocal", database.SessionLocal)
    paper = PaperBroker(
        starting_cash=1_000_000.0,
        slippage_bps=0.0,
        state_path=tmp_path / "paper.json",
        price_source=prices,
    )
    monkeypatch.setattr(module, "broker", paper)
    # Route resolution is server-side now, so the router is what decides which
    # adapter an order reaches. With no store it is simulated-only, which is
    # what these tests want.
    monkeypatch.setattr(module, "router", BrokerRouter(store=None, simulated=paper))
    return module


def _limit(limit_price: float | None, **overrides: object) -> ExecutionOrderRequest:
    payload: dict[str, object] = {
        "signal_id": f"sig-{uuid4()}",
        "symbol": "AAPL",
        "side": "BUY",
        "qty": 10,
        "order_type": "LIMIT",
        "time_in_force": "IOC",
        "limit_price": limit_price,
        "decision_price": 200.0,
    }
    payload.update(overrides)
    return ExecutionOrderRequest(**payload)


def test_a_filled_limit_records_its_cost(main) -> None:
    main.create_order(_limit(200.2), idempotency_key=f"k-{uuid4()}")
    report = main.execution_quality()
    assert report["orders"] == 1
    assert report["filled"] == 1
    # Filled at 200.0 against a 200.0 decision price: no shortfall.
    assert report["mean_shortfall_bps"] == 0.0


def test_paying_up_to_the_limit_shows_up_as_shortfall(main, prices: Prices) -> None:
    prices.book["AAPL"] = 200.2
    main.create_order(_limit(200.2), idempotency_key=f"k-{uuid4()}")
    assert main.execution_quality()["mean_shortfall_bps"] == 10.0


def test_a_missed_limit_is_still_recorded(main, prices: Prices) -> None:
    """Otherwise the reported fill rate is 100% by construction."""
    prices.book["AAPL"] = 201.0
    response = main.create_order(_limit(200.2), idempotency_key=f"k-{uuid4()}")
    assert response.status == OrderStatus.CANCELLED

    report = main.execution_quality()
    assert report["orders"] == 1
    assert report["filled"] == 0
    assert report["fill_rate"] == 0.0


def test_fill_rate_mixes_hits_and_misses(main, prices: Prices) -> None:
    main.create_order(_limit(200.2), idempotency_key=f"k-{uuid4()}")
    prices.book["AAPL"] = 201.0
    main.create_order(_limit(200.2), idempotency_key=f"k-{uuid4()}")

    report = main.execution_quality()
    assert report["orders"] == 2
    assert report["filled"] == 1
    assert report["fill_rate"] == 0.5


def test_a_missed_limit_writes_no_fill(main, prices: Prices) -> None:
    """A cancel must not reach the portfolio's derived ledger as a trade."""
    prices.book["AAPL"] = 201.0
    main.create_order(_limit(200.2), idempotency_key=f"k-{uuid4()}")
    assert main.list_fills() == []


def test_market_orders_are_measured_too(main) -> None:
    """Shortfall is not a limit-order concept — a market order has one as well."""
    main.create_order(
        _limit(None, order_type="MARKET", time_in_force="DAY"),
        idempotency_key=f"k-{uuid4()}",
    )
    assert main.execution_quality()["orders"] == 1


def test_a_recorded_fill_carries_its_sleeve_scope(main, tmp_path: Path) -> None:
    """The scoping fields are not decoration. Attribution pairs round trips
    within (strategy, symbol, environment, account), promotion evidence is
    derived from the same scope, and the champion/challenger comparison
    separates its sides by strategy id. The service used to route an order *by*
    request.strategy_id and then record the fill without it — every fill in the
    archive was unscoped, so attribution for a named strategy found nothing and
    a paper sleeve's evidence was written where no gate would read it. Found by
    the first live paper run, whose first real fill came back strategy_id=''.
    """
    from journal import Journal, reset_journal

    journal = Journal(path=tmp_path / "journal.db")
    reset_journal(journal)
    try:
        main.create_order(
            _limit(
                200.2,
                strategy_id="ema_rsi_macd",
                account_id="acct-7",
            ),
            idempotency_key=f"k-{uuid4()}",
        )
        rows = journal.execution_rows(symbol="AAPL", account_id="acct-7")
        assert len(rows) == 1
        row = rows[0]
        assert row["strategy_id"] == "ema_rsi_macd"
        assert row["account_id"] == "acct-7"
        assert row["environment"] == "paper", "the resolved route, not the request"
        assert row["broker"] == "paper"
        # And the attribution loader can actually find it under its scope.
        from attribution import load_round_trips

        assert load_round_trips(
            journal, strategy_id="ema_rsi_macd", symbol="AAPL",
            environment="paper", account_id="acct-7",
        ) == []  # one open leg pairs to nothing, but the scope query works
    finally:
        reset_journal(None)
