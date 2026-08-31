"""The thresholds the veto rejects against.

ADR 0001 constraint 2: thresholds live in validated, versioned configuration,
not scattered through the code that reads them. Versioned because a rejection
recorded six months ago is only interpretable if you can recover which policy
produced it — "the veto rejected this" is not a finding unless you know what it
was rejecting against.

Defaults live on the fields and nowhere else. Keeping a second copy inside
`from_env` is how a default gets changed in one place and silently not the
other, which has already happened once in this codebase.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, fields
from typing import Any, ClassVar

#: Bumped whenever a field's meaning or default changes, so a stored decision
#: can be read against the policy that produced it rather than today's.
POLICY_VERSION = "1"


@dataclass(frozen=True)
class VetoPolicy:
    """What makes a subject unfit to reason about. Rejections only.

    Every rule here answers "is there a reason not to act on this", never "is
    this a good idea". The veto has no approval criteria because it has no
    authority to approve, and a policy field expressing merit would be the
    first step towards it acquiring one.
    """

    min_archived_bars: int = 60
    """Below this, there is not enough history for any downstream claim to
    rest on. The same floor the technical role uses, for the same reason."""

    max_stale_minutes: float = 120.0
    """How old the freshest bar may be. Reasoning about a symbol whose series
    stopped two days ago is reasoning about a memory."""

    max_gap_count: int = 3
    """Interior holes tolerated in the window. Some are inevitable around
    halts; a series full of them cannot support a claim about continuity."""

    window_hours: float = 48.0
    """How far back completeness is assessed."""

    expected_interval_minutes: float = 15.0
    """Bar cadence, for gap detection. Must match the timeframe being read."""

    max_recent_rejected_orders: int = 5
    """A symbol whose orders keep being rejected has something wrong with it —
    an instrument mapping, a permission, a halt — and research that ignores
    that is research about a symbol the system cannot actually trade."""

    _ENV: ClassVar[dict[str, str]] = {
        "min_archived_bars": "VETO_MIN_ARCHIVED_BARS",
        "max_stale_minutes": "VETO_MAX_STALE_MINUTES",
        "max_gap_count": "VETO_MAX_GAP_COUNT",
        "window_hours": "VETO_WINDOW_HOURS",
        "expected_interval_minutes": "VETO_EXPECTED_INTERVAL_MINUTES",
        "max_recent_rejected_orders": "VETO_MAX_REJECTED_ORDERS",
    }

    @classmethod
    def from_env(cls) -> "VetoPolicy":
        """Field defaults, overridden only where a variable is set.

        An unparseable value is refused rather than ignored: a veto silently
        running on a default the operator believes they replaced is a veto
        nobody has actually configured.
        """
        overrides: dict[str, Any] = {}
        types = {f.name: f.type for f in fields(cls)}
        for name, variable in cls._ENV.items():
            raw = os.getenv(variable)
            if raw is None or raw.strip() == "":
                continue
            caster = int if str(types[name]).startswith("int") else float
            try:
                overrides[name] = caster(raw)
            except ValueError as exc:
                raise ValueError(
                    f"{variable}={raw!r} is not a valid {caster.__name__}"
                ) from exc
        return cls(**overrides)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": POLICY_VERSION,
            **{f.name: getattr(self, f.name) for f in fields(self)},
        }
