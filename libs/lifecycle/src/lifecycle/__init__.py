"""Which (strategy, symbol) sleeves are permitted to trade real money.

A shared library rather than a module inside one service, because the
orchestrator, the strategy worker and execution-service can all reach a broker
and must agree about what is permitted. A safety control that one code path can
walk around is worse than no control, because it creates confidence that is not
warranted.

There is deliberately **one** implementation. An earlier version kept a JSON
registry alongside the PostgreSQL store as a "single-process development
option"; it was removed because a second authority is precisely the failure it
was meant to guard against — a process holding a stale roster and believing a
sleeve was live minutes after it had been demoted. With no shared authority
configured, nothing attempts an entry, which is the safe answer rather than a
convenient one.
"""

from .evidence import DerivedEvidence, derive_backtest_evidence, derive_paper_evidence
from .gates import GateResult, GateThresholds, evaluate_to_live, evaluate_to_paper
from .health import (
    HealthCheck,
    HealthThresholds,
    LiveMetrics,
    SweepResult,
    evaluate_health,
    run_health_sweep,
)
from .routing import (
    NON_LIVE_ROUTES,
    ExecutionRoute,
    OrderIntent,
    RouteDecision,
    assert_not_live,
    resolve_route,
)
from .service import (
    GateAnswer,
    LifecycleService,
    get_lifecycle_service,
    reset_lifecycle_service,
)
from .store import (
    STATES,
    ConcurrentTransitionError,
    LifecycleStoreError,
    LifecycleUnavailableError,
    PostgresLifecycleStore,
    Sleeve,
    SleeveNotFoundError,
    StoreSettings,
    sleeve_key,
)

#: The rule the live strategy-service runs. Signals from producers that do not
#: name a strategy are attributed to it.
DEFAULT_LIVE_STRATEGY = "ema_rsi_macd"

__all__ = [
    "DEFAULT_LIVE_STRATEGY",
    "NON_LIVE_ROUTES",
    "STATES",
    "ConcurrentTransitionError",
    "DerivedEvidence",
    "ExecutionRoute",
    "GateAnswer",
    "GateResult",
    "GateThresholds",
    "HealthCheck",
    "HealthThresholds",
    "LifecycleService",
    "LifecycleStoreError",
    "LifecycleUnavailableError",
    "LiveMetrics",
    "OrderIntent",
    "PostgresLifecycleStore",
    "RouteDecision",
    "Sleeve",
    "SleeveNotFoundError",
    "StoreSettings",
    "SweepResult",
    "assert_not_live",
    "derive_backtest_evidence",
    "derive_paper_evidence",
    "evaluate_health",
    "evaluate_to_live",
    "evaluate_to_paper",
    "get_lifecycle_service",
    "reset_lifecycle_service",
    "resolve_route",
    "run_health_sweep",
    "sleeve_key",
]
