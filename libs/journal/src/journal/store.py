"""Writer and reader for the point-in-time archive.

**A journal failure must never stop a trade, and must never corrupt one.**
Every public write is best-effort: it catches everything, logs, and returns.
A full disk should degrade research, not halt the loop or — worse — raise
mid-order and leave the system in a half-committed state.

SQLite in WAL mode is used deliberately. Several services write here
concurrently, WAL lets readers proceed during writes, and the whole archive
stays a single file you can copy, inspect with any SQLite client, or hand to
pandas. At the volumes this system produces (a handful of symbols at minute
resolution) it is nowhere near a bottleneck.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import create_engine, event, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from .models import (
    BarObservation,
    Base,
    Decision,
    ExecutionQuality,
    PriceObservation,
)

logger = logging.getLogger(__name__)

DEFAULT_PATH = "./journal.db"


class InvalidMarketTimestamp(ValueError):
    """A bar arrived without a usable market timestamp.

    Raised rather than substituted. Stamping such a bar with the fetch time
    files it under a moment the market never printed it, silently corrupting
    the series it lands in and every point-in-time reconstruction that reads
    through it.
    """


def _utc(value: datetime | None) -> datetime:
    """Coerce to aware UTC, defaulting to now.

    Only for timestamps that legitimately mean "when this happened here", such
    as observed_at. Market timestamps must go through `_market_ts`.
    """
    if value is None:
        return datetime.now(timezone.utc)
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _market_ts(value: object) -> datetime:
    """A market timestamp, or an exception. Never a substitute."""
    if not isinstance(value, datetime):
        raise InvalidMarketTimestamp(
            f"bar has no market timestamp (got {value!r}); refusing to file it "
            "under the time it was fetched"
        )
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _payload_hash(open_: float, high: float, low: float, close: float, volume: float) -> str:
    """Identity of a bar's *values*, so a revision is distinguishable from a
    re-fetch of the same bar."""
    payload = f"{open_!r}|{high!r}|{low!r}|{close!r}|{volume!r}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _read_utc(value: datetime | None) -> datetime | None:
    """Re-attach UTC on read.

    SQLite has no timezone type, so an aware datetime written as UTC comes back
    naive. Everything here is written in UTC, so restoring it keeps the archive
    unambiguous for research rather than quietly local.
    """
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _json(payload: object) -> str:
    """Serialise loosely — a journal entry is never worth an exception."""
    try:
        return json.dumps(payload, default=str, sort_keys=True)
    except Exception:
        return json.dumps({"unserialisable": str(type(payload))})


class Journal:
    """Append-only archive of what the system saw and what it decided."""

    def __init__(self, path: str | Path | None = None, enabled: bool | None = None) -> None:
        self._enabled = (
            enabled
            if enabled is not None
            else os.getenv("JOURNAL_ENABLED", "true").lower() == "true"
        )
        self._path = Path(path or os.getenv("JOURNAL_PATH", DEFAULT_PATH))
        self._lock = threading.Lock()
        self._engine = None
        self._session_factory: sessionmaker[Session] | None = None
        if self._enabled:
            self._open()

    @property
    def enabled(self) -> bool:
        return self._enabled and self._session_factory is not None

    @property
    def path(self) -> Path:
        return self._path

    def _open(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._engine = create_engine(
                f"sqlite+pysqlite:///{self._path}",
                future=True,
                connect_args={"check_same_thread": False, "timeout": 10.0},
            )

            @event.listens_for(self._engine, "connect")
            def _set_pragmas(dbapi_connection, _record):  # noqa: ANN001
                cursor = dbapi_connection.cursor()
                # WAL so concurrent readers are not blocked by writers.
                cursor.execute("PRAGMA journal_mode=WAL")
                # NORMAL trades a little durability for throughput. The archive is
                # research data, not the trading ledger — losing the last few rows
                # to a hard crash is acceptable, blocking the loop on fsync is not.
                cursor.execute("PRAGMA synchronous=NORMAL")
                cursor.close()

            Base.metadata.create_all(bind=self._engine)
            self._session_factory = sessionmaker(bind=self._engine, future=True)
            logger.info("Journal open at %s", self._path)
        except Exception as exc:
            logger.error("Journal unavailable (%s) — continuing without it", exc)
            self._enabled = False
            self._engine = None
            self._session_factory = None

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------
    def record_bars(
        self,
        symbol: str,
        timeframe: str,
        bars: Sequence[Any],
        source: str = "unknown",
        provider_meta: dict[str, Any] | None = None,
    ) -> int:
        """Archive OHLCV bars as *observations*. Returns rows written.

        Deduplicated on (symbol, timeframe, bar_ts, payload_hash): re-fetching
        an unchanged bar is a no-op, but a provider revising one stores a new
        observation rather than discarding the correction. Use `bars_as_of` to
        read back what was known at a particular moment.

        A bar without a usable market timestamp is rejected and counted, never
        stamped with the fetch time.
        """
        if not self.enabled or not bars:
            return 0

        now = datetime.now(timezone.utc)
        rows = []
        rejected = 0
        for bar in bars:
            try:
                bar_ts = _market_ts(getattr(bar, "timestamp", None))
                open_ = float(bar.open)
                high = float(bar.high)
                low = float(bar.low)
                close = float(bar.close)
                volume = float(getattr(bar, "volume", 0.0) or 0.0)
            except InvalidMarketTimestamp as exc:
                # Loud, not debug: a provider emitting timestampless bars is a
                # data-quality problem someone needs to see.
                logger.warning("Rejected bar for %s: %s", symbol, exc)
                rejected += 1
                continue
            except Exception as exc:
                logger.warning("Rejected unarchivable bar for %s: %s", symbol, exc)
                rejected += 1
                continue

            rows.append(
                {
                    "symbol": symbol.upper(),
                    "timeframe": timeframe,
                    "bar_ts": bar_ts,
                    "open": open_,
                    "high": high,
                    "low": low,
                    "close": close,
                    "volume": volume,
                    "source": source,
                    "recorded_at": now,
                    "observed_at": now,
                    "payload_hash": _payload_hash(open_, high, low, close, volume),
                    "revision": 0,
                    "provider_meta": json.dumps(provider_meta or {}, default=str),
                }
            )

        if rejected:
            self._record_rejected_bars(symbol, timeframe, rejected)
        if not rows:
            return 0

        # Number each row against what is already stored for its bar_ts, so a
        # revision is recorded as revision 1, 2, … rather than all as 0.
        self._number_revisions(rows)
        return self._insert_ignoring_duplicates(BarObservation, rows)

    def _number_revisions(self, rows: list[dict]) -> None:
        """Set `revision` from the observations already held for each bar_ts."""
        try:
            with self._session_factory() as session:  # type: ignore[misc]
                for row in rows:
                    count = session.scalar(
                        select(func.count())
                        .select_from(BarObservation)
                        .where(
                            BarObservation.symbol == row["symbol"],
                            BarObservation.timeframe == row["timeframe"],
                            BarObservation.bar_ts == row["bar_ts"],
                        )
                    )
                    row["revision"] = int(count or 0)
        except Exception as exc:  # pragma: no cover - numbering is cosmetic
            logger.debug("Could not number bar revisions: %s", exc)

    def _record_rejected_bars(self, symbol: str, timeframe: str, count: int) -> None:
        """Rejections are themselves an observation worth keeping.

        A gap in the series has a cause; recording the rejection is what lets
        journal-health explain one instead of merely reporting it.
        """
        try:
            self.record_decision(
                stage="data_quality",
                outcome="bars_rejected",
                symbol=symbol,
                reason="missing_or_invalid_market_timestamp",
                inputs={"timeframe": timeframe, "rejected": count},
            )
        except Exception as exc:  # pragma: no cover
            logger.debug("Could not record bar rejection: %s", exc)

    def bars_as_of(
        self,
        symbol: str,
        timeframe: str,
        as_of: datetime,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """The series exactly as this system knew it at `as_of`.

        For each market timestamp, the most recent observation this system had
        *by that moment* — so a revision received afterwards is correctly
        absent, and research reading through this cannot see a correction the
        live system had not been told about yet.

        This is the query the archive exists to serve, and it was impossible
        before revisions were kept: the old schema stored only the first
        version of each bar, so "what did we know at 14:35?" and "what did we
        learn first?" were the same answer.
        """
        if not self.enabled:
            return []
        cutoff = _utc(as_of)
        try:
            with self._session_factory() as session:  # type: ignore[misc]
                rows = session.scalars(
                    select(BarObservation)
                    .where(
                        BarObservation.symbol == symbol.upper(),
                        BarObservation.timeframe == timeframe,
                        BarObservation.observed_at <= cutoff,
                    )
                    .order_by(
                        BarObservation.bar_ts.desc(),
                        BarObservation.observed_at.desc(),
                    )
                ).all()
        except Exception as exc:
            logger.warning("Point-in-time read failed for %s: %s", symbol, exc)
            return []

        # Keep the newest observation per bar_ts; rows are already ordered so
        # the first one seen for each timestamp is the one that was current.
        latest: dict[datetime, BarObservation] = {}
        for row in rows:
            if row.bar_ts not in latest:
                latest[row.bar_ts] = row

        series = sorted(latest.values(), key=lambda r: r.bar_ts)[-limit:]
        return [
            {
                "bar_ts": _read_utc(r.bar_ts).isoformat(),
                "open": r.open,
                "high": r.high,
                "low": r.low,
                "close": r.close,
                "volume": r.volume,
                "source": r.source,
                "revision": r.revision,
                "observed_at": _read_utc(r.observed_at).isoformat(),
                "payload_hash": r.payload_hash,
            }
            for r in series
        ]

    def bar_revisions(
        self, symbol: str, timeframe: str, bar_ts: datetime
    ) -> list[dict[str, Any]]:
        """Every version of one bar, oldest first. The audit trail for a revision."""
        if not self.enabled:
            return []
        try:
            with self._session_factory() as session:  # type: ignore[misc]
                rows = session.scalars(
                    select(BarObservation)
                    .where(
                        BarObservation.symbol == symbol.upper(),
                        BarObservation.timeframe == timeframe,
                        BarObservation.bar_ts == _utc(bar_ts),
                    )
                    .order_by(BarObservation.observed_at)
                ).all()
        except Exception as exc:
            logger.warning("Revision read failed for %s: %s", symbol, exc)
            return []
        return [
            {
                "revision": r.revision,
                "observed_at": _read_utc(r.observed_at).isoformat(),
                "close": r.close,
                "volume": r.volume,
                "source": r.source,
                "payload_hash": r.payload_hash,
                "provider_meta": r.provider_meta,
            }
            for r in rows
        ]

    def record_price(
        self,
        symbol: str,
        price: float,
        price_ts: datetime | None = None,
        source: str = "unknown",
        accepted: bool = True,
    ) -> None:
        """Archive a resolved price, including one refused for staleness."""
        if not self.enabled:
            return
        observed_at = datetime.now(timezone.utc)
        stamp = _utc(price_ts)
        self._insert(
            PriceObservation,
            {
                "symbol": symbol.upper(),
                "price": float(price),
                "price_ts": stamp,
                "observed_at": observed_at,
                "age_seconds": max(0.0, (observed_at - stamp).total_seconds()),
                "source": source,
                "accepted": 1 if accepted else 0,
            },
        )

    def record_decision(
        self,
        stage: str,
        outcome: str,
        symbol: str = "",
        action: str = "",
        reason: str = "",
        inputs: object = None,
        outputs: object = None,
        correlation_id: str = "",
        ts: datetime | None = None,
    ) -> str:
        """Archive one decision. Returns its id.

        ``inputs`` is the important field: whatever the system could see at the
        moment it chose. Without it, a later post-mortem can observe that a
        trade lost money but never why the system thought otherwise.
        """
        decision_id = str(uuid4())
        if not self.enabled:
            return decision_id
        self._insert(
            Decision,
            {
                "decision_id": decision_id,
                "correlation_id": correlation_id or decision_id,
                "ts": _utc(ts),
                "symbol": symbol.upper(),
                "stage": stage,
                "action": str(action or ""),
                "outcome": outcome,
                "reason": reason[:2000],
                "inputs_json": _json(inputs if inputs is not None else {}),
                "outputs_json": _json(outputs if outputs is not None else {}),
            },
        )
        return decision_id

    def record_execution(
        self,
        symbol: str,
        side: str,
        qty: float,
        decision_price: float | None,
        fill_price: float | None,
        order_type: str = "MARKET",
        limit_price: float | None = None,
        order_id: str = "",
        signal_id: str = "",
        outcome: str = "filled",
        decision_ts: datetime | None = None,
        *,
        strategy_id: str = "",
        strategy_version: str = "",
        environment: str = "paper",
        account_id: str = "default",
        portfolio_id: str = "default",
        broker: str = "",
        decision_id: str = "",
        order_intent_id: str = "",
        requested_at: datetime | None = None,
        submitted_at: datetime | None = None,
        filled_at: datetime | None = None,
        fees: float = 0.0,
        spread_bps: float | None = None,
        filled_qty: float | None = None,
        cancelled: bool = False,
        rejected: bool = False,
    ) -> float | None:
        """Record what an order cost. Returns the shortfall in bps, if computable.

        Call this for misses as well as fills: a limit that did not fill is a
        cost of the execution policy, and omitting it turns any later fill-rate
        figure into 100%.

        `environment` is the field that makes any later performance figure
        honest. Paper, live and backtest fills are otherwise indistinguishable
        rows, and averaging across them silently mixes a simulator's fills with
        a venue's.
        """
        from contracts.execution import implementation_shortfall_bps

        filled = fill_price is not None and fill_price > 0
        shortfall = (
            implementation_shortfall_bps(decision_price, fill_price, side)
            if filled and decision_price
            else None
        )
        if not self.enabled:
            return shortfall
        self._insert(
            ExecutionQuality,
            {
                "order_id": order_id,
                "signal_id": signal_id,
                "symbol": symbol.upper(),
                "side": side.upper(),
                "qty": float(qty or 0.0),
                "order_type": order_type.upper(),
                "limit_price": limit_price,
                "decision_price": decision_price,
                "fill_price": fill_price,
                "shortfall_bps": shortfall,
                "filled": 1 if filled else 0,
                "outcome": outcome,
                "decision_ts": _utc(decision_ts),
                "strategy_id": strategy_id,
                "strategy_version": strategy_version,
                "environment": environment,
                "account_id": account_id,
                "portfolio_id": portfolio_id,
                "broker": broker,
                "decision_id": decision_id,
                "order_intent_id": order_intent_id,
                "requested_at": requested_at,
                "submitted_at": submitted_at,
                "filled_at": filled_at,
                "fees": float(fees or 0.0),
                "spread_bps": spread_bps,
                "filled_qty": float(
                    filled_qty if filled_qty is not None else (qty if filled else 0.0)
                ),
                "cancelled": 1 if cancelled else 0,
                "rejected": 1 if rejected else 0,
                "recorded_at": datetime.now(timezone.utc),
            },
        )
        return shortfall

    # ------------------------------------------------------------------
    # Reads — for research and the operator endpoints
    # ------------------------------------------------------------------
    def scoped_execution_metrics(
        self,
        *,
        strategy_id: str,
        symbol: str,
        environment: str,
        account_id: str = "default",
        window_start: datetime | None = None,
        window_end: datetime | None = None,
    ) -> dict[str, Any]:
        """Execution facts for exactly one scope, from the durable record.

        This is what promotion evidence is derived from. Every filter here is
        mandatory rather than optional on purpose: a figure computed without
        `environment` would average a simulator's fills together with a
        venue's, and one computed without `strategy_id` would credit a sleeve
        with another's trades.

        Returns raw counts and sums, not judgements. The gates decide what the
        numbers mean; this only reports what happened.
        """
        if not self.enabled:
            return {"available": False, "reason": "journal_disabled"}

        try:
            with self._session_factory() as session:  # type: ignore[misc]
                conditions = [
                    ExecutionQuality.strategy_id == strategy_id,
                    ExecutionQuality.symbol == symbol.upper(),
                    ExecutionQuality.environment == environment,
                    ExecutionQuality.account_id == account_id,
                ]
                if window_start is not None:
                    conditions.append(ExecutionQuality.recorded_at >= _utc(window_start))
                if window_end is not None:
                    conditions.append(ExecutionQuality.recorded_at <= _utc(window_end))

                rows = session.scalars(
                    select(ExecutionQuality).where(*conditions)
                ).all()
        except Exception as exc:
            logger.warning("Scoped execution read failed: %s", exc)
            return {"available": False, "reason": str(exc)}

        if not rows:
            return {
                "available": True,
                "orders": 0,
                "fills": 0,
                "misses": 0,
                "partial_fills": 0,
                "cancellations": 0,
                "rejections": 0,
                "realized_pnl": 0.0,
                "fees": 0.0,
                "mean_shortfall_bps": None,
                "worst_shortfall_bps": None,
                "fill_rate": None,
                "first_at": None,
                "last_at": None,
            }

        fills = [r for r in rows if r.filled]
        shortfalls = [r.shortfall_bps for r in rows if r.shortfall_bps is not None]
        partials = [
            r for r in fills if r.filled_qty and r.qty and r.filled_qty < r.qty
        ]
        stamps = [_read_utc(r.recorded_at) for r in rows]

        # Signed cash effect of each fill against its decision price. Not a
        # portfolio P&L — it is the execution component of one, which is the
        # part this table can honestly speak to.
        realized = 0.0
        for r in fills:
            if r.decision_price and r.fill_price:
                direction = 1.0 if r.side.upper() == "SELL" else -1.0
                realized += direction * (r.fill_price - r.decision_price) * (
                    r.filled_qty or r.qty or 0.0
                )

        return {
            "available": True,
            "orders": len(rows),
            "fills": len(fills),
            "misses": len(rows) - len(fills),
            "partial_fills": len(partials),
            "cancellations": sum(1 for r in rows if r.cancelled),
            "rejections": sum(1 for r in rows if r.rejected),
            "realized_pnl": round(realized, 4),
            "fees": round(sum(r.fees or 0.0 for r in rows), 4),
            "mean_shortfall_bps": (
                round(sum(shortfalls) / len(shortfalls), 4) if shortfalls else None
            ),
            "worst_shortfall_bps": round(max(shortfalls), 4) if shortfalls else None,
            "fill_rate": round(len(fills) / len(rows), 4),
            "first_at": min(stamps).isoformat(),
            "last_at": max(stamps).isoformat(),
        }

    def execution_rows(
        self,
        *,
        strategy_id: str | None = None,
        symbol: str | None = None,
        environment: str | None = None,
        account_id: str | None = "default",
        window_start: datetime | None = None,
        window_end: datetime | None = None,
        limit: int = 5000,
    ) -> list[dict[str, Any]]:
        """Raw execution records, for callers that need the individual orders.

        `scoped_execution_metrics` aggregates; attribution has to pair opening
        fills to closing fills, which needs the rows themselves. Filters are
        optional here because a coverage report legitimately spans everything —
        but the caller is responsible for not mixing environments in a
        *calculation*, which `attribution.pair_round_trips` enforces by
        grouping rather than trusting.
        """
        if not self.enabled:
            return []
        try:
            with self._session_factory() as session:  # type: ignore[misc]
                conditions = []
                if strategy_id is not None:
                    conditions.append(ExecutionQuality.strategy_id == strategy_id)
                if symbol is not None:
                    conditions.append(ExecutionQuality.symbol == symbol.upper())
                if environment is not None:
                    conditions.append(ExecutionQuality.environment == environment)
                if account_id is not None:
                    conditions.append(ExecutionQuality.account_id == account_id)
                if window_start is not None:
                    conditions.append(ExecutionQuality.recorded_at >= _utc(window_start))
                if window_end is not None:
                    conditions.append(ExecutionQuality.recorded_at <= _utc(window_end))

                rows = session.scalars(
                    select(ExecutionQuality)
                    .where(*conditions)
                    .order_by(ExecutionQuality.recorded_at)
                    .limit(limit)
                ).all()

                return [
                    {
                        "order_id": r.order_id,
                        "signal_id": r.signal_id,
                        "symbol": r.symbol,
                        "side": r.side,
                        "qty": r.qty,
                        "filled_qty": r.filled_qty,
                        "strategy_id": r.strategy_id,
                        "strategy_version": r.strategy_version,
                        "environment": r.environment,
                        "account_id": r.account_id,
                        "portfolio_id": r.portfolio_id,
                        "broker": r.broker,
                        "decision_id": r.decision_id,
                        "order_intent_id": r.order_intent_id,
                        "order_type": r.order_type,
                        "limit_price": r.limit_price,
                        "decision_price": r.decision_price,
                        "fill_price": r.fill_price,
                        "shortfall_bps": r.shortfall_bps,
                        "fees": r.fees,
                        "spread_bps": r.spread_bps,
                        "filled": bool(r.filled),
                        "cancelled": bool(r.cancelled),
                        "rejected": bool(r.rejected),
                        "outcome": r.outcome,
                        "decision_ts": _read_utc(r.decision_ts),
                        "recorded_at": _read_utc(r.recorded_at),
                    }
                    for r in rows
                ]
        except Exception as exc:
            logger.warning("Execution row read failed: %s", exc)
            return []

    def completeness(
        self,
        *,
        symbol: str,
        timeframe: str,
        window_start: datetime,
        window_end: datetime,
        expected_interval_minutes: float,
    ) -> dict[str, Any]:
        """How complete the bar series is over a window, and where the gaps are.

        A gap does not stop an open position from being managed — that would
        turn a data problem into an unmanaged exposure. It does make the window
        ineligible for learning and unusable as promotion evidence.
        """
        if not self.enabled:
            return {"available": False, "reason": "journal_disabled"}

        try:
            with self._session_factory() as session:  # type: ignore[misc]
                stamps = session.scalars(
                    select(BarObservation.bar_ts)
                    .where(
                        BarObservation.symbol == symbol.upper(),
                        BarObservation.timeframe == timeframe,
                        BarObservation.bar_ts >= _utc(window_start),
                        BarObservation.bar_ts <= _utc(window_end),
                    )
                    .distinct()
                    .order_by(BarObservation.bar_ts)
                ).all()
        except Exception as exc:
            logger.warning("Completeness read failed for %s: %s", symbol, exc)
            return {"available": False, "reason": str(exc)}

        actual = len(stamps)
        span_minutes = (
            _utc(window_end) - _utc(window_start)
        ).total_seconds() / 60.0
        # Reported, not used to decide completeness. It assumes the market is
        # open for every minute of the window, so any span crossing an
        # overnight close or a weekend looks catastrophically short. Halting on
        # that would stop trading every morning.
        expected = (
            max(1, int(span_minutes / expected_interval_minutes))
            if expected_interval_minutes > 0
            else actual
        )

        # A gap is a jump of more than one expected interval between two
        # consecutive bars we actually hold.
        gaps: list[dict[str, str]] = []
        tolerance = expected_interval_minutes * 1.5
        for earlier, later in zip(stamps, stamps[1:], strict=False):
            minutes = (_read_utc(later) - _read_utc(earlier)).total_seconds() / 60.0
            if minutes > tolerance:
                gaps.append(
                    {
                        "from": _read_utc(earlier).isoformat(),
                        "to": _read_utc(later).isoformat(),
                        "minutes": round(minutes, 2),
                    }
                )

        # Staleness: how long since the most recent bar. An interior hole and a
        # series that simply stopped are both losses of coverage, and only the
        # first shows up as a gap between consecutive bars.
        stale_minutes = None
        if stamps:
            stale_minutes = round(
                (_utc(window_end) - _read_utc(stamps[-1])).total_seconds() / 60.0, 2
            )

        stale = stale_minutes is not None and stale_minutes > tolerance
        complete = bool(stamps) and len(gaps) == 0 and not stale

        last_gap_at = gaps[-1]["to"] if gaps else None
        if stale and stamps:
            # A trailing hole has no "to" bar, so date it from the last one we
            # do have — otherwise a series that stopped can never age out of
            # its grace period.
            last_gap_at = _read_utc(stamps[-1]).isoformat()

        return {
            "available": True,
            "symbol": symbol.upper(),
            "timeframe": timeframe,
            "expected_observations": expected,
            "actual_observations": actual,
            "gap_count": len(gaps),
            "gaps": gaps[:20],
            "stale_minutes": stale_minutes,
            "stale": stale,
            "complete": complete,
            "last_gap_at": last_gap_at,
        }

    def execution_quality(self, limit: int = 200) -> dict[str, object]:
        """Measured execution cost — the replacement for guessed backtest costs."""
        if not self.enabled:
            return {"enabled": False}
        try:
            from sqlalchemy import func

            with self._session_factory() as session:  # type: ignore[misc]
                total = session.scalar(
                    select(func.count()).select_from(ExecutionQuality)
                ) or 0
                fills = session.scalar(
                    select(func.count())
                    .select_from(ExecutionQuality)
                    .where(ExecutionQuality.filled == 1)
                ) or 0
                mean_bps = session.scalar(
                    select(func.avg(ExecutionQuality.shortfall_bps)).where(
                        ExecutionQuality.shortfall_bps.is_not(None)
                    )
                )
                worst = session.scalar(
                    select(func.max(ExecutionQuality.shortfall_bps))
                )
                rows = session.scalars(
                    select(ExecutionQuality)
                    .order_by(ExecutionQuality.recorded_at.desc())
                    .limit(limit)
                ).all()
                by_symbol: dict[str, list[float]] = {}
                for row in rows:
                    if row.shortfall_bps is not None:
                        by_symbol.setdefault(row.symbol, []).append(row.shortfall_bps)

            return {
                "enabled": True,
                "orders": total,
                "filled": fills,
                "fill_rate": round(fills / total, 4) if total else None,
                "mean_shortfall_bps": round(mean_bps, 2) if mean_bps is not None else None,
                "worst_shortfall_bps": round(worst, 2) if worst is not None else None,
                "mean_shortfall_by_symbol": {
                    symbol: round(sum(v) / len(v), 2) for symbol, v in sorted(by_symbol.items())
                },
            }
        except Exception as exc:
            logger.warning("Execution quality read failed: %s", exc)
            return {"enabled": True, "error": str(exc)}


    def decisions_as_of(
        self,
        as_of: datetime,
        *,
        symbol: str | None = None,
        stage: str | None = None,
        window_start: datetime | None = None,
        limit: int = 500,
    ) -> list[dict]:
        """Decisions the system had already made by `as_of`, oldest first.

        The point-in-time read of the decision log. `recent_decisions` answers
        "what happened lately", which is the wrong question for research: a
        specialist reasoning about a moment must not see a decision taken after
        it. A decision row is point-in-time by construction — `inputs_json` is
        what the system could see when it decided — so this is the one archive
        besides bars that supports an honest as-of query.
        """
        if not self.enabled:
            return []
        cutoff = _utc(as_of)
        try:
            with self._session_factory() as session:  # type: ignore[misc]
                conditions = [Decision.ts <= cutoff]
                if symbol is not None:
                    conditions.append(Decision.symbol == symbol.upper())
                if stage is not None:
                    conditions.append(Decision.stage == stage)
                if window_start is not None:
                    conditions.append(Decision.ts >= _utc(window_start))
                rows = session.scalars(
                    select(Decision)
                    .where(*conditions)
                    .order_by(Decision.ts)
                    .limit(limit)
                ).all()
                return [
                    {
                        "decision_id": r.decision_id,
                        "correlation_id": r.correlation_id,
                        "ts": _read_utc(r.ts),
                        "symbol": r.symbol,
                        "stage": r.stage,
                        "action": r.action,
                        "outcome": r.outcome,
                        "reason": r.reason,
                        "inputs": json.loads(r.inputs_json or "{}"),
                        "outputs": json.loads(r.outputs_json or "{}"),
                    }
                    for r in rows
                ]
        except Exception as exc:
            logger.warning("Decision as-of read failed: %s", exc)
            return []

    def recent_decisions(self, limit: int = 50, symbol: str | None = None) -> list[dict]:
        if not self.enabled:
            return []
        try:
            with self._session_factory() as session:  # type: ignore[misc]
                stmt = select(Decision).order_by(Decision.ts.desc()).limit(limit)
                if symbol:
                    stmt = stmt.where(Decision.symbol == symbol.upper())
                return [
                    {
                        "decision_id": row.decision_id,
                        "correlation_id": row.correlation_id,
                        "ts": _read_utc(row.ts).isoformat(),
                        "symbol": row.symbol,
                        "stage": row.stage,
                        "action": row.action,
                        "outcome": row.outcome,
                        "reason": row.reason,
                        "inputs": json.loads(row.inputs_json or "{}"),
                        "outputs": json.loads(row.outputs_json or "{}"),
                    }
                    for row in session.scalars(stmt).all()
                ]
        except Exception as exc:
            logger.warning("Journal read failed: %s", exc)
            return []

    def stats(self) -> dict[str, object]:
        """Row counts and coverage — what the archive actually holds."""
        if not self.enabled:
            return {"enabled": False, "path": str(self._path)}
        try:
            from sqlalchemy import func

            with self._session_factory() as session:  # type: ignore[misc]
                bars = session.scalar(select(func.count()).select_from(BarObservation)) or 0
                prices = session.scalar(select(func.count()).select_from(PriceObservation)) or 0
                decisions = session.scalar(select(func.count()).select_from(Decision)) or 0
                symbols = session.scalars(
                    select(BarObservation.symbol).distinct()
                ).all()
                earliest = session.scalar(select(func.min(BarObservation.bar_ts)))
                latest = session.scalar(select(func.max(BarObservation.bar_ts)))
            return {
                "enabled": True,
                "path": str(self._path),
                "size_bytes": self._path.stat().st_size if self._path.exists() else 0,
                "bar_observations": bars,
                "price_observations": prices,
                "decisions": decisions,
                "symbols": sorted(symbols),
                "coverage_from": (
                    _read_utc(earliest).isoformat() if earliest else None
                ),
                "coverage_to": _read_utc(latest).isoformat() if latest else None,
            }
        except Exception as exc:
            logger.warning("Journal stats failed: %s", exc)
            return {"enabled": True, "path": str(self._path), "error": str(exc)}

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _insert(self, model: type, row: dict) -> None:
        if not self.enabled:
            return
        try:
            with self._lock, self._session_factory() as session:  # type: ignore[misc]
                session.add(model(**row))
                session.commit()
        except Exception as exc:
            logger.warning("Journal write failed (%s): %s", model.__name__, exc)

    def _insert_ignoring_duplicates(self, model: type, rows: Iterable[dict]) -> int:
        """Bulk insert, skipping rows already present. Returns rows actually added.

        RETURNING gives an exact count of what was inserted rather than what was
        offered — executemany does not populate rowcount, and the difference
        between the two is precisely the deduplication we care about.
        """
        if not self.enabled:
            return 0
        try:
            payload = list(rows)
            with self._lock, self._session_factory() as session:  # type: ignore[misc]
                result = session.execute(
                    sqlite_insert(model).on_conflict_do_nothing().returning(model.id),
                    payload,
                )
                written = len(list(result))
                session.commit()
                return written
        except Exception as exc:
            logger.warning("Journal bulk write failed (%s): %s", model.__name__, exc)
            return 0


_default: Journal | None = None
_default_lock = threading.Lock()


def get_journal() -> Journal:
    """Process-wide journal. Services share one archive file."""
    global _default
    if _default is None:
        with _default_lock:
            if _default is None:
                _default = Journal()
    return _default


def reset_journal(journal: Journal | None = None) -> None:
    """Replace the process-wide journal. Used by tests."""
    global _default
    with _default_lock:
        _default = journal
