"""The per-sleeve position cap, enforced where the orders arrive.

The strategy worker re-signals every cycle, and a persistent signal used to
re-enter every cycle: the first live paper run stacked one sleeve's short
6 → 12 → 19 shares in three cycles with nothing anywhere deciding a bigger
position was wanted. The worker now checks its own book before submitting,
but that check lives in the thing being constrained — this cap is the
server-side control behind it, read from the journal's own fill record.

Two properties are load-bearing:

- Reduce-only orders are never refused by the cap. A guard that can trap a
  position behind itself is a risk control that adds risk.
- An unknowable book refuses entries. None is not zero: a journal that is
  disabled or unreadable does not know the sleeve is flat.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from brokers import PaperBroker
from contracts import ExecutionOrderRequest, OrderStatus
from execution_service.routing import BrokerRouter
from journal import Journal, reset_journal


class Prices:
    def __init__(self, book: dict[str, float]) -> None:
        self.book = dict(book)

    def get_price(self, symbol: str) -> float | None:
        return self.book.get(symbol.upper())


@pytest.fixture
def main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
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
        price_source=Prices({"NVDA": 200.0}),
    )
    monkeypatch.setattr(module, "broker", paper)
    monkeypatch.setattr(module, "router", BrokerRouter(store=None, simulated=paper))
    # The cap under test. The journal the cap reads is per-test via the
    # suite-wide JOURNAL_PATH fixture.
    module.config_settings = replace(config.settings, max_position_qty=15)
    yield module
    module.config_settings = config.settings


def _order(side: str, qty: int, *, reduce_only: bool = False) -> ExecutionOrderRequest:
    return ExecutionOrderRequest(
        signal_id=f"sig-{uuid4()}",
        symbol="NVDA",
        side=side,
        qty=qty,
        order_type="MARKET",
        time_in_force="DAY",
        decision_price=200.0,
        strategy_id="ema_rsi_macd",
        reduce_only=reduce_only,
    )


def _submit(main, side: str, qty: int, **kw):
    return main.create_order(_order(side, qty, **kw), idempotency_key=f"k-{uuid4()}")


def test_entries_stop_stacking_at_the_cap(main) -> None:
    """The first live paper run's failure mode, replayed: the same entry
    signal arriving cycle after cycle. The first two fit under the cap of 15;
    the third is refused by the service, whatever the caller thinks."""
    assert _submit(main, "SELL", 6).status != OrderStatus.REJECTED
    assert _submit(main, "SELL", 6).status != OrderStatus.REJECTED

    third = _submit(main, "SELL", 7)
    assert third.status == OrderStatus.REJECTED
    assert "position_cap" in (third.rejection_reason or "")


def test_a_reducing_entry_is_not_refused(main) -> None:
    """A BUY against a short reduces exposure even without the reduce_only
    flag. Refusing it would hold the position open in the cap's name."""
    _submit(main, "SELL", 12)
    assert _submit(main, "BUY", 6).status != OrderStatus.REJECTED


def test_a_reduce_only_exit_passes_even_over_the_cap(main) -> None:
    """Risk-reducing exits stay possible whatever the cap says — here the cap
    is dropped below the position that already exists."""
    _submit(main, "SELL", 12)
    main.config_settings = replace(main.config_settings, max_position_qty=5)

    exit_ = _submit(main, "BUY", 12, reduce_only=True)
    assert exit_.status != OrderStatus.REJECTED


def test_an_unknowable_book_refuses_even_a_claimed_reduce_only_order(
    main, tmp_path: Path
) -> None:
    """The reduce-only flag is a claim, not a broker guarantee. If the ledger
    cannot verify the position, execution must not risk a reversal."""
    _submit(main, "SELL", 6)  # a real position, journalled
    reset_journal(Journal(path=tmp_path / "dead.db", enabled=False))
    try:
        entry = _submit(main, "SELL", 6)
        assert entry.status == OrderStatus.REJECTED
        assert "position_unknowable" in (entry.rejection_reason or "")

        exit_ = _submit(main, "BUY", 6, reduce_only=True)
        assert exit_.status == OrderStatus.REJECTED
        assert "position_unknowable" in (exit_.rejection_reason or "")
    finally:
        reset_journal(None)


def test_the_refusal_is_a_recorded_decision(main) -> None:
    """A cap refusal is a decision the system made; it must be visible in the
    order record, not only absent from the fills."""
    _submit(main, "SELL", 12)
    refused = _submit(main, "SELL", 12)
    assert refused.status == OrderStatus.REJECTED

    stored = main.get_order(refused.order_id)
    assert "position_cap" in (stored.rejection_reason or "")


def test_the_service_trades_and_reports_from_one_paper_book(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The router used to build a second PaperBroker over the same state
    file: fills landed on its in-memory book while /v1/positions and the
    reconciler's broker-side view answered from the module's own instance,
    loaded once at startup. The first orchestrator drill placed a fill the
    position endpoint could not see."""
    import execution_service.main as module

    assert isinstance(module.broker, PaperBroker)
    assert module.router._simulated is module.broker


def test_a_close_returns_the_ledger_to_flat(main, monkeypatch: pytest.MonkeyPatch) -> None:
    """/v1/orders/close is the exit path the stop-loss and take-profit
    monitors and the orchestrator's exit pass use — and it used to close at
    the broker without journalling the fill, so the position ledger recorded
    entries only. After the first stop fired, net_position stayed at the
    entry forever: the worker's already-positioned gate wedged, the cap
    drifted toward permanent refusal, and an opposite 'entry' passed both
    gates against a phantom position."""
    from contracts import ClosePositionRequest
    from journal import get_journal

    entry = _submit(main, "SELL", 6)
    assert entry.status != OrderStatus.REJECTED
    net = get_journal().net_position(
        strategy_id="ema_rsi_macd", symbol="NVDA", environment="paper"
    )
    assert net == -6.0

    # The service wrapper binds its own module-global broker; point it at the
    # fixture's book so the close operates on the position just opened.
    import execution_service.broker as broker_module

    monkeypatch.setattr(broker_module, "broker", main.broker)
    result = main.close_order(
        ClosePositionRequest(
            symbol="NVDA",
            position_id="NVDA",
            signal_id="stop-loss-drill",
            strategy_id="ema_rsi_macd",
        )
    )
    assert result["status"] == "closed"
    net_after = get_journal().net_position(
        strategy_id="ema_rsi_macd", symbol="NVDA", environment="paper"
    )
    assert net_after == 0.0, "the exit fill must land in the same ledger the entry did"
