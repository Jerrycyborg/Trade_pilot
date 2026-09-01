"""eToro broker adapter."""

from __future__ import annotations

import logging
from uuid import uuid4

import httpx
from contracts import AccountInfo, BrokerPosition, ExecutionOrderRequest, OrderStatus

from .base import BrokerResult

logger = logging.getLogger(__name__)


class EtoroBroker:
    """Broker adapter using the public eToro API."""

    def __init__(self, api_key: str, user_key: str, demo: bool = True) -> None:
        self._api_key = api_key
        self._user_key = user_key
        self._demo = demo
        self._base_url = "https://public-api.etoro.com/api/v1"
        self._instrument_cache: dict[str, int] = {}
        self._client = httpx.Client(base_url=self._base_url, timeout=15.0)

    @property
    def is_live_trading(self) -> bool:
        return not self._demo

    def submit(self, request: ExecutionOrderRequest) -> BrokerResult:
        return self.place_order(
            request,
            stop_loss_rate=request.stop_loss_rate,
            take_profit_rate=request.take_profit_rate,
        )

    def place_order(
        self,
        request: ExecutionOrderRequest,
        stop_loss_rate: float | None = None,
        take_profit_rate: float | None = None,
    ) -> BrokerResult:
        try:
            instrument_id = self._resolve_instrument_id(request.symbol)
            payload = {
                "InstrumentID": instrument_id,
                "IsBuy": request.side.upper() == "BUY",
                "Leverage": 1,
                "Amount": float(request.qty),
            }
            if stop_loss_rate is None:
                payload["IsNoStopLoss"] = True
            else:
                payload["StopLossRate"] = float(stop_loss_rate)
            if take_profit_rate is None:
                payload["IsNoTakeProfit"] = True
            else:
                payload["TakeProfitRate"] = float(take_profit_rate)
            response = self._request(
                "POST",
                f"/trading/execution/{self._execution_prefix()}market-open-orders/by-amount",
                json=payload,
            )
            order_id = str(
                response.get("positionId")
                or response.get("orderId")
                or response.get("id")
                or uuid4()
            )
            status = OrderStatus.ACCEPTED if response else OrderStatus.REJECTED
            return BrokerResult(
                status=status,
                external_order_id=order_id,
                fill_price=_extract_fill_price(response),
                rejection_reason=None if status == OrderStatus.ACCEPTED else "etoro_order_rejected",
            )
        except Exception as exc:
            logger.error("EtoroBroker.place_order failed: %s", exc)
            return BrokerResult(
                status=OrderStatus.REJECTED,
                external_order_id=str(uuid4()),
                rejection_reason=f"etoro_error: {exc}",
            )

    def cancel_order(self, order_id: str) -> bool:
        try:
            self._request(
                "DELETE",
                f"/trading/execution/{self._execution_prefix()}market-open-orders/{order_id}",
            )
            return True
        except Exception as exc:
            logger.warning("EtoroBroker.cancel_order failed for %s: %s", order_id, exc)
            return False

    def get_positions(self) -> list[BrokerPosition]:
        try:
            payload = self._request("GET", f"/trading/info/{self._execution_prefix()}portfolio")
            positions = payload.get("positions") or payload.get("Positions") or []
            return [
                BrokerPosition(
                    symbol=str(item.get("internalSymbolFull") or item.get("symbol") or "").upper(),
                    qty=float(item.get("amount") or item.get("qty") or 0.0),
                    market_value=float(item.get("marketValue") or item.get("market_value") or 0.0),
                    average_price=float(
                        item.get("averageOpen") or item.get("average_price") or 0.0
                    ),
                    unrealized_pnl=float(
                        item.get("profitLoss") or item.get("unrealized_pnl") or 0.0
                    ),
                )
                for item in positions
            ]
        except Exception as exc:
            logger.error("EtoroBroker.get_positions failed: %s", exc)
            return []

    def get_account(self) -> AccountInfo:
        try:
            portfolio = self._request("GET", f"/trading/info/{self._execution_prefix()}portfolio")
            pnl = self._request("GET", f"/trading/info/{self._execution_prefix()}pnl")
            equity = float(
                portfolio.get("equity")
                or portfolio.get("Equity")
                or pnl.get("equity")
                or pnl.get("Equity")
                or 0.0
            )
            cash = float(portfolio.get("cash") or portfolio.get("Cash") or 0.0)
            buying_power = float(
                portfolio.get("availableFunds")
                or portfolio.get("AvailableFunds")
                or portfolio.get("buyingPower")
                or equity
            )
            return AccountInfo(
                buying_power=buying_power,
                equity=equity,
                cash=cash,
                mode="paper" if self._demo else "live",
            )
        except Exception as exc:
            logger.error("EtoroBroker.get_account failed: %s", exc)
            return AccountInfo(
                buying_power=0.0, equity=0.0, cash=0.0, mode="paper" if self._demo else "live"
            )

    def close_position(
        self,
        position_id: str,
        instrument_id: int,
        units: float | None = None,
        symbol: str | None = None,
    ) -> dict[str, object] | bool:
        """Close an open position by the broker's position id."""
        try:
            payload: dict[str, object] = {"InstrumentId": instrument_id}
            if units is not None:
                payload["UnitsToDeduct"] = units
            response = self._request(
                "POST",
                f"/trading/execution/{self._execution_prefix()}market-close-orders/positions/{position_id}",
                json=payload,
            )
            return {
                "order_id": str(response.get("orderId") or response.get("id") or uuid4()),
                "position_id": position_id,
                "symbol": (symbol or "").upper(),
                "qty": float(units or 0.0),
                "side": str(response.get("side") or "").upper(),
                "fill_price": _extract_fill_price(response),
            }
        except Exception as exc:
            logger.error("EtoroBroker.close_position failed for %s: %s", position_id, exc)
            return False

    def get_order_history(self) -> list[dict[str, object]]:
        try:
            payload = self._request(
                "GET",
                f"/trading/info/{self._execution_prefix()}trade/history",
                params={"minDate": "1970-01-01"},
            )
            trades = payload.get("trades") or payload.get("history") or payload.get("items") or []
            return [item for item in trades if isinstance(item, dict)]
        except Exception as exc:
            logger.error("EtoroBroker.get_order_history failed: %s", exc)
            return []

    def _resolve_instrument_id(self, symbol: str) -> int:
        normalized = symbol.upper()
        cached = self._instrument_cache.get(normalized)
        if cached is not None:
            return cached

        payload = self._request(
            "GET",
            "/market-data/search",
            params={
                "fields": "instrumentId,internalSymbolFull,displayname",
                "internalSymbolFull": normalized,
            },
        )
        items = (
            payload.get("items") or payload.get("results") or payload.get("instruments") or payload
        )
        if isinstance(items, list):
            for item in items:
                internal = str(item.get("internalSymbolFull") or "").upper()
                if internal == normalized:
                    instrument_id = int(item["instrumentId"])
                    self._instrument_cache[normalized] = instrument_id
                    return instrument_id
            if items:
                instrument_id = int(items[0]["instrumentId"])
                self._instrument_cache[normalized] = instrument_id
                return instrument_id
        raise ValueError(f"instrument_not_found:{normalized}")

    def resolve_instrument_id(self, symbol: str) -> int:
        return self._resolve_instrument_id(symbol)

    def _execution_prefix(self) -> str:
        return "demo/" if self._demo else ""

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._api_key,
            "x-user-key": self._user_key,
            "x-request-id": str(uuid4()),
        }

    def close(self) -> None:
        """Release pooled broker connections during an orderly shutdown."""
        self._client.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, object] | None = None,
        json: dict[str, object] | None = None,
    ) -> dict[str, object]:
        response = self._client.request(
            method,
            path,
            headers=self._headers(),
            params=params,
            json=json,
        )
        response.raise_for_status()
        if not response.content:
            return {}
        payload = response.json()
        return payload if isinstance(payload, dict) else {"items": payload}


def _extract_fill_price(payload: dict[str, object]) -> float | None:
    for key in ("fillPrice", "price", "executedPrice"):
        value = payload.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return None
