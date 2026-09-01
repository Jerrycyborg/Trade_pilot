"""L1's deliverable: the arguments, and whether they can be trusted to be real.

Not a recommendation. There is no verdict field, no score across stances, and
nothing here that another component reads to decide anything — per ADR 0001,
specialist output is an argument about data and the phase after this one is
the risk veto, not an executor.

The headline number is the share of specified roles that could say anything at
all, because that is the finding: five roles are specified and the archive
supports all five.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .archive import PointInTimeArchive
from .models import Argument
from .reproduce import reproduce
from .roles import UnarchivedRole, default_roster


def build_argument(
    journal: Any,
    symbol: str,
    as_of: datetime | None = None,
    roster: list[Any] | None = None,
    timeframe: str = "15m",
) -> Argument:
    """Every role's reading of one symbol at one moment."""
    moment = as_of or datetime.now(timezone.utc)
    argument = Argument(symbol=symbol, as_of=moment)
    for specialist in roster or default_roster():
        # A fresh archive per role, so one role's reads never appear in
        # another's provenance trail.
        argument.assessments.append(
            specialist.assess(PointInTimeArchive(journal, moment, timeframe), symbol)
        )
    return argument


def build_report(
    journal: Any,
    symbols: list[str],
    as_of: datetime | None = None,
    check_reproducibility: bool = True,
    roster: list[Any] | None = None,
    timeframe: str = "15m",
) -> dict[str, Any]:
    """Arguments for each symbol, plus what the archive could not support.

    `timeframe` must name the cadence that was actually archived: the roles
    read one timeframe's slice of the bar store, and reading the wrong one
    reports a well-stocked archive as empty.
    """
    moment = as_of or datetime.now(timezone.utc)
    active = roster or default_roster()
    arguments = [
        build_argument(journal, s, moment, active, timeframe=timeframe) for s in symbols
    ]

    reproductions: list[dict[str, Any]] = []
    if check_reproducibility:
        for symbol in symbols:
            for specialist in active:
                if isinstance(specialist, UnarchivedRole):
                    continue
                reproductions.append(
                    reproduce(
                        specialist, journal, symbol, moment, timeframe=timeframe
                    ).to_dict()
                )

    blocked = {
        s.role: {"reason": s.assess(PointInTimeArchive(journal, moment), "").unavailable[0],
                 "needed": s.needed}
        for s in active
        if isinstance(s, UnarchivedRole)
    }

    specified = len(active)
    supported = specified - len(blocked)

    return {
        "as_of": moment.isoformat(),
        "roles": {
            "specified": specified,
            "with_an_archive": supported,
            "blocked": blocked,
            "verdict": _roles_verdict(supported, specified),
        },
        "reproducibility": {
            "checked": len(reproductions),
            "all_reproducible": all(r["reproducible"] for r in reproductions)
            if reproductions
            else None,
            "results": reproductions,
        },
        "arguments": [a.to_dict() for a in arguments],
    }


def _roles_verdict(supported: int, specified: int) -> str:
    if supported == specified:
        return "every specified role has a point-in-time archive to read"
    return (
        f"{supported} of {specified} specified roles have a point-in-time archive. "
        "The rest are blocked on storage that does not exist yet, and are "
        "reported as unavailable rather than built against a live source — an "
        "assessment 'as of' a past moment built from today's data is the "
        "leakage the archive exists to prevent, and it would not be visible in "
        "the output."
    )
