"""L2 of the adaptive-learning roadmap (docs/adr/0001): the risk veto.

An independent component with authority to reject and none to approve. Built
before anything can propose, deliberately — so that the thing capable of
refusing exists before the thing capable of suggesting.

Its independence is a signature rather than a discipline: `review()` has no
parameter for a specialist argument, so it cannot be handed one, and cannot be
influenced by the conclusions it was meant to check separately.

`VetoDecision.__bool__` raises. "Not rejected" is not approval, and the way
that distinction normally gets lost is a caller writing `if veto_ok(x):` — so
a decision cannot be used in a condition at all.
"""

from .decision import NO_OBJECTION, REJECTED, Objection, VetoDecision
from .policy import POLICY_VERSION, VetoPolicy
from .review import review

__all__ = [
    "NO_OBJECTION",
    "POLICY_VERSION",
    "Objection",
    "REJECTED",
    "VetoDecision",
    "VetoPolicy",
    "review",
]
