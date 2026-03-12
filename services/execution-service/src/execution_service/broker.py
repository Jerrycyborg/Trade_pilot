"""Broker selection for execution-service.

Re-exports from libs/brokers. Chooses AlpacaBroker when ALPACA_API_KEY is set,
PaperBroker otherwise — zero-config fallback is always available.
"""

from __future__ import annotations

from brokers import BrokerResult, PaperBroker, get_broker  # noqa: F401

from .config import settings

broker = get_broker(max_qty=settings.max_qty)
