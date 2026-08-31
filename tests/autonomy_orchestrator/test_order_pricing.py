"""Order pricing and sizing on the orchestrator's submission path.

Two things have to be true of every order the orchestrator sends: it carries a
marketable limit and its decision price (so the fill can be measured later),
and it is trimmed to a share of what the symbol actually trades.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
from autonomy_orchestrator import main as orchestrator
from contracts import CandidateAction, SignalCandidate


@dataclass
class Bar:
    volume: float


def _signal(action: CandidateAction = CandidateAction.BUY) -> SignalCandidate:
    return SignalCandidate(
        signal_id="sig-1",
        symbol="AAPL",
        ts=datetime.now(timezone.utc),
        candidate_action=action,
        confidence=0.8,
        size_pct=0.05,
        model_version="test",
    )


@pytest.fixture
def submitted(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Capture the payload the orchestrator posts to execution-service."""
    captured: dict[str, object] = {}

    async def fake_post(self, url: str, *, json: dict[str, object], headers: dict[str, str]):
        captured.update(json)

        class Response:
            def raise_for_status(self) -> None:
                return None

            @staticmethod
            def json() -> dict[str, object]:
                return {"status": "ACCEPTED", "order_id": "order-1"}

        return Response()

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    async def fake_quote(_symbol: str) -> float:
        return 200.0

    monkeypatch.setattr(orchestrator, "_get_quote_price", fake_quote)
    monkeypatch.setattr(orchestrator, "_bars_per_day", lambda: 1.0)
    return captured


async def _submit(
    *,
    bars: list[Bar] | None = None,
    action: CandidateAction = CandidateAction.BUY,
    buying_power: float = 100_000.0,
    size_pct: float = 0.05,
) -> dict[str, object]:
    return await orchestrator._submit_order(
        _signal(action),
        SimpleNamespace(adjusted_size_pct=size_pct, tier=1),
        {"stop_loss_pct": 0.03, "take_profit_pct": 0.06, "trading_mode": "demo"},
        {"buying_power": buying_power},
        bars,
    )


@pytest.mark.asyncio
async def test_a_buy_is_sent_as_a_marketable_limit(
    submitted: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("USE_LIMIT_ORDERS", "true")
    monkeypatch.setenv("LIMIT_TOLERANCE_BPS", "10")
    await _submit()

    assert submitted["order_type"] == "LIMIT"
    # IOC: fill now or not at all. Nothing is left working that needs managing.
    assert submitted["time_in_force"] == "IOC"
    assert submitted["limit_price"] == 200.2


@pytest.mark.asyncio
async def test_a_sell_limit_is_priced_on_the_other_side(
    submitted: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression guard: str(CandidateAction.SELL) is not 'SELL'."""
    monkeypatch.setenv("USE_LIMIT_ORDERS", "true")
    monkeypatch.setenv("LIMIT_TOLERANCE_BPS", "10")
    await _submit(action=CandidateAction.SELL)

    assert submitted["limit_price"] == 199.8


@pytest.mark.asyncio
async def test_every_order_carries_the_decision_price(
    submitted: dict[str, object]
) -> None:
    """Without it there is nothing to measure the fill against."""
    await _submit()
    assert submitted["decision_price"] == 200.0


@pytest.mark.asyncio
async def test_limit_orders_can_be_turned_off(
    submitted: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("USE_LIMIT_ORDERS", "false")
    await _submit()

    assert submitted["order_type"] == "MARKET"
    assert submitted["time_in_force"] == "DAY"
    assert submitted["limit_price"] is None


@pytest.mark.asyncio
async def test_a_wider_tolerance_prices_further_through_the_touch(
    submitted: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("USE_LIMIT_ORDERS", "true")
    monkeypatch.setenv("LIMIT_TOLERANCE_BPS", "50")
    await _submit()

    assert submitted["limit_price"] == 201.0


@pytest.mark.asyncio
async def test_order_is_trimmed_to_a_share_of_volume(
    submitted: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """$5,000 at $200 is 25 shares; 1% of a 1,000-share day is 10."""
    monkeypatch.setenv("MAX_ADV_PARTICIPATION", "0.01")
    await _submit(bars=[Bar(1_000.0)])

    assert submitted["qty"] == 10


@pytest.mark.asyncio
async def test_a_liquid_symbol_is_not_trimmed(
    submitted: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAX_ADV_PARTICIPATION", "0.01")
    await _submit(bars=[Bar(50_000_000.0)])

    assert submitted["qty"] == 25


@pytest.mark.asyncio
async def test_without_volume_data_the_size_is_left_alone(
    submitted: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ADV estimate is not evidence of a thin symbol."""
    monkeypatch.setenv("MAX_ADV_PARTICIPATION", "0.01")
    await _submit(bars=None)

    assert submitted["qty"] == 25


@pytest.mark.asyncio
async def test_a_symbol_too_thin_to_trade_is_refused_not_rounded_up(
    submitted: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """1% of 50 shares/day is nothing tradeable — skip rather than force a size."""
    monkeypatch.setenv("MAX_ADV_PARTICIPATION", "0.01")
    result = await _submit(bars=[Bar(50.0)])

    assert result["status"] == "REJECTED"
    assert result["rejection_reason"] == "qty_below_one_share"
    assert submitted == {}
