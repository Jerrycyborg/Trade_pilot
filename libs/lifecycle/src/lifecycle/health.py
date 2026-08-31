"""Demotion triggers, and the scheduled check that applies them.

Promotion is slow and needs every gate. Demotion is fast and needs any one
trigger — the asymmetry is the point: being slow to promote costs opportunity,
being slow to demote costs money.

This was previously reachable only by an operator calling an endpoint by hand,
which made "automatic demotion on decay" a claim rather than a behaviour. It is
now driven from the orchestrator's scheduler as well, so a sleeve that stops
working is taken off live whether or not anyone is watching.

Journal completeness is evaluated here too. A gap does not stop an open
position being managed — that would turn a data problem into an unmanaged
exposure — but it makes the window ineligible for learning, unusable as
promotion evidence, and after a bounded grace period it stops new entries.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HealthThresholds:
    max_live_drawdown_pct: float = 0.15
    """A hard breach. Demotes at any sample size, because it is a fact about
    money already lost rather than a statistical claim."""

    min_live_trades_before_decay_check: int = 20
    """Below this, live underperformance is noise. Demoting on it would churn a
    working strategy out of the portfolio."""

    max_sharpe_decay: float = 2.0
    """How far live Sharpe may fall below the validated out-of-sample figure
    before the sleeve is treated as broken rather than unlucky."""

    journal_gap_grace_minutes: float = 60.0
    """How long a journal gap may persist before new entries stop. Long enough
    that a brief provider outage does not halt trading; short enough that
    trading blind is bounded."""

    @classmethod
    def from_env(cls) -> "HealthThresholds":
        return cls(
            max_live_drawdown_pct=float(os.getenv("LIFECYCLE_MAX_LIVE_DRAWDOWN", "0.15")),
            min_live_trades_before_decay_check=int(
                os.getenv("LIFECYCLE_MIN_LIVE_TRADES", "20")
            ),
            max_sharpe_decay=float(os.getenv("LIFECYCLE_MAX_SHARPE_DECAY", "2.0")),
            journal_gap_grace_minutes=float(
                os.getenv("JOURNAL_GAP_GRACE_MINUTES", "60")
            ),
        )


@dataclass
class LiveMetrics:
    """What a live sleeve has actually done. Every field may be unknown."""

    trades: int = 0
    sharpe: float | None = None
    max_drawdown_pct: float | None = None
    validated_sharpe: float | None = None
    """The out-of-sample figure this sleeve was promoted on, for comparison."""


@dataclass
class HealthCheck:
    healthy: bool
    demote_to: str | None = None
    reasons: list[str] = field(default_factory=list)


def evaluate_health(
    state: str, metrics: LiveMetrics, thresholds: HealthThresholds | None = None
) -> HealthCheck:
    """Whether a live sleeve should stay live.

    Triggers are not weighed against each other. A profitable sleeve breaching
    its drawdown limit still demotes — that is a trade nobody would approve if
    asked directly.
    """
    t = thresholds or HealthThresholds.from_env()
    if state != "live":
        return HealthCheck(healthy=True)

    reasons: list[str] = []

    drawdown = metrics.max_drawdown_pct
    if drawdown is not None and drawdown > t.max_live_drawdown_pct:
        reasons.append(
            f"live drawdown {drawdown:.1%} exceeds the {t.max_live_drawdown_pct:.1%} limit"
        )

    if metrics.trades >= t.min_live_trades_before_decay_check:
        live, expected = metrics.sharpe, metrics.validated_sharpe
        if live is not None and expected is not None and (expected - live) > t.max_sharpe_decay:
            reasons.append(
                f"live Sharpe {live:.2f} is {expected - live:.2f} below the validated "
                f"{expected:.2f} over {metrics.trades} trades"
            )

    if not reasons:
        return HealthCheck(healthy=True)
    return HealthCheck(healthy=False, demote_to="probation", reasons=reasons)


# ---------------------------------------------------------------------------
# The scheduled sweep
# ---------------------------------------------------------------------------
@dataclass
class SweepResult:
    checked: int = 0
    demoted: list[str] = field(default_factory=list)
    journal_gaps: list[str] = field(default_factory=list)
    entries_blocked: bool = False
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "checked": self.checked,
            "demoted": self.demoted,
            "journal_gaps": self.journal_gaps,
            "entries_blocked": self.entries_blocked,
            "errors": self.errors,
        }


def run_health_sweep(
    service: Any,
    journal: Any,
    *,
    thresholds: HealthThresholds | None = None,
    timeframe: str = "15m",
    expected_interval_minutes: float = 15.0,
    window_hours: float = 24.0,
    now: datetime | None = None,
) -> SweepResult:
    """Check every live sleeve, and the journal behind them.

    Called on a schedule. Demotion happens here rather than being reported for
    somebody to action, because a demotion trigger that waits for a human is
    not a safety control.
    """
    t = thresholds or HealthThresholds.from_env()
    moment = now or datetime.now(timezone.utc)
    window_start = moment - timedelta(hours=window_hours)
    result = SweepResult()

    if not getattr(service, "configured", False):
        result.errors.append("no lifecycle authority configured")
        return result

    try:
        sleeves = [s for s in service.all() if s.state == "live"]
    except Exception as exc:
        result.errors.append(f"roster unreadable: {exc}")
        return result

    for sleeve in sleeves:
        result.checked += 1

        # --- journal completeness for this sleeve's symbol ------------------
        try:
            completeness = journal.completeness(
                symbol=sleeve.symbol,
                timeframe=timeframe,
                window_start=window_start,
                window_end=moment,
                expected_interval_minutes=expected_interval_minutes,
            )
        except Exception as exc:  # pragma: no cover - health must not raise
            result.errors.append(f"{sleeve.key}: completeness failed: {exc}")
            completeness = {"available": False}

        if completeness.get("available") and not completeness.get("complete"):
            result.journal_gaps.append(sleeve.key)
            _record_gap(service, sleeve, completeness, window_start, moment, t, result)

        # --- decay and breach ------------------------------------------------
        metrics = _live_metrics(service, journal, sleeve, window_start, moment)
        check = evaluate_health(sleeve.state, metrics, t)
        if check.healthy:
            continue

        reason = "; ".join(check.reasons)
        try:
            service.demote(
                sleeve.strategy_id, sleeve.symbol, check.demote_to or "probation",
                reason, actor="health-sweep",
            )
            result.demoted.append(f"{sleeve.key}: {reason}")
            logger.error("Health sweep demoted %s: %s", sleeve.key, reason)
        except Exception as exc:
            result.errors.append(f"{sleeve.key}: demotion failed: {exc}")

    return result


def _record_gap(
    service: Any,
    sleeve: Any,
    completeness: dict[str, Any],
    window_start: datetime,
    moment: datetime,
    thresholds: HealthThresholds,
    result: SweepResult,
) -> None:
    """Persist the gap and, past the grace period, halt entries.

    The halt is expressed through the reconciliation latch rather than a second
    mechanism: everything that stops entries goes through one place, and that
    place already leaves exits alone.
    """
    try:
        service.store.record_journal_health(
            scope_key=sleeve.key,
            strategy_id=sleeve.strategy_id,
            symbol=sleeve.symbol,
            environment=sleeve.position_environment or "paper",
            window_start=window_start,
            window_end=moment,
            expected_observations=completeness.get("expected_observations", 0),
            actual_observations=completeness.get("actual_observations", 0),
            gap_count=completeness.get("gap_count", 0),
        )
    except Exception as exc:  # pragma: no cover
        result.errors.append(f"{sleeve.key}: journal health not recorded: {exc}")
        return

    last_gap = completeness.get("last_gap_at")
    if not last_gap:
        return
    try:
        gap_at = datetime.fromisoformat(str(last_gap))
    except ValueError:
        return
    if gap_at.tzinfo is None:
        gap_at = gap_at.replace(tzinfo=timezone.utc)

    age_minutes = (moment - gap_at).total_seconds() / 60.0
    if age_minutes < thresholds.journal_gap_grace_minutes:
        logger.warning(
            "Journal gap for %s %.0f minutes old, inside the %.0f-minute grace period",
            sleeve.key, age_minutes, thresholds.journal_gap_grace_minutes,
        )
        return

    environment = sleeve.position_environment or "paper"
    try:
        # halt_entries, not record_reconciliation: the latter needs a break to
        # survive several consecutive checks before it latches, and the grace
        # period above has already established persistence by a different
        # route. Going through the counter as well delayed the real halt by
        # another sweep interval, while this function reported it as done —
        # the sweep said entries_blocked=True and the store said halted=False.
        halt = service.store.halt_entries(
            broker=environment,
            environment=environment,
            reason=(
                f"journal gap for {sleeve.key} unresolved for {age_minutes:.0f} "
                f"minutes (grace {thresholds.journal_gap_grace_minutes:.0f})"
            ),
        )
        result.entries_blocked = halt.halted
    except Exception as exc:  # pragma: no cover
        result.errors.append(f"{sleeve.key}: could not halt on journal gap: {exc}")


def _live_metrics(
    service: Any, journal: Any, sleeve: Any, window_start: datetime, moment: datetime
) -> LiveMetrics:
    """What this sleeve has done live, from the durable record.

    Scoped to the live environment: mixing in paper fills would let a healthy
    simulator hide a failing live sleeve.
    """
    metrics = LiveMetrics()
    try:
        execution = journal.scoped_execution_metrics(
            strategy_id=sleeve.strategy_id,
            symbol=sleeve.symbol,
            environment="live",
            account_id=sleeve.account_id,
            window_start=window_start,
            window_end=moment,
        )
    except Exception:  # pragma: no cover - health must not raise
        return metrics

    if not execution.get("available"):
        return metrics
    metrics.trades = int(execution.get("fills", 0) or 0)
    return metrics
