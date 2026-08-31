"""What the system is allowed to trade, decided from its own evidence.

The archive records what was seen, execution quality records what trading cost,
walk-forward records whether an edge survived validation, and the portfolio
records whether strategies diversify. None of that changed what the system
would actually trade: a human still decided, from opinion, and nothing removed
a strategy once it stopped working. This closes that loop.

Every (strategy, symbol) sleeve holds a lifecycle state:

    candidate -> paper -> live
                   ^        |
                   +-- probation <-+
                            |
                        retired

- **candidate** — registered, no evidence yet. Cannot trade.
- **paper** — validated on history. Runs in the live loop and records its
  decisions, but places no orders.
- **live** — permitted to place real orders.
- **probation** — was live; decayed or breached a limit. New entries blocked,
  exits always allowed.
- **retired** — off. Coming back requires re-registration, deliberately.

Three principles decide every rule below, and they are worth stating because
they are what make this a safety mechanism rather than a dashboard.

**Refuse by default.** Missing evidence is not neutral, it is a no. A gate that
cannot read the number it needs fails closed. The alternative — promoting on an
absent measurement — is how a system ends up trading something nobody checked.

**Promotion is slow, demotion is fast.** The asymmetry is deliberate. Being
slow to promote costs opportunity; being slow to demote costs money. So
promotion needs every gate to pass, and demotion needs any one trigger to fire.

**A small sample cannot promote, but a breach can always demote.** You cannot
conclude decay from five trades, so performance-based demotion waits for enough
of them. A drawdown breach is not a statistical claim — it is a fact about
money already lost — and demotes immediately, whatever the sample size.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class State(str, Enum):
    CANDIDATE = "candidate"
    PAPER = "paper"
    LIVE = "live"
    PROBATION = "probation"
    RETIRED = "retired"


#: The rule the live strategy-service runs. Signals from producers that do
#: not name a strategy are attributed to it.
DEFAULT_LIVE_STRATEGY = "ema_rsi_macd"

#: States from which a sleeve may place a real order.
TRADING_STATES = frozenset({State.LIVE})

#: States in which a sleeve still runs and records decisions.
ACTIVE_STATES = frozenset({State.PAPER, State.LIVE, State.PROBATION})


@dataclass(frozen=True)
class LifecycleSettings:
    """The bar each gate sets. Every one is an argued default, not a law."""

    enabled: bool = True

    # --- candidate -> paper -------------------------------------------
    min_deflated_sharpe: float = 0.95
    """The conventional 95% reading of the deflated Sharpe ratio. Below this a
    walk-forward result is not distinguishable from a lucky parameter search."""
    min_oos_trades: int = 30
    """Out-of-sample round trips behind the validation. Thirty is the
    rule-of-thumb point at which a sample mean starts to behave — a convention,
    not a derivation."""
    require_positive_oos_return: bool = True

    # --- paper -> live --------------------------------------------------
    min_paper_days: int = 20
    """Calendar days of recorded paper decisions before real money. Long enough
    to cross a few different market days rather than one quiet week."""
    min_paper_decisions: int = 20
    require_measured_costs: bool = True
    """Refuse to go live on an assumed slippage figure when a measured one is
    available from /v1/execution/quality."""
    max_correlation_with_live: float = 0.7
    """Above this a new sleeve is an existing live one at a larger size, and
    promoting it doubles the position while appearing to diversify."""

    # --- live -> probation ------------------------------------------------
    max_live_drawdown_pct: float = 0.15
    """A hard breach. Demotes immediately, regardless of sample size."""
    min_live_trades_before_decay_check: int = 20
    """Below this, live underperformance is noise and is not acted on."""
    max_sharpe_decay: float = 2.0
    """How far live Sharpe may fall below the validated out-of-sample figure
    before the sleeve is treated as broken rather than unlucky."""

    # --- probation --------------------------------------------------------
    max_probations_before_retirement: int = 3
    """A sleeve that keeps recovering and breaking again is not recovering."""

    state_path: Path = Path("./strategy-lifecycle.json")

    @classmethod
    def from_env(cls) -> "LifecycleSettings":
        def _float(name: str, default: float) -> float:
            return float(os.getenv(name, str(default)))

        def _int(name: str, default: int) -> int:
            return int(os.getenv(name, str(default)))

        return cls(
            enabled=os.getenv("LIFECYCLE_ENABLED", "true").lower() == "true",
            min_deflated_sharpe=_float("LIFECYCLE_MIN_DSR", 0.95),
            min_oos_trades=_int("LIFECYCLE_MIN_OOS_TRADES", 30),
            require_positive_oos_return=os.getenv(
                "LIFECYCLE_REQUIRE_POSITIVE_OOS", "true"
            ).lower()
            == "true",
            min_paper_days=_int("LIFECYCLE_MIN_PAPER_DAYS", 20),
            min_paper_decisions=_int("LIFECYCLE_MIN_PAPER_DECISIONS", 20),
            require_measured_costs=os.getenv(
                "LIFECYCLE_REQUIRE_MEASURED_COSTS", "true"
            ).lower()
            == "true",
            max_correlation_with_live=_float("LIFECYCLE_MAX_CORRELATION", 0.7),
            max_live_drawdown_pct=_float("LIFECYCLE_MAX_LIVE_DRAWDOWN", 0.15),
            min_live_trades_before_decay_check=_int("LIFECYCLE_MIN_LIVE_TRADES", 20),
            max_sharpe_decay=_float("LIFECYCLE_MAX_SHARPE_DECAY", 2.0),
            max_probations_before_retirement=_int("LIFECYCLE_MAX_PROBATIONS", 3),
            state_path=Path(os.getenv("LIFECYCLE_STATE_PATH", "./strategy-lifecycle.json")),
        )


class Evidence(BaseModel):
    """What is known about a sleeve. Every field may legitimately be unknown.

    `None` means "not measured", and every gate treats it as a refusal rather
    than a pass. That is the difference between a system that requires evidence
    and one that merely accepts it when offered.
    """

    deflated_sharpe_ratio: float | None = None
    out_of_sample_sharpe: float | None = None
    out_of_sample_return_pct: float | None = None
    out_of_sample_trades: int | None = None
    validated_at: datetime | None = None

    paper_decisions: int = 0
    paper_started_at: datetime | None = None
    measured_shortfall_bps: float | None = None
    max_correlation_with_live: float | None = None

    live_trades: int = 0
    live_sharpe: float | None = None
    live_max_drawdown_pct: float | None = None


@dataclass
class GateResult:
    """Why a promotion was or was not allowed."""

    allowed: bool
    target: State | None
    passed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    @property
    def reason(self) -> str:
        if self.allowed:
            return f"all {len(self.passed)} gates passed"
        return "; ".join(self.failed) or "no gate evaluated"


@dataclass
class SleeveRecord:
    """One (strategy, symbol) sleeve and its history."""

    strategy: str
    symbol: str
    state: State = State.CANDIDATE
    since: datetime | None = None
    reason: str = "registered"
    probation_count: int = 0
    history: list[dict[str, str]] = field(default_factory=list)

    @property
    def key(self) -> str:
        return sleeve_key(self.strategy, self.symbol)

    @property
    def can_trade(self) -> bool:
        return self.state in TRADING_STATES

    @property
    def is_active(self) -> bool:
        return self.state in ACTIVE_STATES


def sleeve_key(strategy: str, symbol: str) -> str:
    return f"{symbol.upper()}:{strategy}"


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------
def evaluate_promotion(
    record: SleeveRecord, evidence: Evidence, settings: LifecycleSettings
) -> GateResult:
    """Whether this sleeve may move up one step, and exactly why not if not.

    Promotion is one step at a time. A candidate with excellent backtest
    evidence still cannot jump to live: the paper stage exists to find the
    things a backtest cannot show, and skipping it would make the gates that
    read live measurements unreachable.
    """
    if record.state is State.CANDIDATE:
        return _gate_to_paper(evidence, settings)
    if record.state is State.PAPER:
        return _gate_to_live(evidence, settings)
    if record.state is State.PROBATION:
        return _gate_off_probation(record, evidence, settings)
    if record.state is State.LIVE:
        return GateResult(allowed=False, target=None, failed=["already live"])
    return GateResult(
        allowed=False,
        target=None,
        failed=["retired sleeves must be re-registered before they can be promoted"],
    )


def _check(
    passed: list[str], failed: list[str], ok: bool, description: str
) -> None:
    (passed if ok else failed).append(description)


def _gate_to_paper(evidence: Evidence, settings: LifecycleSettings) -> GateResult:
    """Backtest evidence only. Nothing here involves real or simulated money."""
    passed: list[str] = []
    failed: list[str] = []

    dsr = evidence.deflated_sharpe_ratio
    _check(
        passed,
        failed,
        dsr is not None and dsr >= settings.min_deflated_sharpe,
        f"deflated Sharpe ratio {_fmt(dsr)} >= {settings.min_deflated_sharpe}"
        if dsr is not None
        else "no walk-forward result on file — run --walk-forward first",
    )

    trades = evidence.out_of_sample_trades
    _check(
        passed,
        failed,
        trades is not None and trades >= settings.min_oos_trades,
        f"out-of-sample trades {trades} >= {settings.min_oos_trades}"
        if trades is not None
        else "out-of-sample trade count unknown",
    )

    if settings.require_positive_oos_return:
        oos = evidence.out_of_sample_return_pct
        _check(
            passed,
            failed,
            oos is not None and oos > 0,
            f"out-of-sample return {_pct(oos)} is positive"
            if oos is not None
            else "out-of-sample return unknown",
        )

    return GateResult(allowed=not failed, target=State.PAPER if not failed else None,
                      passed=passed, failed=failed)


def _gate_to_live(evidence: Evidence, settings: LifecycleSettings) -> GateResult:
    """Real money. Every gate here reads something only paper trading produces."""
    passed: list[str] = []
    failed: list[str] = []

    started = evidence.paper_started_at
    days = (
        (datetime.now(timezone.utc) - _aware(started)).days if started is not None else None
    )
    _check(
        passed,
        failed,
        days is not None and days >= settings.min_paper_days,
        f"paper traded for {days} days >= {settings.min_paper_days}"
        if days is not None
        else "no paper start recorded",
    )

    _check(
        passed,
        failed,
        evidence.paper_decisions >= settings.min_paper_decisions,
        f"{evidence.paper_decisions} paper decisions >= {settings.min_paper_decisions}",
    )

    if settings.require_measured_costs:
        shortfall = evidence.measured_shortfall_bps
        _check(
            passed,
            failed,
            shortfall is not None,
            f"execution cost measured at {shortfall:.2f}bps"
            if shortfall is not None
            else "no measured execution cost — the backtest is still assuming one",
        )

    correlation = evidence.max_correlation_with_live
    # Unknown correlation is a pass only when there is nothing live to correlate
    # against, which the caller signals by passing 0.0 rather than None.
    _check(
        passed,
        failed,
        correlation is not None and correlation < settings.max_correlation_with_live,
        f"correlation with live sleeves {_fmt(correlation)} < "
        f"{settings.max_correlation_with_live}"
        if correlation is not None
        else "correlation with live sleeves not measured",
    )

    return GateResult(allowed=not failed, target=State.LIVE if not failed else None,
                      passed=passed, failed=failed)


def _gate_off_probation(
    record: SleeveRecord, evidence: Evidence, settings: LifecycleSettings
) -> GateResult:
    """Back to paper, never straight back to live.

    A sleeve that broke has to re-earn the live gates from the paper stage. The
    alternative is a sleeve bouncing in and out of live on noise, which is how
    a bad week becomes a bad month.
    """
    passed: list[str] = []
    failed: list[str] = []

    _check(
        passed,
        failed,
        record.probation_count < settings.max_probations_before_retirement,
        f"probation {record.probation_count} of "
        f"{settings.max_probations_before_retirement}",
    )

    drawdown = evidence.live_max_drawdown_pct
    _check(
        passed,
        failed,
        drawdown is not None and drawdown <= settings.max_live_drawdown_pct,
        f"live drawdown {_pct(drawdown)} back within "
        f"{_pct(settings.max_live_drawdown_pct)}"
        if drawdown is not None
        else "live drawdown unknown",
    )

    return GateResult(allowed=not failed, target=State.PAPER if not failed else None,
                      passed=passed, failed=failed)


@dataclass
class HealthCheck:
    """Whether a live sleeve should stay live."""

    healthy: bool
    demote_to: State | None = None
    reasons: list[str] = field(default_factory=list)


def evaluate_health(
    record: SleeveRecord, evidence: Evidence, settings: LifecycleSettings
) -> HealthCheck:
    """Demotion triggers. Any one of them fires; they are not weighed together.

    Weighing them would let a sleeve stay live because it was profitable while
    breaching its drawdown limit, which is exactly the trade nobody would
    approve if asked directly.
    """
    if record.state is not State.LIVE:
        return HealthCheck(healthy=True)

    reasons: list[str] = []

    # A hard breach. Not a statistical claim, so no sample-size condition.
    drawdown = evidence.live_max_drawdown_pct
    if drawdown is not None and drawdown > settings.max_live_drawdown_pct:
        reasons.append(
            f"live drawdown {_pct(drawdown)} exceeds the "
            f"{_pct(settings.max_live_drawdown_pct)} limit"
        )

    # Decay. This *is* a statistical claim, so it waits for a sample.
    if evidence.live_trades >= settings.min_live_trades_before_decay_check:
        live = evidence.live_sharpe
        expected = evidence.out_of_sample_sharpe
        if live is not None and expected is not None:
            decay = expected - live
            if decay > settings.max_sharpe_decay:
                reasons.append(
                    f"live Sharpe {live:.2f} is {decay:.2f} below the validated "
                    f"{expected:.2f} over {evidence.live_trades} trades"
                )

    if not reasons:
        return HealthCheck(healthy=True)
    return HealthCheck(healthy=False, demote_to=State.PROBATION, reasons=reasons)


def _fmt(value: float | None) -> str:
    return "unknown" if value is None else f"{value:.3f}"


def _pct(value: float | None) -> str:
    return "unknown" if value is None else f"{value:.1%}"


def _aware(stamp: datetime) -> datetime:
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
class LifecycleRegistry:
    """The roster, persisted, with every transition recorded.

    State survives restarts because it has to: a sleeve demoted to probation on
    Friday must still be on probation on Monday. Losing the file would silently
    re-enable everything, so a load failure keeps the registry empty and refuses
    trading rather than defaulting to permissive.
    """

    def __init__(self, settings: LifecycleSettings | None = None) -> None:
        self._settings = settings or LifecycleSettings.from_env()
        self._lock = threading.Lock()
        self._sleeves: dict[str, SleeveRecord] = {}
        self._load()

    # -- persistence ----------------------------------------------------
    def _load(self) -> None:
        path = self._settings.state_path
        if not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            for key, payload in raw.get("sleeves", {}).items():
                self._sleeves[key] = SleeveRecord(
                    strategy=payload["strategy"],
                    symbol=payload["symbol"],
                    state=State(payload["state"]),
                    since=_parse(payload.get("since")),
                    reason=payload.get("reason", ""),
                    probation_count=int(payload.get("probation_count", 0)),
                    history=list(payload.get("history", [])),
                )
        except Exception as exc:
            # Empty is the safe failure: nothing is permitted to trade until the
            # roster is readable again. Assuming the previous states would let a
            # corrupt file re-enable a retired sleeve.
            logger.error("Lifecycle state unreadable (%s) — no sleeve may trade", exc)
            self._sleeves = {}

    def _save(self) -> None:
        path = self._settings.state_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "sleeves": {
                    key: {
                        **asdict(record),
                        "state": record.state.value,
                        "since": record.since.isoformat() if record.since else None,
                    }
                    for key, record in self._sleeves.items()
                }
            }
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(path)
        except Exception as exc:
            logger.error("Could not persist lifecycle state: %s", exc)

    # -- reads -----------------------------------------------------------
    @property
    def settings(self) -> LifecycleSettings:
        return self._settings

    def get(self, strategy: str, symbol: str) -> SleeveRecord | None:
        return self._sleeves.get(sleeve_key(strategy, symbol))

    def all(self) -> list[SleeveRecord]:
        return sorted(self._sleeves.values(), key=lambda r: r.key)

    def live_sleeves(self) -> list[SleeveRecord]:
        return [r for r in self.all() if r.state is State.LIVE]

    def can_trade(self, strategy: str, symbol: str) -> bool:
        """Whether this sleeve may place a real order.

        An unregistered sleeve cannot. That is the point of the roster: a
        strategy nobody validated does not get to trade because it happened to
        emit a signal.
        """
        if not self._settings.enabled:
            return True
        record = self.get(strategy, symbol)
        return record is not None and record.can_trade

    def gate_reason(self, strategy: str, symbol: str) -> str:
        if not self._settings.enabled:
            return "lifecycle_disabled"
        record = self.get(strategy, symbol)
        if record is None:
            return "sleeve_not_registered"
        if record.can_trade:
            return "live"
        return f"sleeve_{record.state.value}"

    # -- writes ----------------------------------------------------------
    def register(self, strategy: str, symbol: str) -> SleeveRecord:
        """Add a sleeve as a candidate. Idempotent for anything already active.

        Re-registering a retired sleeve is allowed and returns it to candidate —
        that is the deliberate re-entry path, and it resets nothing else: the
        probation count carries over so a repeatedly-broken sleeve keeps its
        record.
        """
        with self._lock:
            key = sleeve_key(strategy, symbol)
            existing = self._sleeves.get(key)
            if existing is not None and existing.state is not State.RETIRED:
                return existing
            record = SleeveRecord(
                strategy=strategy,
                symbol=symbol.upper(),
                state=State.CANDIDATE,
                since=datetime.now(timezone.utc),
                reason="re-registered" if existing else "registered",
                probation_count=existing.probation_count if existing else 0,
                history=list(existing.history) if existing else [],
            )
            self._sleeves[key] = record
            self._transition(record, State.CANDIDATE, record.reason, evidence=None)
            self._save()
            return record

    def promote(
        self, strategy: str, symbol: str, evidence: Evidence
    ) -> tuple[SleeveRecord | None, GateResult]:
        """Move a sleeve up one step, if every gate for that step passes."""
        with self._lock:
            record = self.get(strategy, symbol)
            if record is None:
                return None, GateResult(
                    allowed=False, target=None, failed=["sleeve is not registered"]
                )

            result = evaluate_promotion(record, evidence, self._settings)
            if not result.allowed or result.target is None:
                return record, result

            self._apply(record, result.target, f"promoted: {result.reason}", evidence)
            self._save()
            return record, result

    def demote(
        self,
        strategy: str,
        symbol: str,
        to: State,
        reason: str,
        evidence: Evidence | None = None,
    ) -> SleeveRecord | None:
        """Move a sleeve down. Always permitted — safety must never need a gate."""
        with self._lock:
            record = self.get(strategy, symbol)
            if record is None:
                return None
            if to is State.PROBATION and record.state is not State.PROBATION:
                record.probation_count += 1
                if record.probation_count >= self._settings.max_probations_before_retirement:
                    to = State.RETIRED
                    reason = (
                        f"{reason} (probation {record.probation_count} of "
                        f"{self._settings.max_probations_before_retirement} — retired)"
                    )
            self._apply(record, to, reason, evidence)
            self._save()
            return record

    def check_health(
        self, strategy: str, symbol: str, evidence: Evidence
    ) -> HealthCheck:
        """Evaluate a live sleeve and demote it if it has stopped working."""
        record = self.get(strategy, symbol)
        if record is None:
            return HealthCheck(healthy=True)

        check = evaluate_health(record, evidence, self._settings)
        if not check.healthy and check.demote_to is not None:
            self.demote(strategy, symbol, check.demote_to, "; ".join(check.reasons), evidence)
        return check

    # -- internals --------------------------------------------------------
    def _apply(
        self, record: SleeveRecord, to: State, reason: str, evidence: Evidence | None
    ) -> None:
        previous = record.state
        record.state = to
        record.since = datetime.now(timezone.utc)
        record.reason = reason
        self._transition(record, previous, reason, evidence)

    def _transition(
        self,
        record: SleeveRecord,
        previous: State,
        reason: str,
        evidence: Evidence | None,
    ) -> None:
        """Append to the sleeve's history and to the decision journal.

        The journal entry is what makes a transition auditable months later:
        the state alone says a sleeve is on probation, not what the numbers were
        when that call was made.
        """
        entry = {
            "at": datetime.now(timezone.utc).isoformat(),
            "from": previous.value,
            "to": record.state.value,
            "reason": reason,
        }
        record.history.append(entry)
        record.history[:] = record.history[-50:]

        logger.info(
            "Lifecycle %s: %s -> %s (%s)",
            record.key, previous.value, record.state.value, reason,
        )
        try:
            from journal import get_journal

            get_journal().record_decision(
                stage="lifecycle",
                outcome=record.state.value,
                symbol=record.symbol,
                action=record.strategy,
                reason=reason,
                inputs=evidence.model_dump(mode="json") if evidence else {},
                outputs={"from": previous.value, "to": record.state.value},
                correlation_id=record.key,
            )
        except Exception as exc:  # pragma: no cover - journalling is best effort
            logger.debug("Lifecycle transition not journalled: %s", exc)


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    return _aware(stamp)


def summarise(registry: LifecycleRegistry) -> dict[str, object]:
    """The roster, for the status endpoint and the dashboard."""
    records = registry.all()
    by_state: dict[str, int] = {}
    for record in records:
        by_state[record.state.value] = by_state.get(record.state.value, 0) + 1
    return {
        "enabled": registry.settings.enabled,
        "counts": by_state,
        "trading": [r.key for r in records if r.can_trade],
        "sleeves": [
            {
                "key": r.key,
                "strategy": r.strategy,
                "symbol": r.symbol,
                "state": r.state.value,
                "since": r.since.isoformat() if r.since else None,
                "reason": r.reason,
                "probation_count": r.probation_count,
            }
            for r in records
        ],
    }
