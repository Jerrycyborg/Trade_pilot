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
from dataclasses import dataclass, field, fields
from datetime import datetime, timedelta, timezone
from typing import Any, ClassVar

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HealthThresholds:
    max_live_drawdown_pct: float = 0.15
    """A hard breach. Demotes at any sample size, because it is a fact about
    money already lost rather than a statistical claim."""

    min_live_trades_before_decay_check: int = 20
    """Below this, live underperformance is noise. Demoting on it would churn a
    working strategy out of the portfolio."""

    sharpe_decay_sigmas: float = 1.0
    """How many standard errors below the validated figure live performance
    must fall before the sleeve is treated as broken rather than unlucky.

    This replaces a fixed absolute gap, which was the wrong shape: the same
    shortfall means something very different over 20 trades and over 500, and
    a constant cannot express that. Scaling by the estimate's own standard
    error does, and the sample size is already recorded.

    One sigma, not the conventional two, because the two errors do not cost
    the same. A false demotion parks a working sleeve in probation and is
    reversible on the next promotion. A false clean bill leaves a broken one
    trading real money. At the sample sizes a live sleeve actually reaches,
    two sigmas is a wide enough band that a sleeve running at an annualised
    -2.4 against a validated +2.5 still passes, which is not a health check.
    Evidence thresholds for *entering* live are deliberately strict; the
    threshold for stepping back from it should not be."""

    min_sharpe_decay: float = 0.5
    """A floor, in annualised Sharpe, beneath the statistical test.

    With a long enough live record, a trivially small shortfall becomes
    statistically significant, and demoting on it would churn a working sleeve
    for a difference nobody would act on if shown it directly. This number is
    a convention; the sigma band beside it is not."""

    max_losing_win_rate: float = 0.2
    """A sleeve losing money on this share of trades or fewer wins is not
    decaying, it is broken. This exists because the other two triggers can both
    go quiet on the worst case: a sleeve that only ever loses has no positive
    peak to measure a drawdown against, and one whose losses are identical has
    no variance and therefore no computable Sharpe."""

    journal_gap_grace_minutes: float = 60.0
    """How long a journal gap may persist before new entries stop. Long enough
    that a brief provider outage does not halt trading; short enough that
    trading blind is bounded."""

    #: Which environment variable overrides which field. The defaults live on
    #: the fields above and nowhere else: a second copy inside from_env is how
    #: a default gets changed in one place and silently not in the other, which
    #: is exactly what happened to sharpe_decay_sigmas while this was written.
    #:
    #: LIFECYCLE_MAX_SHARPE_DECAY is deliberately absent rather than rebound to
    #: something new. It named an absolute Sharpe gap and the check no longer
    #: uses one; a variable that quietly stops meaning what its name says is
    #: worse than a variable that is gone.
    _ENV: ClassVar[dict[str, str]] = {
        "max_live_drawdown_pct": "LIFECYCLE_MAX_LIVE_DRAWDOWN",
        "min_live_trades_before_decay_check": "LIFECYCLE_MIN_LIVE_TRADES",
        "sharpe_decay_sigmas": "LIFECYCLE_SHARPE_DECAY_SIGMAS",
        "min_sharpe_decay": "LIFECYCLE_MIN_SHARPE_DECAY",
        "max_losing_win_rate": "LIFECYCLE_MAX_LOSING_WIN_RATE",
        "journal_gap_grace_minutes": "JOURNAL_GAP_GRACE_MINUTES",
    }

    @classmethod
    def from_env(cls) -> "HealthThresholds":
        """Field defaults, overridden only where an environment variable is set.

        An unparseable value is refused rather than silently ignored. A
        LIFECYCLE_MAX_LIVE_DRAWDOWN of "fifteen percent" must not leave a
        trading system running on a default the operator believes they
        replaced.
        """
        overrides: dict[str, Any] = {}
        types = {f.name: f.type for f in fields(cls)}
        for name, variable in cls._ENV.items():
            raw = os.getenv(variable)
            if raw is None or raw.strip() == "":
                continue
            caster = int if types[name] == "int" else float
            try:
                overrides[name] = caster(raw)
            except ValueError as exc:
                raise ValueError(
                    f"{variable}={raw!r} is not a valid {caster.__name__}"
                ) from exc
        return cls(**overrides)


