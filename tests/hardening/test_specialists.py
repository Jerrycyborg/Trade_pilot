"""L1: typed specialist roles over the point-in-time archive.

The load-bearing tests here are in `TestPointInTimeIsolation`. A role can be
perfectly deterministic and still silently improve every time the archive is
corrected, which makes every historical conclusion unfalsifiable — re-running
it never reproduces what was originally said. Determinism is the easy half.

The second theme is that L1 produces no proposals and no control inputs. ADR
0001 puts the risk veto at L2, before anything can propose, so an L1 that could
influence a decision would have skipped the phase built to refuse it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from specialists import (
    Assessment,
    Claim,
    EvidenceRef,
    MarketSpecialist,
    PointInTimeArchive,
    TechnicalSpecialist,
    UnarchivedRole,
    assess_at,
    build_argument,
    build_report,
    default_roster,
    reproduce,
)

SYMBOL = "AAPL"


@pytest.fixture
def journal(tmp_path: Path):
    from journal import Journal

    return Journal(path=tmp_path / "journal.db")


def _bars(closes, *, start, step_minutes=15):
    return [
        SimpleNamespace(
            timestamp=start + timedelta(minutes=step_minutes * i),
            open=c, high=c + 0.5, low=c - 0.5, close=c, volume=1000.0,
        )
        for i, c in enumerate(closes)
    ]


def _rising(n=90, base=100.0, step=0.4):
    return [base + step * i for i in range(n)]


@pytest.fixture
def archive_start():
    return datetime.now(timezone.utc) - timedelta(days=3)


class TestTheArchiveIsTheOnlyWayIn:
    def test_a_specialist_cannot_reach_the_corrected_series(self) -> None:
        """The constraint is the absence of a method, not a rule in a docstring.

        A documented "read only through the archive" lasts until the first role
        that needs one more field and reaches for the journal."""
        archive = PointInTimeArchive(object(), datetime.now(timezone.utc))

        assert not hasattr(archive, "journal")
        assert not hasattr(archive, "latest")
        for name in ("bars", "decisions", "executions"):
            assert hasattr(archive, name)

    def test_every_read_is_recorded_for_provenance(self, journal, archive_start) -> None:
        journal.record_bars(SYMBOL, "15m", _bars(_rising(), start=archive_start))
        archive = PointInTimeArchive(journal, datetime.now(timezone.utc))
        archive.bars(SYMBOL)
        archive.executions(SYMBOL)

        sources = [q.source for q in archive.queries]
        assert sources == ["bar_observations", "execution_quality"]
        assert archive.queries[0].rows > 0

    def test_a_missing_archive_is_an_empty_read_not_a_crash(self) -> None:
        """One unreadable source must not take down the other roles in the run."""

        class Broken:
            def bars_as_of(self, *_a, **_k):
                raise RuntimeError("archive on fire")

        archive = PointInTimeArchive(Broken(), datetime.now(timezone.utc))
        assert archive.bars(SYMBOL) == []
        assert archive.queries[0].rows == 0

    def test_the_archive_reads_the_timeframe_it_was_built_for(
        self, journal, archive_start
    ) -> None:
        """The first live paper run archived daily bars, and every role read
        the (empty) 15m slice: 41 real bars per symbol reported as zero and
        the veto refused subjects the archive covered. The cadence is fixed
        at construction, like the moment — the caller knows what was
        archived; the role must not mix slices mid-argument."""
        journal.record_bars(
            SYMBOL, "1d", _bars(_rising(), start=archive_start, step_minutes=1440)
        )
        now = datetime.now(timezone.utc) + timedelta(days=90)

        daily = PointInTimeArchive(journal, now, timeframe="1d")
        assert len(daily.bars(SYMBOL)) == 90
        assert PointInTimeArchive(journal, now).bars(SYMBOL) == []

        report = build_report(
            journal, [SYMBOL], as_of=now, check_reproducibility=False, timeframe="1d"
        )
        assert "technical" in report["arguments"][0]["roles_reporting"]


class TestClaimsCarryTheirEvidence:
    def test_a_claim_without_evidence_is_refused_at_construction(self) -> None:
        """Not filtered out later by something that might not run."""
        with pytest.raises(ValueError, match="carries no evidence"):
            Claim(statement="it will go up", stance="bull", measure=1.0, threshold=0.0)

    def test_a_claim_names_the_threshold_it_was_judged_against(
        self, journal, archive_start
    ) -> None:
        """So a reader can disagree with the threshold rather than only with
        the conclusion."""
        journal.record_bars(SYMBOL, "15m", _bars(_rising(), start=archive_start))
        assessment = assess_at(
            MarketSpecialist(), journal, SYMBOL, datetime.now(timezone.utc)
        )

        trend = assessment.claims[0]
        assert trend.threshold == 20.0
        assert trend.measure is not None
        assert trend.evidence[0].source == "bar_observations"

    def test_an_invalid_stance_is_refused(self) -> None:
        with pytest.raises(ValueError, match="stance must be"):
            Claim(
                statement="x", stance="very bullish", measure=1.0, threshold=0.0,
                evidence=(EvidenceRef("s", "d", 1),),
            )


class TestRolesRead:
    def test_the_market_role_classifies_an_uptrend(self, journal, archive_start) -> None:
        journal.record_bars(SYMBOL, "15m", _bars(_rising(), start=archive_start))
        assessment = assess_at(
            MarketSpecialist(), journal, SYMBOL, datetime.now(timezone.utc)
        )

        assert assessment.available
        assert assessment.claims[0].stance == "bull"
        assert "uptrend" in assessment.claims[0].statement

    def test_the_market_role_calls_a_range_neutral_not_bullish(
        self, journal, archive_start
    ) -> None:
        chop = [100.0 + (1.5 if i % 2 else -1.5) for i in range(90)]
        journal.record_bars(SYMBOL, "15m", _bars(chop, start=archive_start))
        assessment = assess_at(
            MarketSpecialist(), journal, SYMBOL, datetime.now(timezone.utc)
        )

        assert assessment.claims[0].stance == "neutral"
        assert "not built for" in assessment.claims[0].statement

    def test_the_technical_role_reports_the_indicators_the_strategy_uses(
        self, journal, archive_start
    ) -> None:
        journal.record_bars(SYMBOL, "15m", _bars(_rising(), start=archive_start))
        assessment = assess_at(
            TechnicalSpecialist(), journal, SYMBOL, datetime.now(timezone.utc)
        )

        assert assessment.available
        statements = " ".join(c.statement for c in assessment.claims)
        assert "average" in statements and "MACD" in statements

    def test_too_little_history_names_the_shortfall(self, journal, archive_start) -> None:
        """Not silence, and not a claim built on a seed value."""
        journal.record_bars(SYMBOL, "15m", _bars(_rising(n=8), start=archive_start))
        assessment = assess_at(
            TechnicalSpecialist(), journal, SYMBOL, datetime.now(timezone.utc)
        )

        assert assessment.available is False
        assert assessment.claims == []
        assert "have 8" in assessment.unavailable[0]


class TestPointInTimeIsolation:
    """The half of reproducibility that actually bites."""

    def test_a_revision_arriving_later_does_not_change_a_past_assessment(
        self, journal, archive_start
    ) -> None:
        """A role that silently improves whenever the archive is corrected makes
        every historical conclusion unfalsifiable: re-running it never
        reproduces what was originally said."""
        journal.record_bars(SYMBOL, "15m", _bars(_rising(), start=archive_start))
        moment = datetime.now(timezone.utc)
        before = assess_at(MarketSpecialist(), journal, SYMBOL, moment)

        # A provider revises bars the assessment already used, downward and
        # hard enough to change the classification if it were visible.
        revised = _bars(
            [100.0 - 0.4 * i for i in range(90)], start=archive_start
        )
        assert journal.record_bars(SYMBOL, "15m", revised) > 0, "the revision was stored"

        after = assess_at(MarketSpecialist(), journal, SYMBOL, moment)

        assert after.digest() == before.digest()
        assert after.claims[0].stance == "bull", "the revision was not knowable then"

    def test_the_revision_is_visible_to_an_assessment_made_after_it(
        self, journal, archive_start
    ) -> None:
        """The isolation above must be about time, not about ignoring revisions
        altogether — otherwise the first test passes for the wrong reason."""
        journal.record_bars(SYMBOL, "15m", _bars(_rising(), start=archive_start))
        earlier = datetime.now(timezone.utc)
        journal.record_bars(
            SYMBOL, "15m", _bars([100.0 - 0.4 * i for i in range(90)], start=archive_start)
        )
        later = datetime.now(timezone.utc) + timedelta(seconds=1)

        assert (
            assess_at(MarketSpecialist(), journal, SYMBOL, later).digest()
            != assess_at(MarketSpecialist(), journal, SYMBOL, earlier).digest()
        )

    def test_a_bar_from_after_the_moment_is_not_read(self, journal, archive_start) -> None:
        journal.record_bars(SYMBOL, "15m", _bars(_rising(), start=archive_start))
        moment = datetime.now(timezone.utc)
        before = PointInTimeArchive(journal, moment).bars(SYMBOL)

        journal.record_bars(
            SYMBOL, "15m",
            _bars([200.0, 201.0, 202.0], start=datetime.now(timezone.utc) + timedelta(days=1)),
        )
        after = PointInTimeArchive(journal, moment).bars(SYMBOL)

        assert len(after) == len(before)


class TestReproducibility:
    def test_the_same_role_at_the_same_moment_gives_the_same_digest(
        self, journal, archive_start
    ) -> None:
        journal.record_bars(SYMBOL, "15m", _bars(_rising(), start=archive_start))
        result = reproduce(
            MarketSpecialist(), journal, SYMBOL, datetime.now(timezone.utc), runs=5
        )

        assert result.reproducible
        assert len(set(result.digests)) == 1

    def test_the_digest_ignores_how_the_archive_was_read(self) -> None:
        """Provenance is not a conclusion. A change in read order must not
        register as a changed opinion."""
        moment = datetime.now(timezone.utc)
        claim = Claim("x", "bull", 1.0, 0.0, (EvidenceRef("s", "d", 1),))
        one = Assessment("r", SYMBOL, moment, "test", claims=[claim], queries=[{"a": 1}])
        two = Assessment("r", SYMBOL, moment, "test", claims=[claim], queries=[{"b": 2}])

        assert one.digest() == two.digest()

    def test_a_changed_conclusion_changes_the_digest(self) -> None:
        moment = datetime.now(timezone.utc)
        evidence = (EvidenceRef("s", "d", 1),)
        bull = Assessment(
            "r", SYMBOL, moment, "test", claims=[Claim("x", "bull", 1.0, 0.0, evidence)]
        )
        bear = Assessment(
            "r", SYMBOL, moment, "test", claims=[Claim("x", "bear", 1.0, 0.0, evidence)]
        )

        assert bull.digest() != bear.digest()


class TestTheRosterIsHonestAboutWhatItCannotDo:
    def test_all_five_specified_roles_are_present(self) -> None:
        """A missing role is a gap someone has to close; an absent one is a gap
        nobody can see."""
        assert [s.role for s in default_roster()] == [
            "market", "technical", "news", "sentiment", "fundamentals",
        ]

    def test_the_unarchived_roles_name_why_and_what_is_needed(self) -> None:
        """Every specified role now has a point-in-time archive — sentiment,
        fundamentals and news left this list one by one as their sources
        started journalling into append-only archives. The class stays for
        the next role someone specifies before its storage exists."""
        blocked = [s for s in default_roster() if isinstance(s, UnarchivedRole)]
        assert blocked == []

    def test_an_unarchived_role_produces_no_claims(self, journal) -> None:
        """Rather than reading the live source, which would be worse than
        producing nothing: an assessment 'as of' a past moment built from
        today's data is invisible leakage."""
        stub = UnarchivedRole(
            "options_flow",
            "no options archive: nothing stores the chain with an observed-at time",
            needed="an options store with observed_at, written on fetch",
        )
        assessment = assess_at(stub, journal, SYMBOL, datetime.now(timezone.utc))

        assert assessment.claims == []
        assert "observed-at" in assessment.unavailable[0]
        assert assessment.produced_by == "unavailable:options_flow"

    def test_the_report_leads_with_how_many_roles_have_an_archive(
        self, journal, archive_start
    ) -> None:
        journal.record_bars(SYMBOL, "15m", _bars(_rising(), start=archive_start))
        report = build_report(journal, [SYMBOL])

        assert report["roles"]["specified"] == 5
        assert report["roles"]["with_an_archive"] == 5
        assert report["roles"]["blocked"] == {}
        assert report["reproducibility"]["all_reproducible"] is True


