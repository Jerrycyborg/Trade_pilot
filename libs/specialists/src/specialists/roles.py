"""The typed roles from ADR 0001, and which of them the archive can support.

Five roles are specified, and all five now have a point-in-time archive to
read: market and technical from the bar archive, and sentiment, fundamentals
and news unblocked one by one as their sources started journalling every
computed score, generated report and fetched headline into append-only
archives. UnarchivedRole remains for the next role someone specifies before
its storage exists: declaring the gap beats reading the live source, because
an assessment "as of" last Tuesday built from today's data is the exact
leakage the archive exists to prevent, and it would be invisible in the
output.
"""

from __future__ import annotations

from typing import Any, Protocol

from attribution.regime import ADX_TREND_THRESHOLD, classify
from market_data.indicators import (
    ADX_MIN_BARS,
    compute_ema,
    compute_macd,
    compute_rsi,
)

from .archive import PointInTimeArchive
from .models import Assessment, Claim, EvidenceRef

#: Enough bars for the slowest indicator used here to be a measurement rather
#: than a seed value. compute_ema over 50 needs the history to mean anything.
MIN_BARS_FOR_TECHNICALS = 60

RSI_OVERBOUGHT = 70.0
RSI_OVERSOLD = 30.0


class Specialist(Protocol):
    """A role. Reads an archive, returns an assessment, changes nothing."""

    role: str
    input_scope: tuple[str, ...]

    def assess(self, archive: PointInTimeArchive, symbol: str) -> Assessment: ...


class _Base:
    role = "base"
    input_scope: tuple[str, ...] = ()
    version = 1

    @property
    def produced_by(self) -> str:
        return f"deterministic:{self.role}/{self.version}"

    def _blank(self, archive: PointInTimeArchive, symbol: str, reason: str) -> Assessment:
        return Assessment(
            role=self.role,
            symbol=symbol,
            as_of=archive.as_of,
            produced_by=self.produced_by,
            unavailable=[reason],
            queries=[q.to_dict() for q in archive.queries],
        )


class MarketSpecialist(_Base):
    """Regime and volatility, from the same classifier attribution uses.

    Deliberately the same one. A specialist with its own notion of "trending"
    would disagree with post-trade attribution about the conditions a trade was
    taken in, and every such disagreement would be an artefact of two
    implementations rather than a finding.
    """

    role = "market"
    input_scope = ("bar_observations",)

    def assess(self, archive: PointInTimeArchive, symbol: str) -> Assessment:
        bars = archive.bars(symbol)
        if len(bars) < ADX_MIN_BARS:
            return self._blank(
                archive,
                symbol,
                f"need {ADX_MIN_BARS} archived bars to classify a regime, have {len(bars)}",
            )

        reading = classify(bars, archive.as_of)
        if not reading.available:
            return self._blank(archive, symbol, reading.reason or "regime not classifiable")

        evidence = (
            EvidenceRef(
                source="bar_observations",
                detail=f"bars_as_of({symbol}) -> {reading.bars_used} bars",
                value={"adx": reading.adx, "net_move_pct": reading.net_move_pct},
            ),
        )
        claims: list[Claim] = []

        if reading.label == "trending_up":
            claims.append(
                Claim(
                    statement="the archived series is in an established uptrend",
                    stance="bull",
                    measure=reading.adx,
                    threshold=ADX_TREND_THRESHOLD,
                    evidence=evidence,
                )
            )
        elif reading.label == "trending_down":
            claims.append(
                Claim(
                    statement="the archived series is in an established downtrend",
                    stance="bear",
                    measure=reading.adx,
                    threshold=ADX_TREND_THRESHOLD,
                    evidence=evidence,
                )
            )
        else:
            claims.append(
                Claim(
                    statement=(
                        "no directional trend: a trend-following entry here is "
                        "being taken in conditions it is not built for"
                    ),
                    stance="neutral",
                    measure=reading.adx,
                    threshold=ADX_TREND_THRESHOLD,
                    evidence=evidence,
                )
            )

        if reading.volatility in ("calm", "agitated"):
            claims.append(
                Claim(
                    statement=(
                        f"volatility is {reading.volatility} relative to this "
                        f"symbol's own recent range"
                    ),
                    # Unusual volatility is a reason for caution in both
                    # directions, which is what neutral means here — not
                    # "no view", but "this does not argue for a side".
                    stance="neutral",
                    measure=reading.atr_pct,
                    threshold=None,
                    evidence=(
                        EvidenceRef(
                            source="bar_observations",
                            detail=f"ATR% against median bar range over {reading.bars_used} bars",
                            value=reading.atr_pct,
                        ),
                    ),
                )
            )

        return Assessment(
            role=self.role,
            symbol=symbol,
            as_of=archive.as_of,
            produced_by=self.produced_by,
            claims=claims,
            queries=[q.to_dict() for q in archive.queries],
        )


