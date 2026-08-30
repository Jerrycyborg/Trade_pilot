from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from contracts import CandidateAction


def _ta_snapshot(*, price: float, rsi: float):
    return SimpleNamespace(
        current_price=price,
        trend_direction="neutral",
        signal_tags=["test"],
        data_source="test",
        as_of=datetime.now(timezone.utc),
        indicators=SimpleNamespace(
            rsi_14=rsi,
            macd_histogram=0.0,
            bb_position=0.5,
        ),
    )


def test_build_exit_signal_for_long_stop_loss(monkeypatch, stub_prices) -> None:
    import strategy_service.worker as worker_mod

    worker_mod.settings = worker_mod.settings.__class__(
        database_url=worker_mod.settings.database_url,
        stop_loss_pct=0.03,
        take_profit_pct=0.06,
        max_hold_hours=48,
    )
    worker = worker_mod.TradeWorker()
    # The exit check reads the live price first, so drive the scenario there.
    stub_prices.set("AAPL", 96.0)
    monkeypatch.setattr(
        worker, "_get_ta_snapshot", lambda symbol: _ta_snapshot(price=96.0, rsi=50.0)
    )

    signal = worker._build_exit_signal(
        {
            "symbol": "AAPL",
            "net_qty": 10,
            "average_cost": 100.0,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    )

    assert signal is not None
    assert signal.candidate_action == CandidateAction.EXIT
    assert signal.symbol == "AAPL"
    assert "stop_loss_hit" in (signal.research_summary or "")


def test_build_exit_signal_for_short_reversal_and_max_hold(monkeypatch) -> None:
    import strategy_service.worker as worker_mod

    worker_mod.settings = worker_mod.settings.__class__(
        database_url=worker_mod.settings.database_url,
        stop_loss_pct=0.03,
        take_profit_pct=0.06,
        max_hold_hours=48,
    )
    worker = worker_mod.TradeWorker()
    monkeypatch.setattr(worker, "_get_ta_snapshot", lambda symbol: _ta_snapshot(price=100.0, rsi=25.0))

    signal = worker._build_exit_signal(
        {
            "symbol": "TSLA",
            "net_qty": -5,
            "average_cost": 100.0,
            "updated_at": (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat(),
        }
    )

    assert signal is not None
    assert signal.candidate_action == CandidateAction.EXIT
    assert "signal_reversal" in (signal.research_summary or "")
    assert "max_hold_time" in (signal.research_summary or "")
