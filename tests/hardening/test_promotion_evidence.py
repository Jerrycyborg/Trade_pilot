"""Promotion evidence is derived by the server, never supplied by the caller.

Acceptance criteria 6 and 7. The previous API took an Evidence body and every
gate read what the requester sent, so promotion to live was available to
anyone who could construct a JSON object with big numbers in it.

These tests come at it from the attacker's side: each one is an attempt to get
a sleeve promoted on evidence that was not measured.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_LIFECYCLE_POSTGRES_URL"),
    reason="set TEST_LIFECYCLE_POSTGRES_URL to run the evidence-derivation tests",
)

NOW = datetime.now(timezone.utc)


@pytest.fixture
def journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("JOURNAL_PATH", str(tmp_path / "journal.db"))
    from journal import Journal

    return Journal(path=tmp_path / "journal.db")


def _walk_forward_artifact(store, *, symbol="AAPL", strategy="ema_rsi_macd", **payload):
    body = {
        "deflated_sharpe_ratio": 0.97,
        "out_of_sample_trades": 45,
        "out_of_sample_return_pct": 0.08,
        "out_of_sample_sharpe": 1.4,
    }
    body.update(payload)
    return store.record_validation_artifact(
        kind="walk_forward",
        strategy_id=strategy,
        strategy_version="v1",
        symbol=symbol,
        environment="backtest",
        window_start=NOW - timedelta(days=60),
        window_end=NOW,
        payload=body,
        produced_by="run_backtest.py --walk-forward",
    )


class TestEvidenceCannotBeFabricated:
    def test_a_promotion_with_no_artifact_is_refused(self, store) -> None:
        from lifecycle.evidence import derive_backtest_evidence
        from lifecycle.gates import evaluate_to_paper

        sleeve = store.register("ema_rsi_macd", "AAPL", strategy_version="v1")
        evidence = derive_backtest_evidence(store=store, sleeve=sleeve, artifact_ids=[])
        result = evaluate_to_paper(evidence)
        assert result.allowed is False
        assert "no validation artifact cited" in result.reason

    def test_an_artifact_that_does_not_exist_is_refused(self, store) -> None:
        from lifecycle.evidence import derive_backtest_evidence

        sleeve = store.register("ema_rsi_macd", "AAPL", strategy_version="v1")
        evidence = derive_backtest_evidence(store=store, sleeve=sleeve, artifact_ids=[424242])
        assert evidence.usable is False
        assert "does not exist" in evidence.problems[0]

    def test_another_symbols_result_cannot_promote_this_sleeve(self, store) -> None:
        """The exact shape of a plausible mistake: a good walk-forward on a
        liquid name used to promote an illiquid one."""
        from lifecycle.evidence import derive_backtest_evidence

        sleeve = store.register("ema_rsi_macd", "THIN", strategy_version="v1")
        artifact = _walk_forward_artifact(store, symbol="AAPL")
        evidence = derive_backtest_evidence(store=store, sleeve=sleeve, artifact_ids=[artifact])
        assert evidence.usable is False
        assert "is for AAPL" in evidence.problems[0]

    def test_another_strategys_result_cannot_promote_this_sleeve(self, store) -> None:
        from lifecycle.evidence import derive_backtest_evidence

        sleeve = store.register("bollinger_reversion", "AAPL", strategy_version="v1")
        artifact = _walk_forward_artifact(store, strategy="ema_rsi_macd")
        evidence = derive_backtest_evidence(store=store, sleeve=sleeve, artifact_ids=[artifact])
        assert evidence.usable is False
        assert "strategy" in evidence.problems[0]

    def test_a_stale_strategy_version_is_refused(self, store) -> None:
        """Code changed; the old validation no longer describes what would run."""
        from lifecycle.evidence import derive_backtest_evidence

        sleeve = store.register("ema_rsi_macd", "AAPL", strategy_version="v2")
        artifact = _walk_forward_artifact(store)  # recorded for v1
        evidence = derive_backtest_evidence(store=store, sleeve=sleeve, artifact_ids=[artifact])
        assert evidence.usable is False
        assert "strategy version" in evidence.problems[0]

    def test_a_correlation_artifact_cannot_pose_as_a_walk_forward(self, store) -> None:
        from lifecycle.evidence import derive_backtest_evidence

        sleeve = store.register("ema_rsi_macd", "AAPL", strategy_version="v1")
        artifact = store.record_validation_artifact(
            kind="portfolio_correlation",
            strategy_id="ema_rsi_macd",
            strategy_version="v1",
            symbol="AAPL",
            environment="backtest",
            window_start=NOW - timedelta(days=60),
            window_end=NOW,
            payload={"deflated_sharpe_ratio": 0.99},
        )
        evidence = derive_backtest_evidence(store=store, sleeve=sleeve, artifact_ids=[artifact])
        assert evidence.usable is False
        assert "expected walk_forward" in evidence.problems[0]

    def test_a_matching_artifact_produces_usable_evidence(self, store) -> None:
        from lifecycle.evidence import derive_backtest_evidence
        from lifecycle.gates import evaluate_to_paper

        sleeve = store.register("ema_rsi_macd", "AAPL", strategy_version="v1")
        artifact = _walk_forward_artifact(store)
        evidence = derive_backtest_evidence(store=store, sleeve=sleeve, artifact_ids=[artifact])

        assert evidence.usable
        assert evidence.metrics["deflated_sharpe_ratio"] == 0.97
        assert evaluate_to_paper(evidence).allowed is True

    def test_a_weak_artifact_still_fails_the_gate(self, store) -> None:
        """Deriving honestly is not the same as passing."""
        from lifecycle.evidence import derive_backtest_evidence
        from lifecycle.gates import evaluate_to_paper

        sleeve = store.register("ema_rsi_macd", "AAPL", strategy_version="v1")
        artifact = _walk_forward_artifact(store, deflated_sharpe_ratio=0.4)
        evidence = derive_backtest_evidence(store=store, sleeve=sleeve, artifact_ids=[artifact])
        assert evidence.usable
        assert evaluate_to_paper(evidence).allowed is False


class TestEvidenceCarriesItsProvenance:
    def test_the_snapshot_records_which_artifact_it_came_from(self, store) -> None:
        from lifecycle.evidence import derive_backtest_evidence

        sleeve = store.register("ema_rsi_macd", "AAPL", strategy_version="v1")
        artifact_id = _walk_forward_artifact(store)
        evidence = derive_backtest_evidence(store=store, sleeve=sleeve, artifact_ids=[artifact_id])

        assert evidence.source_artifacts[0]["id"] == artifact_id
        assert evidence.source_artifacts[0]["content_hash"]
        assert evidence.content_hash()

    def test_the_snapshot_is_stored_immutably_and_linked(self, store) -> None:
        from lifecycle.evidence import derive_backtest_evidence

        sleeve = store.register("ema_rsi_macd", "AAPL", strategy_version="v1")
        evidence = derive_backtest_evidence(
            store=store, sleeve=sleeve, artifact_ids=[_walk_forward_artifact(store)]
        )
        snapshot_id = store.record_evidence(
            metrics=evidence.metrics,
            source_artifacts=evidence.source_artifacts,
            **evidence.scope,
        )
        moved = store.transition(
            sleeve, "paper", "promoted", evidence_snapshot_id=snapshot_id, actor="operator"
        )

        stored = store.evidence(snapshot_id)
        assert stored["scope"]["symbol"] == "AAPL"
        assert stored["metrics"]["deflated_sharpe_ratio"] == 0.97
        assert store.transitions(moved.id)[0]["evidence_snapshot_id"] == snapshot_id


class TestPaperEvidenceComesFromTheJournal:
    def _paper_sleeve(self, store):
        sleeve = store.register("ema_rsi_macd", "AAPL", strategy_version="v1")
        return store.transition(sleeve, "paper", "promoted")

    def _complete_bars(self, journal, *, hours=3):
        """A gapless series over the window, so the journal gate passes and the
        other gates are actually reached."""
        from market_data.models import OHLCVBar

        start = NOW - timedelta(hours=hours)
        bars = [
            OHLCVBar(symbol="AAPL", timestamp=start + timedelta(minutes=15 * i),
                     open=1, high=1, low=1, close=1, volume=1)
            for i in range(hours * 4 + 1)
        ]
        journal.record_bars("AAPL", "15m", bars)
        return start

    def _fills(self, journal, *, environment="paper", count=25, fill=200.1):
        for _ in range(count):
            journal.record_execution(
                symbol="AAPL", side="BUY", qty=10,
                decision_price=200.0, fill_price=fill,
                strategy_id="ema_rsi_macd", environment=environment, fees=0.5,
            )

    def test_live_and_backtest_records_cannot_pad_a_paper_window(
        self, store, journal
    ) -> None:
        """Acceptance criterion 7. Only paper rows count toward paper evidence."""
        from lifecycle.evidence import derive_paper_evidence

        sleeve = self._paper_sleeve(store)
        self._fills(journal, environment="paper", count=5)
        self._fills(journal, environment="live", count=100)
        self._fills(journal, environment="backtest", count=100)

        evidence = derive_paper_evidence(
            store=store, journal=journal, sleeve=sleeve,
            window_start=NOW - timedelta(days=30),
        )
        assert evidence.metrics["paper_orders"] == 5, (
            "live and backtest fills must not be counted as paper evidence"
        )

    def test_a_short_paper_run_fails_the_gate(self, store, journal) -> None:
        from lifecycle.evidence import derive_paper_evidence
        from lifecycle.gates import evaluate_to_live

        sleeve = self._paper_sleeve(store)
        self._fills(journal, count=25)
        start = self._complete_bars(journal, hours=3)
        evidence = derive_paper_evidence(
            store=store, journal=journal, sleeve=sleeve, window_start=start,
        )
        result = evaluate_to_live(evidence)
        assert result.allowed is False
        assert any("paper traded for" in f for f in result.failed)

    def test_a_journal_gap_disqualifies_the_window(self, store, journal) -> None:
        """A window with a hole cannot support a claim about what happened in it."""
        from datetime import timedelta as td

        from lifecycle.evidence import derive_paper_evidence
        from market_data.models import OHLCVBar

        sleeve = self._paper_sleeve(store)
        self._fills(journal, count=25)
        start = NOW - td(hours=3)
        bars = [
            OHLCVBar(symbol="AAPL", timestamp=start + td(minutes=15 * i),
                     open=1, high=1, low=1, close=1, volume=1)
            for i in range(12) if i not in (5, 6)
        ]
        journal.record_bars("AAPL", "15m", bars)

        evidence = derive_paper_evidence(
            store=store, journal=journal, sleeve=sleeve, window_start=start,
        )
        from lifecycle.gates import evaluate_to_live

        # Derivable, but not promotable: the gate is where a hole in the window
        # stops a promotion, so the operator sees every other failure too.
        assert evidence.metrics["journal_complete"] is False
        assert evidence.metrics["journal_gap_count"] == 1
        result = evaluate_to_live(evidence)
        assert result.allowed is False
        assert any("gap" in f for f in result.failed)

    def test_measured_costs_come_from_actual_fills(self, store, journal) -> None:
        from lifecycle.evidence import derive_paper_evidence

        sleeve = self._paper_sleeve(store)
        self._fills(journal, count=25, fill=200.2)  # 10bps against a 200.0 decision
        evidence = derive_paper_evidence(
            store=store, journal=journal, sleeve=sleeve,
            window_start=NOW - timedelta(days=30),
        )
        assert evidence.metrics["measured_shortfall_bps"] == pytest.approx(10.0)
        assert evidence.metrics["fees"] == pytest.approx(12.5)

    def test_an_unmeasured_correlation_blocks_going_live(self, store, journal) -> None:
        from lifecycle.evidence import derive_paper_evidence
        from lifecycle.gates import evaluate_to_live

        sleeve = self._paper_sleeve(store)
        self._fills(journal, count=25)
        start = self._complete_bars(journal, hours=3)
        evidence = derive_paper_evidence(
            store=store, journal=journal, sleeve=sleeve, window_start=start,
            correlation_artifact_id=None,
        )
        result = evaluate_to_live(evidence)
        assert any("correlation" in f for f in result.failed)
