"""Broker adapters for Trade Pilot."""

from __future__ import annotations

import os

from .alpaca_broker import AlpacaBroker
from .base import BaseBroker, BrokerResult
from .etoro_broker import EtoroBroker
from .paper_broker import PaperBroker

__all__ = ["AlpacaBroker", "BaseBroker", "BrokerResult", "EtoroBroker", "PaperBroker", "get_broker"]


def get_broker(max_qty: int = 1000) -> BaseBroker:
    """Return the configured broker adapter with a paper fallback."""
    broker_name = os.getenv("BROKER", "").strip().lower()
    if broker_name == "etoro":
        api_key = os.getenv("ETORO_API_KEY", "")
        user_key = os.getenv("ETORO_USER_KEY", "")
        if api_key and user_key:
            demo = os.getenv("ETORO_DEMO", "true").lower() == "true"
            return EtoroBroker(api_key=api_key, user_key=user_key, demo=demo)

    api_key = os.getenv("ALPACA_API_KEY", "")
    secret_key = os.getenv("ALPACA_SECRET_KEY", "")
    if broker_name in {"", "alpaca"} and api_key and secret_key:
        paper = os.getenv("ALPACA_PAPER", "true").lower() == "true"
        return AlpacaBroker(api_key=api_key, secret_key=secret_key, paper=paper)
    return PaperBroker(max_qty=max_qty)
