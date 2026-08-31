"""Is an assessment reproducible from the archive?

ADR 0001 gives L1 exactly one purpose: "establish whether the arguments are
reproducible from the archive". That is a measurable property, so it is
measured here rather than asserted in a document.

Two distinct things have to hold, and only the first is obvious:

**Determinism.** The same role over the same archive at the same moment
produces the same conclusions. A deterministic analyser gets this for free,
which is most of the answer to the ADR's open question about whether these
roles should be model-backed.

**Point-in-time isolation.** An assessment made as of a past moment does not
change when *later* data arrives — including a revision of a bar the
assessment already used. This is the one that actually bites: a role can be
perfectly deterministic and still silently improve every time the archive is
corrected, which makes every historical conclusion unfalsifiable, because
re-running it never reproduces what was originally said.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .archive import PointInTimeArchive
from .models import Assessment


@dataclass
class ReproductionResult:
    role: str
    symbol: str
    as_of: datetime
    runs: int
    digests: list[str] = field(default_factory=list)

    @property
    def reproducible(self) -> bool:
        return len(set(self.digests)) == 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "symbol": self.symbol,
            "as_of": self.as_of.isoformat(),
            "runs": self.runs,
            "reproducible": self.reproducible,
            "digests": sorted(set(self.digests)),
        }


def reproduce(
    specialist: Any,
    journal: Any,
    symbol: str,
    as_of: datetime,
    runs: int = 3,
    timeframe: str = "15m",
) -> ReproductionResult:
    """Run one role several times over a freshly built archive each time.

    A fresh archive per run on purpose: reusing one would also reuse whatever
    it had cached, which would prove that a cache is stable rather than that
    the role is.
    """
    result = ReproductionResult(
        role=getattr(specialist, "role", "unknown"), symbol=symbol, as_of=as_of, runs=runs
    )
    for _ in range(runs):
        assessment = specialist.assess(
            PointInTimeArchive(journal, as_of, timeframe), symbol
        )
        result.digests.append(assessment.digest())
    return result


def assess_at(
    specialist: Any, journal: Any, symbol: str, as_of: datetime, timeframe: str = "15m"
) -> Assessment:
    """One assessment through a fresh point-in-time archive."""
    return specialist.assess(PointInTimeArchive(journal, as_of, timeframe), symbol)
