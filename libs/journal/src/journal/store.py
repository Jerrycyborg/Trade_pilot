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

import json
import logging
import os
import threading
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import create_engine, event, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from .models import BarObservation, Base, Decision, PriceObservation

logger = logging.getLogger(__name__)

DEFAULT_PATH = "./journal.db"


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


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
    ) -> int:
        """Archive OHLCV bars. Returns rows written; repeats are no-ops.

        Deduplicated on (symbol, timeframe, bar_ts), so calling this on every
        fetch builds one clean series instead of a pile of overlapping copies.
        """
        if not self.enabled or not bars:
            return 0

        now = datetime.now(timezone.utc)
        rows = []
        for bar in bars:
            try:
                rows.append(
                    {
                        "symbol": symbol.upper(),
                        "timeframe": timeframe,
                        "bar_ts": _utc(getattr(bar, "timestamp", None)),
                        "open": float(bar.open),
                        "high": float(bar.high),
                        "low": float(bar.low),
                        "close": float(bar.close),
                        "volume": float(getattr(bar, "volume", 0.0) or 0.0),
                        "source": source,
                        "recorded_at": now,
                    }
                )
            except Exception as exc:
                logger.debug("Skipping unarchivable bar for %s: %s", symbol, exc)

        if not rows:
            return 0
        return self._insert_ignoring_duplicates(BarObservation, rows)

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

    # ------------------------------------------------------------------
    # Reads — for research and the operator endpoints
    # ------------------------------------------------------------------
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
