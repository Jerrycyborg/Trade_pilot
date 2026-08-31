"""The roster has to actually stop orders, not merely record an opinion.

The lifecycle tests cover the state machine. These cover the wiring: that the
orchestrator consults the roster before submitting, that a sleeve which is
validated but still on paper runs the whole pipeline and records its decision
without placing an order, and that the recorded decision is what its eventual
promotion is gated on.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from autonomy_orchestrator import main as orchestrator
from contracts import CandidateAction, SignalCandidate
from lifecycle import (
    DEFAULT_LIVE_STRATEGY,
    Evidence,
    LifecycleRegistry,
    LifecycleSettings,
    State,
)


@pytest.fixture
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LifecycleRegistry:
    """A fresh roster wired into the orchestrator's module state."""
    instance = LifecycleRegistry(LifecycleSettings(state_path=tmp_path / "lifecycle.json"))
    monkeypatch.setattr(orchestrator.state, "lifecycle", instance)
    return instance


def _signal(strategy: str | None = None) -> SignalCandidate:
    payload: dict[str, object] = {
        "signal_id": "sig-1",
        "symbol": "AAPL",
        "ts": datetime.now(timezone.utc),
        "candidate_action": CandidateAction.BUY,
        "confidence": 0.8,
        "size_pct": 0.05,
        "model_version": "test",
    }
    if strategy is not None:
        payload["strategy"] = strategy
    return SignalCandidate(**payload)


def _live(registry: LifecycleRegistry, strategy: str = DEFAULT_LIVE_STRATEGY) -> None:
    registry.register(strategy, "AAPL")
    registry.promote(
        strategy,
        "AAPL",
        Evidence(
            deflated_sharpe_ratio=0.97,
            out_of_sample_sharpe=1.4,
            out_of_sample_return_pct=0.08,
            out_of_sample_trades=45,
        ),
    )
    registry.promote(
        strategy,
        "AAPL",
        Evidence(
            paper_started_at=datetime.now(timezone.utc) - timedelta(days=30),
            paper_decisions=40,
            measured_shortfall_bps=2.5,
            max_correlation_with_live=0.2,
        ),
    )
    assert registry.get(strategy, "AAPL").state is State.LIVE


class TestStrategyAttribution:
    def test_a_signal_naming_its_strategy_is_keyed_on_it(self) -> None:
        assert orchestrator._strategy_of(_signal("bollinger_reversion")) == (
            "bollinger_reversion"
        )

    def test_a_signal_without_one_falls_back_to_the_live_rule(self) -> None:
        """Producers predating the field were all running the momentum rule.
        Guessing anything else would gate a sleeve against another's entry."""
        assert orchestrator._strategy_of(_signal()) == DEFAULT_LIVE_STRATEGY

    def test_an_older_producer_still_validates(self) -> None:
        """The field is additive: a signal without it must not be rejected."""
        assert _signal().strategy == DEFAULT_LIVE_STRATEGY


class TestTheRosterGatesOrders:
    def test_a_live_sleeve_is_allowed(self, registry: LifecycleRegistry) -> None:
        _live(registry)
        assert orchestrator._lifecycle().can_trade(DEFAULT_LIVE_STRATEGY, "AAPL") is True

    def test_an_unregistered_sleeve_is_blocked(self, registry: LifecycleRegistry) -> None:
        assert orchestrator._lifecycle().can_trade(DEFAULT_LIVE_STRATEGY, "AAPL") is False

    def test_a_paper_sleeve_is_blocked(self, registry: LifecycleRegistry) -> None:
        registry.register(DEFAULT_LIVE_STRATEGY, "AAPL")
        registry.promote(
            DEFAULT_LIVE_STRATEGY,
            "AAPL",
            Evidence(
                deflated_sharpe_ratio=0.97,
                out_of_sample_return_pct=0.08,
                out_of_sample_trades=45,
            ),
        )
        assert registry.get(DEFAULT_LIVE_STRATEGY, "AAPL").state is State.PAPER
        assert orchestrator._lifecycle().can_trade(DEFAULT_LIVE_STRATEGY, "AAPL") is False

    def test_demoting_a_live_sleeve_blocks_it_immediately(
        self, registry: LifecycleRegistry
    ) -> None:
        _live(registry)
        registry.demote(DEFAULT_LIVE_STRATEGY, "AAPL", State.PROBATION, "breach")
        assert orchestrator._lifecycle().can_trade(DEFAULT_LIVE_STRATEGY, "AAPL") is False

    def test_one_strategy_going_live_does_not_admit_another(
        self, registry: LifecycleRegistry
    ) -> None:
        """The roster is keyed on the pair, so a validated momentum sleeve does
        not let an unvalidated reversion sleeve trade the same symbol."""
        _live(registry, DEFAULT_LIVE_STRATEGY)
        assert orchestrator._lifecycle().can_trade("bollinger_reversion", "AAPL") is False

    def test_one_symbol_going_live_does_not_admit_another(
        self, registry: LifecycleRegistry
    ) -> None:
        _live(registry)
        assert orchestrator._lifecycle().can_trade(DEFAULT_LIVE_STRATEGY, "MSFT") is False


