"""End-to-end test of the intraday trading loop.

Exercises the path the previous commits rewired, in one place and offline:

    intraday bars -> TA -> sizing at the real price -> policy (with an observed
    market context) -> execution -> paper broker ledger -> stop-loss on a live
    price.

The bar series is generated, not recorded: it is shaped like an intraday feed
(15-minute bars inside a regular session) so the resolution-dependent logic is
genuinely exercised, but no claim is made that it is real market history.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from brokers import PaperBroker
from contracts import ExecutionOrderRequest
from market_data.indicators import build_ta_summary
from market_data.models import OHLCVBar
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

SYMBOL = "AAPL"
BAR_MINUTES = 15


def _intraday_series(
    count: int = 120,
    start_price: float = 200.0,
    drift: float = 0.05,
) -> list[OHLCVBar]:
    """A rising 15-minute series ending at the most recent bar boundary."""
    end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    bars: list[OHLCVBar] = []
    price = start_price
    for index in range(count):
        stamp = end - timedelta(minutes=BAR_MINUTES * (count - index))
        open_price = price
        price = round(price + drift, 4)
        bars.append(
            OHLCVBar(
                symbol=SYMBOL,
                timestamp=stamp,
                open=open_price,
                high=max(open_price, price) + 0.10,
                low=min(open_price, price) - 0.10,
                close=price,
                volume=10_000.0 + index * 25,
            )
        )
    return bars


@pytest.fixture
def bars() -> list[OHLCVBar]:
    return _intraday_series()


@pytest.fixture
def broker(tmp_path: Path, stub_prices) -> PaperBroker:
    stub_prices.set(SYMBOL, 200.0)
    return PaperBroker(
        starting_cash=100_000.0,
        slippage_bps=0.0,
        state_path=tmp_path / "paper.json",
        price_source=stub_prices,
    )


def _execution_client(db_path: Path, broker: PaperBroker, monkeypatch: pytest.MonkeyPatch):
    import execution_service.config as config
    import execution_service.database as database
    import execution_service.main as main
    from fastapi.testclient import TestClient

    config.settings = config.ExecutionSettings(database_url=f"sqlite+pysqlite:///{db_path}")
    database.settings = config.settings
    database.connect_args = {"check_same_thread": False}
    database.engine = create_engine(
        config.settings.database_url, future=True, connect_args=database.connect_args
    )
    database.SessionLocal = sessionmaker(
        bind=database.engine, autoflush=False, autocommit=False, future=True
    )
    database.Base.metadata.create_all(bind=database.engine)
    main.engine = database.engine
    main.SessionLocal = database.SessionLocal
    # monkeypatch so the module-level broker is restored: other integration
    # tests reuse this module and would otherwise inherit our price stub.
    monkeypatch.setattr(main, "broker", broker)
    return TestClient(main.app)


class TestIntradayDataReachesTheStrategy:
    def test_worker_requests_intraday_bars_when_configured(
        self, monkeypatch: pytest.MonkeyPatch, bars: list[OHLCVBar]
    ) -> None:
        """The regression that motivated this work: the worker used to call
        fetch(period_days=60) and receive daily bars regardless of timeframe."""
        monkeypatch.setenv("MARKET_DATA_TIMEFRAME", "intraday")
        monkeypatch.setenv("INTRADAY_MINUTES", str(BAR_MINUTES))

        import strategy_service.worker as worker_mod

        captured: dict[str, object] = {}

        def _fetch_bars(symbol, settings):
            captured["symbol"] = symbol
            captured["timeframe"] = settings.timeframe
            captured["minutes"] = settings.intraday_minutes
            return bars

        monkeypatch.setattr(worker_mod, "fetch_bars", _fetch_bars)

        worker = worker_mod.TradeWorker()
        ta, returned = worker._get_market_snapshot(SYMBOL)

        assert captured == {
            "symbol": SYMBOL,
            "timeframe": "intraday",
            "minutes": BAR_MINUTES,
        }
        assert returned == bars
        assert ta.data_source == "intraday"

    def test_indicators_compute_over_the_intraday_series(
        self, bars: list[OHLCVBar]
    ) -> None:
        summary = build_ta_summary(SYMBOL, bars, data_source="intraday")
        assert summary.bars_count == len(bars)
        assert summary.current_price == bars[-1].close
        # A monotonically rising series must read as an uptrend, not neutral.
        assert summary.indicators.rsi_14 > 60


class TestSizingAndExecution:
    def test_order_is_sized_at_the_market_price_and_fills(
        self, tmp_path: Path, broker: PaperBroker, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from strategy_service.worker import _compute_qty

        qty = _compute_qty(size_pct=0.02, buying_power=100_000.0, reference_price=200.0)
        assert qty == 10  # $2,000 / $200 — not $2,000 / $100

        client = _execution_client(tmp_path / "exec.db", broker, monkeypatch)
        response = client.post(
            "/v1/orders",
            json=ExecutionOrderRequest(
                signal_id="sig-intraday-1",
                symbol=SYMBOL,
                side="BUY",
                qty=qty,
                order_type="MARKET",
            ).model_dump(mode="json"),
            headers={"Idempotency-Key": "intraday-1"},
        )

        assert response.status_code == 200
        assert response.json()["status"] == "ACCEPTED"

        positions = broker.get_positions()
        assert len(positions) == 1
        assert positions[0].qty == 10
        assert positions[0].average_price == 200.0
        assert broker.get_account().cash == 98_000.0

    def test_fill_is_persisted_for_the_order(
        self, tmp_path: Path, broker: PaperBroker, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fill must be recorded, or the portfolio can never reconcile."""
        client = _execution_client(tmp_path / "exec.db", broker, monkeypatch)
        order = client.post(
            "/v1/orders",
            json=ExecutionOrderRequest(
                signal_id="sig-intraday-2",
                symbol=SYMBOL,
                side="BUY",
                qty=5,
                order_type="MARKET",
            ).model_dump(mode="json"),
            headers={"Idempotency-Key": "intraday-2"},
        ).json()

        fills = client.get(f"/v1/orders/{order['order_id']}/fills").json()
        assert len(fills) == 1
        assert fills[0]["price"] == 200.0
        assert fills[0]["qty"] == 5

        events = client.get("/v1/execution/events?limit=50").json()
        assert any(event["event_type"] == "fill.recorded" for event in events)


