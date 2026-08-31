import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "libs" / "contracts" / "src"))
sys.path.insert(0, str(ROOT / "libs" / "lifecycle" / "src"))
sys.path.insert(0, str(ROOT / "libs" / "attribution" / "src"))
sys.path.insert(0, str(ROOT / "libs" / "brokers" / "src"))
sys.path.insert(0, str(ROOT / "libs" / "market_data" / "src"))
sys.path.insert(0, str(ROOT / "services" / "audit-logger" / "src"))
sys.path.insert(0, str(ROOT / "services" / "autonomy-orchestrator" / "src"))
sys.path.insert(0, str(ROOT / "services" / "approval-gateway" / "src"))
sys.path.insert(0, str(ROOT / "services" / "sentiment-aggregator" / "src"))
sys.path.insert(0, str(ROOT / "services" / "notification-service" / "src"))
sys.path.insert(0, str(ROOT / "services" / "strategy-service" / "src"))
sys.path.insert(0, str(ROOT / "services" / "policy-service" / "src"))
sys.path.insert(0, str(ROOT / "services" / "execution-service" / "src"))
sys.path.insert(0, str(ROOT / "services" / "portfolio-service" / "src"))


# ---------------------------------------------------------------------------
# Deterministic, offline market data for the whole suite.
#
# PaperBroker now prices fills from live market data. Without this fixture the
# unit tests would reach out to Yahoo/Alpaca — slow, flaky, and dependent on
# whether the market happens to be open. Every test gets a fixed price book
# instead; tests that care about a specific price override it via the
# `stub_prices` fixture.
# ---------------------------------------------------------------------------

import pytest  # noqa: E402

DEFAULT_TEST_PRICE = 100.0


class StubPriceSource:
    """Stands in for RealtimePriceSource with a fixed, inspectable price book."""

    def __init__(self, prices: dict[str, float] | None = None) -> None:
        self.prices: dict[str, float] = {
            symbol.upper(): price for symbol, price in (prices or {}).items()
        }
        self.default_price: float | None = DEFAULT_TEST_PRICE

    def set(self, symbol: str, price: float) -> None:
        self.prices[symbol.upper()] = price

    def get_price(self, symbol: str) -> float | None:
        return self.prices.get(symbol.upper(), self.default_price)

    def get_snapshot(self, symbol: str):
        from datetime import datetime, timezone

        from market_data.models import PriceSnapshot

        price = self.get_price(symbol)
        if price is None:
            return None
        return PriceSnapshot(
            symbol=symbol.upper(),
            price=price,
            timestamp=datetime.now(timezone.utc),
            source="stub",
        )

    def age_seconds(self, symbol: str) -> float | None:
        return 0.0 if self.get_price(symbol) is not None else None


_active_prices: StubPriceSource | None = None


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "real_price_source: exercise the real RealtimePriceSource instead of the "
        "suite-wide stub (for tests of the price resolver itself)",
    )
    config.addinivalue_line(
        "markers",
        "real_earnings_calendar: exercise the real earnings-calendar lookup "
        "instead of the suite-wide stub (for tests of the gate itself, which "
        "patch yfinance directly)",
    )


@pytest.fixture(autouse=True)
def _offline_market_data(request, monkeypatch: pytest.MonkeyPatch, tmp_path) -> StubPriceSource:
    """Keep every test off the network and off the developer's paper ledger."""
    global _active_prices
    source = StubPriceSource()
    _active_prices = source
    monkeypatch.setenv("PAPER_STATE_PATH", str(tmp_path / "paper-broker-state.json"))
    monkeypatch.setenv("PAPER_SLIPPAGE_BPS", "0")
    # The risk monitors persist tracked stops/targets; keep test state out of
    # the developer's working directory, like the paper ledger above.
    monkeypatch.setenv("STOP_LOSS_STATE_PATH", str(tmp_path / "stop-loss-state.json"))
    monkeypatch.setenv("TAKE_PROFIT_STATE_PATH", str(tmp_path / "take-profit-state.json"))
    # Each test gets its own archive. Without this the suite would append to the
    # developer's real journal.db and pollute the research record with fixtures.
    monkeypatch.setenv("JOURNAL_PATH", str(tmp_path / "journal.db"))
    from journal import reset_journal

    reset_journal(None)
    # Each test gets its own strategy roster. Without this the suite would
    # write to the developer's real strategy-lifecycle.json — and, worse, read
    # it, so whether a test passed would depend on what was live locally.
    # No shared lifecycle authority unless a test supplies one. That is the
    # fail-closed default: a process without the roster attempts no entry.
    monkeypatch.delenv("LIFECYCLE_DATABASE_URL", raising=False)
    from lifecycle.service import reset_lifecycle_service

    reset_lifecycle_service(None)
    # The earnings gate consults yfinance and caches verdicts process-wide.
    # Every test starts with an empty cache and, unless it is a test of the
    # gate itself, a stubbed calendar — a real lookup would leave the suite's
    # outcome depending on the network and on whose earnings are this week.
    from strategy_service.earnings_calendar import BlackoutCheck, reset_earnings_gate

    reset_earnings_gate()
    if not request.node.get_closest_marker("real_earnings_calendar"):
        monkeypatch.setattr(
            "strategy_service.earnings_calendar._consult_calendar",
            lambda symbol, blackout_days, fail_closed: BlackoutCheck(
                active=False, checked=True, reason="suite-wide offline stub"
            ),
        )
    if request.node.get_closest_marker("real_price_source"):
        # Tests of the resolver itself inject their own fetcher, so they never
        # reach the network either.
        return source
    monkeypatch.setattr(
        "market_data.realtime.RealtimePriceSource.get_price",
        lambda _self, symbol: source.get_price(symbol),
    )
    monkeypatch.setattr(
        "market_data.realtime.RealtimePriceSource.get_snapshot",
        lambda _self, symbol: source.get_snapshot(symbol),
    )
    # The fill-grade read consults the provider ahead of the cache; offline it
    # answers from the same stub book as the other two.
    monkeypatch.setattr(
        "market_data.realtime.RealtimePriceSource.get_fresh_price",
        lambda _self, symbol: source.get_price(symbol),
    )
    return source


@pytest.fixture
def stub_prices(_offline_market_data: StubPriceSource) -> StubPriceSource:
    """The price book this test runs against. Mutate it to drive a scenario."""
    return _offline_market_data
