# ADR-003: Broker Abstraction Layer

## Status: Accepted

## Date: 2026-03-12

## Context

Milestone 1 hardcoded `PaperBroker` inside execution-service. Milestone 2 requires Alpaca Markets integration while preserving the zero-config paper fallback and all existing tests.

## Decision

Introduce `libs/brokers` as a shared library with:
- `BrokerResult` dataclass (adds `fill_price: Optional[float]` over M1)
- `PaperBroker` — deterministic, no external dependencies
- `AlpacaBroker` — uses `alpaca.trading.TradingClient`
- `get_broker(max_qty)` factory — returns `AlpacaBroker` when `ALPACA_API_KEY` is set, else `PaperBroker`

`execution-service/broker.py` becomes a thin shim that calls `get_broker()`.

## Consequences

- Zero-config operation preserved — no env vars needed for development/testing
- `ALPACA_PAPER=true` (default) means setting an API key never triggers live trades accidentally
- `fill_price` from broker is used for real portfolio cost basis; PaperBroker returns `100.0` as before
- `GET /v1/account` added to execution-service — reads `broker.get_account()`; PaperBroker returns simulated $100k
