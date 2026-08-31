"""What a specialist produces, and what makes it checkable.

A claim is not an opinion with a number attached. It is a statement, the
measurement behind it, the threshold that measurement was compared against,
and a reference to the archive rows that produced it. All four, or it does not
go in — because the whole point of L1 is to establish whether the arguments are
*reproducible from the archive*, and a claim you cannot trace back is not
reproducible, it is just plausible.

Everything here is deliberately inert. An assessment has no method that changes
anything, no path to lifecycle state, and no field a downstream component reads
as a control input. Per ADR 0001 constraint 5, model output — and by extension
any specialist output — is an argument about data, never a decision.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

#: The three stances an argument can take. Deliberately not a score: a
#: specialist that emits 0.63 invites a reader to average it with another 0.63,
#: which is arithmetic on opinions.
STANCES = ("bull", "bear", "neutral")


@dataclass(frozen=True)
class EvidenceRef:
    """One traceable fact: where it came from and what it was."""

    source: str
    """The archive table or derived view, e.g. "bar_observations"."""

    detail: str
    """The query, precisely enough to re-run it."""

    value: Any
    """What was read or computed. Kept as a value, not a formatted string, so
    two runs can be compared exactly rather than by their prose."""

    def to_dict(self) -> dict[str, Any]:
        return {"source": self.source, "detail": self.detail, "value": self.value}


@dataclass(frozen=True)
class Claim:
    """A statement a specialist is prepared to be wrong about."""

    statement: str
    stance: str
    measure: float | None
    """The number the claim rests on. None when the claim is qualitative, which
    should be rare and is worth noticing when it is not."""

    threshold: float | None
    """What `measure` was compared against. Stated so a reader can disagree
    with the threshold rather than only with the conclusion."""

    evidence: tuple[EvidenceRef, ...] = ()

    def __post_init__(self) -> None:
        if self.stance not in STANCES:
            raise ValueError(f"stance must be one of {STANCES}, got {self.stance!r}")
        if not self.evidence:
            # A claim with no evidence is the failure mode this phase exists to
            # rule out, so it is refused at construction rather than filtered
            # out later by something that might not run.
            raise ValueError(f"claim {self.statement!r} carries no evidence")

    def to_dict(self) -> dict[str, Any]:
        return {
            "statement": self.statement,
            "stance": self.stance,
            "measure": self.measure,
            "threshold": self.threshold,
            "evidence": [e.to_dict() for e in self.evidence],
        }


@dataclass
class Assessment:
    """One role's reading of one symbol at one moment."""

    role: str
    symbol: str
    as_of: datetime
    produced_by: str
    """What generated this — "deterministic:market/1" today. Recorded because
    the open question in ADR 0001 is whether these roles should be model-backed
    at all, and that argument needs assessments that say which they were."""

    claims: list[Claim] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)
    """Why a claim could not be made. Named rather than omitted: a role that
    silently produces nothing looks identical to a role that looked and found
    nothing to say."""

    queries: list[dict[str, Any]] = field(default_factory=list)

    @property
    def available(self) -> bool:
        return bool(self.claims)

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "symbol": self.symbol,
            "as_of": self.as_of.isoformat(),
            "produced_by": self.produced_by,
            "available": self.available,
            "claims": [c.to_dict() for c in self.claims],
            "unavailable": self.unavailable,
            "queries": self.queries,
            "digest": self.digest(),
        }

    def digest(self) -> str:
        """A content hash over everything that is a conclusion.

        `queries` is excluded — it records how the archive was read, not what
        was concluded, and a change in read order would otherwise register as a
        changed opinion. Two runs at the same `as_of` must produce the same
        digest; a test asserts it, because "reproducible" is the property L1
        exists to establish and an unmeasured claim to it is worth nothing.
        """
        payload = {
            "role": self.role,
            "symbol": self.symbol,
            "as_of": self.as_of.isoformat(),
            "produced_by": self.produced_by,
            "claims": [c.to_dict() for c in self.claims],
            "unavailable": self.unavailable,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


@dataclass
class Argument:
    """The bull, bear and neutral positions, assembled from every role.

    Not a verdict. There is deliberately no `conclusion` field and no scoring
    across stances: the ADR wants the positions kept separate so that a later
    reader can see which claim turned out to be wrong, and collapsing them into
    a single number destroys exactly that.
    """

    symbol: str
    as_of: datetime
    assessments: list[Assessment] = field(default_factory=list)

    def by_stance(self, stance: str) -> list[tuple[str, Claim]]:
        return [
            (a.role, claim)
            for a in self.assessments
            for claim in a.claims
            if claim.stance == stance
        ]

    @property
    def roles_reporting(self) -> list[str]:
        return [a.role for a in self.assessments if a.available]

    @property
    def roles_silent(self) -> list[str]:
        return [a.role for a in self.assessments if not a.available]

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "as_of": self.as_of.isoformat(),
            "roles_reporting": self.roles_reporting,
            "roles_silent": self.roles_silent,
            "bull": [{"role": r, **c.to_dict()} for r, c in self.by_stance("bull")],
            "bear": [{"role": r, **c.to_dict()} for r, c in self.by_stance("bear")],
            "neutral": [
                {"role": r, **c.to_dict()} for r, c in self.by_stance("neutral")
            ],
            "assessments": [a.to_dict() for a in self.assessments],
            "digest": self.digest(),
        }

    def digest(self) -> str:
        joined = "|".join(a.digest() for a in self.assessments)
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]
