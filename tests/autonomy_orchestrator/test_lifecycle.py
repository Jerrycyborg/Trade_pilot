"""Tests for the strategy lifecycle.

This is a safety mechanism, so the tests are written around the three
principles it is built on rather than around its methods:

- refuse by default — a missing measurement is a no, never a pass;
- promotion is slow, demotion is fast;
- a small sample cannot promote, but a hard breach can always demote.

If any of those stop holding, the roster stops being a control and becomes a
dashboard that happens to say "live".
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from lifecycle import (
    Evidence,
    LifecycleRegistry,
    LifecycleSettings,
    SleeveRecord,
    State,
    evaluate_health,
    evaluate_promotion,
    sleeve_key,
    summarise,
)

STRATEGY = "ema_rsi_macd"
SYMBOL = "AAPL"


@pytest.fixture
def settings(tmp_path: Path) -> LifecycleSettings:
    return LifecycleSettings(state_path=tmp_path / "lifecycle.json")


@pytest.fixture
def registry(settings: LifecycleSettings) -> LifecycleRegistry:
    return LifecycleRegistry(settings)


def _backtest_evidence(**overrides: object) -> Evidence:
    """Evidence sufficient to reach paper."""
    payload: dict[str, object] = {
        "deflated_sharpe_ratio": 0.97,
        "out_of_sample_sharpe": 1.4,
        "out_of_sample_return_pct": 0.08,
        "out_of_sample_trades": 45,
    }
    payload.update(overrides)
    return Evidence(**payload)


def _paper_evidence(**overrides: object) -> Evidence:
    """Evidence sufficient to reach live."""
    payload: dict[str, object] = {
        "paper_started_at": datetime.now(timezone.utc) - timedelta(days=30),
        "paper_decisions": 40,
        "measured_shortfall_bps": 2.5,
        "max_correlation_with_live": 0.2,
    }
    payload.update(overrides)
    return _backtest_evidence().model_copy(update=payload)


def _promote_to_live(registry: LifecycleRegistry) -> SleeveRecord:
    registry.register(STRATEGY, SYMBOL)
    registry.promote(STRATEGY, SYMBOL, _backtest_evidence())
    record, result = registry.promote(STRATEGY, SYMBOL, _paper_evidence())
    assert result.allowed, result.reason
    return record


# ---------------------------------------------------------------------------
# Refuse by default
# ---------------------------------------------------------------------------
class TestRefuseByDefault:
    def test_an_unregistered_sleeve_cannot_trade(self, registry: LifecycleRegistry) -> None:
        """A strategy nobody validated does not get to trade because it
        happened to emit a signal."""
        assert registry.can_trade(STRATEGY, "NVDA") is False
        assert registry.gate_reason(STRATEGY, "NVDA") == "sleeve_not_registered"

    def test_a_freshly_registered_sleeve_cannot_trade(
        self, registry: LifecycleRegistry
    ) -> None:
        registry.register(STRATEGY, SYMBOL)
        assert registry.can_trade(STRATEGY, SYMBOL) is False

    def test_no_evidence_fails_every_gate(self, settings: LifecycleSettings) -> None:
        record = SleeveRecord(strategy=STRATEGY, symbol=SYMBOL)
        result = evaluate_promotion(record, Evidence(), settings)
        assert result.allowed is False
        assert len(result.failed) == 3

    @pytest.mark.parametrize(
        "missing",
        ["deflated_sharpe_ratio", "out_of_sample_trades", "out_of_sample_return_pct"],
    )
    def test_any_single_missing_measurement_blocks_promotion(
        self, settings: LifecycleSettings, missing: str
    ) -> None:
        """Unknown is not neutral. Promoting on an absent measurement is how a
        system ends up trading something nobody checked."""
        record = SleeveRecord(strategy=STRATEGY, symbol=SYMBOL)
        evidence = _backtest_evidence().model_copy(update={missing: None})
        assert evaluate_promotion(record, evidence, settings).allowed is False

    def test_unmeasured_correlation_blocks_going_live(
        self, settings: LifecycleSettings
    ) -> None:
        record = SleeveRecord(strategy=STRATEGY, symbol=SYMBOL, state=State.PAPER)
        evidence = _paper_evidence().model_copy(update={"max_correlation_with_live": None})
        result = evaluate_promotion(record, evidence, settings)
        assert result.allowed is False
        assert any("correlation" in f for f in result.failed)

    def test_unreadable_state_permits_nothing(self, tmp_path: Path) -> None:
        """Losing the roster must not silently re-enable everything."""
        path = tmp_path / "lifecycle.json"
        path.write_text("{ this is not json", encoding="utf-8")
        registry = LifecycleRegistry(LifecycleSettings(state_path=path))
        assert registry.all() == []
        assert registry.can_trade(STRATEGY, SYMBOL) is False


# ---------------------------------------------------------------------------
# Promotion is slow
# ---------------------------------------------------------------------------
class TestPromotion:
    def test_backtest_evidence_reaches_paper_and_stops_there(
        self, registry: LifecycleRegistry
    ) -> None:
        """A backtest cannot show what paper trading shows, so it cannot skip it."""
        registry.register(STRATEGY, SYMBOL)
        record, result = registry.promote(STRATEGY, SYMBOL, _backtest_evidence())
        assert result.allowed
        assert record.state is State.PAPER
        assert record.can_trade is False

    def test_paper_evidence_is_needed_for_live(self, registry: LifecycleRegistry) -> None:
        registry.register(STRATEGY, SYMBOL)
        registry.promote(STRATEGY, SYMBOL, _backtest_evidence())
        _, result = registry.promote(STRATEGY, SYMBOL, _backtest_evidence())
        assert result.allowed is False
        assert any("paper" in f for f in result.failed)

    def test_the_full_ladder_reaches_live(self, registry: LifecycleRegistry) -> None:
        record = _promote_to_live(registry)
        assert record.state is State.LIVE
        assert registry.can_trade(STRATEGY, SYMBOL) is True

    def test_a_short_paper_run_does_not_qualify(self, registry: LifecycleRegistry) -> None:
        registry.register(STRATEGY, SYMBOL)
        registry.promote(STRATEGY, SYMBOL, _backtest_evidence())
        evidence = _paper_evidence(
            paper_started_at=datetime.now(timezone.utc) - timedelta(days=3)
        )
        _, result = registry.promote(STRATEGY, SYMBOL, evidence)
        assert result.allowed is False

    def test_going_live_on_assumed_costs_is_refused(
        self, registry: LifecycleRegistry
    ) -> None:
        """The measured figure exists by this point; using a guess instead is a
        choice, and not one the gate allows."""
        registry.register(STRATEGY, SYMBOL)
        registry.promote(STRATEGY, SYMBOL, _backtest_evidence())
        evidence = _paper_evidence(measured_shortfall_bps=None)
        _, result = registry.promote(STRATEGY, SYMBOL, evidence)
        assert result.allowed is False
        assert any("execution cost" in f for f in result.failed)

    def test_a_sleeve_correlated_with_a_live_one_is_refused(
        self, registry: LifecycleRegistry
    ) -> None:
        """It is the live sleeve at a larger size, not a second strategy."""
        registry.register(STRATEGY, SYMBOL)
        registry.promote(STRATEGY, SYMBOL, _backtest_evidence())
        _, result = registry.promote(
            STRATEGY, SYMBOL, _paper_evidence(max_correlation_with_live=0.85)
        )
        assert result.allowed is False
        assert any("correlation" in f for f in result.failed)

    def test_a_thin_out_of_sample_record_cannot_promote(
        self, settings: LifecycleSettings
    ) -> None:
        record = SleeveRecord(strategy=STRATEGY, symbol=SYMBOL)
        result = evaluate_promotion(record, _backtest_evidence(out_of_sample_trades=6), settings)
        assert result.allowed is False

    def test_a_losing_out_of_sample_record_cannot_promote(
        self, settings: LifecycleSettings
    ) -> None:
        record = SleeveRecord(strategy=STRATEGY, symbol=SYMBOL)
        evidence = _backtest_evidence(out_of_sample_return_pct=-0.04)
        assert evaluate_promotion(record, evidence, settings).allowed is False

    def test_promoting_an_unregistered_sleeve_says_so(
        self, registry: LifecycleRegistry
    ) -> None:
        record, result = registry.promote(STRATEGY, "TSLA", _backtest_evidence())
        assert record is None
        assert result.allowed is False

    def test_a_live_sleeve_cannot_be_promoted_further(
        self, registry: LifecycleRegistry
    ) -> None:
        _promote_to_live(registry)
        _, result = registry.promote(STRATEGY, SYMBOL, _paper_evidence())
        assert result.allowed is False
        assert "already live" in result.reason

    def test_the_reason_names_every_failed_gate(self, settings: LifecycleSettings) -> None:
        """An operator has to be able to see what to fix without reading code."""
        record = SleeveRecord(strategy=STRATEGY, symbol=SYMBOL)
        result = evaluate_promotion(record, Evidence(), settings)
        assert "walk-forward" in result.reason
        assert "trade count" in result.reason


# ---------------------------------------------------------------------------
# Demotion is fast
# ---------------------------------------------------------------------------
class TestDemotion:
    def test_a_drawdown_breach_demotes_on_any_sample_size(
        self, registry: LifecycleRegistry
    ) -> None:
        """A breach is a fact about money already lost, not a statistical claim,
        so it does not wait for a sample."""
        _promote_to_live(registry)
        check = registry.check_health(
            STRATEGY, SYMBOL, _paper_evidence(live_max_drawdown_pct=0.30, live_trades=2)
        )
        assert check.healthy is False
        assert registry.get(STRATEGY, SYMBOL).state is State.PROBATION

    def test_decay_waits_for_enough_trades(self, settings: LifecycleSettings) -> None:
        """Five bad trades is not evidence of decay, and demoting on it would
        churn a working strategy out of the portfolio."""
        record = SleeveRecord(strategy=STRATEGY, symbol=SYMBOL, state=State.LIVE)
        evidence = Evidence(live_trades=5, live_sharpe=-3.0, out_of_sample_sharpe=1.5)
        assert evaluate_health(record, evidence, settings).healthy is True

    def test_decay_acts_once_the_sample_is_there(
        self, settings: LifecycleSettings
    ) -> None:
        record = SleeveRecord(strategy=STRATEGY, symbol=SYMBOL, state=State.LIVE)
        evidence = Evidence(live_trades=50, live_sharpe=-3.0, out_of_sample_sharpe=1.5)
        check = evaluate_health(record, evidence, settings)
        assert check.healthy is False
        assert check.demote_to is State.PROBATION

    def test_mild_underperformance_is_tolerated(self, settings: LifecycleSettings) -> None:
        """Live always underperforms the backtest a little. Demoting on that
        would leave nothing live."""
        record = SleeveRecord(strategy=STRATEGY, symbol=SYMBOL, state=State.LIVE)
        evidence = Evidence(live_trades=50, live_sharpe=1.0, out_of_sample_sharpe=1.5)
        assert evaluate_health(record, evidence, settings).healthy is True

    def test_triggers_are_not_weighed_against_each_other(
        self, settings: LifecycleSettings
    ) -> None:
        """A profitable sleeve breaching its drawdown limit still demotes —
        that trade is one nobody would approve if asked directly."""
        record = SleeveRecord(strategy=STRATEGY, symbol=SYMBOL, state=State.LIVE)
        evidence = Evidence(
            live_trades=100,
            live_sharpe=3.0,
            out_of_sample_sharpe=1.5,
            live_max_drawdown_pct=0.40,
        )
        assert evaluate_health(record, evidence, settings).healthy is False

    def test_health_checks_do_not_apply_to_sleeves_that_are_not_live(
        self, settings: LifecycleSettings
    ) -> None:
        record = SleeveRecord(strategy=STRATEGY, symbol=SYMBOL, state=State.PAPER)
        evidence = Evidence(live_trades=100, live_max_drawdown_pct=0.9)
        assert evaluate_health(record, evidence, settings).healthy is True

    def test_demotion_never_needs_a_gate(self, registry: LifecycleRegistry) -> None:
        """Safety must not be blocked by the same evidence requirements as
        promotion — there may not be any evidence when it is needed."""
        _promote_to_live(registry)
        record = registry.demote(STRATEGY, SYMBOL, State.PROBATION, "operator judgement")
        assert record.state is State.PROBATION
        assert registry.can_trade(STRATEGY, SYMBOL) is False

    def test_exits_are_not_the_registry_s_business(
        self, registry: LifecycleRegistry
    ) -> None:
        """The roster gates entries only. A demoted sleeve holding a position
        must still be able to close it, which is enforced at the call site by
        gating _submit_order rather than the exit path."""
        _promote_to_live(registry)
        registry.demote(STRATEGY, SYMBOL, State.PROBATION, "test")
        assert registry.get(STRATEGY, SYMBOL).is_active is True


class TestProbation:
    def test_probation_returns_to_paper_never_straight_to_live(
        self, registry: LifecycleRegistry
    ) -> None:
        """Bouncing in and out of live on noise is how a bad week becomes a bad
        month."""
        _promote_to_live(registry)
        registry.demote(STRATEGY, SYMBOL, State.PROBATION, "test")
        record, result = registry.promote(
            STRATEGY, SYMBOL, _paper_evidence(live_max_drawdown_pct=0.05)
        )
        assert result.allowed
        assert record.state is State.PAPER

    def test_a_still_breached_sleeve_stays_on_probation(
        self, registry: LifecycleRegistry
    ) -> None:
        _promote_to_live(registry)
        registry.demote(STRATEGY, SYMBOL, State.PROBATION, "test")
        _, result = registry.promote(
            STRATEGY, SYMBOL, _paper_evidence(live_max_drawdown_pct=0.30)
        )
        assert result.allowed is False

    def test_repeated_probation_retires_the_sleeve(
        self, registry: LifecycleRegistry
    ) -> None:
        """A sleeve that keeps recovering and breaking again is not recovering."""
        _promote_to_live(registry)
        for _ in range(registry.settings.max_probations_before_retirement):
            registry.demote(STRATEGY, SYMBOL, State.PROBATION, "broke again")
            registry.promote(STRATEGY, SYMBOL, _paper_evidence(live_max_drawdown_pct=0.05))
        assert registry.get(STRATEGY, SYMBOL).state is State.RETIRED

    def test_a_retired_sleeve_cannot_be_promoted(self, registry: LifecycleRegistry) -> None:
        registry.register(STRATEGY, SYMBOL)
        registry.demote(STRATEGY, SYMBOL, State.RETIRED, "done")
        _, result = registry.promote(STRATEGY, SYMBOL, _backtest_evidence())
        assert result.allowed is False
        assert "re-registered" in result.reason

    def test_re_registering_keeps_the_probation_record(
        self, registry: LifecycleRegistry
    ) -> None:
        """Otherwise a retired sleeve launders its history by coming back."""
        _promote_to_live(registry)
        registry.demote(STRATEGY, SYMBOL, State.PROBATION, "broke")
        registry.demote(STRATEGY, SYMBOL, State.RETIRED, "done")
        record = registry.register(STRATEGY, SYMBOL)
        assert record.state is State.CANDIDATE
        assert record.probation_count == 1


# ---------------------------------------------------------------------------
# Persistence and audit
# ---------------------------------------------------------------------------
class TestPersistence:
    def test_state_survives_a_restart(self, settings: LifecycleSettings) -> None:
        """A sleeve demoted on Friday is still demoted on Monday."""
        registry = LifecycleRegistry(settings)
        _promote_to_live(registry)
        registry.demote(STRATEGY, SYMBOL, State.PROBATION, "test")

        reloaded = LifecycleRegistry(settings)
        assert reloaded.get(STRATEGY, SYMBOL).state is State.PROBATION
        assert reloaded.can_trade(STRATEGY, SYMBOL) is False

    def test_the_probation_count_survives_a_restart(
        self, settings: LifecycleSettings
    ) -> None:
        registry = LifecycleRegistry(settings)
        _promote_to_live(registry)
        registry.demote(STRATEGY, SYMBOL, State.PROBATION, "test")
        assert LifecycleRegistry(settings).get(STRATEGY, SYMBOL).probation_count == 1

    def test_a_missing_file_starts_empty_rather_than_failing(
        self, tmp_path: Path
    ) -> None:
        registry = LifecycleRegistry(LifecycleSettings(state_path=tmp_path / "nope.json"))
        assert registry.all() == []

    def test_every_transition_is_recorded(self, registry: LifecycleRegistry) -> None:
        """The state says a sleeve is on probation; the history says why."""
        _promote_to_live(registry)
        registry.demote(STRATEGY, SYMBOL, State.PROBATION, "drawdown breach")
        history = registry.get(STRATEGY, SYMBOL).history
        assert [h["to"] for h in history] == ["candidate", "paper", "live", "probation"]
        assert history[-1]["reason"] == "drawdown breach"

    def test_history_is_bounded(self, registry: LifecycleRegistry) -> None:
        """An unbounded list would grow the state file without limit."""
        registry.register(STRATEGY, SYMBOL)
        for i in range(80):
            registry.demote(STRATEGY, SYMBOL, State.PAPER, f"flip {i}")
        assert len(registry.get(STRATEGY, SYMBOL).history) <= 50


class TestRegistryReads:
    def test_registration_is_idempotent(self, registry: LifecycleRegistry) -> None:
        first = registry.register(STRATEGY, SYMBOL)
        registry.promote(STRATEGY, SYMBOL, _backtest_evidence())
        second = registry.register(STRATEGY, SYMBOL)
        assert second.state is State.PAPER, "re-registering must not reset progress"
        assert first.key == second.key

    def test_symbols_are_normalised(self, registry: LifecycleRegistry) -> None:
        registry.register(STRATEGY, "aapl")
        assert registry.get(STRATEGY, "AAPL") is not None
        assert sleeve_key(STRATEGY, "aapl") == sleeve_key(STRATEGY, "AAPL")

    def test_the_same_symbol_under_two_strategies_is_two_sleeves(
        self, registry: LifecycleRegistry
    ) -> None:
        registry.register("ema_rsi_macd", SYMBOL)
        registry.register("bollinger_reversion", SYMBOL)
        assert len(registry.all()) == 2

    def test_live_sleeves_lists_only_what_can_trade(
        self, registry: LifecycleRegistry
    ) -> None:
        _promote_to_live(registry)
        registry.register("bollinger_reversion", SYMBOL)
        assert [r.key for r in registry.live_sleeves()] == [sleeve_key(STRATEGY, SYMBOL)]

    def test_the_summary_reports_the_roster(self, registry: LifecycleRegistry) -> None:
        _promote_to_live(registry)
        registry.register("bollinger_reversion", "MSFT")
        summary = summarise(registry)
        assert summary["counts"] == {"live": 1, "candidate": 1}
        assert summary["trading"] == [sleeve_key(STRATEGY, SYMBOL)]

    def test_disabling_the_lifecycle_permits_everything(self, tmp_path: Path) -> None:
        """An explicit opt-out, for someone who wants the old behaviour back.
        It is not the default, and it is visible in the status endpoint."""
        registry = LifecycleRegistry(
            LifecycleSettings(enabled=False, state_path=tmp_path / "l.json")
        )
        assert registry.can_trade(STRATEGY, "ANYTHING") is True
        assert registry.gate_reason(STRATEGY, "ANYTHING") == "lifecycle_disabled"
