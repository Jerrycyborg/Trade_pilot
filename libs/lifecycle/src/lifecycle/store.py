"""Shared, transactional lifecycle authority backed by PostgreSQL.

The JSON registry this replaces had three defects that made it unusable as a
control for anything long-running, all confirmed by probe before this was
written:

* it loaded state once in ``__init__`` and never re-read, so two service
  processes never saw each other's transitions;
* it rewrote the whole file, so concurrent writers clobbered each other's
  sleeves wholesale;
* ``_save()`` swallowed every exception, so a promotion whose write failed
  still returned success and still reported the new state in memory — until a
  restart silently reverted it.

Everything here is written against those three. There is no cache: every read
is a query, so any process sees a transition the instant it commits. Every
write is a single transaction guarded by an optimistic version check. Nothing
catches and discards a database error — a failed write raises, and because the
in-memory object is only built *from* the committed row, there is no state to
roll back.

PostgreSQL only, deliberately. The JSON registry remains for single-process
development and is labelled as such; it is not a fallback this class silently
degrades to, because a safety control that quietly stops being shared is worse
than one that is plainly absent.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import MetaData, Table, create_engine, select, text, update
from sqlalchemy.engine import Engine

from .routing import (
    POSITION_LIVE,
    POSITION_NONE,
    POSITION_SIMULATED,
)

logger = logging.getLogger(__name__)

SCHEMA = "lifecycle"
DEFAULT_ACCOUNT = "default"

#: States a sleeve may hold. Mirrors the CHECK constraint in migration 0001;
#: the database is the authority, this is here to fail earlier and clearer.
STATES = ("candidate", "paper", "live", "probation", "retired")


def sleeve_key(strategy_id: str, symbol: str) -> str:
    """The display identity of a sleeve. Symbol first, so a roster sorts by it."""
    return f"{symbol.upper()}:{strategy_id}"

#: Entering these states sets where the sleeve's positions live, so a later
#: reduce-only exit knows which broker to reach even after a demotion.
POSITION_ON_ENTRY = {"paper": POSITION_SIMULATED, "live": POSITION_LIVE}


class LifecycleStoreError(RuntimeError):
    """Base for every failure this module reports rather than swallows."""


class ConcurrentTransitionError(LifecycleStoreError):
    """Another process moved this sleeve first.

    Raised instead of overwriting. The caller re-reads and decides again — a
    lost update here would mean one process's demotion silently vanishing under
    another's promotion.
    """


class SleeveNotFoundError(LifecycleStoreError):
    pass


class ChallengerCannotGoLiveError(LifecycleStoreError):
    """A challenger-origin sleeve was asked to go live.

    Not "the evidence was insufficient" — categorically refused. L3's safety
    rested on a proposal having nowhere to go; once one is a sleeve, that has
    to be re-established by something the learner cannot satisfy, rather than
    by the ordinary gates it is designed to pass.

    Clearing it is a named human action (`adopt_challenger`) that is recorded
    as a transition, so the roster shows a person took responsibility.
    """


class LifecycleUnavailableError(LifecycleStoreError):
    """The authority could not be reached.

    Callers must treat this as "no new entries" and never as "carry on".
    Exits stay available: see `routing.resolve_route`.
    """


@dataclass(frozen=True)
class Sleeve:
    """A committed roster row. Only ever constructed from what the database returned."""

    id: int
    strategy_id: str
    strategy_version: str
    symbol: str
    asset_class: str
    account_id: str
    state: str
    version: int
    since: datetime
    reason: str
    probation_count: int
    position_environment: str

    origin: str = "human"
    """Who put this sleeve on the roster. 'challenger' marks one derived from a
    generated proposal, and such a sleeve is refused promotion to live by the
    store itself — not by evidence being insufficient, but categorically.

    L3's safety rested on a proposal having nowhere to go. The moment one
    becomes a sleeve that stops being true structurally and starts depending on
    the lifecycle gates, which is weaker. A barrier the learner cannot satisfy
    at all restores the stronger property: adopting a challenger is a named
    human action, recorded as a transition."""

    @property
    def key(self) -> str:
        return f"{self.symbol}:{self.strategy_id}"

    @property
    def can_enter(self) -> bool:
        """Whether this state permits opening. Says nothing about the route."""
        return self.state in ("paper", "live")


@dataclass(frozen=True)
class ReconciliationHalt:
    account_id: str
    broker: str
    environment: str
    halted: bool
    consecutive_breaks: int
    first_failure_at: datetime | None
    last_ok_at: datetime | None
    last_checked_at: datetime | None
    last_error: str
    halt_reason: str


@dataclass
class StoreSettings:
    url: str = ""
    account_id: str = DEFAULT_ACCOUNT
    #: How long a reconciliation dependency may be unreachable before entries
    #: stop. Bounded on purpose: a momentary blip should not halt trading, and
    #: a sustained outage must.
    dependency_grace_seconds: int = 600
    #: Consecutive genuine breaks before the halt latches.
    breaks_before_halt: int = 2

    @classmethod
    def from_env(cls) -> "StoreSettings":
        return cls(
            url=os.getenv("LIFECYCLE_DATABASE_URL", ""),
            account_id=os.getenv("TRADING_ACCOUNT_ID", DEFAULT_ACCOUNT),
            dependency_grace_seconds=int(
                os.getenv("RECONCILE_DEPENDENCY_GRACE_SECONDS", "600")
            ),
            breaks_before_halt=int(os.getenv("RECONCILE_BREAKS_BEFORE_HALT", "2")),
        )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class PostgresLifecycleStore:
    """The authoritative roster. Every method hits the database."""

    def __init__(self, settings: StoreSettings | None = None, engine: Engine | None = None) -> None:
        self._settings = settings or StoreSettings.from_env()
        if engine is not None:
            self._engine = engine
        else:
            if not self._settings.url:
                raise LifecycleUnavailableError(
                    "No LIFECYCLE_DATABASE_URL. The shared lifecycle authority is "
                    "PostgreSQL; the JSON registry is a single-process development "
                    "store and is not a substitute for it."
                )
            self._engine = create_engine(self._settings.url, future=True, pool_pre_ping=True)

        self._meta = MetaData(schema=SCHEMA)
        self._sleeve = Table("sleeve", self._meta, autoload_with=self._engine)
        self._transition = Table("transition", self._meta, autoload_with=self._engine)
        self._evidence = Table("evidence_snapshot", self._meta, autoload_with=self._engine)
        self._challenger = Table(
            "challenger_proposal", self._meta, autoload_with=self._engine
        )
        self._environment = Table(
            "execution_environment", self._meta, autoload_with=self._engine
        )
        self._reconciliation = Table(
            "reconciliation_state", self._meta, autoload_with=self._engine
        )
        self._journal_health = Table("journal_health", self._meta, autoload_with=self._engine)

    # ------------------------------------------------------------------
    # Reads — never cached
    # ------------------------------------------------------------------
    @property
    def settings(self) -> StoreSettings:
        return self._settings

    def _row_to_sleeve(self, row: Any) -> Sleeve:
        return Sleeve(
            id=row.id,
            strategy_id=row.strategy_id,
            strategy_version=row.strategy_version,
            symbol=row.symbol,
            asset_class=row.asset_class,
            account_id=row.account_id,
            state=row.state,
            version=row.version,
            since=_aware(row.since),
            reason=row.reason,
            probation_count=row.probation_count,
            position_environment=row.position_environment,
            origin=getattr(row, "origin", "human"),
        )

    def get(
        self, strategy_id: str, symbol: str, account_id: str | None = None
    ) -> Sleeve | None:
        account = account_id or self._settings.account_id
        stmt = select(self._sleeve).where(
            self._sleeve.c.strategy_id == strategy_id,
            self._sleeve.c.symbol == symbol.upper(),
            self._sleeve.c.account_id == account,
        )
        with self._engine.connect() as conn:
            row = conn.execute(stmt).first()
        return self._row_to_sleeve(row) if row else None

    def require(self, strategy_id: str, symbol: str, account_id: str | None = None) -> Sleeve:
        sleeve = self.get(strategy_id, symbol, account_id)
        if sleeve is None:
            raise SleeveNotFoundError(
                f"{symbol.upper()}:{strategy_id} is not registered"
            )
        return sleeve

    def all(self, account_id: str | None = None) -> list[Sleeve]:
        account = account_id or self._settings.account_id
        stmt = (
            select(self._sleeve)
            .where(self._sleeve.c.account_id == account)
            .order_by(self._sleeve.c.symbol, self._sleeve.c.strategy_id)
        )
        with self._engine.connect() as conn:
            return [self._row_to_sleeve(row) for row in conn.execute(stmt)]

    def live_sleeves(self, account_id: str | None = None) -> list[Sleeve]:
        return [s for s in self.all(account_id) if s.state == "live"]

    def transitions(self, sleeve_id: int, limit: int = 50) -> list[dict[str, Any]]:
        stmt = (
            select(self._transition)
            .where(self._transition.c.sleeve_id == sleeve_id)
            .order_by(self._transition.c.seq.desc())
            .limit(limit)
        )
        with self._engine.connect() as conn:
            return [
                {
                    "seq": r.seq,
                    "from": r.from_state,
                    "to": r.to_state,
                    "reason": r.reason,
                    "actor": r.actor,
                    "evidence_snapshot_id": r.evidence_snapshot_id,
                    "at": _aware(r.created_at).isoformat(),
                }
                for r in conn.execute(stmt)
            ]

    # ------------------------------------------------------------------
    # Execution environment (operator-controlled global switch)
    # ------------------------------------------------------------------
    def live_mode_enabled(self, account_id: str | None = None) -> bool:
        """Whether real-money execution is permitted at all.

        Absent row means disabled. Real-money execution stays off unless
        somebody deliberately turned it on and the row records who.
        """
        account = account_id or self._settings.account_id
        stmt = select(self._environment.c.live_mode_enabled).where(
            self._environment.c.account_id == account
        )
        with self._engine.connect() as conn:
            value = conn.execute(stmt).scalar()
        return bool(value)

    def set_live_mode(
        self,
        enabled: bool,
        actor: str,
        reason: str = "",
        account_id: str | None = None,
    ) -> bool:
        account = account_id or self._settings.account_id
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO lifecycle.execution_environment "
                    "(account_id, live_mode_enabled, updated_by, reason, updated_at) "
                    "VALUES (:a, :e, :u, :r, now()) "
                    "ON CONFLICT (account_id) DO UPDATE SET "
                    "live_mode_enabled = EXCLUDED.live_mode_enabled, "
                    "updated_by = EXCLUDED.updated_by, "
                    "reason = EXCLUDED.reason, updated_at = now()"
                ),
                {"a": account, "e": enabled, "u": actor, "r": reason},
            )
        logger.warning(
            "Live mode %s for account %s by %s (%s)",
            "ENABLED" if enabled else "disabled", account, actor, reason,
        )
        return enabled

    # ------------------------------------------------------------------
    # Writes — transactional, optimistically locked, never silently failing
    # ------------------------------------------------------------------
    def register(
        self,
        strategy_id: str,
        symbol: str,
        *,
        strategy_version: str = "",
        asset_class: str = "equity",
        account_id: str | None = None,
        actor: str = "operator",
        origin: str = "human",
    ) -> Sleeve:
        """Add a sleeve as a candidate, or return the existing one.

        A retired sleeve returns to candidate and keeps its probation count:
        coming back must not launder the record of why it left.
        """
        account = account_id or self._settings.account_id
        symbol = symbol.upper()

        with self._engine.begin() as conn:
            existing = conn.execute(
                select(self._sleeve)
                .where(
                    self._sleeve.c.strategy_id == strategy_id,
                    self._sleeve.c.symbol == symbol,
                    self._sleeve.c.account_id == account,
                )
                .with_for_update()
            ).first()

            if existing is not None and existing.state != "retired":
                return self._row_to_sleeve(existing)

            if existing is not None:
                new_version = existing.version + 1
                conn.execute(
                    update(self._sleeve)
                    .where(
                        self._sleeve.c.id == existing.id,
                        self._sleeve.c.version == existing.version,
                    )
                    .values(
                        state="candidate",
                        version=new_version,
                        since=_now(),
                        reason="re-registered",
                        updated_at=_now(),
                    )
                )
                self._append_transition(
                    conn, existing.id, new_version, existing.state, "candidate",
                    "re-registered", actor, None,
                )
                row = conn.execute(
                    select(self._sleeve).where(self._sleeve.c.id == existing.id)
                ).first()
                return self._row_to_sleeve(row)

            inserted = conn.execute(
                self._sleeve.insert()
                .values(
                    strategy_id=strategy_id,
                    strategy_version=strategy_version,
                    symbol=symbol,
                    asset_class=asset_class,
                    account_id=account,
                    state="candidate",
                    version=1,
                    since=_now(),
                    reason="registered",
                    origin=origin,
                    position_environment=POSITION_NONE,
                )
                .returning(self._sleeve)
            ).first()
            self._append_transition(
                conn, inserted.id, 1, "none", "candidate", "registered", actor, None
            )
            return self._row_to_sleeve(inserted)

    def _append_transition(
        self,
        conn,
        sleeve_id: int,
        seq: int,
        from_state: str,
        to_state: str,
        reason: str,
        actor: str,
        evidence_snapshot_id: int | None,
    ) -> None:
        conn.execute(
            self._transition.insert().values(
                sleeve_id=sleeve_id,
                seq=seq,
                from_state=from_state,
                to_state=to_state,
                reason=reason,
                actor=actor,
                evidence_snapshot_id=evidence_snapshot_id,
            )
        )

    def transition(
        self,
        sleeve: Sleeve,
        to_state: str,
        reason: str,
        *,
        actor: str = "system",
        evidence_snapshot_id: int | None = None,
        expected_version: int | None = None,
    ) -> Sleeve:
        """Move a sleeve, or raise. There is no third outcome.

        The update asserts the version has not moved since `sleeve` was read.
        A mismatch raises `ConcurrentTransitionError` rather than overwriting,
        because a lost update here means one process's demotion disappearing
        under another's promotion.
        """
        if to_state not in STATES:
            raise LifecycleStoreError(f"Unknown state {to_state!r}")

        expected = sleeve.version if expected_version is None else expected_version
        new_version = expected + 1
        probation_count = sleeve.probation_count + (
            1 if to_state == "probation" and sleeve.state != "probation" else 0
        )
        position_environment = POSITION_ON_ENTRY.get(
            to_state, sleeve.position_environment
        )

        with self._engine.begin() as conn:
            if to_state == "live":
                # Read the origin from the row under lock, not from the caller's
                # copy. A Sleeve is a snapshot, and a barrier that trusts the
                # object handed to it is one an in-memory edit can walk past.
                origin = conn.execute(
                    select(self._sleeve.c.origin)
                    .where(self._sleeve.c.id == sleeve.id)
                    .with_for_update()
                ).scalar()
                if origin == "challenger":
                    raise ChallengerCannotGoLiveError(
                        f"{sleeve.key} was derived from a challenger proposal and "
                        f"cannot be promoted to live. Adopting it is a human "
                        f"action: call adopt_challenger with a named actor, which "
                        f"is recorded as a transition, and then promote it on its "
                        f"own evidence."
                    )

            result = conn.execute(
                update(self._sleeve)
                .where(
                    self._sleeve.c.id == sleeve.id,
                    self._sleeve.c.version == expected,
                )
                .values(
                    state=to_state,
                    version=new_version,
                    since=_now(),
                    reason=reason,
                    probation_count=probation_count,
                    position_environment=position_environment,
                    updated_at=_now(),
                )
            )
            if result.rowcount != 1:
                raise ConcurrentTransitionError(
                    f"{sleeve.key} moved since it was read (expected version "
                    f"{expected}); re-read and decide again"
                )
            self._append_transition(
                conn, sleeve.id, new_version, sleeve.state, to_state,
                reason, actor, evidence_snapshot_id,
            )
            row = conn.execute(
                select(self._sleeve).where(self._sleeve.c.id == sleeve.id)
            ).first()

        logger.info(
            "Lifecycle %s: %s -> %s (%s, by %s)",
            sleeve.key, sleeve.state, to_state, reason, actor,
        )
        return self._row_to_sleeve(row)

    # ------------------------------------------------------------------
    # Evidence — written by the server, never from a request body
    # ------------------------------------------------------------------
    def adopt_challenger(self, sleeve: Sleeve, *, actor: str, reason: str) -> Sleeve:
        """A person takes responsibility for a challenger-derived sleeve.

        Flips `origin` to 'human', which is the only way the live barrier is
        cleared. It does not promote: the sleeve keeps its state and still has
        to earn live through the ordinary gates on its own evidence. All this
        does is stop the categorical refusal, and record who stopped it.

        `actor` is required and must name a person. An automated caller passing
        "system" here would be the learner adopting itself, which is exactly
        what constraint 4 forbids.
        """
        named = (actor or "").strip()
        if not named or named.lower() in {"system", "learner", "auto", "automation"}:
            raise LifecycleStoreError(
                "adopt_challenger needs a named human actor: adoption is the "
                "step where a person takes responsibility, and an automated "
                "caller doing it is the learner promoting itself"
            )
        if not (reason or "").strip():
            raise LifecycleStoreError("adopt_challenger needs a reason")

        with self._engine.begin() as conn:
            row = conn.execute(
                select(self._sleeve)
                .where(self._sleeve.c.id == sleeve.id)
                .with_for_update()
            ).first()
            if row is None:
                raise SleeveNotFoundError(f"sleeve {sleeve.id} is gone")
            if row.origin != "challenger":
                return self._row_to_sleeve(row)

            new_version = row.version + 1
            conn.execute(
                update(self._sleeve)
                .where(
                    self._sleeve.c.id == sleeve.id,
                    self._sleeve.c.version == row.version,
                )
                .values(origin="human", version=new_version, updated_at=_now())
            )
            # Recorded as a transition from the state to itself: nothing moved,
            # but somebody accepted a sleeve the system had refused, and that
            # belongs in the same append-only record as every other decision.
            self._append_transition(
                conn, sleeve.id, new_version, row.state, row.state,
                f"challenger adopted: {reason.strip()}", named, None,
            )
            refreshed = conn.execute(
                select(self._sleeve).where(self._sleeve.c.id == sleeve.id)
            ).first()
            return self._row_to_sleeve(refreshed)

    def record_challenger_proposal(
        self,
        *,
        campaign_id: str,
        challenger: dict[str, Any],
        deflated_sharpe_campaign: float | None = None,
        deflated_sharpe_own_search: float | None = None,
        pooled_trials: int = 0,
        out_of_sample_sharpe: float | None = None,
        survived: bool = False,
        account_id: str | None = None,
    ) -> int:
        """Persist a proposal and what the campaign concluded about it.

        Deliberately not `validation_artifact`. A challenger is a proposal and
        an artifact is a measurement, and promotion reads artifacts — putting
        something a generator produced into the table the promotion gate trusts
        is the one place it must never appear.

        Both deflated figures are stored. A reviewer reading this row months
        later needs to see that the per-run and pooled numbers differ, and by
        how much, without having to reconstruct the campaign.
        """
        account = account_id or self._settings.account_id
        with self._engine.begin() as conn:
            existing = conn.execute(
                select(self._challenger.c.id).where(
                    self._challenger.c.campaign_id == campaign_id,
                    self._challenger.c.challenger_id == challenger["challenger_id"],
                )
            ).scalar()
            if existing is not None:
                # Append-only: a proposal is a historical fact about what was
                # suggested and on what evidence. Re-recording returns the row
                # rather than editing it.
                return int(existing)
            return int(
                conn.execute(
                    self._challenger.insert()
                    .values(
                        challenger_id=challenger["challenger_id"],
                        campaign_id=campaign_id,
                        strategy_id=challenger["strategy_id"],
                        symbol=challenger["symbol"].upper(),
                        base_version=challenger.get("base_version", ""),
                        account_id=account,
                        parameters=challenger["parameters"],
                        rationale=challenger["rationale"],
                        clamped=challenger.get("clamped", []),
                        bounds_version=challenger.get("bounds_version", ""),
                        generator=challenger.get("generator", ""),
                        deflated_sharpe_campaign=deflated_sharpe_campaign,
                        deflated_sharpe_own_search=deflated_sharpe_own_search,
                        pooled_trials=pooled_trials,
                        out_of_sample_sharpe=out_of_sample_sharpe,
                        survived=survived,
                        created_at=_now(),
                    )
                    .returning(self._challenger.c.id)
                ).scalar()
            )

    def challenger_proposals(
        self,
        *,
        strategy_id: str | None = None,
        symbol: str | None = None,
        campaign_id: str | None = None,
        account_id: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """What was proposed, and what the campaign concluded."""
        conditions = []
        if strategy_id is not None:
            conditions.append(self._challenger.c.strategy_id == strategy_id)
        if symbol is not None:
            conditions.append(self._challenger.c.symbol == symbol.upper())
        if campaign_id is not None:
            conditions.append(self._challenger.c.campaign_id == campaign_id)
        if account_id is not None:
            conditions.append(self._challenger.c.account_id == account_id)

        stmt = (
            select(self._challenger)
            .where(*conditions)
            .order_by(self._challenger.c.created_at.desc())
            .limit(limit)
        )
        with self._engine.connect() as conn:
            return [
                {
                    "id": r.id,
                    "challenger_id": r.challenger_id,
                    "campaign_id": r.campaign_id,
                    "strategy_id": r.strategy_id,
                    "symbol": r.symbol,
                    "base_version": r.base_version,
                    "parameters": r.parameters,
                    "rationale": r.rationale,
                    "clamped": r.clamped,
                    "bounds_version": r.bounds_version,
                    "generator": r.generator,
                    "deflated_sharpe_campaign": r.deflated_sharpe_campaign,
                    "deflated_sharpe_own_search": r.deflated_sharpe_own_search,
                    "pooled_trials": r.pooled_trials,
                    "out_of_sample_sharpe": r.out_of_sample_sharpe,
                    "survived": bool(r.survived),
                    "created_at": _aware(r.created_at).isoformat(),
                }
                for r in conn.execute(stmt)
            ]

    def record_evidence(
        self,
        *,
        strategy_id: str,
        strategy_version: str,
        symbol: str,
        asset_class: str,
        environment: str,
        broker: str,
        account_id: str,
        portfolio_id: str,
        window_start: datetime,
        window_end: datetime,
        metrics: dict[str, Any],
        source_artifacts: list[dict[str, Any]],
        data_version: str = "",
        model_version: str = "",
    ) -> int:
        """Store an immutable evidence snapshot. Returns its id.

        The content hash covers the scope, the metrics and the artifact
        references together, so a reviewer can later establish that the numbers
        behind a promotion are the ones that were actually measured.
        """
        payload = {
            "scope": {
                "strategy_id": strategy_id,
                "strategy_version": strategy_version,
                "symbol": symbol.upper(),
                "asset_class": asset_class,
                "environment": environment,
                "broker": broker,
                "account_id": account_id,
                "portfolio_id": portfolio_id,
                "window_start": window_start.isoformat(),
                "window_end": window_end.isoformat(),
                "data_version": data_version,
                "model_version": model_version,
            },
            "metrics": metrics,
            "source_artifacts": source_artifacts,
        }
        content_hash = hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()

        with self._engine.begin() as conn:
            row = conn.execute(
                self._evidence.insert()
                .values(
                    strategy_id=strategy_id,
                    strategy_version=strategy_version,
                    symbol=symbol.upper(),
                    asset_class=asset_class,
                    environment=environment,
                    broker=broker,
                    account_id=account_id,
                    portfolio_id=portfolio_id,
                    window_start=window_start,
                    window_end=window_end,
                    data_version=data_version,
                    model_version=model_version,
                    metrics=metrics,
                    source_artifacts=source_artifacts,
                    content_hash=content_hash,
                )
                .returning(self._evidence.c.id)
            ).scalar_one()
        return int(row)

    def evidence(self, snapshot_id: int) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(self._evidence).where(self._evidence.c.id == snapshot_id)
            ).first()
        if row is None:
            return None
        return {
            "id": row.id,
            "scope": {
                "strategy_id": row.strategy_id,
                "strategy_version": row.strategy_version,
                "symbol": row.symbol,
                "asset_class": row.asset_class,
                "environment": row.environment,
                "broker": row.broker,
                "account_id": row.account_id,
                "portfolio_id": row.portfolio_id,
                "window_start": _aware(row.window_start).isoformat(),
                "window_end": _aware(row.window_end).isoformat(),
                "data_version": row.data_version,
                "model_version": row.model_version,
            },
            "metrics": row.metrics,
            "source_artifacts": row.source_artifacts,
            "content_hash": row.content_hash,
            "created_at": _aware(row.created_at).isoformat(),
        }

    # ------------------------------------------------------------------
    # Reconciliation halt — persisted, latched, operator-cleared
    # ------------------------------------------------------------------
    def reconciliation_state(
        self, broker: str, environment: str, account_id: str | None = None
    ) -> ReconciliationHalt:
        """The halt latch as the database holds it.

        Previously this lived in an instance attribute, so restarting the
        orchestrator cleared a halt no human had cleared. An absent row means
        "nothing has gone wrong yet", not "everything is fine" — the difference
        matters only for the first check, which sets it either way.
        """
        account = account_id or self._settings.account_id
        stmt = select(self._reconciliation).where(
            self._reconciliation.c.account_id == account,
            self._reconciliation.c.broker == broker,
            self._reconciliation.c.environment == environment,
        )
        with self._engine.connect() as conn:
            row = conn.execute(stmt).first()

        if row is None:
            return ReconciliationHalt(
                account_id=account, broker=broker, environment=environment,
                halted=False, consecutive_breaks=0, first_failure_at=None,
                last_ok_at=None, last_checked_at=None, last_error="", halt_reason="",
            )
        return ReconciliationHalt(
            account_id=row.account_id,
            broker=row.broker,
            environment=row.environment,
            halted=row.halted,
            consecutive_breaks=row.consecutive_breaks,
            first_failure_at=_aware(row.first_failure_at),
            last_ok_at=_aware(row.last_ok_at),
            last_checked_at=_aware(row.last_checked_at),
            last_error=row.last_error,
            halt_reason=row.halt_reason,
        )

    def record_reconciliation(
        self,
        *,
        broker: str,
        environment: str,
        ok: bool,
        breaks: int = 0,
        error: str = "",
        dependency_available: bool = True,
        account_id: str | None = None,
        now: datetime | None = None,
    ) -> ReconciliationHalt:
        """Record one check and update the latch. Returns the resulting state.

        Two distinct failure modes, handled differently on purpose:

        *A genuine break* — the broker and the ledger disagree — latches the
        halt once it has survived `breaks_before_halt` consecutive checks. A
        single mismatch is normal while a fill is in flight.

        *An unavailable dependency* — we cannot see the broker at all — is not
        a divergence, so it does not count breaks. But it cannot be ignored
        either: after `dependency_grace_seconds` of not being able to check,
        continuing to open positions is the unsafe choice, so it latches too.
        `first_failure_at` is what makes that window measurable across
        restarts.

        Neither ever blocks a reduce-only exit; `routing.resolve_route` decides
        that, and it does not consult this at all for an exit.
        """
        account = account_id or self._settings.account_id
        moment = now or _now()
        current = self.reconciliation_state(broker, environment, account)

        first_failure_at = current.first_failure_at
        consecutive = current.consecutive_breaks
        halted = current.halted
        halt_reason = current.halt_reason

        if not dependency_available:
            # Cannot see the broker. Start (or continue) the grace clock.
            if first_failure_at is None:
                first_failure_at = moment
            unavailable_for = (moment - first_failure_at).total_seconds()
            if unavailable_for >= self._settings.dependency_grace_seconds:
                halted = True
                halt_reason = (
                    f"reconciliation dependency unavailable for "
                    f"{int(unavailable_for)}s (grace "
                    f"{self._settings.dependency_grace_seconds}s): {error}"
                )
        elif ok:
            consecutive = 0
            first_failure_at = None
            # A latched halt is NOT cleared by a good check. It took a human to
            # cause it and it takes a human to clear it — otherwise a flapping
            # break silently un-halts between checks.
        else:
            consecutive += 1
            if first_failure_at is None:
                first_failure_at = moment
            if consecutive >= self._settings.breaks_before_halt:
                halted = True
                halt_reason = (
                    f"{breaks} position break(s) persisted across {consecutive} checks"
                )

        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO lifecycle.reconciliation_state "
                    "(account_id, broker, environment, halted, consecutive_breaks, "
                    " first_failure_at, last_ok_at, last_checked_at, last_error, "
                    " halt_reason, updated_at) "
                    "VALUES (:a, :b, :e, :h, :c, :ff, :lo, :lc, :err, :hr, now()) "
                    "ON CONFLICT (account_id, broker, environment) DO UPDATE SET "
                    "halted = EXCLUDED.halted, "
                    "consecutive_breaks = EXCLUDED.consecutive_breaks, "
                    "first_failure_at = EXCLUDED.first_failure_at, "
                    "last_ok_at = COALESCE(EXCLUDED.last_ok_at, "
                    "                      lifecycle.reconciliation_state.last_ok_at), "
                    "last_checked_at = EXCLUDED.last_checked_at, "
                    "last_error = EXCLUDED.last_error, "
                    "halt_reason = EXCLUDED.halt_reason, updated_at = now()"
                ),
                {
                    "a": account, "b": broker, "e": environment,
                    "h": halted, "c": consecutive,
                    "ff": first_failure_at,
                    "lo": moment if (ok and dependency_available) else None,
                    "lc": moment, "err": error, "hr": halt_reason,
                },
            )

        if halted and not current.halted:
            logger.error(
                "ENTRIES HALTED for %s/%s/%s: %s. Exits remain available.",
                account, broker, environment, halt_reason,
            )
        return self.reconciliation_state(broker, environment, account)

    def halt_entries(
        self,
        *,
        broker: str,
        environment: str,
        reason: str,
        account_id: str | None = None,
    ) -> ReconciliationHalt:
        """Latch the entry halt immediately, for a cause already proven persistent.

        `record_reconciliation` deliberately requires a break to survive several
        consecutive checks, because a single mismatch is normal while a fill is
        in flight. A journal gap that has outlived its grace period has already
        met that bar by a different route, and routing it through the counter
        again would delay the halt by another sweep interval or two. Clearing it
        still takes a named operator.
        """
        account = account_id or self._settings.account_id
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO lifecycle.reconciliation_state "
                    "(account_id, broker, environment, halted, consecutive_breaks, "
                    " first_failure_at, last_checked_at, last_error, halt_reason, updated_at) "
                    "VALUES (:a, :b, :e, TRUE, 1, now(), now(), :r, :r, now()) "
                    "ON CONFLICT (account_id, broker, environment) DO UPDATE SET "
                    "halted = TRUE, "
                    "first_failure_at = COALESCE("
                    "    lifecycle.reconciliation_state.first_failure_at, now()), "
                    "last_checked_at = now(), last_error = EXCLUDED.last_error, "
                    "halt_reason = EXCLUDED.halt_reason, updated_at = now()"
                ),
                {"a": account, "b": broker, "e": environment, "r": reason},
            )
        logger.error(
            "ENTRIES HALTED for %s/%s/%s: %s. Exits remain available.",
            account, broker, environment, reason,
        )
        return self.reconciliation_state(broker, environment, account)

    def clear_reconciliation_halt(
        self,
        *,
        broker: str,
        environment: str,
        actor: str,
        reason: str,
        account_id: str | None = None,
    ) -> ReconciliationHalt:
        """Clear a latched halt. Requires a named actor and a reason.

        Deliberately not automatic and deliberately not a restart: a break that
        nobody looked at is a break that is still there.
        """
        if not actor.strip():
            raise LifecycleStoreError("Clearing a halt requires an identified actor")
        account = account_id or self._settings.account_id
        with self._engine.begin() as conn:
            result = conn.execute(
                update(self._reconciliation)
                .where(
                    self._reconciliation.c.account_id == account,
                    self._reconciliation.c.broker == broker,
                    self._reconciliation.c.environment == environment,
                )
                .values(
                    halted=False,
                    consecutive_breaks=0,
                    first_failure_at=None,
                    halt_reason="",
                    cleared_by=actor,
                    cleared_at=_now(),
                    updated_at=_now(),
                )
            )
            if result.rowcount != 1:
                raise LifecycleStoreError(
                    f"No reconciliation state for {account}/{broker}/{environment}"
                )
        logger.warning(
            "Reconciliation halt cleared for %s/%s/%s by %s: %s",
            account, broker, environment, actor, reason,
        )
        return self.reconciliation_state(broker, environment, account)

    # ------------------------------------------------------------------
    # Journal health
    # ------------------------------------------------------------------
    def record_journal_health(
        self,
        *,
        scope_key: str,
        window_start: datetime,
        window_end: datetime,
        expected_observations: int,
        actual_observations: int,
        gap_count: int,
        strategy_id: str = "",
        symbol: str = "",
        environment: str = "paper",
        last_gap_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Record completeness for one scope and window.

        A gap does not stop an open position from being managed — that would
        turn a data problem into an unmanaged exposure. It does make the window
        ineligible for learning and unusable as promotion evidence, and after a
        grace period it stops new entries.
        """
        eligible = gap_count == 0 and actual_observations >= expected_observations
        status = "ok" if eligible else ("gap" if gap_count else "incomplete")

        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO lifecycle.journal_health "
                    "(scope_key, strategy_id, symbol, environment, window_start, "
                    " window_end, expected_observations, actual_observations, "
                    " gap_count, eligible_for_learning, last_gap_at, status, updated_at) "
                    "VALUES (:k, :st, :sy, :e, :ws, :we, :exp, :act, :g, :el, :lg, :s, now()) "
                    "ON CONFLICT (scope_key, window_start, window_end) DO UPDATE SET "
                    "expected_observations = EXCLUDED.expected_observations, "
                    "actual_observations = EXCLUDED.actual_observations, "
                    "gap_count = EXCLUDED.gap_count, "
                    "eligible_for_learning = EXCLUDED.eligible_for_learning, "
                    "last_gap_at = EXCLUDED.last_gap_at, "
                    "status = EXCLUDED.status, updated_at = now()"
                ),
                {
                    "k": scope_key, "st": strategy_id, "sy": symbol.upper(),
                    "e": environment, "ws": window_start, "we": window_end,
                    "exp": expected_observations, "act": actual_observations,
                    "g": gap_count, "el": eligible, "lg": last_gap_at, "s": status,
                },
            )

        if not eligible:
            logger.warning(
                "Journal health %s for %s (%s): %d/%d observations, %d gap(s). "
                "Window is not eligible for learning or promotion evidence.",
                status, scope_key, environment, actual_observations,
                expected_observations, gap_count,
            )
        return {
            "scope_key": scope_key,
            "status": status,
            "eligible_for_learning": eligible,
            "gap_count": gap_count,
        }


    # ------------------------------------------------------------------
    # Validation artifacts — written when a validation runs, cited later
    # ------------------------------------------------------------------
    def record_validation_artifact(
        self,
        *,
        kind: str,
        strategy_id: str,
        symbol: str,
        environment: str,
        window_start: datetime,
        window_end: datetime,
        payload: dict[str, Any],
        strategy_version: str = "",
        asset_class: str = "equity",
        account_id: str | None = None,
        data_version: str = "",
        model_version: str = "",
        produced_by: str = "",
    ) -> int:
        """Store a validation result so a later promotion can cite it.

        Written when the validation actually runs. A promotion names the id; it
        does not carry the numbers, which is the whole point — see
        `lifecycle.evidence`.
        """
        account = account_id or self._settings.account_id
        content_hash = hashlib.sha256(
            json.dumps(
                {
                    "kind": kind,
                    "strategy_id": strategy_id,
                    "strategy_version": strategy_version,
                    "symbol": symbol.upper(),
                    "environment": environment,
                    "window_start": window_start.isoformat(),
                    "window_end": window_end.isoformat(),
                    "payload": payload,
                },
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        ).hexdigest()

        artifact = Table("validation_artifact", self._meta, autoload_with=self._engine)
        with self._engine.begin() as conn:
            new_id = conn.execute(
                artifact.insert()
                .values(
                    kind=kind,
                    strategy_id=strategy_id,
                    strategy_version=strategy_version,
                    symbol=symbol.upper(),
                    asset_class=asset_class,
                    environment=environment,
                    account_id=account,
                    window_start=window_start,
                    window_end=window_end,
                    data_version=data_version,
                    model_version=model_version,
                    payload=payload,
                    content_hash=content_hash,
                    produced_by=produced_by,
                )
                .returning(artifact.c.id)
            ).scalar_one()
        return int(new_id)

    def validation_artifact(self, artifact_id: int) -> dict[str, Any] | None:
        artifact = Table("validation_artifact", self._meta, autoload_with=self._engine)
        with self._engine.connect() as conn:
            row = conn.execute(
                select(artifact).where(artifact.c.id == artifact_id)
            ).first()
        if row is None:
            return None
        return {
            "id": row.id,
            "kind": row.kind,
            "strategy_id": row.strategy_id,
            "strategy_version": row.strategy_version,
            "symbol": row.symbol,
            "asset_class": row.asset_class,
            "environment": row.environment,
            "account_id": row.account_id,
            "window_start": _aware(row.window_start),
            "window_end": _aware(row.window_end),
            "data_version": row.data_version,
            "model_version": row.model_version,
            "payload": row.payload,
            "content_hash": row.content_hash,
            "produced_by": row.produced_by,
            "created_at": _aware(row.created_at),
        }