class TestL1ProposesNothing:
    def test_an_argument_keeps_the_stances_apart(self, journal, archive_start) -> None:
        """No verdict field and no score across stances: the ADR wants the
        positions separable so a later reader can see which claim was wrong,
        and collapsing them destroys exactly that."""
        journal.record_bars(SYMBOL, "15m", _bars(_rising(), start=archive_start))
        argument = build_argument(journal, SYMBOL)

        rendered = argument.to_dict()
        assert set(rendered) >= {"bull", "bear", "neutral"}
        assert "conclusion" not in rendered
        assert "score" not in rendered
        assert "recommendation" not in rendered

    def test_an_assessment_has_no_method_that_changes_anything(
        self, journal, archive_start
    ) -> None:
        journal.record_bars(SYMBOL, "15m", _bars(_rising(), start=archive_start))
        assessment = assess_at(
            MarketSpecialist(), journal, SYMBOL, datetime.now(timezone.utc)
        )

        forbidden = {"promote", "demote", "apply", "deploy", "save", "write", "execute"}
        assert forbidden.isdisjoint(dir(assessment))

    def test_the_package_never_imports_the_lifecycle_authority(self) -> None:
        """L1 comes before the risk veto deliberately. Nothing capable of
        proposing should exist before the thing capable of refusing, and the
        cheapest guarantee is that this package cannot reach lifecycle state
        at all."""
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[2] / "libs/specialists/src"
        for module in root.rglob("*.py"):
            source = module.read_text()
            assert "import lifecycle" not in source
            assert "from lifecycle" not in source


