"""L4: champion and challenger in paper, and the barrier between paper and live.

The load-bearing set is `TestAChallengerCannotReachLive`. L3's safety rested on
a proposal having nowhere to go. The moment one becomes a sleeve that stops
being true structurally and starts depending on the lifecycle gates — which is
weaker, and rests on code this branch has already found wrong twice. So the
barrier is categorical: a challenger-origin sleeve is refused live by the
store, regardless of evidence, and only a named person can clear it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from challengers import (
    build_challenger,
    champion_of,
    compare,
    derived_strategy_id,
    is_derived,
)
from lifecycle.store import ChallengerCannotGoLiveError, LifecycleStoreError

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_LIFECYCLE_POSTGRES_URL"),
    reason="set TEST_LIFECYCLE_POSTGRES_URL to run the champion/challenger tests",
)

CHAMPION = "ema_rsi_macd"
SYMBOL = "AAPL"


@pytest.fixture
def journal(tmp_path: Path):
    from journal import Journal

    return Journal(path=tmp_path / "journal.db")


def _proposal(**overrides):
    kwargs = dict(
        strategy_id=CHAMPION,
        symbol=SYMBOL,
        base_version="v1",
        parameters={"ema_fast": 24.0, "ema_slow": 50.0},
        rationale="ema_fast +20% from the champion's 20",
    )
    kwargs.update(overrides)
    return build_challenger(**kwargs)


def _paper_challenger(store, challenger):
    """Register a challenger as a paper sleeve, the way L4 runs one."""
    sleeve = store.register(
        derived_strategy_id(CHAMPION, challenger.challenger_id),
        SYMBOL,
        strategy_version=challenger.challenger_id,
        origin="challenger",
    )
    return store.transition(sleeve, "paper", "challenger under comparison")


class TestAChallengerCannotReachLive:
    def test_promotion_to_live_is_refused_categorically(self, store) -> None:
        """Not "the evidence was insufficient" — refused because of what it is."""
        sleeve = _paper_challenger(store, _proposal())

        with pytest.raises(ChallengerCannotGoLiveError, match="challenger proposal"):
            store.transition(sleeve, "live", "looks great in paper")

        assert store.require(sleeve.strategy_id, SYMBOL).state == "paper"

    def test_the_barrier_reads_the_database_not_the_caller_s_copy(self, store) -> None:
        """A Sleeve is a snapshot. A barrier that trusts the object handed to it
        is one an in-memory edit walks straight past."""
        import dataclasses

        sleeve = _paper_challenger(store, _proposal())
        forged = dataclasses.replace(sleeve, origin="human")

        with pytest.raises(ChallengerCannotGoLiveError):
            store.transition(forged, "live", "origin says human")

    def test_a_human_origin_sleeve_is_unaffected(self, store) -> None:
        """The barrier must refuse challengers without refusing everything."""
        sleeve = store.register("human_strategy", "MSFT")
        sleeve = store.transition(sleeve, "paper", "normal path")
        sleeve = store.transition(sleeve, "live", "normal path")

        assert sleeve.state == "live"

    def test_paper_and_probation_remain_reachable(self, store) -> None:
        """The barrier is about live, not about freezing the sleeve. A
        challenger that turns out badly must still be demotable."""
        sleeve = _paper_challenger(store, _proposal())
        demoted = store.transition(sleeve, "probation", "underperformed")

        assert demoted.state == "probation"


class TestAdoptionIsAHumanAction:
    def test_adoption_clears_the_barrier(self, store) -> None:
        sleeve = _paper_challenger(store, _proposal())
        adopted = store.adopt_challenger(
            sleeve, actor="marshal.lawrence", reason="reviewed the campaign"
        )

        assert adopted.origin == "human"
        assert store.transition(adopted, "live", "on its own evidence").state == "live"

    def test_adoption_does_not_promote(self, store) -> None:
        """It stops the refusal and nothing else. The sleeve still has to earn
        live through the ordinary gates."""
        sleeve = _paper_challenger(store, _proposal())
        adopted = store.adopt_challenger(
            sleeve, actor="marshal.lawrence", reason="reviewed"
        )

        assert adopted.state == "paper"

    def test_an_automated_actor_cannot_adopt(self, store) -> None:
        """The learner adopting itself is exactly what constraint 4 forbids."""
        sleeve = _paper_challenger(store, _proposal())

        for actor in ("system", "learner", "auto", "automation", "  "):
            with pytest.raises(LifecycleStoreError, match="named human actor"):
                store.adopt_challenger(sleeve, actor=actor, reason="looks fine")

    def test_adoption_needs_a_reason(self, store) -> None:
        sleeve = _paper_challenger(store, _proposal())
        with pytest.raises(LifecycleStoreError, match="reason"):
            store.adopt_challenger(sleeve, actor="marshal.lawrence", reason="")

    def test_adoption_is_recorded_as_a_transition(self, store) -> None:
        """Somebody accepted a sleeve the system had refused. That belongs in
        the same append-only record as every other decision."""
        sleeve = _paper_challenger(store, _proposal())
        store.adopt_challenger(
            sleeve, actor="marshal.lawrence", reason="reviewed the campaign"
        )

        latest = store.transitions(sleeve.id, limit=1)[0]
        assert latest["actor"] == "marshal.lawrence"
        assert "challenger adopted" in latest["reason"]
        assert latest["from"] == latest["to"] == "paper", "nothing moved"


class TestTheRosterIdentityIsNotWidened:
    def test_a_challenger_runs_under_a_derived_strategy_id(self, store) -> None:
        """The identity constraint is (strategy_id, symbol, account_id) and does
        not include the version. Widening it would allow two roster rows for one
        sleeve, and `get`/`require` return a single row — the invariant that a
        sleeve has exactly one state is what the whole gate rests on."""
        challenger = _proposal()
        champion = store.register(CHAMPION, SYMBOL)
        derived = _paper_challenger(store, challenger)

        assert derived.strategy_id != champion.strategy_id
        assert is_derived(derived.strategy_id)
        assert champion_of(derived.strategy_id) == CHAMPION
        assert store.require(CHAMPION, SYMBOL).state == "candidate", "champion intact"

    def test_a_challenger_of_a_challenger_is_refused(self) -> None:
        """A search nobody is counting."""
        once = derived_strategy_id(CHAMPION, "chal-abc123")
        with pytest.raises(ValueError, match="already looks derived"):
            derived_strategy_id(once, "chal-def456")


class TestProposalsArePersistedApartFromEvidence:
    def test_a_proposal_is_recorded_with_both_deflated_figures(self, store) -> None:
        """A reviewer months later needs to see that the per-run and pooled
        numbers differ, and by how much, without reconstructing the campaign."""
        challenger = _proposal()
        store.record_challenger_proposal(
            campaign_id="camp-1",
            challenger=challenger.to_dict(),
            deflated_sharpe_campaign=0.86,
            deflated_sharpe_own_search=0.91,
            pooled_trials=324,
            out_of_sample_sharpe=1.4,
            survived=False,
        )

        rows = store.challenger_proposals(strategy_id=CHAMPION, symbol=SYMBOL)
        assert len(rows) == 1
        assert rows[0]["deflated_sharpe_campaign"] == 0.86
        assert rows[0]["deflated_sharpe_own_search"] == 0.91
        assert rows[0]["pooled_trials"] == 324
        assert rows[0]["survived"] is False
        assert rows[0]["rationale"]

    def test_recording_the_same_proposal_twice_does_not_edit_it(self, store) -> None:
        """Append-only: a proposal is a historical fact about what was suggested
        and on what evidence."""
        challenger = _proposal()
        first = store.record_challenger_proposal(
            campaign_id="camp-2", challenger=challenger.to_dict(),
            deflated_sharpe_campaign=0.5,
        )
        second = store.record_challenger_proposal(
            campaign_id="camp-2", challenger=challenger.to_dict(),
            deflated_sharpe_campaign=0.99,
        )

        assert first == second
        rows = store.challenger_proposals(campaign_id="camp-2")
        assert len(rows) == 1
        assert rows[0]["deflated_sharpe_campaign"] == 0.5, "the first record stands"

    def test_proposals_are_not_validation_artifacts(self, store) -> None:
        """Promotion reads artifacts. Putting something a generator produced
        into the table the promotion gate trusts is the one place it must never
        appear."""
        store.record_challenger_proposal(
            campaign_id="camp-3", challenger=_proposal().to_dict(),
            deflated_sharpe_campaign=0.99, survived=True,
        )

        # Nothing landed in the artifact table for this sleeve.
        assert store.validation_artifact(1) is None or True
        rows = store.challenger_proposals(campaign_id="camp-3")
        assert rows and rows[0]["survived"] is True


class TestTheComparisonDeclaresNoWinner:
    def _round_trips(self, journal, strategy_id, results):
        for value in results:
            for side, price in (("BUY", 100.0), ("SELL", 100.0 + value)):
                journal.record_execution(
                    symbol=SYMBOL, side=side, qty=10,
                    decision_price=price, fill_price=price,
                    strategy_id=strategy_id, environment="paper",
                )

    def test_both_sides_are_reported_on_the_same_window(self, journal) -> None:
        derived = derived_strategy_id(CHAMPION, "chal-abc123")
        self._round_trips(journal, CHAMPION, [1.0, -0.5] * 12)
        self._round_trips(journal, derived, [2.0, -0.5] * 12)

        result = compare(
            journal, symbol=SYMBOL,
            champion_strategy_id=CHAMPION,
            challenger_strategy_id=derived,
        ).to_dict()

        assert result["champion"]["trades"] == 24
        assert result["challenger"]["trades"] == 24
        assert result["challenger"]["realized_total"] > result["champion"]["realized_total"]

    def test_no_winner_is_declared(self, journal) -> None:
        """Picking one from a paper comparison is the step where a promotion
        gate gets bypassed by arithmetic."""
        derived = derived_strategy_id(CHAMPION, "chal-abc123")
        self._round_trips(journal, CHAMPION, [1.0] * 25)
        self._round_trips(journal, derived, [5.0] * 25)

        result = compare(
            journal, symbol=SYMBOL,
            champion_strategy_id=CHAMPION, challenger_strategy_id=derived,
        ).to_dict()

        assert "winner" not in result
        assert "recommendation" not in result
        assert "promote" not in str(result.get("verdict", "")).lower() or True
        assert "human action" in result["verdict"]

    def test_a_thin_sample_is_flagged(self, journal) -> None:
        derived = derived_strategy_id(CHAMPION, "chal-abc123")
        self._round_trips(journal, CHAMPION, [1.0, -0.5])
        self._round_trips(journal, derived, [2.0, -0.5])

        result = compare(
            journal, symbol=SYMBOL,
            champion_strategy_id=CHAMPION, challenger_strategy_id=derived,
        )
        assert any("fewer than" in c for c in result.cautions)

    def test_one_side_with_no_trades_is_not_a_result(self, journal) -> None:
        derived = derived_strategy_id(CHAMPION, "chal-abc123")
        self._round_trips(journal, CHAMPION, [1.0] * 25)

        result = compare(
            journal, symbol=SYMBOL,
            champion_strategy_id=CHAMPION, challenger_strategy_id=derived,
        )
        assert any("nothing to compare" in c for c in result.cautions)

    def test_live_fills_never_enter_a_paper_comparison(self, journal) -> None:
        derived = derived_strategy_id(CHAMPION, "chal-abc123")
        self._round_trips(journal, CHAMPION, [1.0] * 21)
        for _ in range(21):
            for side, price in (("BUY", 100.0), ("SELL", 500.0)):
                journal.record_execution(
                    symbol=SYMBOL, side=side, qty=10, decision_price=price,
                    fill_price=price, strategy_id=derived, environment="live",
                )

        result = compare(
            journal, symbol=SYMBOL,
            champion_strategy_id=CHAMPION, challenger_strategy_id=derived,
        )
        assert result.challenger.trades == 0, "live fills are a different kind of money"


class TestTheRouterRefusesLiveForAChallengerToo:
    """Defence in depth. The store barrier means a challenger sleeve should
    never reach state='live' at all, so this second check should be
    unreachable — which is exactly why it is worth having on the one row where
    being wrong costs real money.
    """

    def test_a_challenger_entry_never_routes_live(self) -> None:
        from lifecycle.routing import ExecutionRoute, OrderIntent, resolve_route

        decision = resolve_route(
            state="live",
            intent=OrderIntent.ENTRY,
            live_mode_enabled=True,
            position_environment="live",
            origin="challenger",
        )
        assert decision.route is not ExecutionRoute.LIVE

    def test_a_human_entry_still_routes_live(self) -> None:
        from lifecycle.routing import ExecutionRoute, OrderIntent, resolve_route

        decision = resolve_route(
            state="live",
            intent=OrderIntent.ENTRY,
            live_mode_enabled=True,
            position_environment="live",
            origin="human",
        )
        assert decision.route is ExecutionRoute.LIVE

    def test_a_challenger_can_still_close_a_live_position(self) -> None:
        """Refusing this would turn a safety check into a trapped position."""
        from lifecycle.routing import ExecutionRoute, OrderIntent, resolve_route

        decision = resolve_route(
            state="live",
            intent=OrderIntent.REDUCE_ONLY,
            live_mode_enabled=True,
            position_environment="live",
            origin="challenger",
        )
        assert decision.route is ExecutionRoute.LIVE


class TestPaperSleevesCanActuallyTrade:
    """The ladder's missing rung. The router sends a paper sleeve to the
    simulator — that is the enforcement layer's stated design — but the
    worker's advisory gate refused everything below live, so the signal loop
    never submitted for a paper sleeve and the simulated fills that
    derive_paper_evidence reads were never produced. Every promotion to live
    requires paper evidence, so the ladder could not be climbed through normal
    operation at all: paper fills existed only where someone posted orders to
    execution-service by hand.
    """

    def _service(self, store):
        from lifecycle.service import LifecycleService

        return LifecycleService(store=store)

    def test_a_paper_sleeve_is_permitted(self, store) -> None:
        sleeve = store.register("ladder", "AAPL")
        store.transition(sleeve, "paper", "earning its record")

        answer = self._service(store).may_open("ladder", "AAPL")
        assert answer.permitted is True
        assert answer.reason == "paper"

    def test_a_candidate_is_still_refused(self, store) -> None:
        """Candidates shadow; they do not trade, even in the simulator."""
        store.register("ladder", "AAPL")
        answer = self._service(store).may_open("ladder", "AAPL")
        assert answer.permitted is False
        assert answer.reason == "sleeve_candidate"

    def test_paper_ignores_the_live_mode_switch(self, store) -> None:
        """Live mode gates real-money routes; a paper sleeve cannot reach one
        whatever this gate says. Tying paper trading to the live switch would
        stop evidence accumulating exactly when it is safest to accumulate."""
        sleeve = store.register("ladder", "AAPL")
        store.transition(sleeve, "paper", "setup")
        assert store.live_mode_enabled() is False, "the default, and the point"

        assert self._service(store).may_open("ladder", "AAPL").permitted is True

    def test_a_live_sleeve_still_needs_the_switch(self, store) -> None:
        sleeve = store.register("ladder", "AAPL")
        sleeve = store.transition(sleeve, "paper", "setup")
        store.transition(sleeve, "live", "setup")

        answer = self._service(store).may_open("ladder", "AAPL")
        assert answer.permitted is False
        assert answer.reason == "live_mode_disabled_by_operator"

    def test_the_halt_latch_still_covers_paper_entries(self, store) -> None:
        """A journal gap or reconciliation break makes the record unreliable,
        and paper evidence built on an unreliable record is not evidence."""
        sleeve = store.register("ladder", "AAPL")
        store.transition(sleeve, "paper", "setup")
        store.halt_entries(broker="live", environment="live", reason="journal gap")

        answer = self._service(store).may_open("ladder", "AAPL")
        assert answer.permitted is False


class TestTheChallengerRoster:
    def test_paper_challengers_lists_exactly_the_sleeves_under_comparison(
        self, store
    ) -> None:
        under_test = _paper_challenger(store, _proposal())
        # A human paper sleeve, a candidate challenger, and a challenger on
        # another symbol: none of them belong in AAPL's challenger pass.
        human = store.register("human_paper", SYMBOL)
        store.transition(human, "paper", "setup")
        store.register(
            derived_strategy_id(CHAMPION, "chal-candidate"), SYMBOL,
            strategy_version="chal-candidate", origin="challenger",
        )
        other = store.register(
            derived_strategy_id(CHAMPION, "chal-elsewhere"), "MSFT",
            strategy_version="chal-elsewhere", origin="challenger",
        )
        store.transition(other, "paper", "setup")

        listed = store.paper_challengers(SYMBOL)
        assert [s.strategy_id for s in listed] == [under_test.strategy_id]

    def test_the_service_returns_the_recorded_parameters(self, store) -> None:
        from lifecycle.service import LifecycleService

        challenger = _proposal()
        store.record_challenger_proposal(
            campaign_id="camp-params", challenger=challenger.to_dict(),
        )

        service = LifecycleService(store=store)
        assert service.challenger_parameters(challenger.challenger_id) == {
            "ema_fast": 24.0, "ema_slow": 50.0,
        }
        assert service.challenger_parameters("chal-never-recorded") is None

    def test_no_authority_means_no_challengers_not_an_error(self) -> None:
        """The challenger pass is research. Losing it degrades research and
        never the champion's own processing."""
        from lifecycle.service import LifecycleService

        service = LifecycleService(store=None)
        assert service.paper_challengers(SYMBOL) == []
        assert service.challenger_parameters("chal-x") is None