class TechnicalSpecialist(_Base):
    """Signal quality from the archived series: momentum, trend structure.

    Says nothing about whether to trade. It reports what the indicators the
    live strategy uses were reading at the moment in question, so that a later
    reader can ask whether the signal was well-founded separately from whether
    it worked.
    """

    role = "technical"
    input_scope = ("bar_observations",)

    def assess(self, archive: PointInTimeArchive, symbol: str) -> Assessment:
        bars = archive.bars(symbol)
        if len(bars) < MIN_BARS_FOR_TECHNICALS:
            return self._blank(
                archive,
                symbol,
                f"need {MIN_BARS_FOR_TECHNICALS} archived bars for the slower "
                f"averages to be measurements, have {len(bars)}",
            )

        closes = [float(b["close"]) for b in bars if b.get("close") is not None]
        if len(closes) < MIN_BARS_FOR_TECHNICALS:
            return self._blank(archive, symbol, "archived bars are missing close prices")

        rsi = compute_rsi(closes)
        _line, _signal, histogram = compute_macd(closes)
        ema_20 = compute_ema(closes, 20)
        ema_50 = compute_ema(closes, 50)
        detail = f"bars_as_of({symbol}) -> {len(closes)} closes"
        claims: list[Claim] = []

        def ref(name: str, value: Any) -> tuple[EvidenceRef, ...]:
            return (
                EvidenceRef(
                    source="bar_observations", detail=f"{name}; {detail}", value=value
                ),
            )

        if rsi >= RSI_OVERBOUGHT:
            claims.append(
                Claim(
                    statement="momentum is stretched; entries here buy from a strong hand",
                    stance="bear",
                    measure=round(rsi, 4),
                    threshold=RSI_OVERBOUGHT,
                    evidence=ref("RSI(14)", round(rsi, 4)),
                )
            )
        elif rsi <= RSI_OVERSOLD:
            claims.append(
                Claim(
                    statement="momentum is washed out",
                    stance="bull",
                    measure=round(rsi, 4),
                    threshold=RSI_OVERSOLD,
                    evidence=ref("RSI(14)", round(rsi, 4)),
                )
            )
        else:
            claims.append(
                Claim(
                    statement="momentum is unremarkable and argues for neither side",
                    stance="neutral",
                    measure=round(rsi, 4),
                    threshold=None,
                    evidence=ref("RSI(14)", round(rsi, 4)),
                )
            )

        claims.append(
            Claim(
                statement=(
                    "the fast average is above the slow one"
                    if ema_20 > ema_50
                    else "the fast average is below the slow one"
                ),
                stance="bull" if ema_20 > ema_50 else "bear",
                measure=round(ema_20 - ema_50, 6),
                threshold=0.0,
                evidence=ref("EMA(20) - EMA(50)", round(ema_20 - ema_50, 6)),
            )
        )
        claims.append(
            Claim(
                statement=(
                    "MACD momentum is positive"
                    if histogram > 0
                    else "MACD momentum is negative"
                ),
                stance="bull" if histogram > 0 else "bear",
                measure=round(histogram, 6),
                threshold=0.0,
                evidence=ref("MACD histogram", round(histogram, 6)),
            )
        )

        return Assessment(
            role=self.role,
            symbol=symbol,
            as_of=archive.as_of,
            produced_by=self.produced_by,
            claims=claims,
            queries=[q.to_dict() for q in archive.queries],
        )


