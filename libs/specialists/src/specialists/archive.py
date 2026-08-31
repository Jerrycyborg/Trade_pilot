"""The only thing a specialist is allowed to read.

The ADR says every specialist reads "only through the point-in-time archive",
because a role that can see a revision the live system never received produces
conclusions nobody can reproduce. Written as a rule in a document, that lasts
until the first specialist that needs one more field and reaches for the
journal directly.

So it is a seam instead. A specialist is handed a `PointInTimeArchive` pinned
to a moment, never the journal, and the archive exposes only as-of reads. It
does not have a method that returns the corrected series, so no specialist can
call one — the constraint is enforced by what exists rather than by what is
permitted.

Every read is also recorded. That serves two purposes: a claim can name the
query behind it, and two runs over the same archive can be compared query for
query, which is how L1 answers whether its arguments are reproducible at all.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Query:
    """One read a specialist made, for the provenance trail."""

    source: str
    detail: str
    rows: int

    def to_dict(self) -> dict[str, Any]:
        return {"source": self.source, "detail": self.detail, "rows": self.rows}


class PointInTimeArchive:
    """A read-only view of the archive as it stood at one moment.

    Construct it with the moment being reasoned about. Every method answers
    from what was knowable then, and there is deliberately no escape hatch: no
    `journal` property, no `as_of` override per call, no "latest" read.
    """

    def __init__(self, journal: Any, as_of: datetime, timeframe: str = "15m") -> None:
        if as_of.tzinfo is None:
            as_of = as_of.replace(tzinfo=timezone.utc)
        self._journal = journal
        self._as_of = as_of
        # The cadence this archive answers in, fixed at construction like the
        # moment is: a role must not mix timeframes mid-argument, and the
        # caller — not the role — knows what cadence was actually archived.
        # The first live paper run archived daily bars and every role read the
        # (empty) 15m slice, so 41 real bars per symbol reported as zero.
        self._timeframe = timeframe
        self._queries: list[Query] = []

    @property
    def as_of(self) -> datetime:
        return self._as_of

    @property
    def queries(self) -> list[Query]:
        """Every read served, in order. The provenance trail."""
        return list(self._queries)

    def bars(self, symbol: str) -> list[dict[str, Any]]:
        """The series as the system held it at `as_of`, in this archive's cadence.

        `bars_as_of` returns, for each market timestamp, the most recent
        observation received *by* the cutoff — so a correction that arrived
        afterwards is correctly absent rather than silently improving the
        analysis. There is deliberately no per-call timeframe, for the same
        reason there is no per-call as-of.
        """
        rows = self._safe(
            lambda: self._journal.bars_as_of(symbol, self._timeframe, self._as_of),
            f"bars_as_of({symbol}, {self._timeframe})",
            "bar_observations",
        )
        return rows

    def sentiment(self, symbol: str) -> list[dict[str, Any]]:
        """Sentiment scores observed by `as_of`, oldest first.

        Served from the append-only sentiment archive, never from the
        aggregator's TTL cache: the cache holds today's answer, and an
        assessment 'as of' a past moment built from today's sentiment is the
        leakage the archive exists to prevent.
        """
        return self._safe(
            lambda: self._journal.sentiment_as_of(symbol, self._as_of),
            f"sentiment_as_of({symbol})",
            "sentiment_observations",
        )

    def decisions(
        self, symbol: str | None = None, stage: str | None = None
    ) -> list[dict[str, Any]]:
        """What the system decided, and what it could see when it decided.

        Point-in-time by construction: a decision row records its own inputs at
        the moment it was taken.
        """
        return self._safe(
            lambda: self._journal.decisions_as_of(
                self._as_of, symbol=symbol, stage=stage
            ),
            f"decisions_as_of({symbol or 'all'}, stage={stage or 'all'})",
            "decisions",
        )

    def executions(
        self, symbol: str | None = None, environment: str | None = None
    ) -> list[dict[str, Any]]:
        """Fills recorded by `as_of`, with their shortfall."""
        return self._safe(
            lambda: self._journal.execution_rows(
                symbol=symbol, environment=environment, window_end=self._as_of
            ),
            f"execution_rows({symbol or 'all'}, {environment or 'all'})",
            "execution_quality",
        )

    def _safe(self, read, detail: str, source: str) -> list[dict[str, Any]]:
        """A missing archive is an empty read, never an exception.

        A specialist that cannot read its source must report the role as
        unavailable, which it can only do if it gets control back. Crashing
        would take down every other role in the same run.
        """
        try:
            rows = list(read() or [])
        except Exception as exc:  # pragma: no cover - archives vary by deployment
            logger.debug("Archive read failed (%s): %s", detail, exc)
            rows = []
        self._queries.append(Query(source=source, detail=detail, rows=len(rows)))
        return rows


@dataclass
class ArchiveCoverage:
    """What the archive can and cannot support, per source.

    L1's honest deliverable. Three of the five roles the ADR specifies have no
    point-in-time store to read, and that is a finding about the system rather
    than a gap in this code.
    """

    available: dict[str, str] = field(default_factory=dict)
    unavailable: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"available": self.available, "unavailable": self.unavailable}
