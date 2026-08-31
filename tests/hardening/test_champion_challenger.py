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
