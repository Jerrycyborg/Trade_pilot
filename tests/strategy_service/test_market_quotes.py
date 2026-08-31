"""The ticker's quote endpoint: live prices, degrading per symbol.

Found by putting the dashboard in front of the first live paper run: the
ticker showed "prices unavailable" over a feed that was serving quotes,
because the endpoint read only archived bars (a session-old "live" price)
and let any non-DataUnavailableError from one symbol 500 the whole request.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from market_data.fetcher import DataUnavailableError
from market_data.models import PriceSnapshot
from strategy_service.main import get_quotes


def _bar(close: float, days_ago: int):
    return SimpleNamespace(
        timestamp=datetime.now(timezone.utc) - timedelta(days=days_ago),
        open=close, high=close, low=close, close=close, volume=1000.0,
    )


class StubFetcher:
    """One symbol with a live quote, one with bars only, one empty, one on fire."""

    def __init__(self) -> None:
        self.bars = {
            "NVDA": [_bar(218.0, 2), _bar(219.5, 1)],
            "MSFT": [_bar(505.0, 2), _bar(513.5, 1)],
        }
        self.quotes = {
            "NVDA": PriceSnapshot(
                symbol="NVDA", price=220.74,
                timestamp=datetime.now(timezone.utc), source="file_quote",
            )
        }

    def latest_price(self, symbol: str):
        if symbol == "BROKEN":
            raise RuntimeError("provider on fire")
        return self.quotes.get(symbol)

    def fetch(self, symbol: str, period_days: int = 5):
        if symbol == "BROKEN":
            raise RuntimeError("provider on fire")
        bars = self.bars.get(symbol)
        if not bars:
            raise DataUnavailableError(f"no bars for {symbol}")
        return bars


@pytest.fixture
def quotes(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("strategy_service.main.get_fetcher", lambda _s: StubFetcher())
    return lambda symbols: {row["symbol"]: row for row in get_quotes(symbols)}


def test_the_live_quote_beats_the_archived_close(quotes) -> None:
    """A ticker that reads only bars shows Friday's close all Monday and
    calls it live."""
    row = quotes("NVDA")["NVDA"]
    assert row["price"] == 220.74
    assert row["change_pct"] == round((220.74 - 219.5) / 219.5 * 100, 2)
    assert row["direction"] == "up"


def test_bars_alone_still_price_the_ticker(quotes) -> None:
    row = quotes("MSFT")["MSFT"]
    assert row["price"] == 513.5
    assert row["change_pct"] == round((513.5 - 505.0) / 505.0 * 100, 2)


def test_an_unknown_symbol_is_a_placeholder_row(quotes) -> None:
    row = quotes("GOOGL")["GOOGL"]
    assert row["price"] is None
    assert row["direction"] == "neutral"


def test_one_broken_symbol_does_not_blank_the_ticker(quotes) -> None:
    """Only DataUnavailableError used to be caught; a RuntimeError from one
    provider call 500'd the whole request and the dashboard blamed a missing
    API key."""
    rows = quotes("NVDA,BROKEN,MSFT")
    assert rows["NVDA"]["price"] == 220.74
    assert rows["BROKEN"]["price"] is None
    assert rows["MSFT"]["price"] == 513.5


def test_the_single_quote_endpoint_prefers_the_live_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The orchestrator prices its marketable limits from this endpoint's
    `price`. As a session-old close, every entry on a gap-up day cancelled
    limit_not_marketable — the drill went four for four."""
    from strategy_service.main import get_quote

    stub = StubFetcher()
    stub.bars["NVDA"] = [_bar(216.0 + i * 0.5, 30 - i) for i in range(30)]
    monkeypatch.setattr("strategy_service.main.get_fetcher", lambda _s: stub)
    row = get_quote("NVDA")
    assert row["price"] == 220.74, "the live quote, not the archived close"
    assert row["rsi"] is not None, "indicators still come from the archived bars"
