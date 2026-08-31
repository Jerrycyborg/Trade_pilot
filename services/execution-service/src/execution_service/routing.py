"""Server-side broker routing. The final say on where an order may go.

Previously `broker.py` bound one process-global adapter chosen from
environment variables, and the only lifecycle check lived in the *callers*
(the orchestrator and the strategy worker). Anything else that could reach
`POST /v1/orders` — a script, a stale service, a mistake — placed an order at
whatever broker the environment happened to configure, whatever the sleeve's
state was.

This module moves the decision to the service that actually talks to the
broker. It resolves the route from shared lifecycle state, selects the adapter
from that route, and refuses to hand a non-live route to a live adapter.

**Fail closed when the authority is missing.** With no shared lifecycle
database configured, this returns a simulated-only router. A deployment that
has not been wired up cannot reach a live venue by accident — the failure mode
is paper trading, not real orders.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass

from brokers import PaperBroker, get_broker
from lifecycle.routing import (
    ExecutionRoute,
    OrderIntent,
    RouteDecision,
    resolve_route,
)
from lifecycle.store import (
    LifecycleUnavailableError,
    PostgresLifecycleStore,
    StoreSettings,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RoutedOrder:
    """The resolved route and the adapter, if any, that may serve it."""

    decision: RouteDecision
    adapter: object | None
    adapter_name: str

    @property
    def places_order(self) -> bool:
        return self.decision.places_order and self.adapter is not None


class BrokerRouter:
    """Chooses the adapter for an order from lifecycle state, not from env vars."""

    def __init__(
        self,
        store: PostgresLifecycleStore | None = None,
        simulated: object | None = None,
        live: object | None = None,
        max_qty: int = 1000,
        store_factory: "Callable[[], PostgresLifecycleStore] | None" = None,
    ) -> None:
        self._store = store
        # When a factory is supplied the store is built lazily and retried on
        # every order. Building it once at import meant a database that was
        # briefly unreachable at startup left the service running for its whole
        # life with no roster — silently, and with no way back short of a
        # restart. A configured-but-unreachable authority must block entries
        # and keep trying, not degrade to unrouted trading.
        self._store_factory = store_factory
        # The simulated adapter is always a real PaperBroker: a PAPER sleeve
        # must produce genuine simulated fills, P&L and shortfall records, not
        # a no-op that reports success.
        self._simulated = simulated or PaperBroker(max_qty=max_qty)
        self._live = live
        self._live_resolved = live is not None

    # ------------------------------------------------------------------
    def _live_adapter(self):
        """The real-money adapter, built lazily and only when actually needed.

        Constructing it eagerly would create a live client in every process
        that imports this module, including tests.
        """
        if not self._live_resolved:
            adapter = get_broker(max_qty=1000)
            # get_broker falls back to PaperBroker when no credentials are
            # present. That is a simulated adapter, not a live one, and calling
            # it "live" would let a PAPER sleeve's assertions pass vacuously.
            self._live = None if isinstance(adapter, PaperBroker) else adapter
            self._live_resolved = True
        return self._live

    @property
    def live_adapter_available(self) -> bool:
        return self._live_adapter() is not None

    def _live_mode_enabled(self, account_id: str) -> bool:
        if self._store is None:
            return False
        return self._store.live_mode_enabled(account_id)

    # ------------------------------------------------------------------
    def _resolve_store(self) -> PostgresLifecycleStore | None:
        """The authority, building it on demand when a factory was supplied."""
        if self._store is not None or self._store_factory is None:
            return self._store
        try:
            self._store = self._store_factory()
        except Exception as exc:
            logger.error("Lifecycle authority still unreachable: %s", exc)
            return None
        logger.info("Lifecycle authority connected")
        return self._store

    def route(
        self,
        *,
        strategy_id: str,
        symbol: str,
        account_id: str,
        reduce_only: bool,
    ) -> RoutedOrder:
        """Resolve where this order may go."""
        intent = OrderIntent.REDUCE_ONLY if reduce_only else OrderIntent.ENTRY

        store = self._resolve_store()
        if store is None and self._store_factory is not None:
            # An authority was configured and cannot be reached. Not the same
            # as "none configured": block entries, keep exits.
            return self._authority_lost(intent, "lifecycle_authority_unreachable")

        if self._store is None:
            # No shared authority. Simulated only — see the module docstring.
            decision = (
                RouteDecision(ExecutionRoute.SIMULATED, "no_lifecycle_authority_simulated_only")
                if not reduce_only
                else RouteDecision(ExecutionRoute.SIMULATED, "reduce_only_exit_to_simulated")
            )
            return RoutedOrder(decision, self._simulated, "paper")

        try:
            sleeve = self._store.get(strategy_id, symbol, account_id)
            live_mode = self._live_mode_enabled(account_id)
            halt = self._store.reconciliation_state(
                broker="paper" if not live_mode else "live",
                environment="paper" if not live_mode else "live",
                account_id=account_id,
            )
        except LifecycleUnavailableError as exc:
            return self._authority_lost(intent, str(exc))
        except Exception as exc:  # database down, network partition, bad schema
            logger.error("Lifecycle authority unreadable: %s", exc)
            return self._authority_lost(intent, f"lifecycle_unavailable: {exc}")

        if sleeve is None:
            # An unregistered sleeve has never been permitted anything. It
            # cannot hold a position either, so there is nothing to reduce.
            return RoutedOrder(
                RouteDecision(ExecutionRoute.BLOCKED, "sleeve_not_registered"), None, "none"
            )

        decision = resolve_route(
            state=sleeve.state,
            intent=intent,
            live_mode_enabled=live_mode,
            position_environment=sleeve.position_environment,
            entries_halted=halt.halted,
            halt_reason=halt.halt_reason,
            origin=getattr(sleeve, "origin", "human"),
        )
        return self._bind(decision)

    def _authority_lost(self, intent: OrderIntent, reason: str) -> RoutedOrder:
        """Losing the roster blocks entries and preserves exits.

        An exit routes to the simulated adapter rather than a live one: without
        the roster we cannot know whether this sleeve's positions are real, and
        sending a real order on a guess is the worse error.
        """
        if intent is OrderIntent.REDUCE_ONLY:
            return RoutedOrder(
                RouteDecision(ExecutionRoute.SIMULATED, f"exit_allowed_despite_{reason}"),
                self._simulated,
                "paper",
            )
        return RoutedOrder(RouteDecision(ExecutionRoute.BLOCKED, reason), None, "none")

    def _bind(self, decision: RouteDecision) -> RoutedOrder:
        """Attach the adapter the route names, and only that one."""
        if decision.route is ExecutionRoute.SIMULATED:
            return RoutedOrder(decision, self._simulated, "paper")

        if decision.route is ExecutionRoute.LIVE:
            adapter = self._live_adapter()
            if adapter is None:
                # Route says live, but no live adapter is configured. Refuse
                # rather than quietly filling on the simulator and reporting it
                # as a real fill.
                return RoutedOrder(
                    RouteDecision(ExecutionRoute.BLOCKED, "no_live_adapter_configured"),
                    None,
                    "none",
                )
            return RoutedOrder(decision, adapter, type(adapter).__name__)

        # SHADOW and BLOCKED place nothing.
        return RoutedOrder(decision, None, "none")


def build_router(max_qty: int = 1000) -> BrokerRouter:
    """The process-wide router, wired to the shared authority when configured."""
    url = os.getenv("LIFECYCLE_DATABASE_URL", "").strip()
    if not url:
        logger.warning(
            "No LIFECYCLE_DATABASE_URL: execution-service will route every order "
            "to the paper broker. Real-money execution requires the shared "
            "lifecycle authority."
        )
        return BrokerRouter(store=None, max_qty=max_qty)

    def _connect() -> PostgresLifecycleStore:
        return PostgresLifecycleStore(StoreSettings.from_env())

    try:
        return BrokerRouter(store=_connect(), max_qty=max_qty, store_factory=_connect)
    except Exception as exc:
        # Configured but not reachable yet. Entries are blocked and exits
        # preserved until a later order succeeds in connecting.
        logger.error(
            "Lifecycle authority configured but unreachable (%s). Entries are "
            "blocked until it responds; exits remain available.", exc,
        )
        return BrokerRouter(store=None, max_qty=max_qty, store_factory=_connect)
