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

import hashlib
import json
import logging
import math
import os
import threading
from copy import deepcopy
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
_DEFAULT_STRATEGY = "ema_rsi_macd"
_STATE_SCHEMA_VERSION = 3
_MAX_STATE_BYTES = 32 * 1024 * 1024
_MAX_ORDER_HISTORY = 500
_MAX_REPLAY_RECORDS = 25_000
_REPLAY_EXIT_RESERVE = 1_000


class PaperStateError(RuntimeError):
    """The simulated account cannot prove its durable state."""


class PaperStateCorruptError(PaperStateError):
    pass


class PaperStatePersistenceError(PaperStateError):
    pass


def _position_key(symbol: str, strategy_id: str, account_id: str) -> str:
    material = json.dumps(
        [account_id, strategy_id, symbol.upper()],
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _replay_key(
    kind: str,
    signal_id: str,
    strategy_id: str,
    account_id: str,
) -> str:
    material = json.dumps(
        [kind, account_id, strategy_id, signal_id],
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _payload_hash(payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass
class PaperPosition:
    symbol: str
    strategy_id: str = _DEFAULT_STRATEGY
    account_id: str = "default"
    qty: float = 0.0
    average_price: float = 0.0
    opened_at: Optional[str] = None
    position_id: str = ""
    order_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "strategy_id": self.strategy_id,
            "account_id": self.account_id,
            "qty": self.qty,
            "average_price": self.average_price,
            "opened_at": self.opened_at,
            "position_id": self.position_id,
            "order_ids": self.order_ids,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "PaperPosition":
        raw_order_ids = payload.get("order_ids", [])
        order_ids = (
            [str(value) for value in raw_order_ids if value]
            if isinstance(raw_order_ids, list)
            else []
        )
        return cls(
            symbol=str(payload.get("symbol", "")).upper(),
            strategy_id=str(payload.get("strategy_id") or _DEFAULT_STRATEGY),
            account_id=str(payload.get("account_id") or "default"),
            qty=float(payload.get("qty", 0.0)),
            average_price=float(payload.get("average_price", 0.0)),
            opened_at=payload.get("opened_at"),  # type: ignore[arg-type]
            position_id=str(payload.get("position_id") or ""),
            order_ids=order_ids,
        )


@dataclass
class PaperState:
    cash: float
    starting_cash: float
    realized_pnl: float = 0.0
    positions: dict[str, PaperPosition] = field(default_factory=dict)
    orders: list[dict[str, object]] = field(default_factory=list)
    replays: dict[str, dict[str, object]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": _STATE_SCHEMA_VERSION,
            "cash": self.cash,
            "starting_cash": self.starting_cash,
            "realized_pnl": self.realized_pnl,
            "positions": {s: p.to_dict() for s, p in self.positions.items()},
            "orders": self.orders[-_MAX_ORDER_HISTORY:],
            "replays": self.replays,
        }


class PaperBroker:
    """Fill simulator with mark-to-market positions and persisted cash accounting."""

    supports_sleeve_positions = True

    @property
    def is_live_trading(self) -> bool:
        return False

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

    def _entry_payload(
        self,
        request: ExecutionOrderRequest,
        symbol: str,
        side: str,
    ) -> dict[str, object]:
        return {
            "symbol": symbol,
            "side": side,
            "qty": float(request.qty),
            "order_type": request.order_type.upper(),
            "time_in_force": request.time_in_force,
            "limit_price": request.limit_price,
            "decision_price": request.decision_price,
            "stop_loss_rate": request.stop_loss_rate,
            "take_profit_rate": request.take_profit_rate,
            "reduce_only": bool(request.reduce_only),
        }

    def _entry_replay_unlocked(
        self,
        replay_id: str,
        payload_hash: str,
    ) -> BrokerResult | None:
        record = self._state.replays.get(replay_id)
        if record is None:
            return None
        if record.get("payload_hash") != payload_hash:
            return self._reject("signal_id_payload_mismatch")
        result = record.get("result")
        if not isinstance(result, dict):
            raise PaperStateCorruptError("paper replay result is invalid")
        return BrokerResult(
            status=OrderStatus(str(result["status"])),
            external_order_id=str(result["external_order_id"]),
            fill_price=float(result["fill_price"]),
            rejection_reason=(
                str(result["rejection_reason"])
                if result.get("rejection_reason") is not None
                else None
            ),
        )

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

        strategy_id = str(request.strategy_id)
        account_id = str(request.account_id)
        key = _position_key(symbol, strategy_id, account_id)
        replay_id = _replay_key("entry", request.signal_id, strategy_id, account_id)
        payload_hash = _payload_hash(self._entry_payload(request, symbol, side))
        replay_limit = (
            _MAX_REPLAY_RECORDS
            if request.reduce_only
            else _MAX_REPLAY_RECORDS - _REPLAY_EXIT_RESERVE
        )

        # Look up the durable replay before a market-data call. A retry must
        # return the original fill even if the quote source is now down or the
        # market has moved.
        with self._lock:
            replayed = self._entry_replay_unlocked(replay_id, payload_hash)
            if replayed is not None:
                return replayed
            if len(self._state.replays) >= replay_limit:
                return self._reject("paper_replay_ledger_full")

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
                    side,
                    symbol,
                    request.limit_price,
                    price,
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
            # A concurrent request may have committed while the quote was
            # fetched. Re-check under the mutation lock before changing cash.
            replayed = self._entry_replay_unlocked(replay_id, payload_hash)
            if replayed is not None:
                return replayed
            if len(self._state.replays) >= replay_limit:
                return self._reject("paper_replay_ledger_full")
            if side == "BUY" and notional > self._state.cash:
                return self._reject(
                    f"insufficient_cash: need ${notional:,.2f}, have ${self._state.cash:,.2f}"
                )
            before = deepcopy(self._state)
            order_id = str(uuid4())
            try:
                if side == "BUY":
                    self._apply_buy(
                        key,
                        symbol,
                        strategy_id,
                        account_id,
                        qty,
                        price,
                        order_id,
                    )
                else:
                    self._apply_sell(
                        key,
                        symbol,
                        strategy_id,
                        account_id,
                        qty,
                        price,
                        order_id,
                    )
                order_record: dict[str, object] = {
                    "order_id": order_id,
                    "signal_id": request.signal_id,
                    "symbol": symbol,
                    "strategy_id": strategy_id,
                    "account_id": account_id,
                    "side": side,
                    "qty": qty,
                    "order_type": request.order_type.upper(),
                    "time_in_force": request.time_in_force,
                    "limit_price": request.limit_price,
                    "decision_price": request.decision_price,
                    "stop_loss_rate": request.stop_loss_rate,
                    "take_profit_rate": request.take_profit_rate,
                    "reduce_only": bool(request.reduce_only),
                    "fill_price": price,
                    "notional": round(notional, 2),
                    "submitted_at": datetime.now(timezone.utc).isoformat(),
                }
                self._state.orders.append(order_record)
                self._state.orders = self._state.orders[-_MAX_ORDER_HISTORY:]
                self._state.replays[replay_id] = {
                    "payload_hash": payload_hash,
                    "result": {
                        "status": OrderStatus.ACCEPTED.value,
                        "external_order_id": order_id,
                        "fill_price": price,
                        "rejection_reason": None,
                    },
                }
                self._save_state()
            except PaperStatePersistenceError as exc:
                self._state = before
                logger.error(
                    "PaperBroker: order refused because state was not durable: %s",
                    exc,
                )
                return self._reject("paper_state_persistence_failed")

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

    def _apply_buy(
        self,
        key: str,
        symbol: str,
        strategy_id: str,
        account_id: str,
        qty: float,
        price: float,
        order_id: str,
    ) -> None:
        position = self._state.positions.get(key)
        if position is None or position.qty == 0:
            self._state.positions[key] = PaperPosition(
                symbol=symbol,
                strategy_id=strategy_id,
                account_id=account_id,
                qty=qty,
                average_price=price,
                opened_at=datetime.now(timezone.utc).isoformat(),
                position_id=order_id,
                order_ids=[order_id],
            )
        elif position.qty > 0:
            total_cost = position.average_price * position.qty + price * qty
            position.qty += qty
            position.average_price = total_cost / position.qty
            position.order_ids.append(order_id)
        else:
            # Buying back a short: realize P&L on the covered portion.
            covered = min(qty, abs(position.qty))
            self._state.realized_pnl += (position.average_price - price) * covered
            position.qty += qty
            if position.qty > 0:
                position.average_price = price
                position.opened_at = datetime.now(timezone.utc).isoformat()
                position.position_id = order_id
                position.order_ids = [order_id]
            elif position.qty == 0:
                self._state.positions.pop(key, None)
        self._state.cash -= price * qty

    def _apply_sell(
        self,
        key: str,
        symbol: str,
        strategy_id: str,
        account_id: str,
        qty: float,
        price: float,
        order_id: str,
    ) -> None:
        position = self._state.positions.get(key)
        if position is None or position.qty == 0:
            # Opening a short.
            self._state.positions[key] = PaperPosition(
                symbol=symbol,
                strategy_id=strategy_id,
                account_id=account_id,
                qty=-qty,
                average_price=price,
                opened_at=datetime.now(timezone.utc).isoformat(),
                position_id=order_id,
                order_ids=[order_id],
            )
        elif position.qty > 0:
            closed = min(qty, position.qty)
            self._state.realized_pnl += (price - position.average_price) * closed
            position.qty -= qty
            if position.qty == 0:
                self._state.positions.pop(key, None)
            elif position.qty < 0:
                position.average_price = price
                position.opened_at = datetime.now(timezone.utc).isoformat()
                position.position_id = order_id
                position.order_ids = [order_id]
        else:
            total = position.average_price * abs(position.qty) + price * qty
            position.qty -= qty
            position.average_price = total / abs(position.qty)
            position.order_ids.append(order_id)
        self._state.cash += price * qty

    def cancel_order(self, order_id: str) -> bool:
        # Market orders fill immediately in simulation, so there is nothing to cancel.
        return False

    @staticmethod
    def _position_identifier_matches(
        position_id: str,
        position: PaperPosition,
    ) -> bool:
        supplied = str(position_id)
        return (
            (bool(position.position_id) and supplied.upper() == position.position_id.upper())
            or supplied.upper() in {order_id.upper() for order_id in position.order_ids}
            or supplied.upper() == position.symbol.upper()
        )

    def _resolve_position_key_unlocked(
        self,
        position_id: str,
        symbol: str | None,
        strategy_id: str | None,
        account_id: str,
    ) -> str | None:
        if symbol and strategy_id:
            candidate = _position_key(symbol, strategy_id, account_id)
            position = self._state.positions.get(candidate)
            if position is not None and self._position_identifier_matches(position_id, position):
                return candidate
            return None

        matches = [
            (key, position)
            for key, position in self._state.positions.items()
            if position.account_id == account_id
            and (symbol is None or position.symbol == symbol.upper())
            and (strategy_id is None or position.strategy_id == strategy_id)
            and self._position_identifier_matches(position_id, position)
        ]
        return matches[0][0] if len(matches) == 1 else None

    def _resolve_position_key(
        self,
        position_id: str,
        symbol: str | None,
        strategy_id: str | None,
        account_id: str,
    ) -> str | None:
        with self._lock:
            return self._resolve_position_key_unlocked(
                position_id,
                symbol,
                strategy_id,
                account_id,
            )

    @staticmethod
    def _close_payload(
        *,
        position_id: str,
        instrument_id: int | str,
        units: float | None,
        symbol: str | None,
        strategy_id: str | None,
        account_id: str,
    ) -> dict[str, object]:
        return {
            "position_id": str(position_id).upper(),
            "instrument_id": str(instrument_id),
            "units": float(units) if units is not None else None,
            "symbol": symbol.upper() if symbol else None,
            "strategy_id": strategy_id or _DEFAULT_STRATEGY,
            "account_id": account_id,
        }

    def _close_replay_unlocked(
        self,
        replay_id: str | None,
        payload_hash: str | None,
    ) -> dict[str, object] | bool | None:
        if replay_id is None:
            return None
        record = self._state.replays.get(replay_id)
        if record is None:
            return None
        if record.get("payload_hash") != payload_hash:
            logger.warning("PaperBroker: close signal id was reused with a new payload")
            return False
        result = record.get("result")
        if not isinstance(result, dict):
            raise PaperStateCorruptError("paper close replay result is invalid")
        return deepcopy(result)

    def close_position(
        self,
        position_id: str,
        instrument_id: int | str = 0,
        units: float | None = None,
        symbol: str | None = None,
        strategy_id: str | None = None,
        account_id: str = "default",
        signal_id: str | None = None,
    ) -> dict[str, object] | bool:
        """Flatten (or partially reduce) one exact paper sleeve.

        A broker order id identifies the current position generation. Replaying
        a stop from a closed generation therefore cannot flatten a later
        replacement. Passing the symbol as position_id remains an explicit
        operator flatten for the uniquely selected sleeve.

        When signal_id is supplied, the durable replay ledger makes close
        retries return the original fill and rejects the same id with a changed
        payload.
        """
        replay_id = (
            _replay_key(
                "close",
                signal_id,
                strategy_id or _DEFAULT_STRATEGY,
                account_id,
            )
            if signal_id
            else None
        )
        payload_hash = (
            _payload_hash(
                self._close_payload(
                    position_id=position_id,
                    instrument_id=instrument_id,
                    units=units,
                    symbol=symbol,
                    strategy_id=strategy_id,
                    account_id=account_id,
                )
            )
            if replay_id
            else None
        )

        with self._lock:
            replayed = self._close_replay_unlocked(replay_id, payload_hash)
            if replayed is not None:
                return replayed
            key = self._resolve_position_key_unlocked(
                position_id,
                symbol,
                strategy_id,
                account_id,
            )
            if key is None:
                logger.info(
                    "PaperBroker: could not resolve a position for %s / %s",
                    position_id,
                    symbol,
                )
                return False
            position = self._state.positions.get(key)
            if position is None or position.qty == 0:
                return False
            observed_generation = position.position_id
            priced_symbol = position.symbol
            side = "SELL" if position.qty > 0 else "BUY"

        # Price outside the mutation lock: a slow quote must not block account
        # reads. Generation and replay identity are checked again afterwards.
        price = self._fill_price(priced_symbol, side)
        if price is None:
            logger.error(
                "PaperBroker: cannot price %s — close refused, position left open",
                priced_symbol,
            )
            return False

        with self._lock:
            replayed = self._close_replay_unlocked(replay_id, payload_hash)
            if replayed is not None:
                return replayed
            key = self._resolve_position_key_unlocked(
                position_id,
                symbol,
                strategy_id,
                account_id,
            )
            position = self._state.positions.get(key) if key else None
            if position is None or position.qty == 0 or position.position_id != observed_generation:
                logger.info("PaperBroker: close target changed while it was priced")
                return False
            if replay_id and len(self._state.replays) >= _MAX_REPLAY_RECORDS:
                logger.error(
                    "PaperBroker: replay ledger exhausted — close refused rather "
                    "than becoming non-idempotent"
                )
                return False

            qty = abs(position.qty) if units is None else min(abs(float(units)), abs(position.qty))
            side = "SELL" if position.qty > 0 else "BUY"
            before = deepcopy(self._state)
            order_id = str(uuid4())
            close_record: dict[str, object] = {
                "order_id": order_id,
                "signal_id": signal_id or f"close-{position.symbol}-{order_id}",
                "symbol": position.symbol,
                "strategy_id": position.strategy_id,
                "account_id": position.account_id,
                "side": side,
                "qty": qty,
                "fill_price": price,
                "notional": round(price * qty, 2),
                "submitted_at": datetime.now(timezone.utc).isoformat(),
                "close": True,
            }
            try:
                if side == "SELL":
                    self._apply_sell(
                        key,
                        position.symbol,
                        position.strategy_id,
                        position.account_id,
                        qty,
                        price,
                        order_id,
                    )
                else:
                    self._apply_buy(
                        key,
                        position.symbol,
                        position.strategy_id,
                        position.account_id,
                        qty,
                        price,
                        order_id,
                    )
                self._state.orders.append(close_record)
                self._state.orders = self._state.orders[-_MAX_ORDER_HISTORY:]
                if replay_id:
                    self._state.replays[replay_id] = {
                        "payload_hash": payload_hash,
                        "result": close_record,
                    }
                self._save_state()
            except PaperStatePersistenceError as exc:
                self._state = before
                logger.error(
                    "PaperBroker: close refused because state was not durable: %s",
                    exc,
                )
                return False
        logger.info("PaperBroker: closed %.4f %s @ %.4f", qty, priced_symbol, price)
        return close_record

    # ------------------------------------------------------------------
    # Account / positions
    # ------------------------------------------------------------------
    def get_positions(self) -> list[BrokerPosition]:
        with self._lock:
            positions = list(self._state.positions.values())
        grouped: dict[str, list[PaperPosition]] = {}
        for position in positions:
            if position.qty == 0:
                continue
            grouped.setdefault(position.symbol, []).append(position)

        results: list[BrokerPosition] = []
        for symbol, sleeves in grouped.items():
            qty = sum(position.qty for position in sleeves)
            if abs(qty) <= 1e-9:
                continue
            mark = self.mark_price(symbol)
            if mark is None:
                mark = sum(
                    position.average_price * abs(position.qty) for position in sleeves
                ) / sum(abs(position.qty) for position in sleeves)
            market_value = mark * qty
            average_price = sum(
                position.average_price * abs(position.qty) for position in sleeves
            ) / sum(abs(position.qty) for position in sleeves)
            unrealized = sum((mark - position.average_price) * position.qty for position in sleeves)
            results.append(
                BrokerPosition(
                    symbol=symbol,
                    qty=qty,
                    position_id=symbol,
                    market_value=round(market_value, 2),
                    average_price=round(average_price, 6),
                    unrealized_pnl=round(unrealized, 2),
                )
            )
        return results

    def get_sleeve_positions(self) -> list[PaperPosition]:
        """Exact virtual positions used for challenger isolation."""
        with self._lock:
            return [deepcopy(position) for position in self._state.positions.values()]

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
            before = self._state
            replacement = PaperState(cash=self._starting_cash, starting_cash=self._starting_cash)
            self._state = replacement
            try:
                self._save_state()
            except PaperStatePersistenceError:
                self._state = before
                raise

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    @staticmethod
    def _legacy_entry_payload(order: dict[str, object]) -> dict[str, object]:
        return {
            "symbol": str(order.get("symbol", "")).upper(),
            "side": str(order.get("side", "")).upper(),
            "qty": float(order.get("qty", 0.0)),
            "order_type": str(order.get("order_type", "MARKET")).upper(),
            "time_in_force": str(order.get("time_in_force", "DAY")),
            "limit_price": order.get("limit_price"),
            "decision_price": order.get("decision_price"),
            "stop_loss_rate": order.get("stop_loss_rate"),
            "take_profit_rate": order.get("take_profit_rate"),
            "reduce_only": bool(order.get("reduce_only", False)),
        }

    @classmethod
    def _legacy_replays(
        cls,
        orders: list[dict[str, object]],
    ) -> dict[str, dict[str, object]]:
        replays: dict[str, dict[str, object]] = {}
        for order in orders:
            signal_id = str(order.get("signal_id") or "")
            order_id = str(order.get("order_id") or "")
            if not signal_id or not order_id or order.get("close"):
                continue
            strategy_id = str(order.get("strategy_id") or _DEFAULT_STRATEGY)
            account_id = str(order.get("account_id") or "default")
            replay_id = _replay_key("entry", signal_id, strategy_id, account_id)
            replays[replay_id] = {
                "payload_hash": _payload_hash(cls._legacy_entry_payload(order)),
                "result": {
                    "status": OrderStatus.ACCEPTED.value,
                    "external_order_id": order_id,
                    "fill_price": float(order["fill_price"]),
                    "rejection_reason": None,
                },
            }
        return replays

    @staticmethod
    def _validate_replays(
        raw_replays: object,
    ) -> dict[str, dict[str, object]]:
        if not isinstance(raw_replays, dict):
            raise ValueError("paper replay ledger must be an object")
        if len(raw_replays) > _MAX_REPLAY_RECORDS:
            raise ValueError("paper replay ledger exceeds record limit")
        replays: dict[str, dict[str, object]] = {}
        for replay_id, record in raw_replays.items():
            if (
                not isinstance(replay_id, str)
                or len(replay_id) != 64
                or any(char not in "0123456789abcdef" for char in replay_id)
                or not isinstance(record, dict)
            ):
                raise ValueError("paper replay record is invalid")
            payload_hash = record.get("payload_hash")
            result = record.get("result")
            if (
                not isinstance(payload_hash, str)
                or len(payload_hash) != 64
                or any(char not in "0123456789abcdef" for char in payload_hash)
                or not isinstance(result, dict)
            ):
                raise ValueError("paper replay result is invalid")
            if result.get("status") is not None:
                external_order_id = result.get("external_order_id")
                fill_price = result.get("fill_price")
                if (
                    result.get("status") != OrderStatus.ACCEPTED.value
                    or not isinstance(external_order_id, str)
                    or not external_order_id
                    or len(external_order_id) > 128
                    or not isinstance(fill_price, (int, float))
                    or not math.isfinite(float(fill_price))
                    or float(fill_price) <= 0
                ):
                    raise ValueError("paper entry replay result is invalid")
            else:
                order_id = result.get("order_id")
                side = str(result.get("side", "")).upper()
                qty = result.get("qty")
                fill_price = result.get("fill_price")
                if (
                    not isinstance(order_id, str)
                    or not order_id
                    or len(order_id) > 128
                    or side not in _SUPPORTED_SIDES
                    or not isinstance(qty, (int, float))
                    or not math.isfinite(float(qty))
                    or float(qty) <= 0
                    or not isinstance(fill_price, (int, float))
                    or not math.isfinite(float(fill_price))
                    or float(fill_price) <= 0
                ):
                    raise ValueError("paper close replay result is invalid")
            replays[replay_id] = dict(record)
        return replays

    @staticmethod
    def _restore_legacy_position_ids(
        positions: dict[str, PaperPosition],
        orders: list[dict[str, object]],
    ) -> None:
        for position in positions.values():
            if position.position_id:
                if not position.order_ids:
                    position.order_ids = [position.position_id]
                continue
            expected_side = "BUY" if position.qty > 0 else "SELL"
            matching = [
                order
                for order in orders
                if not order.get("close")
                and str(order.get("symbol", "")).upper() == position.symbol
                and str(order.get("strategy_id") or _DEFAULT_STRATEGY) == position.strategy_id
                and str(order.get("account_id") or "default") == position.account_id
                and str(order.get("side", "")).upper() == expected_side
            ]
            if not matching:
                raise ValueError("legacy open position has no recoverable position identity")
            position.position_id = str(matching[-1].get("order_id") or "")
            if not position.position_id:
                raise ValueError("legacy position order identity is invalid")
            position.order_ids = [position.position_id]

    def _load_state(self) -> PaperState:
        if not self._state_path.exists():
            return PaperState(
                cash=self._starting_cash,
                starting_cash=self._starting_cash,
            )
        try:
            if self._state_path.stat().st_size > _MAX_STATE_BYTES:
                raise ValueError("paper state exceeds size limit")
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("paper state root must be an object")
            schema_version = int(payload.get("schema_version", 1))
            if schema_version > _STATE_SCHEMA_VERSION:
                raise ValueError("paper state schema is newer than this broker")

            raw_positions = payload.get("positions", {})
            if not isinstance(raw_positions, dict):
                raise ValueError("paper positions must be an object")
            positions: dict[str, PaperPosition] = {}
            for raw in raw_positions.values():
                if not isinstance(raw, dict):
                    raise ValueError("paper position must be an object")
                position = PaperPosition.from_dict(raw)
                if (
                    not position.symbol
                    or not position.strategy_id
                    or not position.account_id
                    or not math.isfinite(position.qty)
                    or not math.isfinite(position.average_price)
                    or position.average_price <= 0
                ):
                    raise ValueError("paper position contains invalid values")
                positions[
                    _position_key(
                        position.symbol,
                        position.strategy_id,
                        position.account_id,
                    )
                ] = position

            raw_orders = payload.get("orders", [])
            if not isinstance(raw_orders, list) or any(
                not isinstance(order, dict) for order in raw_orders
            ):
                raise ValueError("paper order history must be a list of objects")
            orders = list(raw_orders[-_MAX_ORDER_HISTORY:])
            self._restore_legacy_position_ids(positions, orders)
            for position in positions.values():
                if (
                    not position.position_id
                    or len(position.position_id) > 128
                    or not position.order_ids
                    or len(position.order_ids) > 10_000
                    or any(not order_id or len(order_id) > 128 for order_id in position.order_ids)
                    or position.position_id not in position.order_ids
                ):
                    raise ValueError("paper position identity is invalid")

            raw_replays = payload.get("replays")
            replays = (
                self._legacy_replays(orders)
                if raw_replays is None
                else self._validate_replays(raw_replays)
            )
            state = PaperState(
                cash=float(payload.get("cash", self._starting_cash)),
                starting_cash=float(payload.get("starting_cash", self._starting_cash)),
                realized_pnl=float(payload.get("realized_pnl", 0.0)),
                positions=positions,
                orders=orders,
                replays=replays,
            )
            if (
                not all(
                    math.isfinite(value)
                    for value in (state.cash, state.starting_cash, state.realized_pnl)
                )
                or state.starting_cash <= 0
            ):
                raise ValueError("paper account contains invalid values")
            return state
        except Exception as exc:
            logger.error(
                "PaperBroker: could not read %s (%s) — refusing to start",
                self._state_path,
                exc,
            )
            raise PaperStateCorruptError(f"paper state is unreadable: {self._state_path}") from exc

    def _save_state(self) -> None:
        temp = self._state_path.with_name(f".{self._state_path.name}.{uuid4().hex}.tmp")
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            descriptor = os.open(temp, flags, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(
                    self._state.to_dict(),
                    handle,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self._state_path)
            os.chmod(self._state_path, 0o600)
            directory = os.open(self._state_path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except Exception as exc:
            logger.error("PaperBroker: failed to persist state: %s", exc)
            try:
                temp.unlink(missing_ok=True)
            except Exception:
                pass
            raise PaperStatePersistenceError(
                f"could not persist paper state: {self._state_path}"
            ) from exc