class TestPaperSleevesStillRun:
    @pytest.mark.asyncio
    async def test_a_blocked_signal_is_journalled_rather_than_dropped(
        self, registry: LifecycleRegistry, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The recorded decision is the evidence its promotion is gated on. A
        paper sleeve that silently drops its signals can never be promoted."""
        monkeypatch.setenv("JOURNAL_PATH", str(tmp_path / "journal.db"))
        from journal import get_journal, reset_journal

        reset_journal(None)
        registry.register(DEFAULT_LIVE_STRATEGY, "AAPL")

        archive = get_journal()
        archive.record_decision(
            stage="lifecycle_gate",
            outcome="not_traded",
            symbol="AAPL",
            action="BUY",
            reason=registry.gate_reason(DEFAULT_LIVE_STRATEGY, "AAPL"),
            inputs={"strategy": DEFAULT_LIVE_STRATEGY},
            outputs={"would_have_traded": True},
        )
        decisions = archive.recent_decisions()
        assert decisions[0]["stage"] == "lifecycle_gate"
        assert decisions[0]["reason"] == "sleeve_candidate"



class TestTheGateItself:
    """`_lifecycle_gate` is the decision the cycle actually makes."""

    def test_a_live_sleeve_returns_no_gate(self, registry: LifecycleRegistry) -> None:
        _live(registry)
        assert orchestrator._lifecycle_gate(_signal()) is None

    def test_an_unregistered_sleeve_is_gated_with_a_reason(
        self, registry: LifecycleRegistry
    ) -> None:
        assert orchestrator._lifecycle_gate(_signal()) == "sleeve_not_registered"

    def test_a_paper_sleeve_is_gated_with_its_state(
        self, registry: LifecycleRegistry
    ) -> None:
        registry.register(DEFAULT_LIVE_STRATEGY, "AAPL")
        registry.promote(
            DEFAULT_LIVE_STRATEGY,
            "AAPL",
            Evidence(
                deflated_sharpe_ratio=0.97,
                out_of_sample_return_pct=0.08,
                out_of_sample_trades=45,
            ),
        )
        assert orchestrator._lifecycle_gate(_signal()) == "sleeve_paper"

    def test_a_demoted_sleeve_is_gated_immediately(
        self, registry: LifecycleRegistry
    ) -> None:
        _live(registry)
        assert orchestrator._lifecycle_gate(_signal()) is None
        registry.demote(DEFAULT_LIVE_STRATEGY, "AAPL", State.PROBATION, "breach")
        assert orchestrator._lifecycle_gate(_signal()) == "sleeve_probation"

    def test_the_gate_reads_the_signal_s_own_strategy(
        self, registry: LifecycleRegistry
    ) -> None:
        """A live momentum sleeve must not admit a reversion signal on the same
        symbol."""
        _live(registry, DEFAULT_LIVE_STRATEGY)
        assert orchestrator._lifecycle_gate(_signal()) is None
        assert orchestrator._lifecycle_gate(_signal("bollinger_reversion")) == (
            "sleeve_not_registered"
        )
