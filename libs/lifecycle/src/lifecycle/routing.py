"""Which broker, if any, an order may reach.

Previously the broker was chosen from environment variables alone
(``get_broker()`` reading ``BROKER`` / ``ALPACA_API_KEY``) and a sleeve's
lifecycle state had no influence on it whatsoever. If credentials were present,
a CANDIDATE sleeve's order would reach the live venue exactly like a LIVE one.
The lifecycle "gate" was a boolean checked by the *callers*, so anything that
could POST to execution-service bypassed it entirely.

This module makes the route a decision rather than an accident. It is a pure
function of four inputs — the sleeve's state, whether the operator has enabled
live mode, whether the order reduces or increases exposure, and whether a
safety halt is active — so it can be exhaustively tested, and so the same rule
applies wherever it is asked.

Two rules deserve stating outright, because they are load-bearing:

**A reduce-only exit is never blocked.** Every halt in this system stops
entries. Refusing to let a position close — because reconciliation is unhappy,
because the journal has a gap, because an operator pulled live mode — turns a
bookkeeping problem into a financial one. The exit routes to wherever the
position actually is.

**Live mode off does not strand a live position.** A LIVE sleeve with live mode
disabled cannot open anything, but it can still close. That is deliberate: live
mode is the switch an operator pulls in a hurry, and the thing they most need
afterwards is the ability to get flat.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class OrderIntent(str, Enum):
    """Whether an order increases exposure or reduces it."""

    ENTRY = "entry"
    REDUCE_ONLY = "reduce_only"


class ExecutionRoute(str, Enum):
    """Where an order is permitted to go."""

    BLOCKED = "blocked"
    """No order is placed. The decision is still journalled."""

    SHADOW = "shadow"
    """Journal the decision; place nothing. A CANDIDATE sleeve's whole output."""

    SIMULATED = "simulated"
    """PaperBroker, or an approved broker sandbox. Never a live venue."""

    LIVE = "live"
    """A real broker with real money."""


#: Routes that must never touch a live venue. Asserted at the adapter boundary
#: as well as here, so a mistake in one place does not become a real order.
NON_LIVE_ROUTES = frozenset(
    {ExecutionRoute.BLOCKED, ExecutionRoute.SHADOW, ExecutionRoute.SIMULATED}
)

#: Position environments a sleeve can hold inventory in.
POSITION_NONE = "none"
POSITION_SIMULATED = "simulated"
POSITION_LIVE = "live"


@dataclass(frozen=True)
class RouteDecision:
    """The route, and why — the reason is journalled and shown to operators."""

    route: ExecutionRoute
    reason: str

    @property
    def places_order(self) -> bool:
        return self.route in (ExecutionRoute.SIMULATED, ExecutionRoute.LIVE)

    @property
    def is_live(self) -> bool:
        return self.route is ExecutionRoute.LIVE


def _position_route(position_environment: str) -> ExecutionRoute:
    """Where a sleeve's existing positions live, for a reduce-only exit."""
    if position_environment == POSITION_LIVE:
        return ExecutionRoute.LIVE
    if position_environment == POSITION_SIMULATED:
        return ExecutionRoute.SIMULATED
    # Never executed anywhere, so there is nothing to reduce.
    return ExecutionRoute.SHADOW


def resolve_route(
    *,
    state: str,
    intent: OrderIntent,
    live_mode_enabled: bool,
    position_environment: str = POSITION_NONE,
    entries_halted: bool = False,
    halt_reason: str = "",
    origin: str = "human",
) -> RouteDecision:
    """The only place the broker route is decided.

    `state` is the sleeve's lifecycle state. `live_mode_enabled` is the
    operator-controlled global switch, read from shared state rather than an
    environment variable. `entries_halted` folds in every safety latch —
    reconciliation breaks, journal gaps, the kill switch — because they all
    have the same effect and none of them touches exits.
    """
    reducing = intent is OrderIntent.REDUCE_ONLY

    # A challenger-origin sleeve is refused a live route here as well as at the
    # store. The store barrier means one should never reach state='live' in the
    # first place, so this is defence in depth against the single row that
    # matters most — and it costs one comparison. It does not touch exits: a
    # challenger that somehow holds a live position must still be able to close
    # it, and refusing that would turn a safety check into a trapped position.
    if origin == "challenger" and not reducing:
        live_mode_enabled = False

    # Exits first. Nothing below may block one.
    if reducing:
        route = _position_route(position_environment)
        if route is ExecutionRoute.SHADOW:
            return RouteDecision(
                ExecutionRoute.SHADOW,
                "reduce_only_but_no_position_environment",
            )
        return RouteDecision(route, f"reduce_only_exit_to_{route.value}")

    # From here on the order increases exposure.
    if entries_halted:
        return RouteDecision(
            ExecutionRoute.BLOCKED, halt_reason or "entries_halted"
        )

    if state == "candidate":
        # Registered but unproven: the decision is recorded and nothing is sent.
        return RouteDecision(ExecutionRoute.SHADOW, "candidate_shadow_only")

    if state == "paper":
        return RouteDecision(ExecutionRoute.SIMULATED, "paper_sleeve")

    if state == "live":
        if not live_mode_enabled:
            return RouteDecision(
                ExecutionRoute.BLOCKED, "live_mode_disabled_by_operator"
            )
        return RouteDecision(ExecutionRoute.LIVE, "live_sleeve_live_mode_enabled")

    if state in ("probation", "retired"):
        return RouteDecision(ExecutionRoute.BLOCKED, f"sleeve_{state}")

    # An unrecognised state is not a reason to guess permissively.
    return RouteDecision(ExecutionRoute.BLOCKED, f"unknown_state_{state}")


def assert_not_live(route: ExecutionRoute, adapter_name: str) -> None:
    """Guard at the adapter boundary.

    `resolve_route` already decides this, but a routing bug that reached a live
    adapter would place a real order. Two independent checks make that take two
    mistakes instead of one.
    """
    if route not in NON_LIVE_ROUTES:
        return
    raise PermissionError(
        f"Route {route.value!r} must not reach live adapter {adapter_name!r}"
    )
