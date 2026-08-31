"""The typed roles from ADR 0001, and which of them the archive can support.

Five roles are specified. Three can be built today — market, technical, and
sentiment (unblocked when the aggregator started journalling every computed
score into an append-only archive). The remaining two are declared here as
unavailable, with the specific reason each is blocked, because that is L1's
real finding and deleting them from the table would hide it:

- **News** has no headline archive. Nothing stores headlines with an
  observed-at time, so there is no way to ask what was known at a moment.
- **Fundamentals** has a research-report table, but it is a TTL cache keyed by
  symbol: a new report overwrites the old one and expiry deletes it. It records
  the current answer, not the sequence of answers, which is the opposite of a
  point-in-time archive.

Each of those could be made to produce output today by reading the live source
instead. That would be worse than producing nothing: an assessment "as of" last
Tuesday built from today's sentiment is the exact leakage the archive exists to
prevent, and it would be invisible in the output.
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
        UnarchivedRole(
            "news",
            "no headline archive: nothing stores headlines with an observed-at "
            "time, so there is no moment to ask about",
            needed="a headline store with observed_at, written on fetch",
        ),
        SentimentSpecialist(),
        UnarchivedRole(
            "fundamentals",
            "research reports are a TTL cache keyed by symbol: a new report "
            "overwrites the previous one and expiry deletes it, so the table "
            "holds the current answer rather than the sequence of answers",
            needed="append research reports as observations instead of upserting",
        ),
    ]
