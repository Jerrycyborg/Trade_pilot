import os
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
sys.path.insert(0, str(ROOT / "services" / "research-service" / "src"))


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
def _service_auth_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the suite the service credentials the endpoints now require.

    Auth fails closed on an unset INTERNAL_API_KEY (503), which is correct for
    a deployment and wrong for a test run: CI supplies these in ci.yml, so a
    clean checkout failed 8 tests that CI showed green. Set here, the suite
    needs no secrets to run. Real values in the environment win, and the tests
    that assert the fail-closed behaviour delete these themselves.
    """
    for name, value in (
        ("INTERNAL_API_KEY", "test-internal-key"),
        ("ADMIN_API_KEY", "test-admin-key"),
    ):
        if not os.environ.get(name):
            monkeypatch.setenv(name, value)


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


def deterministic_daily_bars(symbol: str = "AAPL", n: int = 60, split: int = 45) -> list:
    """A bar series the rule engine reads as a clean BUY.

    Shaped against the real indicators rather than guessed: EMA20 > EMA50,
    RSI ~68 (inside the 45-70 band), MACD histogram positive, ADX ~28 (clear
    of the regime gate's 20 floor), 60 bars so ADX is computable at all.
    """
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace

    now = datetime.now(timezone.utc)
    closes: list[float] = []
    price = 150.0
    for i in range(n):
        up, down = (1.0, 0.7) if i < split else (2.2, 0.8)
        price += up if i % 2 == 0 else -down
        closes.append(round(price, 2))
    return [
        SimpleNamespace(
            symbol=symbol.upper(),
            timestamp=now - timedelta(days=n - i),
            open=close - 0.3,
            high=close + 0.7,
            low=close - 1.0,
            close=close,
            volume=40_000_000.0,
        )
        for i, close in enumerate(closes)
    ]


@pytest.fixture
def stub_bars(monkeypatch: pytest.MonkeyPatch) -> list:
    """Deterministic bars for tests that need a signal to actually be produced.

    `_offline_market_data` above stubs *prices*, but bar fetches went to the
    live provider — so any test asserting on a generated signal was asserting
    on the real market. Two ways that bites: in an egress-restricted
    environment (the one FileDropFetcher exists for) the fetch fails and the
    signal is correctly HOLD, and in CI it passes only for as long as the real
    symbol keeps printing the indicator shape the test wants. Neither is a
    property of this codebase. These bars make the assertion about the rule.
    """
    bars = deterministic_daily_bars()
    for target in (
        "strategy_service.main.fetch_bars",
        "strategy_service.worker.fetch_bars",
    ):
        monkeypatch.setattr(target, lambda _symbol, _settings=None, **_kw: bars)
    return bars