class TestTheSentimentRoleReadsTheArchive:
    """Sentiment was an UnarchivedRole until the aggregator started
    journalling every computed score. The properties that matter are the same
    ones the other roles carry: claims rest on archived observations, an
    empty archive is unavailability rather than neutrality, and a score
    recorded after the moment in question does not exist for it."""

    def _record(self, journal, scores, symbol=SYMBOL):
        for score in scores:
            journal.record_sentiment(symbol, score=score, confidence=0.5, sources=["t"])

    def test_positive_scores_make_a_bull_claim(self, journal) -> None:
        from specialists.roles import SentimentSpecialist

        self._record(journal, [0.5, 0.4, 0.6])
        assessment = SentimentSpecialist().assess(
            PointInTimeArchive(journal, datetime.now(timezone.utc)), SYMBOL
        )
        assert not assessment.unavailable
        assert assessment.claims[0].stance == "bull"
        assert assessment.claims[0].measure == pytest.approx(0.5)
        assert assessment.claims[0].evidence

    def test_negative_scores_make_a_bear_claim(self, journal) -> None:
        from specialists.roles import SentimentSpecialist

        self._record(journal, [-0.5, -0.4, -0.6])
        assessment = SentimentSpecialist().assess(
            PointInTimeArchive(journal, datetime.now(timezone.utc)), SYMBOL
        )
        assert assessment.claims[0].stance == "bear"

    def test_too_few_observations_is_unavailability_not_neutrality(
        self, journal
    ) -> None:
        from specialists.roles import SentimentSpecialist

        self._record(journal, [0.9])
        assessment = SentimentSpecialist().assess(
            PointInTimeArchive(journal, datetime.now(timezone.utc)), SYMBOL
        )
        assert assessment.unavailable
        assert not assessment.claims

    def test_a_score_recorded_after_the_moment_does_not_exist_for_it(
        self, journal
    ) -> None:
        """Point-in-time isolation — the property that makes historical
        claims falsifiable at all."""
        from specialists.roles import SentimentSpecialist

        moment = datetime.now(timezone.utc) - timedelta(minutes=5)
        self._record(journal, [0.5, 0.4, 0.6])  # recorded now, after `moment`
        assessment = SentimentSpecialist().assess(
            PointInTimeArchive(journal, moment), SYMBOL
        )
        assert assessment.unavailable

    def test_the_roster_supports_every_specified_role(self, journal) -> None:
        report = build_report(journal, [SYMBOL], check_reproducibility=False)
        assert report["roles"]["with_an_archive"] == 5
        assert report["roles"]["blocked"] == {}


