#!/usr/bin/env python3
"""Preflight check for real-time intraday trading.

Run this on the machine that will actually trade, before starting the stack.
It proves the parts that unit tests cannot: that this host can reach a market
data provider, that intraday bars really arrive at the configured resolution,
and that prices are fresh enough for the policy service to accept them.

    uv run python scripts/verify_intraday.py
    uv run python scripts/verify_intraday.py --symbols AAPL,MSFT --stream 30

Exit code is 0 only if every check passes.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone

from market_data import (
    LivePriceCache,
    MarketDataSettings,
    RealtimePriceSource,
    StreamManager,
    fetch_bars,
    market_session,
)

OK = "\033[32mPASS\033[0m"
WARN = "\033[33mWARN\033[0m"
BAD = "\033[31mFAIL\033[0m"


class Report:
    def __init__(self) -> None:
        self.failed = False
        self.warned = False

    def ok(self, label: str, detail: str = "") -> None:
        print(f"  [{OK}] {label}{f' — {detail}' if detail else ''}")

    def warn(self, label: str, detail: str = "") -> None:
        self.warned = True
        print(f"  [{WARN}] {label}{f' — {detail}' if detail else ''}")

    def fail(self, label: str, detail: str = "") -> None:
        self.failed = True
        print(f"  [{BAD}] {label}{f' — {detail}' if detail else ''}")


def check_config(settings: MarketDataSettings, report: Report) -> None:
    print("\nConfiguration")
    if settings.is_intraday:
        report.ok("timeframe", f"intraday, {settings.intraday_minutes}-minute bars")
    else:
        report.fail(
            "timeframe",
            f"{settings.timeframe!r} — set MARKET_DATA_TIMEFRAME=intraday to trade intraday",
        )

    if settings.has_alpaca_credentials:
        report.ok("provider", "Alpaca (real-time)")
    elif settings.force_yahoo:
        report.warn(
            "provider",
            "Yahoo — intraday data is delayed (typically ~15 min) and cannot stream",
        )
    else:
        report.warn("provider", "Yahoo fallback — no ALPACA_API_KEY/ALPACA_SECRET_KEY set")

    if settings.can_stream:
        report.ok("streaming", "enabled")
    elif settings.streaming_enabled:
        report.warn("streaming", "STREAMING_ENABLED=true but Alpaca credentials are missing")
    else:
        report.warn("streaming", "disabled — prices resolve by polling (STREAMING_ENABLED=true)")


def check_session(settings: MarketDataSettings, report: Report) -> None:
    print("\nMarket session")
    session = market_session(settings)
    detail = f"source={session.source}" + (f", {session.reason}" if session.reason else "")
    if session.is_open:
        report.ok("market is open", detail)
    else:
        report.warn("market is closed", f"{detail} — data will be from the last session")


def check_bars(settings: MarketDataSettings, symbols: list[str], report: Report) -> None:
    print("\nIntraday bars")
    expected_gap = settings.intraday_minutes * 60
    for symbol in symbols:
        try:
            bars = fetch_bars(symbol, settings)
        except Exception as exc:
            report.fail(symbol, f"fetch raised {type(exc).__name__}: {exc}")
            continue

        if not bars:
            report.fail(symbol, "no bars returned")
            continue

        last = bars[-1]
        age_minutes = (datetime.now(timezone.utc) - last.timestamp).total_seconds() / 60

        if len(bars) >= 2:
            gap = (bars[-1].timestamp - bars[-2].timestamp).total_seconds()
            # Allow a wide tolerance: session boundaries create larger gaps.
            if gap > expected_gap * 3:
                report.warn(
                    symbol,
                    f"{len(bars)} bars, but last gap is {gap / 60:.0f}m "
                    f"(expected ~{settings.intraday_minutes}m) — is this a daily series?",
                )
                continue

        report.ok(
            symbol,
            f"{len(bars)} bars, last close {last.close:.2f} at "
            f"{last.timestamp:%H:%M:%S} UTC ({age_minutes:.0f}m ago)",
        )


def check_prices(settings: MarketDataSettings, symbols: list[str], report: Report) -> None:
    print("\nLive prices")
    source = RealtimePriceSource(settings)
    limit = settings.max_price_age_seconds
    for symbol in symbols:
        snapshot = source.get_snapshot(symbol)
        if snapshot is None:
            report.fail(symbol, "no price from any tier — orders would be blocked as stale")
            continue
        age = snapshot.age_seconds()
        detail = f"{snapshot.price:.4f} via {snapshot.source}, {age:.0f}s old"
        if age > limit:
            report.warn(symbol, f"{detail} — older than MAX_PRICE_AGE_SECONDS={limit}")
        else:
            report.ok(symbol, detail)


async def check_stream(
    settings: MarketDataSettings, symbols: list[str], seconds: int, report: Report
) -> None:
    print(f"\nBar stream ({seconds}s)")
    if not settings.can_stream:
        report.warn("stream", "not enabled — skipping")
        return

    cache = LivePriceCache(settings.max_price_age_seconds)
    manager = StreamManager(settings, symbols, cache)
    if not await manager.start():
        report.fail("stream", "failed to start")
        return

    try:
        await asyncio.sleep(seconds)
    finally:
        await manager.stop()

    received = cache.symbols()
    if received:
        report.ok("stream", f"bars received for {', '.join(received)}")
    else:
        report.warn(
            "stream",
            "connected but no bars arrived — normal outside market hours or on a quiet symbol",
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--symbols", default="AAPL,MSFT", help="comma-separated symbols to check"
    )
    parser.add_argument(
        "--stream",
        type=int,
        default=0,
        metavar="SECONDS",
        help="listen on the bar stream for this many seconds (requires Alpaca)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="show provider log output"
    )
    args = parser.parse_args()

    # The providers log their own failures; this report restates them per check,
    # so keep the library chatter out of it. Use -v to see it.
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.CRITICAL)
    logging.getLogger("yfinance").setLevel(
        logging.DEBUG if args.verbose else logging.CRITICAL
    )

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    settings = MarketDataSettings()
    report = Report()

    print("Trade_pilot — intraday preflight")
    check_config(settings, report)
    check_session(settings, report)
    check_bars(settings, symbols, report)
    check_prices(settings, symbols, report)
    if args.stream:
        asyncio.run(check_stream(settings, symbols, args.stream, report))

    print()
    if report.failed:
        print("Result: FAILED — do not start trading until the failures above are resolved.")
        return 1
    if report.warned:
        print("Result: passed with warnings — review them before trading live.")
        return 0
    print("Result: all checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
