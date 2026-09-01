"""The adaptive loop learns from paper evidence but cannot deploy itself."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from challengers import LearningThresholds, run_learning_cycle


@dataclass
class Outcome:
    out_of_sample_sharpe: float = 1.4
    out_of_sample_trades: int = 40
    parameter_stability: float = 0.75
    deflated_sharpe_ratio: float = 0.98
    trial_sharpes: tuple[float, ...] = (0.1, 0.2)
    out_of_sample_returns: tuple[float, ...] = (0.01, -0.002, 0.008)
    n_trials: int = 2


class Veto:
    rejected = False
    unchecked: tuple[str, ...] = ()

    def to_dict(self):
        return {
            "rejected": self.rejected,
            "unchecked": list(self.unchecked),
            "verdict": "NO_OBJECTION",
        }


class StoreSpy:
    def __init__(self):
        self.proposals = []
        self.cycles = []

    def record_challenger_proposal(self, **values):
        self.proposals.append(values)
        return len(self.proposals)

    def record_learning_cycle(self, **values):
        self.cycles.append(values)
        return len(self.cycles)

    def register(self, *_args, **_kwargs):
        raise AssertionError("the learner must not register a sleeve")

    def transition(self, *_args, **_kwargs):
        raise AssertionError("the learner must not promote a sleeve")


def _run(store, veto=None, feedback=None):
    return run_learning_cycle(
        strategy_id="ema_rsi_macd",
        symbol="AAPL",
        base_version="v1",
        champion={"ema_fast": 20.0, "ema_slow": 50.0},
        paper_feedback=feedback or {"trades": 30, "realized_total": -25.0},
        veto_decision=veto or Veto(),
        run_walk_forward=lambda _candidate: Outcome(),
        deflate=lambda _returns, _trials: 0.97,
        store=store,
        as_of=datetime(2026, 1, 1, tzinfo=timezone.utc),
        thresholds=LearningThresholds(min_out_of_sample_trades=30),
    )


def test_cycle_records_bounded_proposals_but_has_no_deployment_authority() -> None:
    store = StoreSpy()
    report = _run(store)

    assert report.status == "RECORDED"
    assert store.proposals
    assert len(store.proposals) <= 8
    assert store.cycles
    assert report.deployment_authority is False
    assert report.promotion_authority is False
    assert all(row["survived"] for row in store.proposals)


def test_veto_rejection_stops_evaluation_and_records_the_refusal() -> None:
    store = StoreSpy()
    veto = Veto()
    veto.rejected = True
    called = False

    def should_not_run(_candidate):
        nonlocal called
        called = True
        return Outcome()

    report = run_learning_cycle(
        strategy_id="ema_rsi_macd",
        symbol="AAPL",
        base_version="v1",
        champion={"ema_fast": 20.0, "ema_slow": 50.0},
        paper_feedback={"trades": 30},
        veto_decision=veto,
        run_walk_forward=should_not_run,
        deflate=lambda _returns, _trials: 0.99,
        store=store,
        as_of=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    assert report.status == "VETOED"
    assert called is False
    assert store.proposals == []
    assert len(store.cycles) == 1


def test_unchecked_veto_and_thin_paper_history_fail_closed() -> None:
    store = StoreSpy()
    veto = Veto()
    veto.unchecked = ("coverage unavailable",)
    assert _run(store, veto=veto).status == "VETO_INCOMPLETE"
    assert _run(StoreSpy(), feedback={"trades": 3}).status == (
        "INSUFFICIENT_PAPER_EVIDENCE"
    )


def test_cycle_is_content_addressed_and_declares_no_winner() -> None:
    report = _run(StoreSpy()).to_dict()

    assert len(report["content_hash"]) == 64
    assert "winner" not in report["campaign"]
    assert "promote" not in report["qualified_proposals"]
    assert report["deployment_authority"] is False
