"""Promotion gates, evaluated against server-derived evidence.

The gate logic is much the same as before; what changed is where its inputs
come from. Previously they arrived in the request body, so the gates checked
numbers the requester had chosen. Now they read a `DerivedEvidence` the server
built from stored artifacts and journal records, and a derivation that could
not be completed is a refusal rather than a gap to be filled in.

Every threshold here is an argued default, not a law, and each is an
environment variable so it can be argued with. None of them is reachable from
an LLM prompt or a request field.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from .evidence import DerivedEvidence


@dataclass(frozen=True)
class GateThresholds:
    """The bar each step sets."""

    # candidate -> paper
    min_deflated_sharpe: float = 0.95
    min_oos_trades: int = 30
    require_positive_oos_return: bool = True

    # paper -> live
    min_paper_days: float = 20.0
    min_paper_orders: int = 20
    min_paper_fills: int = 10
    require_measured_costs: bool = True
    require_complete_journal: bool = True
    max_correlation_with_live: float = 0.7
    require_non_negative_paper_pnl: bool = True

    @classmethod
    def from_env(cls) -> "GateThresholds":
        def _f(name: str, default: float) -> float:
            return float(os.getenv(name, str(default)))

        def _i(name: str, default: int) -> int:
            return int(os.getenv(name, str(default)))

        def _b(name: str, default: bool) -> bool:
            return os.getenv(name, "true" if default else "false").lower() == "true"

        return cls(
            min_deflated_sharpe=_f("LIFECYCLE_MIN_DSR", 0.95),
            min_oos_trades=_i("LIFECYCLE_MIN_OOS_TRADES", 30),
            require_positive_oos_return=_b("LIFECYCLE_REQUIRE_POSITIVE_OOS", True),
            min_paper_days=_f("LIFECYCLE_MIN_PAPER_DAYS", 20),
            min_paper_orders=_i("LIFECYCLE_MIN_PAPER_ORDERS", 20),
            min_paper_fills=_i("LIFECYCLE_MIN_PAPER_FILLS", 10),
            require_measured_costs=_b("LIFECYCLE_REQUIRE_MEASURED_COSTS", True),
            require_complete_journal=_b("LIFECYCLE_REQUIRE_COMPLETE_JOURNAL", True),
            max_correlation_with_live=_f("LIFECYCLE_MAX_CORRELATION", 0.7),
            require_non_negative_paper_pnl=_b("LIFECYCLE_REQUIRE_PAPER_PNL", True),
        )


@dataclass
class GateResult:
    allowed: bool
    target: str | None
    passed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    @property
    def reason(self) -> str:
        return (
            f"all {len(self.passed)} gates passed"
            if self.allowed
            else "; ".join(self.failed) or "no gate evaluated"
        )


def _check(result: GateResult, ok: bool, description: str) -> None:
    (result.passed if ok else result.failed).append(description)


def _num(metrics: dict[str, Any], key: str) -> float | None:
    value = metrics.get(key)
    return None if value is None else float(value)


def evaluate_to_paper(
    evidence: DerivedEvidence, thresholds: GateThresholds | None = None
) -> GateResult:
    """candidate -> paper. Backtest evidence only; no money is involved yet."""
    t = thresholds or GateThresholds.from_env()
    result = GateResult(allowed=False, target=None)

    if not evidence.usable:
        result.failed.extend(evidence.problems)
        return result

    dsr = _num(evidence.metrics, "deflated_sharpe_ratio")
    _check(
        result,
        dsr is not None and dsr >= t.min_deflated_sharpe,
        f"deflated Sharpe ratio {dsr} >= {t.min_deflated_sharpe}"
        if dsr is not None
        else "the cited artifact has no deflated Sharpe ratio",
    )

    trades = _num(evidence.metrics, "out_of_sample_trades")
    _check(
        result,
        trades is not None and trades >= t.min_oos_trades,
        f"out-of-sample trades {trades:g} >= {t.min_oos_trades}"
        if trades is not None
        else "the cited artifact has no out-of-sample trade count",
    )

    if t.require_positive_oos_return:
        ret = _num(evidence.metrics, "out_of_sample_return_pct")
        _check(
            result,
            ret is not None and ret > 0,
            f"out-of-sample return {ret:.2%} is positive"
            if ret is not None
            else "the cited artifact has no out-of-sample return",
        )

    result.allowed = not result.failed
    result.target = "paper" if result.allowed else None
    return result


def evaluate_to_live(
    evidence: DerivedEvidence, thresholds: GateThresholds | None = None
) -> GateResult:
    """paper -> live. Every gate reads something only a paper run produces."""
    t = thresholds or GateThresholds.from_env()
    result = GateResult(allowed=False, target=None)

    if not evidence.usable:
        result.failed.extend(evidence.problems)
        return result

    m = evidence.metrics

    days = _num(m, "paper_days")
    _check(
        result,
        days is not None and days >= t.min_paper_days,
        f"paper traded for {days:.1f} days >= {t.min_paper_days}",
    )

    orders = _num(m, "paper_orders") or 0
    _check(result, orders >= t.min_paper_orders, f"{orders:g} paper orders >= {t.min_paper_orders}")

    fills = _num(m, "paper_fills") or 0
    _check(
        result,
        fills >= t.min_paper_fills,
        f"{fills:g} simulated fills >= {t.min_paper_fills}",
    )

    if t.require_measured_costs:
        shortfall = _num(m, "measured_shortfall_bps")
        _check(
            result,
            shortfall is not None,
            f"execution cost measured at {shortfall:.2f}bps"
            if shortfall is not None
            else "no measured execution cost — the backtest is still assuming one",
        )

    if t.require_complete_journal:
        complete = m.get("journal_complete")
        _check(
            result,
            complete is True,
            "journal complete over the evidence window"
            if complete
            else f"journal has {m.get('journal_gap_count')} gap(s) in the evidence window",
        )

    if t.require_non_negative_paper_pnl:
        pnl = _num(m, "realized_pnl")
        _check(
            result,
            pnl is not None and pnl >= 0,
            f"paper execution P&L {pnl} is not negative"
            if pnl is not None
            else "no paper P&L recorded",
        )

    correlation = _num(m, "max_correlation_with_live")
    _check(
        result,
        correlation is not None and correlation < t.max_correlation_with_live,
        f"correlation with live sleeves {correlation} < {t.max_correlation_with_live}"
        if correlation is not None
        else "correlation with live sleeves not measured",
    )

    result.allowed = not result.failed
    result.target = "live" if result.allowed else None
    return result
