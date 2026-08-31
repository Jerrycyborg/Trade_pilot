"""L1 of the adaptive-learning roadmap (docs/adr/0001): specialist artifacts.

Typed roles that read the point-in-time archive and produce structured
assessments with provenance. They propose nothing, write nothing back, and have
no path to changing what the system trades — L1 comes before the risk veto
deliberately, so that nothing capable of proposing exists before the thing
capable of refusing.

Two constraints are enforced in code rather than documented:

- A specialist is handed a `PointInTimeArchive`, never the journal, and that
  object exposes only as-of reads. There is no method returning the corrected
  series, so no role can accidentally consult one.
- A `Claim` with no evidence raises at construction. The phase exists to
  establish whether the arguments are reproducible from the archive, and an
  untraceable claim is not reproducible, only plausible.
"""

from .archive import ArchiveCoverage, PointInTimeArchive, Query
from .models import STANCES, Argument, Assessment, Claim, EvidenceRef
from .report import build_argument, build_report
from .reproduce import ReproductionResult, assess_at, reproduce
from .roles import (
    FundamentalsSpecialist,
    MarketSpecialist,
    SentimentSpecialist,
    Specialist,
    TechnicalSpecialist,
    UnarchivedRole,
    default_roster,
)

__all__ = [
    "STANCES",
    "ArchiveCoverage",
    "Argument",
    "Assessment",
    "Claim",
    "EvidenceRef",
    "FundamentalsSpecialist",
    "MarketSpecialist",
    "PointInTimeArchive",
    "Query",
    "ReproductionResult",
    "SentimentSpecialist",
    "Specialist",
    "TechnicalSpecialist",
    "UnarchivedRole",
    "assess_at",
    "build_argument",
    "build_report",
    "default_roster",
    "reproduce",
]
