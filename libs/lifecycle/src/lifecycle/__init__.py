"""Which (strategy, symbol) sleeves are permitted to trade real money.

A shared library rather than a module inside the orchestrator, because the
orchestrator is not the only thing that can place an order: strategy-service's
worker posts to execution-service directly. A safety control that one code path
can walk around is worse than no control, because it creates confidence that
is not warranted. Both paths read the same roster from the same state file.
"""

from .evidence import DerivedEvidence, derive_backtest_evidence, derive_paper_evidence
from .gates import GateThresholds, evaluate_to_live, evaluate_to_paper
from .registry import (
    ACTIVE_STATES,
    DEFAULT_LIVE_STRATEGY,
    TRADING_STATES,
    Evidence,
    GateResult,
    HealthCheck,
    LifecycleRegistry,
    LifecycleSettings,
    SleeveRecord,
    State,
    evaluate_health,
    evaluate_promotion,
    sleeve_key,
    summarise,
)

__all__ = [
    "DerivedEvidence",
    "GateThresholds",
    "derive_backtest_evidence",
    "derive_paper_evidence",
    "evaluate_to_live",
    "evaluate_to_paper",
    "ACTIVE_STATES",
    "DEFAULT_LIVE_STRATEGY",
    "TRADING_STATES",
    "Evidence",
    "GateResult",
    "HealthCheck",
    "LifecycleRegistry",
    "LifecycleSettings",
    "SleeveRecord",
    "State",
    "evaluate_health",
    "evaluate_promotion",
    "sleeve_key",
    "summarise",
]
