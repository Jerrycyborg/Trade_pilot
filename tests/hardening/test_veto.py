"""L2: an independent risk veto that can reject and cannot approve.

The tests that matter most are in `TestItCannotApprove`. "Authority to reject
and no authority to approve" is easy to write and easy to lose, and the way it
gets lost is ordinary: the veto returns something boolean-ish, a caller writes
`if veto_ok(x):`, and within a release a reader believes a green light means
the subject was checked and endorsed. Nothing objects, because `not rejected`
and `approved` are the same bit — unless the code makes them different.
"""

from __future__ import annotations

import dataclasses
import inspect
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from veto import NO_OBJECTION, REJECTED, VetoPolicy, review

SYMBOL = "AAPL"


@pytest.fixture
def journal(tmp_path: Path):
    from journal import Journal

    return Journal(path=tmp_path / "journal.db")


def _bars(n, *, end=None, step_minutes=15, gap_after=None):
    end = end or datetime.now(timezone.utc)
    out = []
    skew = 0
    for i in range(n):
        if gap_after is not None and i > gap_after:
            skew = 600  # a hole, in minutes
        out.append(
            SimpleNamespace(
                timestamp=end - timedelta(minutes=step_minutes * (n - i) + skew),
                open=100.0, high=100.5, low=99.5, close=100.0, volume=1000.0,
            )
        )
    return out


class TestItCannotApprove:
    def test_a_decision_has_no_truth_value(self, journal) -> None:
        """`if decision:` is the idiom that turns a veto into a green light.
        It raises rather than quietly meaning something."""
        journal.record_bars(SYMBOL, "15m", _bars(200))
        decision = review(journal, SYMBOL)

        with pytest.raises(TypeError, match="no truth value"):
            bool(decision)
        with pytest.raises(TypeError, match="not approval"):
            if decision:  # noqa: SIM103
                pass

    def test_there_is_no_approved_field(self, journal) -> None:
        journal.record_bars(SYMBOL, "15m", _bars(200))
        decision = review(journal, SYMBOL)

        for name in ("approved", "ok", "passed", "allow", "endorsed"):
            assert not hasattr(decision, name)
        assert set(decision.to_dict()) & {"approved", "ok", "passed"} == set()

    def test_a_clean_review_says_no_objection_not_approved(self, journal) -> None:
        journal.record_bars(SYMBOL, "15m", _bars(200))
        decision = review(journal, SYMBOL)

        assert decision.verdict == NO_OBJECTION
        assert decision.rejected is False
        assert "not approval" in decision.to_dict()["note"]

    def test_a_decision_cannot_be_edited_after_the_fact(self, journal) -> None:
        """"Rejection is final within the loop" is not a convention if a caller
        can clear a flag."""
        journal.record_bars(SYMBOL, "15m", _bars(10))
        decision = review(journal, SYMBOL)
        assert decision.rejected is True

        with pytest.raises(dataclasses.FrozenInstanceError):
            decision.objections = ()  # type: ignore[misc]

    def test_no_rule_can_clear_another_rules_objection(self) -> None:
        """No scoring across objections, and no threshold at which several
        small ones become acceptable."""
        source = inspect.getsource(review)
        for clearing in (".remove(", ".pop(", "objections = []", "objections.clear"):
            assert clearing not in source


class TestItIsIndependent:
    def test_review_cannot_be_handed_a_specialist_argument(self) -> None:
        """"Does not see their conclusions before forming its own" is a
        signature, not a discipline: there is no parameter to pass one in."""
        parameters = set(inspect.signature(review).parameters)

        assert parameters == {"journal", "subject", "as_of", "policy", "timeframe"}
        assert not parameters & {"argument", "assessment", "claims", "specialists"}

    def test_the_package_never_imports_the_specialists(self) -> None:
        root = Path(__file__).resolve().parents[2] / "libs/veto/src"
        for module in root.rglob("*.py"):
            source = module.read_text()
            assert "import specialists" not in source
            assert "from specialists" not in source


