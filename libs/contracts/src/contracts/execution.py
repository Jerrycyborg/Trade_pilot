"""Pure helpers for order pricing and execution quality.

Two ideas, both about the gap between the price a decision was based on and the
price actually paid.

**Marketable limit orders.** A market order accepts any price the book offers,
which on a thin symbol or during a spike can be far from what the strategy
assumed. A *marketable* limit is priced through the touch — aggressive enough
to fill immediately in normal conditions — but refuses a fill beyond a stated
tolerance. Paired with immediate-or-cancel it gives price protection without
leaving working orders to manage: it fills now, or not at all.

**Implementation shortfall** is the standard measure of what execution cost.
It compares the price when the decision was made against the price actually
paid. Positive is a cost; negative means the market moved in your favour
between deciding and filling.
"""

from __future__ import annotations

BUY = "BUY"
SELL = "SELL"


def marketable_limit_price(
    reference_price: float,
    side: str,
    tolerance_bps: float,
) -> float | None:
    """A limit priced `tolerance_bps` through the reference, in the paying direction.

    A buy is willing to pay slightly above the reference, a sell to accept
    slightly below. Widening the tolerance raises the fill rate and weakens the
    protection; narrowing it does the reverse. Returns None when the reference
    price is unusable, so the caller must decide rather than guess.
    """
    if reference_price is None or reference_price <= 0:
        return None
    if tolerance_bps < 0:
        raise ValueError("tolerance_bps must not be negative")

    drift = reference_price * (tolerance_bps / 10_000.0)
    limit = reference_price + drift if side.upper() == BUY else reference_price - drift
    return round(max(limit, 0.0001), 4)


def limit_is_marketable(limit_price: float, market_price: float, side: str) -> bool:
    """Whether a limit would fill against the current market.

    A buy limit fills when the market is at or below it; a sell limit when the
    market is at or above it.
    """
    if side.upper() == BUY:
        return market_price <= limit_price
    return market_price >= limit_price


def limit_fill_price(limit_price: float, market_price: float, side: str) -> float | None:
    """The price a marketable limit would fill at, or None if it would not fill.

    You get the market price when it is better than your limit — a limit caps
    what you pay, it does not force you to pay it.
    """
    if not limit_is_marketable(limit_price, market_price, side):
        return None
    if side.upper() == BUY:
        return min(market_price, limit_price)
    return max(market_price, limit_price)


def implementation_shortfall_bps(
    decision_price: float,
    fill_price: float,
    side: str,
) -> float | None:
    """Execution cost in basis points, signed so positive is always a cost.

    A buy filled above the decision price cost money; a sell filled below it did
    the same. The sign convention matters: without it, averaging buys and sells
    together silently cancels real costs out.
    """
    if not decision_price or decision_price <= 0 or not fill_price or fill_price <= 0:
        return None
    raw = (fill_price - decision_price) / decision_price
    signed = raw if side.upper() == BUY else -raw
    return round(signed * 10_000.0, 4)


def participation_capped_qty(
    qty: int,
    average_volume: float | None,
    max_participation_pct: float,
) -> int:
    """Trim an order to a share of average volume.

    Being a large fraction of a symbol's volume moves the price against you —
    the cost of the trade becomes a function of its own size. Small caps in a
    watchlist are where this bites; mega-caps will never come close to the cap.
    """
    if qty <= 0:
        return 0
    if not average_volume or average_volume <= 0 or max_participation_pct <= 0:
        return qty
    ceiling = int(average_volume * max_participation_pct)
    return max(0, min(qty, ceiling))


def average_daily_volume(bars: list, bars_per_day: float | None = None) -> float | None:
    """Average daily volume inferred from bars.

    Intraday bars carry a slice of a day's volume, so they are scaled up by the
    number of bars in a session. Returns None when there is nothing to measure.
    """
    volumes = [float(getattr(bar, "volume", 0.0) or 0.0) for bar in bars or []]
    volumes = [v for v in volumes if v > 0]
    if not volumes:
        return None
    mean_bar_volume = sum(volumes) / len(volumes)
    return mean_bar_volume * (bars_per_day or 1.0)