class TestTheFundamentalsRoleReadsTheArchive:
    """Fundamentals was blocked on a delete-and-insert TTL cache. The role
    reads only the journal's append-only research archive, states the stance
    the archived report itself took, and treats an empty archive as
    unavailability."""

    def _record(self, journal, sentiment, risks=(), symbol=SYMBOL):
        journal.record_research(
            symbol, sentiment=sentiment, headline_summary="archived view",
            risk_factors=list(risks), confidence_modifier=0.1, model_version="test",
        )

    def test_a_bullish_report_makes_a_bull_claim(self, journal) -> None:
        from specialists import FundamentalsSpecialist

        self._record(journal, "bullish", risks=["valuation"])
        assessment = FundamentalsSpecialist().assess(
            PointInTimeArchive(journal, datetime.now(timezone.utc)), SYMBOL
        )
        assert assessment.claims[0].stance == "bull"
        assert "bullish" in assessment.claims[0].statement
        # The named risks surface as caution, not as a side.
        assert assessment.claims[1].stance == "neutral"

    def test_the_latest_archived_view_wins(self, journal) -> None:
        from specialists import FundamentalsSpecialist

        self._record(journal, "bullish")
        self._record(journal, "bearish")
        assessment = FundamentalsSpecialist().assess(
            PointInTimeArchive(journal, datetime.now(timezone.utc)), SYMBOL
        )
        assert assessment.claims[0].stance == "bear"

    def test_no_archived_report_is_unavailability(self, journal) -> None:
        from specialists import FundamentalsSpecialist

        assessment = FundamentalsSpecialist().assess(
            PointInTimeArchive(journal, datetime.now(timezone.utc)), SYMBOL
        )
        assert assessment.unavailable
        assert not assessment.claims

    def test_a_report_archived_after_the_moment_does_not_exist_for_it(
        self, journal
    ) -> None:
        from specialists import FundamentalsSpecialist

        moment = datetime.now(timezone.utc) - timedelta(minutes=5)
        self._record(journal, "bullish")
        assessment = FundamentalsSpecialist().assess(
            PointInTimeArchive(journal, moment), SYMBOL
        )
        assert assessment.unavailable


