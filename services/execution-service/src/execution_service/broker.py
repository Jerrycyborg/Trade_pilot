"""Broker selection for execution-service.

Re-exports from libs/brokers. Chooses AlpacaBroker when ALPACA_API_KEY is set,
PaperBroker otherwise — zero-config fallback is always available.
"""

from __future__ import annotations

from brokers import BrokerResult, PaperBroker, get_broker  # noqa: F401

from .config import settings

broker = get_broker(max_qty=settings.max_qty)


def resolve_instrument_id(symbol: str) -> int:
    resolver = getattr(broker, "resolve_instrument_id", None) or getattr(
        broker, "_resolve_instrument_id", None
    )
    if resolver is None:
        raise NotImplementedError("broker_does_not_support_instrument_resolution")
    return int(resolver(symbol))


def close_position(position_id: str, symbol: str, units: float | None = None) -> bool:
    """Close a position at the configured broker.

    Only eToro addresses positions by instrument id. Resolving one
    unconditionally made every paper close raise
    broker_does_not_support_instrument_resolution before it reached the broker,
    so a paper stop-loss could never actually exit a position. Brokers without a
    resolver get the symbol instead.
    """
    closer = getattr(broker, "close_position", None)
    if closer is None:
        raise NotImplementedError("broker_does_not_support_position_close")

    instrument_id = 0
    if _broker_resolves_instruments():
        instrument_id = resolve_instrument_id(symbol)

    return bool(
        closer(
            position_id=position_id,
            instrument_id=instrument_id,
            units=units,
            symbol=symbol,
        )
    )


def _broker_resolves_instruments() -> bool:
    return any(
        getattr(broker, name, None) is not None
        for name in ("resolve_instrument_id", "_resolve_instrument_id")
    )