SENTIMENT_BULL_THRESHOLD = 0.3
SENTIMENT_BEAR_THRESHOLD = -0.3
SENTIMENT_MIN_OBSERVATIONS = 3
"""One score is a mood; a claim needs a short series to rest on."""


class SentimentSpecialist(_Base):
    """Crowd mood from the append-only sentiment archive.

    This role was an UnarchivedRole until the aggregator started journalling
    every computed score: its TTL cache held only the current answer, so no
    past sentiment could be recovered and an as-of read had nothing honest to
    read. It still reports itself unavailable when the archive holds too few
    observations by the moment in question — an empty archive is a finding,
    not a neutral market.
    """

    role = "sentiment"
    input_scope = ("sentiment_observations",)

    def assess(self, archive: PointInTimeArchive, symbol: str) -> Assessment:
        rows = archive.sentiment(symbol)
        if len(rows) < SENTIMENT_MIN_OBSERVATIONS:
            return self._blank(
                archive,
                symbol,
                f"need {SENTIMENT_MIN_OBSERVATIONS} archived sentiment scores "
                f"inside the lookback window, have {len(rows)}",
            )

        scores = [float(r["score"]) for r in rows]
        mean = sum(scores) / len(scores)
        latest = scores[-1]
        evidence = (
            EvidenceRef(
                source="sentiment_observations",
                detail=f"sentiment_as_of({symbol}) -> {len(rows)} scores",
                value={"mean": round(mean, 4), "latest": round(latest, 4)},
            ),
        )
        if mean >= SENTIMENT_BULL_THRESHOLD:
            claim = Claim(
                statement="archived sentiment runs positive across the window",
                stance="bull",
                measure=round(mean, 4),
                threshold=SENTIMENT_BULL_THRESHOLD,
                evidence=evidence,
            )
        elif mean <= SENTIMENT_BEAR_THRESHOLD:
            claim = Claim(
                statement="archived sentiment runs negative across the window",
                stance="bear",
                measure=round(mean, 4),
                threshold=SENTIMENT_BEAR_THRESHOLD,
                evidence=evidence,
            )
        else:
            claim = Claim(
                statement="archived sentiment is mixed and argues for neither side",
                stance="neutral",
                measure=round(mean, 4),
                threshold=SENTIMENT_BULL_THRESHOLD,
                evidence=evidence,
            )
        return Assessment(
            role=self.role,
            symbol=symbol,
            as_of=archive.as_of,
            produced_by=self.produced_by,
            claims=[claim],
            queries=[q.to_dict() for q in archive.queries],
        )


