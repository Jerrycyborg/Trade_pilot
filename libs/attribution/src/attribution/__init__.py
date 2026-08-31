"""Post-trade attribution over the point-in-time archive.

L0 of the adaptive-learning roadmap (docs/adr/0001). Attribution only: this
package reads the archive and explains what happened. It proposes nothing,
writes nothing back, and has no path to changing what the system trades.

That constraint is the design, not a limitation to be lifted later by
convenience. The ADR gates every subsequent phase on this one having shown that
the recorded data can explain outcomes at all — so the honest outcome of L0 is
a coverage number and a list of what was missing, which is what
`build_report` returns.
"""

from .attribute import attribute
from .counterfactual import (
    Counterfactual,
    hold_to_end_of_window,
    perfect_exit,
    run_counterfactuals,
    stop_at,
)
from .models import Attribution, CoverageReport, Leg, RoundTrip
from .report import build_report
from .roundtrips import load_round_trips, pair_round_trips

__all__ = [
    "Attribution",
    "Counterfactual",
    "CoverageReport",
    "Leg",
    "RoundTrip",
    "attribute",
    "build_report",
    "hold_to_end_of_window",
    "load_round_trips",
    "pair_round_trips",
    "perfect_exit",
    "run_counterfactuals",
    "stop_at",
]
