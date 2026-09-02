"""One way in to the lifecycle authority, for every caller.

The orchestrator and the strategy worker each held their own JSON registry,
loaded once at boot and never re-read. execution-service was migrated to the
shared PostgreSQL store first because it is the component that actually reaches
a broker, so the safety property held — but the two callers could still
disagree with the authority about what to *attempt*, which produced orders that
were correctly refused downstream and noise in between.

This facade gives all three the same view. It also puts promotion behind
server-derived evidence in one place: a caller names a sleeve and the artifacts
to read, and the numbers come from stored records rather than from the request.

**Availability is not optional.** Every method that could gate an entry treats
an unreachable authority as a refusal. `available` reports whether the store
answered, so a caller can distinguish "not permitted" from "cannot tell" and
log accordingly — but both outcomes stop an entry, and neither stops an exit.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .evidence import derive_backtest_evidence, derive_paper_evidence
from .gates import GateResult, GateThresholds, evaluate_to_live, evaluate_to_paper
from .store import (
    ConcurrentTransitionError,
    LifecycleStoreError,
    LifecycleUnavailableError,
    PostgresLifecycleStore,
    Sleeve,
    StoreSettings,
)

logger = logging.getLogger(__name__)

#: What a sleeve is allowed to do next, one step at a time.
NEXT_STATE = {"candidate": "paper", "paper": "live", "probation": "paper"}


@dataclass(frozen=True)
class GateAnswer:
    """Whether this sleeve may open a position, and why not if not."""

    permitted: bool
    reason: str
    available: bool = True
    """False when the authority could not be reached. Still not permitted —
    but the distinction matters in a log, because one is a decision and the
    other is an outage."""


class LifecycleService:
    """The authority, plus the evidence derivation promotion depends on."""

    def __init__(
        self,
        store: PostgresLifecycleStore | None = None,
        thresholds: GateThresholds | None = None,
    ) -> None:
        self._store = store
        self._thresholds = thresholds or GateThresholds.from_env()

    # ------------------------------------------------------------------
    @property
    def store(self) -> PostgresLifecycleStore | None:
        return self._store

    @property
    def configured(self) -> bool:
        return self._store is not None

    def _require(self) -> PostgresLifecycleStore:
        if self._store is None:
            raise LifecycleUnavailableError(
                "No lifecycle authority configured. Set LIFECYCLE_DATABASE_URL; "
                "the JSON registry is a single-process development store, not a "
                "substitute for shared state."
            )
        return self._store

    # ------------------------------------------------------------------
    # The pre-flight gate
    # ------------------------------------------------------------------
    def may_open(self, strategy_id: str, symbol: str, account_id: str | None = None) -> GateAnswer:
        """Whether it is worth attempting an entry for this sleeve.

        Advisory only: execution-service decides the route and enforces it. The
        value of asking here is that a paper or unregistered sleeve's decision
        gets journalled by the caller with its full context, rather than
        arriving downstream stripped of it.
        """
        if self._store is None:
            return GateAnswer(False, "no_lifecycle_authority", available=False)

        try:
            sleeve = self._store.get(strategy_id, symbol, account_id)
        except Exception as exc:
            logger.error("Lifecycle authority unreachable: %s", exc)
            return GateAnswer(False, f"lifecycle_unavailable: {exc}", available=False)

        if sleeve is None:
            return GateAnswer(False, "sleeve_not_registered")
        if sleeve.state not in ("paper", "live"):
            return GateAnswer(False, f"sleeve_{sleeve.state}")

        # Paper is permitted, and this used to be the ladder's missing rung.
        # The router already sends a paper sleeve to the simulator and nowhere
        # else — but this advisory gate refused everything below live, so the
        # signal loop never *submitted* for a paper sleeve, and the simulated
        # fills that derive_paper_evidence reads were never produced. Every
        # promotion to live requires paper evidence, so the ladder could not be
        # climbed through normal operation at all: paper fills existed only
        # where someone posted orders to execution-service by hand.
        #
        # The live-mode switch is deliberately not consulted for paper. It
        # gates real-money routes; a paper sleeve cannot reach one whatever
        # this gate says, and tying paper trading to the live switch would
        # stop evidence accumulating exactly when it is safest to accumulate.
        try:
            if sleeve.state == "live" and not self._store.live_mode_enabled(account_id):
                return GateAnswer(False, "live_mode_disabled_by_operator")
            environment = "live" if sleeve.state == "live" else "paper"
            halt = self._store.reconciliation_state(environment, environment, account_id)
        except Exception as exc:
            return GateAnswer(False, f"lifecycle_unavailable: {exc}", available=False)

        if halt.halted:
            # The halt latch still covers paper entries: a journal gap or a
            # reconciliation break makes the record unreliable, and paper
            # evidence built on an unreliable record is not evidence.
            return GateAnswer(False, halt.halt_reason or "entries_halted")
        return GateAnswer(True, sleeve.state)

    # ------------------------------------------------------------------
    # Roster operations
    # ------------------------------------------------------------------
    def register(self, strategy_id: str, symbol: str, **kwargs: Any) -> Sleeve:
        return self._require().register(strategy_id, symbol, **kwargs)

    def get(self, strategy_id: str, symbol: str, account_id: str | None = None) -> Sleeve | None:
        return self._require().get(strategy_id, symbol, account_id)

    def all(self, account_id: str | None = None) -> list[Sleeve]:
        return self._require().all(account_id)

    def paper_challengers(self, symbol: str) -> list[Sleeve]:
        """Challenger sleeves under paper comparison for this symbol.

        Empty when no authority is configured or it cannot be reached: the
        challenger pass is research, and losing it must degrade research and
        never the champion's own processing.
        """
        if self._store is None:
            return []
        try:
            return self._store.paper_challengers(symbol)
        except Exception as exc:
            logger.error("Challenger roster unreadable: %s", exc)
            return []

    def challenger_parameters(self, challenger_id: str) -> dict[str, float] | None:
        """The recorded proposal a challenger trades, or None.

        The proposal row is the *only* source of a challenger's parameters —
        never an environment variable, never a default, never something the
        caller supplies. A challenger whose proposal cannot be found trades
        nothing, because trading it on guessed parameters would record evidence
        for a strategy nobody proposed.
        """
        if self._store is None:
            return None
        try:
            rows = self._store.challenger_proposals(
                challenger_id=challenger_id,
                account_id=self._store.settings.account_id,
                limit=1,
            )
        except Exception as exc:
            logger.error("Challenger proposal unreadable: %s", exc)
            return None
        if not rows:
            return None
        parameters = rows[0].get("parameters")
        return dict(parameters) if isinstance(parameters, dict) else None

    def start_paper_challenger(
        self,
        proposal_id: int,
        *,
        actor: str,
        reason: str,
        account_id: str | None = None,
    ) -> Sleeve:
        """Materialise one qualified proposal as a paper-only sleeve.

        This is intentionally an operator action. The scheduled learner has no
        reference to this method, and the resulting challenger-origin sleeve is
        categorically barred from live until a named human adopts it.
        """
        store = self._require()
        account = account_id or store.settings.account_id
        named_actor = (actor or "").strip()
        named_reason = (reason or "").strip()
        if not named_actor:
            raise LifecycleStoreError("paper challenger activation needs an actor")
        if not named_reason:
            raise LifecycleStoreError("paper challenger activation needs a reason")

        def require_proposal() -> dict[str, Any]:
            rows = store.challenger_proposals(
                proposal_id=proposal_id,
                account_id=account,
                limit=1,
            )
            if not rows:
                raise LifecycleStoreError(
                    f"qualified challenger proposal {proposal_id} was not found"
                )
            row = rows[0]
            if not row["survived"]:
                raise LifecycleStoreError(f"challenger proposal {proposal_id} did not qualify")
            return row

        proposal = require_proposal()
        champion = str(proposal["strategy_id"])
        challenger_id = str(proposal["challenger_id"])
        symbol = str(proposal["symbol"]).upper()
        if "@" in champion or not re.fullmatch(r"chal-[0-9a-f]{12}", challenger_id):
            raise LifecycleStoreError("challenger proposal identity is invalid")
        derived = f"{champion}@{challenger_id}"
        maximum = max(
            1,
            min(8, int(os.getenv("PAPER_MAX_ACTIVE_CHALLENGERS", "2"))),
        )

        # The cap is a decision over shared state, so reading the count and
        # committing the transition must be one serialized operation across
        # every service process. Ordinary row locks cannot protect the
        # not-yet-created second row; the account/symbol advisory lock can.
        with store.challenger_activation_lock(account, symbol):
            proposal = require_proposal()
            if (
                str(proposal["strategy_id"]) != champion
                or str(proposal["challenger_id"]) != challenger_id
                or str(proposal["symbol"]).upper() != symbol
            ):
                raise LifecycleStoreError(
                    "challenger proposal changed while activation was pending"
                )

            sleeve = store.get(derived, symbol, account)
            if sleeve is not None and (
                sleeve.origin != "challenger" or sleeve.strategy_version != challenger_id
            ):
                raise LifecycleStoreError(
                    "derived sleeve identity is already occupied by another record"
                )
            if sleeve is not None and sleeve.state == "paper":
                return sleeve
            if sleeve is not None and sleeve.state != "candidate":
                raise LifecycleStoreError(
                    f"{sleeve.key} is {sleeve.state}; only a candidate can start paper"
                )

            active = store.paper_challengers(symbol, account)
            if len(active) >= maximum:
                raise LifecycleStoreError(
                    f"paper challenger cap reached for {symbol}: {len(active)}/{maximum}"
                )

            if sleeve is None:
                sleeve = store.register(
                    derived,
                    symbol,
                    strategy_version=challenger_id,
                    account_id=account,
                    actor=named_actor,
                    origin="challenger",
                )
                if sleeve.origin != "challenger" or sleeve.strategy_version != challenger_id:
                    raise LifecycleStoreError(
                        "derived sleeve identity is already occupied by another record"
                    )

            try:
                return store.transition(
                    sleeve,
                    "paper",
                    f"qualified proposal {proposal_id}: {named_reason}",
                    actor=named_actor,
                )
            except ConcurrentTransitionError:
                refreshed = store.require(derived, symbol, account)
                if (
                    refreshed.state == "paper"
                    and refreshed.origin == "challenger"
                    and refreshed.strategy_version == challenger_id
                ):
                    return refreshed
                raise

    def demote(
        self,
        strategy_id: str,
        symbol: str,
        to: str,
        reason: str,
        actor: str = "operator",
        account_id: str | None = None,
    ) -> Sleeve:
        """Take a sleeve down. Never gated, and retried once on a lost race.

        A demotion losing to a concurrent write and giving up would leave a
        sleeve live that somebody just decided should not be.
        """
        store = self._require()
        sleeve = store.get(strategy_id, symbol, account_id)
        if sleeve is None:
            raise LifecycleUnavailableError(f"{symbol}:{strategy_id} is not registered")
        try:
            return store.transition(sleeve, to, reason, actor=actor)
        except ConcurrentTransitionError:
            fresh = store.get(strategy_id, symbol, account_id)
            if fresh is None or fresh.state == to:
                return fresh or sleeve
            return store.transition(fresh, to, reason, actor=actor)

    # ------------------------------------------------------------------
    # Promotion — evidence derived here, never accepted from a request
    # ------------------------------------------------------------------
    def promote(
        self,
        strategy_id: str,
        symbol: str,
        *,
        artifact_ids: list[int] | None = None,
        correlation_artifact_id: int | None = None,
        paper_window_days: float | None = None,
        journal: Any = None,
        actor: str = "operator",
        account_id: str | None = None,
    ) -> tuple[Sleeve | None, GateResult]:
        """Move a sleeve up one step if the evidence the server can find allows it.

        `artifact_ids` name walk-forward runs already stored; no performance
        number is read from the caller. The paper step additionally reads the
        journal for what the paper run actually did.
        """
        store = self._require()
        sleeve = store.get(strategy_id, symbol, account_id)
        if sleeve is None:
            return None, GateResult(allowed=False, target=None, failed=["sleeve is not registered"])

        target = NEXT_STATE.get(sleeve.state)
        if target is None:
            return sleeve, GateResult(
                allowed=False,
                target=None,
                failed=[
                    "already live"
                    if sleeve.state == "live"
                    else "retired sleeves must be re-registered before promotion"
                ],
            )

        if sleeve.state == "candidate":
            evidence = derive_backtest_evidence(
                store=store, sleeve=sleeve, artifact_ids=artifact_ids or []
            )
            result = evaluate_to_paper(evidence, self._thresholds)
        else:
            if journal is None:
                from journal import get_journal

                journal = get_journal()
            window = timedelta(
                days=paper_window_days
                if paper_window_days is not None
                else self._thresholds.min_paper_days * 2
            )
            evidence = derive_paper_evidence(
                store=store,
                journal=journal,
                sleeve=sleeve,
                window_start=datetime.now(timezone.utc) - window,
                correlation_artifact_id=correlation_artifact_id,
            )
            result = (
                evaluate_to_live(evidence, self._thresholds)
                if sleeve.state == "paper"
                else _probation_result(sleeve, evidence, self._thresholds)
            )

        if not result.allowed:
            return sleeve, result

        snapshot_id = store.record_evidence(
            metrics=evidence.metrics,
            source_artifacts=evidence.source_artifacts,
            **evidence.scope,
        )
        moved = store.transition(
            sleeve,
            result.target or target,
            f"promoted: {result.reason}",
            actor=actor,
            evidence_snapshot_id=snapshot_id,
        )
        return moved, result


def _probation_result(sleeve: Sleeve, evidence: Any, thresholds: GateThresholds) -> GateResult:
    """probation -> paper. Back to paper, never straight to live.

    A sleeve that broke re-earns the live gates from the paper stage; bouncing
    in and out of live on noise is how a bad week becomes a bad month.
    """
    result = GateResult(allowed=False, target=None)
    if evidence.problems:
        result.failed.extend(evidence.problems)
        return result
    result.passed.append("returning to paper to re-earn the live gates")
    result.allowed = True
    result.target = "paper"
    return result


# ---------------------------------------------------------------------------
# Process-wide instance
# ---------------------------------------------------------------------------
_service: LifecycleService | None = None


def get_lifecycle_service() -> LifecycleService:
    """The shared authority for this process, connected lazily.

    Lazy and retried rather than resolved at import: a database briefly
    unreachable at start-up must not leave a service running for its whole life
    with no roster.
    """
    global _service
    if _service is not None and _service.configured:
        return _service

    url = os.getenv("LIFECYCLE_DATABASE_URL", "").strip()
    if not url:
        if _service is None:
            logger.warning(
                "No LIFECYCLE_DATABASE_URL: this process has no shared lifecycle "
                "authority and will not attempt any entry."
            )
            _service = LifecycleService(store=None)
        return _service

    try:
        _service = LifecycleService(store=PostgresLifecycleStore(StoreSettings.from_env()))
        logger.info("Lifecycle authority connected")
    except Exception as exc:
        logger.error("Lifecycle authority unreachable (%s); entries stay blocked", exc)
        _service = LifecycleService(store=None)
    return _service


def reset_lifecycle_service(service: LifecycleService | None = None) -> None:
    """Replace the process-wide instance. For tests and for reconnecting."""
    global _service
    _service = service
