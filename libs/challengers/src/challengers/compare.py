"""Champion against challenger, on the same symbols and the same window.

ADR 0001: "Champion/challenger comparison runs in paper, on the same symbols
and the same window, with both sleeves' fills recorded under
`environment='paper'` and distinguished by strategy version."

One deviation, and the reason for it. The roster's identity constraint is
`UNIQUE (strategy_id, symbol, account_id)` — it does not include the version —
so two sleeves distinguished only by `strategy_version` cannot both exist.
Widening that constraint would allow two roster rows for one
(strategy, symbol, account), and `store.get`/`require` return a single row;
the invariant that a sleeve has exactly one state is what the whole gate rests
on. So a challenger gets a *derived strategy id* — `champion@chal-abc123` —
which keeps the constraint intact, keeps every existing lookup working, and
makes the pairing obvious in the journal.

What this module does not do is decide. It reports both sides with the same
statistics and the same refusals to compute what cannot be computed, and stops
there. Picking a winner from a paper comparison is the step where a promotion
gate gets bypassed by arithmetic, and it belongs to a person.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

#: Separator between a champion's strategy id and the challenger suffix.
#: A character that cannot appear in a normal strategy id, so the two can never
#: be confused and `derived_strategy_id` is reversible.
DERIVED_MARKER = "@"


def derived_strategy_id(champion_strategy_id: str, challenger_id: str) -> str:
    """The roster id a paper challenger runs under."""
    if DERIVED_MARKER in champion_strategy_id:
        raise ValueError(
            f"{champion_strategy_id!r} already looks derived; a challenger of a "
            f"challenger is not a comparison, it is a search nobody is counting"
        )
    return f"{champion_strategy_id}{DERIVED_MARKER}{challenger_id}"


def is_derived(strategy_id: str) -> bool:
    return DERIVED_MARKER in strategy_id


def champion_of(strategy_id: str) -> str:
    """The champion a derived id came from."""
    return strategy_id.split(DERIVED_MARKER, 1)[0]


@dataclass
class Side:
    """One side of the comparison, as the journal has it."""

    label: str
    strategy_id: str
    trades: int = 0
    realized_total: float | None = None
    sharpe: float | None = None
    sharpe_annualised: float | None = None
    win_rate: float | None = None
    max_drawdown_amount: float | None = None
    span_days: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "strategy_id": self.strategy_id,
            "trades": self.trades,
            "realized_total": self.realized_total,
            "sharpe_per_trade": self.sharpe,
            "sharpe_annualised": self.sharpe_annualised,
            "win_rate": self.win_rate,
            "max_drawdown_amount": self.max_drawdown_amount,
            "span_days": self.span_days,
        }


@dataclass
class Comparison:
    """Both sides, and an explicit refusal to declare a winner."""

    symbol: str
    window_start: datetime | None
    window_end: datetime | None
    champion: Side | None = None
    challenger: Side | None = None
    cautions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "window_start": self.window_start.isoformat() if self.window_start else None,
            "window_end": self.window_end.isoformat() if self.window_end else None,
            "champion": self.champion.to_dict() if self.champion else None,
            "challenger": self.challenger.to_dict() if self.challenger else None,
            "cautions": self.cautions,
            "verdict": (
                "No winner is declared here, deliberately. Both sides are "
                "reported on the same window with the same statistics; deciding "
                "between them is a human action, and promotion still requires "
                "the ordinary gates on out-of-sample evidence rather than on a "
                "paper comparison."
            ),
        }


#: Below this many trades on either side, a difference is arithmetic on a
#: handful of round trips. Stated as a caution rather than used to suppress the
#: numbers: hiding them would be its own kind of dishonesty.
MIN_TRADES_TO_COMPARE = 20


def compare(
    journal: Any,
    *,
    symbol: str,
    champion_strategy_id: str,
    challenger_strategy_id: str,
    account_id: str = "default",
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    environment: str = "paper",
) -> Comparison:
    """Both sides over the same window, from realised round trips.

    `environment` defaults to paper and is passed through rather than hardcoded
    so a reader can see which money this is about. Nothing here permits a live
    comparison to promote anything; the barrier for that is in the store.
    """
    from attribution import load_round_trips, performance_from_trades

    result = Comparison(symbol=symbol, window_start=window_start, window_end=window_end)

    def _side(label: str, strategy_id: str) -> Side:
        trips = load_round_trips(
            journal,
            strategy_id=strategy_id,
            symbol=symbol,
            environment=environment,
            account_id=account_id,
            window_start=window_start,
            window_end=window_end,
        )
        performance = performance_from_trades(trips)
        return Side(
            label=label,
            strategy_id=strategy_id,
            trades=performance["trades"],
            realized_total=performance["realized_total"],
            sharpe=performance["sharpe"],
            sharpe_annualised=performance["sharpe_annualised"],
            win_rate=performance["win_rate"],
            max_drawdown_amount=performance["max_drawdown_amount"],
            span_days=performance["span_days"],
        )

    result.champion = _side("champion", champion_strategy_id)
    result.challenger = _side("challenger", challenger_strategy_id)

    thin = [
        s.label
        for s in (result.champion, result.challenger)
        if s.trades < MIN_TRADES_TO_COMPARE
    ]
    if thin:
        result.cautions.append(
            f"{' and '.join(thin)} has fewer than {MIN_TRADES_TO_COMPARE} closed "
            f"trades; a difference over this sample is dominated by which trades "
            f"they happened to be"
        )

    spans = [s.span_days for s in (result.champion, result.challenger) if s.span_days]
    if len(spans) == 2 and max(spans) > 0 and min(spans) / max(spans) < 0.8:
        result.cautions.append(
            "the two sides did not trade over comparable spans, so they did not "
            "face the same market"
        )

    if result.champion.trades == 0 or result.challenger.trades == 0:
        result.cautions.append(
            "one side has no closed trades, so there is nothing to compare — "
            "not a result in either direction"
        )

    return result