class TestTheNewsRoleReadsTheArchive:
    """News was the last blocked role: headlines flowed straight into a
    sentiment score and vanished. The role reports coverage from the headline
    archive — flow is context, so every claim is neutral — and a headline
    fetched after the moment in question does not exist for it."""

    def _record(self, journal, headlines, symbol=SYMBOL):
        journal.record_headlines(
            symbol, [{"headline": h} for h in headlines], source="test"
        )

    def test_archived_headlines_make_a_neutral_flow_claim(self, journal) -> None:
        from specialists import NewsSpecialist

        self._record(journal, ["A beats estimates", "A raises guidance"])
        assessment = NewsSpecialist().assess(
            PointInTimeArchive(journal, datetime.now(timezone.utc)), SYMBOL
        )
        assert not assessment.unavailable
        claim = assessment.claims[0]
        assert claim.stance == "neutral"
        assert claim.measure == 2.0
        assert claim.evidence[0].source == "headline_observations"

    def test_heavy_flow_is_named(self, journal) -> None:
        from specialists import NewsSpecialist

        self._record(journal, [f"headline {i}" for i in range(9)])
        assessment = NewsSpecialist().assess(
            PointInTimeArchive(journal, datetime.now(timezone.utc)), SYMBOL
        )
        assert "heavy" in assessment.claims[0].statement

    def test_no_archived_headlines_is_unavailability(self, journal) -> None:
        from specialists import NewsSpecialist

        assessment = NewsSpecialist().assess(
            PointInTimeArchive(journal, datetime.now(timezone.utc)), SYMBOL
        )
        assert assessment.unavailable
        assert not assessment.claims

    def test_a_headline_fetched_after_the_moment_does_not_exist_for_it(
        self, journal
    ) -> None:
        from specialists import NewsSpecialist

        moment = datetime.now(timezone.utc) - timedelta(minutes=5)
        self._record(journal, ["late-breaking news"])
        assessment = NewsSpecialist().assess(
            PointInTimeArchive(journal, moment), SYMBOL
        )
        assert assessment.unavailable