class TestItRejectsForReasonsItCanShow:
    def test_too_little_history_is_refused(self, journal) -> None:
        journal.record_bars(SYMBOL, "15m", _bars(10))
        decision = review(journal, SYMBOL)

        assert decision.verdict == REJECTED
        rule = decision.objections[0]
        assert rule.rule == "insufficient_history"
        assert rule.measure == 10.0 and rule.threshold == 60.0

    def test_a_stale_series_is_refused(self, journal) -> None:
        """Reasoning about a symbol whose series stopped two days ago is
        reasoning about a memory."""
        old = datetime.now(timezone.utc) - timedelta(days=2)
        journal.record_bars(SYMBOL, "15m", _bars(200, end=old))
        decision = review(journal, SYMBOL, policy=VetoPolicy(window_hours=96))

        assert decision.verdict == REJECTED
        assert any(o.rule == "stale_series" for o in decision.objections)

    def test_a_series_older_than_the_whole_window_is_still_refused(
        self, journal
    ) -> None:
        """The case the staleness rule was written for, and the one it could
        not fire on. `completeness` reports stale_minutes=None when the window
        holds no bars at all — so reading staleness from there let the deadest
        symbol in the archive pass with no objection, while a merely-late one
        was caught. Staleness now comes from the freshest bar actually held."""
        long_dead = datetime.now(timezone.utc) - timedelta(days=3)
        journal.record_bars(SYMBOL, "15m", _bars(200, end=long_dead))

        # The default 48h window contains none of these bars.
        decision = review(journal, SYMBOL, policy=VetoPolicy(window_hours=48))

        assert decision.verdict == REJECTED
        assert any(o.rule == "stale_series" for o in decision.objections)

    def test_an_undateable_series_is_unchecked_not_declared_fresh(self) -> None:
        """"We could not tell how old this is" and "this is current" must not
        produce the same silence."""

        class NoTimestamps:
            def bars_as_of(self, *_a, **_k):
                return [{"close": 100.0} for _ in range(200)]

            def completeness(self, **_k):
                return {"available": True, "gap_count": 0}

            def execution_rows(self, **_k):
                return []

        decision = review(NoTimestamps(), SYMBOL)
        assert any("freshness" in u for u in decision.unchecked)

    def test_a_symbol_whose_orders_keep_being_rejected_is_refused(self, journal) -> None:
        journal.record_bars(SYMBOL, "15m", _bars(200))
        for _ in range(6):
            journal.record_execution(
                symbol=SYMBOL, side="BUY", qty=1, decision_price=100.0,
                fill_price=None, outcome="rejected", rejected=True,
            )
        decision = review(journal, SYMBOL)

        assert decision.verdict == REJECTED
        assert any(o.rule == "orders_keep_being_rejected" for o in decision.objections)

    def test_every_objection_names_its_measurement(self, journal) -> None:
        journal.record_bars(SYMBOL, "15m", _bars(5))
        decision = review(journal, SYMBOL)

        for objection in decision.objections:
            assert objection.detail
            assert objection.measure is not None
            assert objection.threshold is not None

    def test_a_healthy_symbol_draws_no_objection(self, journal) -> None:
        """The veto must refuse the unfit without refusing everything: one that
        rejects every subject is not cautious, it is broken."""
        journal.record_bars(SYMBOL, "15m", _bars(200))
        assert review(journal, SYMBOL).objections == ()


class TestItSaysWhatItCouldNotCheck:
    def test_an_unreadable_archive_is_reported_not_silently_skipped(self) -> None:
        """A veto that skipped half its checks and said nothing is
        indistinguishable from one that ran them all and found nothing."""

        class Broken:
            def bars_as_of(self, *_a, **_k):
                raise RuntimeError("archive on fire")

            def completeness(self, **_k):
                raise RuntimeError("archive on fire")

            def execution_rows(self, **_k):
                raise RuntimeError("archive on fire")

        decision = review(Broken(), SYMBOL)

        assert len(decision.unchecked) == 3
        assert decision.to_dict()["unchecked"]

    def test_an_unreadable_archive_does_not_become_a_clean_bill(self) -> None:
        """It reports NO_OBJECTION because it genuinely found none — but the
        unchecked list is what stops that reading as a pass."""

        class Broken:
            def bars_as_of(self, *_a, **_k):
                raise RuntimeError("nope")

            def completeness(self, **_k):
                raise RuntimeError("nope")

            def execution_rows(self, **_k):
                raise RuntimeError("nope")

        decision = review(Broken(), SYMBOL)
        assert decision.rejected is False
        assert decision.unchecked, "silence here would be indistinguishable from a pass"


class TestThePolicyIsVersionedConfiguration:
    def test_a_decision_records_the_policy_that_produced_it(self, journal) -> None:
        """A rejection recorded six months ago is only interpretable if you can
        recover what it was rejecting against."""
        journal.record_bars(SYMBOL, "15m", _bars(200))
        assert review(journal, SYMBOL).policy_version == "1"

    def test_defaults_live_on_the_fields_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("VETO_MIN_ARCHIVED_BARS", raising=False)
        assert (
            VetoPolicy.from_env().min_archived_bars == VetoPolicy().min_archived_bars
        ), "from_env must not carry its own copy of a default"

    def test_a_set_variable_overrides_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VETO_MIN_ARCHIVED_BARS", "5")
        assert VetoPolicy.from_env().min_archived_bars == 5

    def test_an_unparseable_value_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A veto silently running on a default the operator believes they
        replaced is a veto nobody has configured."""
        monkeypatch.setenv("VETO_MAX_STALE_MINUTES", "two hours")
        with pytest.raises(ValueError, match="VETO_MAX_STALE_MINUTES"):
            VetoPolicy.from_env()

    def test_the_policy_has_no_field_expressing_merit(self) -> None:
        """A field about whether something is a good idea would be the first
        step towards the veto acquiring an approval power."""
        names = set(VetoPolicy().to_dict())
        assert not names & {"min_confidence", "min_sharpe", "approve_above", "min_score"}
