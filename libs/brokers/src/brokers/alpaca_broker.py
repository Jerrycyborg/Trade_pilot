"""Alpaca Markets broker adapter (paper and live)."""

from __future__ import annotations

import logging
import time
from uuid import uuid4

from contracts import AccountInfo, BrokerPosition, ExecutionOrderRequest, OrderStatus

from .base import BrokerResult

logger = logging.getLogger(__name__)

_FILL_POLL_INTERVAL = 0.5  # seconds between Alpaca order status polls
_FILL_POLL_TIMEOUT = 10.0  # max seconds to wait for a fill in paper mode


class AlpacaBroker:
    """Broker adapter using the Alpaca Markets trading API."""

    def __init__(self, api_key: str, secret_key: str, paper: bool = True) -> None:
        self._api_key = api_key
        self._secret_key = secret_key
        self._paper = paper
        self._client = self._build_client()

    def _build_client(self):
        from alpaca.trading.client import TradingClient

        return TradingClient(
            api_key=self._api_key,
            secret_key=self._secret_key,
            paper=self._paper,
        )

    def submit(self, request: ExecutionOrderRequest) -> BrokerResult:
        return self.place_order(request)

    def place_order(self, request: ExecutionOrderRequest) -> BrokerResult:
        try:
            return self._do_submit(request)
        except Exception as exc:
            logger.error("AlpacaBroker.submit failed: %s", exc)
            return BrokerResult(
                status=OrderStatus.REJECTED,
                external_order_id=str(uuid4()),
                fill_price=None,
                rejection_reason=f"alpaca_error: {exc}",
            )

    def _do_submit(self, request: ExecutionOrderRequest) -> BrokerResult:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        side = OrderSide.BUY if request.side.upper() == "BUY" else OrderSide.SELL
        tif_map = {"DAY": TimeInForce.DAY, "GTC": TimeInForce.GTC, "IOC": TimeInForce.IOC}
        tif = tif_map.get(request.time_in_force.upper(), TimeInForce.DAY)

        order_data = MarketOrderRequest(
            symbol=request.symbol,
            qty=request.qty,
            side=side,
            time_in_force=tif,
        )
        order = self._client.submit_order(order_data)
        external_order_id = str(order.id)

        # Poll for fill in paper mode (paper fills quickly)
        fill_price = self._wait_for_fill(external_order_id)
        return BrokerResult(
            status=OrderStatus.ACCEPTED,
            external_order_id=external_order_id,
            fill_price=fill_price,
        )

    def _wait_for_fill(self, order_id: str) -> float | None:
        """Poll Alpaca for fill price. Returns None if not filled within timeout."""
        deadline = time.monotonic() + _FILL_POLL_TIMEOUT
        while time.monotonic() < deadline:
            try:
                order = self._client.get_order_by_id(order_id)
                if order.filled_avg_price is not None:
                    return float(order.filled_avg_price)
            except Exception as exc:
                logger.warning("AlpacaBroker: poll error for order %s: %s", order_id, exc)
            time.sleep(_FILL_POLL_INTERVAL)
        logger.warning("AlpacaBroker: timed out waiting for fill on order %s", order_id)
        return None

    def get_account(self) -> AccountInfo:
        try:
            account = self._client.get_account()
            return AccountInfo(
                buying_power=float(account.buying_power),
                equity=float(account.equity),
                cash=float(account.cash),
                mode="paper" if self._paper else "live",
            )
        except Exception as exc:
            logger.error("AlpacaBroker.get_account failed: %s", exc)
            return AccountInfo(
                buying_power=0.0,
                equity=0.0,
                cash=0.0,
                mode="paper" if self._paper else "live",
            )

    def cancel_order(self, order_id: str) -> bool:
        try:
            self._client.cancel_order_by_id(order_id)
            return True
        except Exception as exc:
            logger.warning("AlpacaBroker.cancel_order failed: %s", exc)
            return False

    def get_positions(self) -> list[BrokerPosition]:
        try:
            return [
                BrokerPosition(
                    symbol=position.symbol,
                    qty=float(position.qty),
                    market_value=float(position.market_value),
                    average_price=float(position.avg_entry_price),
                    unrealized_pnl=float(position.unrealized_pl),
                )
                for position in self._client.get_all_positions()
            ]
        except Exception as exc:
            logger.warning("AlpacaBroker.get_positions failed: %s", exc)
            return []

    def get_order_history(self) -> list[dict[str, object]]:
        try:
            return [order.model_dump() for order in self._client.get_orders()]
        except Exception as exc:
            logger.warning("AlpacaBroker.get_order_history failed: %s", exc)
            return []
