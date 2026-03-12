"""Broker adapters for Trade Pilot."""

from __future__ import annotations

import os

from .alpaca_broker import AlpacaBroker
from .base import BrokerResult
from .paper_broker import PaperBroker

__all__ = ["AlpacaBroker", "BrokerResult", "PaperBroker", "get_broker"]


def get_broker(max_qty: int = 1000) -> AlpacaBroker | PaperBroker:
    """Return AlpacaBroker if credentials are set, else PaperBroker."""
    api_key = os.getenv("ALPACA_API_KEY", "")
    secret_key = os.getenv("ALPACA_SECRET_KEY", "")
    if api_key and secret_key:
        paper = os.getenv("ALPACA_PAPER", "true").lower() == "true"
        return AlpacaBroker(api_key=api_key, secret_key=secret_key, paper=paper)
    return PaperBroker(max_qty=max_qty)
