"""Paper broker: a real fill simulator backed by live market prices.

The previous implementation filled every order at a flat $100 and reported no
positions, which made paper mode untestable — nothing could be marked to market
and no P&L existed. This version fills at the current market price plus a
configurable slippage, maintains a persisted position ledger with cash
accounting, and supports real closes, so a paper run produces numbers that mean
something.

State lives in a JSON file (``PAPER_STATE_PATH``) so positions survive a service
restart, which the monitors depend on.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from uuid import uuid4

from contracts import AccountInfo, BrokerPosition, ExecutionOrderRequest, OrderStatus
from contracts.execution import limit_fill_price

from .base import BrokerResult

logger = logging.getLogger(__name__)

_DEFAULT_MAX_QTY = 1000
_SUPPORTED_SIDES = frozenset({"BUY", "SELL"})


@dataclass
class PaperPosition:
    symbol: str
    qty: float = 0.0
    average_price: float = 0.0
    opened_at: Optional[str] = None

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "qty": self.qty,
            "average_price": self.average_price,
            "opened_at": self.opened_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "PaperPosition":
        return cls(
            symbol=str(payload.get("symbol", "")).upper(),
            qty=float(payload.get("qty", 0.0)),
            average_price=float(payload.get("average_price", 0.0)),
            opened_at=payload.get("opened_at"),  # type: ignore[arg-type]
        )


@dataclass
class PaperState:
    cash: float
    starting_cash: float
    realized_pnl: float = 0.0
    positions: dict[str, PaperPosition] = field(default_factory=dict)
    orders: list[dict[str, object]] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "cash": self.cash,
            "starting_cash": self.starting_cash,
            "realized_pnl": self.realized_pnl,
            "positions": {s: p.to_dict() for s, p in self.positions.items()},
            "orders": self.orders[-500:],
        }


class PaperBroker:
    """Fill simulator with mark-to-market positions and persisted cash accounting."""

    def __init__(
        self,
        max_qty: int = _DEFAULT_MAX_QTY,
        starting_cash: float | None = None,
        slippage_bps: float | None = None,
        state_path: str | Path | None = None,
        price_source: object | None = None,
    ) -> None:
        self._max_qty = max_qty
        self._starting_cash = (
            starting_cash
            if starting_cash is not None
            else float(os.getenv("PAPER_STARTING_CASH", "100000"))
        )
        self._slippage_bps = (
            slippage_bps
            if slippage_bps is not None
            else float(os.getenv("PAPER_SLIPPAGE_BPS", "2"))
        )
        self._state_path = Path(
            state_path or os.getenv("PAPER_STATE_PATH", "./paper-broker-state.json")
        )
        self._price_source = price_source
        self._lock = threading.Lock()
        self._state = self._load_state()

    # ------------------------------------------------------------------
    # Pricing
    # ------------------------------------------------------------------
    def _resolve_price_source(self):
        if self._price_source is None:
            try:
                from market_data import MarketDataSettings, RealtimePriceSource

                self._price_source = RealtimePriceSource(MarketDataSettings())
            except Exception as exc:
                logger.warning("PaperBroker: no market data available (%s)", exc)
                return None
        return self._price_source

    def mark_price(self, symbol: str) -> float | None:
        """Current market price for a symbol, or None if unavailable.

        Fills prefer the source's fill-grade read (`get_fresh_price`) when it
        offers one: the ordinary get_price serves cache entries up to the
        timeframe-scaled age limit — a day, on daily cadence — and the first
        orchestrator drill filled a stop-loss exit from a two-minute-old
        cached price while the stop had fired on the live one. A source
        without the method (test stubs, fixed books) is read as before.
        """
        source = self._resolve_price_source()
        if source is None:
            return None
        try:
            fresh = getattr(source, "get_fresh_price", None)
            if callable(fresh):
                return fresh(symbol)
            return source.get_price(symbol)
        except Exception as exc:
            logger.warning("PaperBroker: price lookup failed for %s: %s", symbol, exc)
            return None

    def _fill_price(self, symbol: str, side: str) -> float | None:
        """Fill price including slippage, or None when the market cannot be priced.

        There is no placeholder. Booking a fill at an invented price writes a
        fictitious cash balance, exposure and P&L into the ledger — precisely
        what this simulator exists to stop.
        """
        price = self.mark_price(symbol)
        if price is None or price <= 0:
            logger.warning("PaperBroker: no market price for %s — refusing to fill", symbol)
            return None
        # Slippage always works against the trader.
        drift = price * (self._slippage_bps / 10_000.0)
        return round(price + drift if side == "BUY" else price - drift, 6)

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------
    def submit(self, request: ExecutionOrderRequest) -> BrokerResult:
        return self.place_order(request)

    def place_order(
        self,
        request: ExecutionOrderRequest,
        stop_loss_rate: float | None = None,
        take_profit_rate: float | None = None,
    ) -> BrokerResult:
        symbol = request.symbol.upper()
        if symbol == "REJECT":
            return self._reject("symbol_rejected")
        if request.qty > self._max_qty:
            return self._reject("qty_limit_exceeded")

        side = str(request.side).upper()
        # Anything that is not exactly BUY used to fall through to the SELL
        # branch, so a typo like "BUYY" opened a simulated short and reported an
        # accepted fill instead of rejecting a malformed order.
        if side not in _SUPPORTED_SIDES:
            return self._reject(f"unsupported_side: {request.side!r}")
        price = self._fill_price(symbol, side)
        if price is None:
            return self._reject("no_market_price")

        # A limit order can miss. Simulating that is the point: a market order
        # always fills, so a backtest or paper run using only market orders
        # cannot show the fill risk that limit pricing trades slippage for.
        if request.order_type.upper() == "LIMIT" and request.limit_price:
            filled = limit_fill_price(float(request.limit_price), price, side)
            if filled is None:
                logger.info(
                    "PaperBroker: %s %s limit %.4f not marketable against %.4f",
                    side, symbol, request.limit_price, price,
                )
                return BrokerResult(
                    status=OrderStatus.CANCELLED,
                    external_order_id=str(uuid4()),
                    fill_price=None,
                    rejection_reason="limit_not_marketable",
                )
            price = filled
        qty = float(request.qty)
        notional = price * qty

        with self._lock:
            if side == "BUY" and notional > self._state.cash:
                return self._reject(
                    f"insufficient_cash: need ${notional:,.2f}, have ${self._state.cash:,.2f}"
                )
            if side == "BUY":
                self._apply_buy(symbol, qty, price)
            else:
                self._apply_sell(symbol, qty, price)

            order_id = str(uuid4())
            self._state.orders.append(
                {
                    "order_id": order_id,
                    "signal_id": request.signal_id,
                    "symbol": symbol,
                    "side": side,
                    "qty": qty,
                    "fill_price": price,
                    "notional": round(notional, 2),
                    "submitted_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            self._save_state()

        logger.info("PaperBroker: %s %s x%.4f @ %.4f", side, symbol, qty, price)
        # ACCEPTED, not FILLED: the stack's contract is that the broker accepts an
        # order and the separate fill.recorded event carries the fill. The
        # dashboard and audit event names both key off that.
        return BrokerResult(
            status=OrderStatus.ACCEPTED,
            external_order_id=order_id,
            fill_price=price,
        )

    def _reject(self, reason: str) -> BrokerResult:
        return BrokerResult(
            status=OrderStatus.REJECTED,
            external_order_id=str(uuid4()),
            fill_price=None,
            rejection_reason=reason,
        )

    def _apply_buy(self, symbol: str, qty: float, price: float) -> None:
        position = self._state.positions.get(symbol)
        if position is None or position.qty == 0:
            self._state.positions[symbol] = PaperPosition(
                symbol=symbol,
                qty=qty,
                average_price=price,
                opened_at=datetime.now(timezone.utc).isoformat(),
            )
        elif position.qty > 0:
            total_cost = position.average_price * position.qty + price * qty
            position.qty += qty
            position.average_price = total_cost / position.qty
        else:
            # Buying back a short: realize P&L on the covered portion.
            covered = min(qty, abs(position.qty))
            self._state.realized_pnl += (position.average_price - price) * covered
            position.qty += qty
            if position.qty > 0:
                position.average_price = price
            elif position.qty == 0:
                self._state.positions.pop(symbol, None)
        self._state.cash -= price * qty

    def _apply_sell(self, symbol: str, qty: float, price: float) -> None:
        position = self._state.positions.get(symbol)
        if position is None or position.qty == 0:
            # Opening a short.
            self._state.positions[symbol] = PaperPosition(
                symbol=symbol,
                qty=-qty,
                average_price=price,
                opened_at=datetime.now(timezone.utc).isoformat(),
            )
        elif position.qty > 0:
            closed = min(qty, position.qty)
            self._state.realized_pnl += (price - position.average_price) * closed
            position.qty -= qty
            if position.qty == 0:
                self._state.positions.pop(symbol, None)
            elif position.qty < 0:
                position.average_price = price
        else:
            total = position.average_price * abs(position.qty) + price * qty
            position.qty -= qty
            position.average_price = total / abs(position.qty)
        self._state.cash += price * qty

    def cancel_order(self, order_id: str) -> bool:
        # Market orders fill immediately in simulation, so there is nothing to cancel.
        return False

    def _resolve_symbol(self, position_id: str, symbol: str | None) -> str | None:
        with self._lock:
            positions = set(self._state.positions)
            orders = list(self._state.orders)
        if symbol and symbol.upper() in positions:
            return symbol.upper()
        candidate = str(position_id).upper()
        if candidate in positions:
            return candidate
        for order in reversed(orders):
            if str(order.get("order_id", "")).upper() == candidate:
                held = str(order.get("symbol", "")).upper()
                if held in positions:
                    return held
        return symbol.upper() if symbol else None

    def close_position(
        self,
        position_id: str,
        instrument_id: int | str = 0,
        units: float | None = None,
        symbol: str | None = None,
    ) -> dict | bool:
        """Flatten (or partially reduce) a position.

        Returns the close fill's details (truthy) on success, False otherwise.

        Callers identify a position inconsistently: the monitors register the
        broker's order id, while a manual close passes the symbol. Prefer an
        explicit symbol, then try position_id as a symbol, then fall back to
        looking the order id up in our own ledger.
        """
        resolved = self._resolve_symbol(position_id, symbol)
        if resolved is None:
            logger.info(
                "PaperBroker: could not resolve a position for %s / %s", position_id, symbol
            )
            return False
        symbol = resolved
        with self._lock:
            position = self._state.positions.get(symbol)
            if position is None or position.qty == 0:
                logger.info("PaperBroker: no open position for %s to close", symbol)
                return False
            qty = abs(position.qty) if not units else min(abs(units), abs(position.qty))
            side = "SELL" if position.qty > 0 else "BUY"
            price = self._fill_price(symbol, side)
            if price is None:
                logger.error(
                    "PaperBroker: cannot price %s — close refused, position left open",
                    symbol,
                )
                return False
            if side == "SELL":
                self._apply_sell(symbol, qty, price)
            else:
                self._apply_buy(symbol, qty, price)
            close_record = {
                "order_id": str(uuid4()),
                "signal_id": f"close-{symbol}",
                "symbol": symbol,
                "side": side,
                "qty": qty,
                "fill_price": price,
                "notional": round(price * qty, 2),
                "submitted_at": datetime.now(timezone.utc).isoformat(),
                "close": True,
            }
            self._state.orders.append(close_record)
            self._save_state()
        logger.info("PaperBroker: closed %.4f %s @ %.4f", qty, symbol, price)
        # The fill details, not a bare True: a close is a fill like any other,
        # and a caller that cannot see its side/qty/price cannot journal it —
        # which left every stop-loss and take-profit exit invisible to the
        # position ledger the entry gates enforce against. Still truthy, so
        # callers that only test success are unchanged.
        return close_record

    # ------------------------------------------------------------------
    # Account / positions
    # ------------------------------------------------------------------
    def get_positions(self) -> list[BrokerPosition]:
        with self._lock:
            positions = list(self._state.positions.values())
        results: list[BrokerPosition] = []
        for position in positions:
            if position.qty == 0:
                continue
            mark = self.mark_price(position.symbol) or position.average_price
            market_value = mark * position.qty
            results.append(
                BrokerPosition(
                    symbol=position.symbol,
                    qty=position.qty,
                    market_value=round(market_value, 2),
                    average_price=round(position.average_price, 6),
                    unrealized_pnl=round((mark - position.average_price) * position.qty, 2),
                )
            )
        return results

    def get_account(self) -> AccountInfo:
        positions_value = sum(p.market_value for p in self.get_positions())
        with self._lock:
            cash = self._state.cash
        equity = cash + positions_value
        return AccountInfo(
            buying_power=round(max(cash, 0.0), 2),
            equity=round(equity, 2),
            cash=round(cash, 2),
            mode="paper",
        )

    def get_order_history(self) -> list[dict[str, object]]:
        with self._lock:
            return list(self._state.orders)

    def realized_pnl(self) -> float:
        with self._lock:
            return round(self._state.realized_pnl, 2)

    def reset(self) -> None:
        """Wipe the ledger back to starting cash. Used by tests and fresh runs."""
        with self._lock:
            self._state = PaperState(
                cash=self._starting_cash, starting_cash=self._starting_cash
            )
            self._save_state()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def _load_state(self) -> PaperState:
        if not self._state_path.exists():
            return PaperState(cash=self._starting_cash, starting_cash=self._starting_cash)
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
            return PaperState(
                cash=float(payload.get("cash", self._starting_cash)),
                starting_cash=float(payload.get("starting_cash", self._starting_cash)),
                realized_pnl=float(payload.get("realized_pnl", 0.0)),
                positions={
                    str(symbol).upper(): PaperPosition.from_dict(data)
                    for symbol, data in dict(payload.get("positions", {})).items()
                },
                orders=list(payload.get("orders", [])),
            )
        except Exception as exc:
            logger.error(
                "PaperBroker: could not read %s (%s) — starting from a clean ledger",
                self._state_path,
                exc,
            )
            return PaperState(cash=self._starting_cash, starting_cash=self._starting_cash)

    def _save_state(self) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            temp = self._state_path.with_suffix(".tmp")
            temp.write_text(json.dumps(self._state.to_dict(), indent=2), encoding="utf-8")
            temp.replace(self._state_path)
        except Exception as exc:
            logger.error("PaperBroker: failed to persist state: %s", exc)