class FundamentalsSpecialist(_Base):
    """The latest archived research view of a symbol, as of a moment.

    Was an UnarchivedRole until freshly generated research reports started
    landing in the journal's append-only archive alongside the service's
    delete-and-insert TTL cache. One report is enough to claim from — a
    report is a whole document, not a tick — but its stance is read verbatim
    from the archived record: this role summarises what was believed then,
    it does not re-research.
    """

    role = "fundamentals"
    input_scope = ("research_observations",)

    def assess(self, archive: PointInTimeArchive, symbol: str) -> Assessment:
        rows = archive.research(symbol)
        if not rows:
            return self._blank(
                archive,
                symbol,
                "no archived research report inside the lookback window",
            )

        latest = rows[-1]
        sentiment = str(latest.get("sentiment", "neutral")).lower()
        stance = {"bullish": "bull", "bearish": "bear"}.get(sentiment, "neutral")
        risk_count = len(latest.get("risk_factors") or [])
        evidence = (
            EvidenceRef(
                source="research_observations",
                detail=(
                    f"research_as_of({symbol}) -> {len(rows)} report(s); latest "
                    f"generated {latest.get('generated_at')}"
                ),
                value={
                    "sentiment": sentiment,
                    "confidence_modifier": latest.get("confidence_modifier"),
                    "risk_factors": risk_count,
                },
            ),
        )
        claims = [
            Claim(
                statement=(
                    f"the research view archived at this moment called {symbol} "
                    f"{sentiment}"
                ),
                stance=stance,
                measure=float(latest.get("confidence_modifier") or 0.0),
                threshold=None,
                evidence=evidence,
            )
        ]
        if risk_count:
            claims.append(
                Claim(
                    statement=(
                        f"that view named {risk_count} risk factor(s) — caution, "
                        f"not a side"
                    ),
                    stance="neutral",
                    measure=float(risk_count),
                    threshold=None,
                    evidence=evidence,
                )
            )
        return Assessment(
            role=self.role,
            symbol=symbol,
            as_of=archive.as_of,
            produced_by=self.produced_by,
            claims=claims,
            queries=[q.to_dict() for q in archive.queries],
        )


NEWS_HEAVY_FLOW = 8
"""Distinct archived headlines in the window above which coverage is heavy."""


class NewsSpecialist(_Base):
    """News coverage from the headline archive: what was in the record, when.

    Deliberately modest. Judging whether a headline is good or bad news is
    the sentiment role's job (and the scores it reads were computed from
    these same headlines); this role reports the *flow* — how much coverage
    the archive holds for the moment in question, and what the most recent
    item said — so a reader of the argument sees the news context the other
    roles were formed under. All claims are neutral: coverage intensity is
    context, not a side.
    """

    role = "news"
    input_scope = ("headline_observations",)

    def assess(self, archive: PointInTimeArchive, symbol: str) -> Assessment:
        rows = archive.headlines(symbol)
        if not rows:
            return self._blank(
                archive,
                symbol,
                "no archived headlines inside the lookback window",
            )

        distinct = {r["headline"] for r in rows}
        latest = rows[-1]
        evidence = (
            EvidenceRef(
                source="headline_observations",
                detail=f"headlines_as_of({symbol}) -> {len(rows)} rows",
                value={
                    "distinct": len(distinct),
                    "latest": str(latest.get("headline"))[:200],
                    "latest_source": latest.get("source"),
                },
            ),
        )
        flow = (
            "heavy — the symbol is in the news"
            if len(distinct) >= NEWS_HEAVY_FLOW
            else "light"
        )
        claims = [
            Claim(
                statement=(
                    f"archived news flow is {flow}: {len(distinct)} distinct "
                    f"headline(s) in the window"
                ),
                stance="neutral",
                measure=float(len(distinct)),
                threshold=float(NEWS_HEAVY_FLOW),
                evidence=evidence,
            )
        ]
        return Assessment(
            role=self.role,
            symbol=symbol,
            as_of=archive.as_of,
            produced_by=self.produced_by,
            claims=claims,
            queries=[q.to_dict() for q in archive.queries],
        )


class UnarchivedRole(_Base):
    """A role ADR 0001 specifies that has no point-in-time archive to read.

    It produces an assessment with no claims and a named reason, rather than
    being quietly left out of the roster. A missing role is a gap someone has
    to close; an absent one is a gap nobody can see.
    """

    def __init__(self, role: str, reason: str, needed: str) -> None:
        self.role = role
        self.input_scope = ()
        self._reason = reason
        self.needed = needed

    @property
    def produced_by(self) -> str:
        return f"unavailable:{self.role}"

    def assess(self, archive: PointInTimeArchive, symbol: str) -> Assessment:
        return self._blank(archive, symbol, self._reason)


def default_roster() -> list[Specialist]:
    """Every role in the ADR, buildable or not."""
    return [
        MarketSpecialist(),
        TechnicalSpecialist(),
        NewsSpecialist(),
        SentimentSpecialist(),
        FundamentalsSpecialist(),
    ]
