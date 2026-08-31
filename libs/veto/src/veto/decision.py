"""What a veto returns, and why it cannot be read as approval.

ADR 0001: "A separate component with authority to reject, and no authority to
approve." That is easy to write and easy to lose. The way it gets lost is
ordinary: the veto returns something boolean-ish, a caller writes
`if veto_ok(x):`, and within a release the veto is a green light that a reader
now believes means the trade was checked and endorsed. Nothing in the code
objected, because `not rejected` and `approved` are the same bit.

So they are not the same bit here.

- There is no `approved` field, no `ok`, and no `passed`.
- `__bool__` raises. A decision cannot be used in a condition at all, which
  makes the ambiguous idiom a crash at the first test run rather than a
  misreading that survives review.
- The verdict is `REJECTED` or `NO_OBJECTION`, and the second is spelled to be
  awkward to mistake for endorsement, because it is not one: it means the veto
  found nothing in its own scope to object to, which is a statement about the
  veto and not about the subject.

The decision is frozen. "Its rejection is final within the loop" is not a
convention if a caller can clear a flag.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

REJECTED = "REJECTED"
NO_OBJECTION = "NO_OBJECTION"


@dataclass(frozen=True)
class Objection:
    """One reason to refuse, with the measurement behind it."""

    rule: str
    detail: str
    measure: float | None = None
    threshold: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "detail": self.detail,
            "measure": self.measure,
            "threshold": self.threshold,
        }


@dataclass(frozen=True)
class VetoDecision:
    """The veto's finding. Final within the loop."""

    subject: str
    as_of: datetime
    policy_version: str
    objections: tuple[Objection, ...] = ()
    unchecked: tuple[str, ...] = field(default=())
    """Rules that could not run. A veto that skipped half its checks and said
    nothing is indistinguishable from one that ran them all and found nothing,
    and only one of those is worth having."""

    @property
    def verdict(self) -> str:
        return REJECTED if self.objections else NO_OBJECTION

    @property
    def rejected(self) -> bool:
        """True when the veto refuses. There is deliberately no inverse
        property: `not rejected` is something a caller has to write themselves,
        which at least makes the claim theirs rather than this object's."""
        return bool(self.objections)

    def __bool__(self) -> bool:
        raise TypeError(
            "A VetoDecision has no truth value. `not rejected` is not approval "
            "— the veto has no authority to approve — so check `.rejected` "
            "explicitly rather than letting a condition decide what this meant."
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "as_of": self.as_of.isoformat(),
            "policy_version": self.policy_version,
            "verdict": self.verdict,
            "rejected": self.rejected,
            "objections": [o.to_dict() for o in self.objections],
            "unchecked": list(self.unchecked),
            "note": (
                "NO_OBJECTION means this veto found nothing in its own scope to "
                "refuse. It is not approval, not an endorsement, and not a "
                "statement that the subject is a good idea."
            ),
        }
