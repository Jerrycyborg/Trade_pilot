# ADR-004: Market Data Fallback Chain

## Status: Accepted

## Date: 2026-03-12

## Context

Real AI signal generation requires OHLCV price data and technical indicators. We need a solution that works without paid API keys and gracefully upgrades when credentials are provided.

## Decision

`libs/market_data` implements a two-tier fetcher chain:

1. **AlpacaFetcher** (primary) — uses Alpaca's `StockHistoricalDataClient` / `CryptoHistoricalDataClient`. Selected when `ALPACA_API_KEY` is set.
2. **YahooFinanceFetcher** (fallback) — uses `yfinance` with no API key required.

`get_fetcher(settings)` returns the appropriate fetcher. `DataUnavailableError` is raised when both fail.

Technical indicators (RSI, MACD, Bollinger Bands, EMA) are implemented as pure functions in `indicators.py` — no external TA library dependency, ensuring reproducible computation and testability with synthetic data.

## Consequences

- Zero-config market data using Yahoo Finance (rate-limited but free)
- Upgrading to paid Alpaca data requires only setting `ALPACA_API_KEY` — no code changes
- Crypto symbols detected by `/` in symbol name (e.g. `BTC/USD`) — routed to crypto client automatically
- Strategy-service exposes `/v1/market/quote/{symbol}`, `/v1/market/chart/{symbol}`, `/v1/market/quotes` for dashboard consumption