class TestStopLossAtIntradayResolution:
    @pytest.mark.asyncio
    async def test_stop_fires_on_a_live_price_move(
        self, broker: PaperBroker, stub_prices
    ) -> None:
        """The monitor used to read a daily close, so an intraday stop could
        only ever fire once a day. It now reacts to the current price."""
        from autonomy_orchestrator.stop_loss_monitor import StopLossMonitor, StopLossRecord

        broker.place_order(
            ExecutionOrderRequest(
                signal_id="sig-stop",
                symbol=SYMBOL,
                side="BUY",
                qty=10,
                order_type="MARKET",
            )
        )

        monitor = StopLossMonitor(broker_url="http://localhost:8002", internal_key="k")
        monitor.register(
            StopLossRecord(
                symbol=SYMBOL,
                entry_price=200.0,
                stop_price=197.0,
                position_id=SYMBOL,
                qty=10,
                created_at=datetime.now(timezone.utc),
            )
        )

        # Still above the stop mid-session.
        stub_prices.set(SYMBOL, 198.5)
        assert await monitor.check_all(stub_prices) == []

        # Price breaks the stop within the same 15-minute bar.
        stub_prices.set(SYMBOL, 196.0)
        closed: list[str] = []

        async def _record_exit(record):
            closed.append(record.symbol)
            broker.close_position(record.symbol)

        monitor._trigger_exit = _record_exit  # type: ignore[method-assign]
        triggered = await monitor.check_all(stub_prices)

        assert triggered == [SYMBOL]
        assert closed == [SYMBOL]
        assert broker.get_positions() == []
        assert broker.realized_pnl() == -40.0  # 10 shares x $4

    @pytest.mark.asyncio
    async def test_stop_is_not_evaluated_without_a_price(self, stub_prices) -> None:
        """No price must mean 'not evaluated', never 'not triggered'."""
        from autonomy_orchestrator.stop_loss_monitor import StopLossMonitor, StopLossRecord

        monitor = StopLossMonitor(broker_url="http://localhost:8002", internal_key="k")
        monitor.register(
            StopLossRecord(
                symbol=SYMBOL,
                entry_price=200.0,
                stop_price=197.0,
                position_id=SYMBOL,
                created_at=datetime.now(timezone.utc),
            )
        )
        stub_prices.default_price = None

        assert await monitor.check_all(stub_prices) == []
        assert monitor.get(SYMBOL) is not None  # still tracked, not silently dropped