@dataclass
class LiveMetrics:
    """What a live sleeve has actually done. Every field may be unknown."""

    trades: int = 0
    sharpe: float | None = None
    """Per-trade. Not comparable with a backtest's annualised figure."""

    sharpe_annualised: float | None = None
    """The per-trade ratio scaled by this sleeve's observed trade frequency —
    the only one of the two that can be compared with `validated_sharpe`.
    None when the live record is too short to measure a frequency from."""

    sharpe_annualised_std_error: float | None = None
    trades_per_year: float | None = None
    span_days: float | None = None

    max_drawdown_pct: float | None = None
    validated_sharpe: float | None = None
    """The out-of-sample figure this sleeve was promoted on, for comparison.
    Annualised and bar-based, which is what forced the scaling above."""
    realized_total: float | None = None
    win_rate: float | None = None


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

    # Annualised against annualised. The raw per-trade ratio used to be
    # compared with the validated figure directly, and the two are not the same
    # kind of number: a per-trade 0.20 at ~250 trades a year is an annualised
    # 3.16, so a sleeve comfortably beating a validated 2.50 read as 2.30 below
    # it and demoted. When the live record is too short to measure a frequency
    # the trigger stays quiet rather than falling back to that comparison —
    # the drawdown and losing-outright triggers still cover the worst case, and
    # they do not need a scaling to be true.
    if metrics.trades >= t.min_live_trades_before_decay_check:
        live, expected = metrics.sharpe_annualised, metrics.validated_sharpe
        if live is not None and expected is not None:
            gap = expected - live
            band = t.min_sharpe_decay
            error = metrics.sharpe_annualised_std_error
            if error is not None:
                band = max(band, t.sharpe_decay_sigmas * error)
            if gap > band:
                reasons.append(
                    f"live Sharpe {live:.2f} annualised is {gap:.2f} below the validated "
                    f"{expected:.2f} over {metrics.trades} trades "
                    f"(demotion band {band:.2f})"
                )

    # Losing outright. Not a comparison against a validated figure and not a
    # drawdown percentage — both of those can go quiet on the worst record
    # there is, so this asks the blunt question directly.
    if (
        metrics.trades >= t.min_live_trades_before_decay_check
        and metrics.realized_total is not None
        and metrics.realized_total < 0
        and metrics.win_rate is not None
        and metrics.win_rate <= t.max_losing_win_rate
    ):
        reasons.append(
            f"losing outright: {metrics.realized_total:+.2f} over {metrics.trades} "
            f"trades at a {metrics.win_rate:.0%} win rate"
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
        # Realised round trips, not raw fills: a Sharpe or a drawdown needs
        # closed positions, and pairing them is what libs/attribution does.
        # It never crosses environments, so a healthy paper record cannot hide
        # a failing live one.
        from attribution import load_round_trips, performance_from_trades

        trips = load_round_trips(
            journal,
            strategy_id=sleeve.strategy_id,
            symbol=sleeve.symbol,
            environment="live",
            account_id=sleeve.account_id,
            window_start=window_start,
            window_end=moment,
        )
        performance = performance_from_trades(trips)
    except Exception as exc:  # pragma: no cover - health must not raise
        logger.debug("Live metrics unavailable for %s: %s", sleeve.key, exc)
        return metrics

    metrics.trades = performance["trades"]
    metrics.sharpe = performance["sharpe"]
    metrics.sharpe_annualised = performance["sharpe_annualised"]
    metrics.sharpe_annualised_std_error = performance["sharpe_annualised_std_error"]
    metrics.trades_per_year = performance["trades_per_year"]
    metrics.span_days = performance["span_days"]
    metrics.max_drawdown_pct = performance["max_drawdown_pct"]
    metrics.realized_total = performance["realized_total"]
    metrics.win_rate = performance["win_rate"]
    metrics.validated_sharpe = _validated_sharpe(service, sleeve)
    return metrics


def _validated_sharpe(service: Any, sleeve: Any) -> float | None:
    """The figure this sleeve was promoted on, for the decay comparison.

    Read from the immutable evidence snapshot attached to its promotion, so the
    comparison is against what was actually claimed rather than against a
    number someone remembers.
    """
    try:
        transitions = service.store.transitions(sleeve.id, limit=20)
    except Exception:  # pragma: no cover
        return None
    for transition in transitions:
        snapshot_id = transition.get("evidence_snapshot_id")
        if transition.get("to") != "live" or not snapshot_id:
            continue
        snapshot = service.store.evidence(snapshot_id)
        if not snapshot:
            continue
        return (snapshot.get("metrics") or {}).get("out_of_sample_sharpe")
    return None
