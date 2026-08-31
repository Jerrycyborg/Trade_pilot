"""The strategies the backtester can run, and the registry that names them.

Until now there was one rule, inlined in the engine. One rule is a fragile
thing to trade: it has a regime it works in, and when that regime ends it does
not stop producing signals — it starts producing losing ones. A portfolio of
strategies is the standard answer, but only if the strategies are actually
different. Two momentum rules with different lookbacks are one strategy with a
typo, and combining them buys nothing.

So the two rules here are deliberately opposed:

- **ema_rsi_macd** buys strength. It wants a trend and it wants confirmation
  that the trend is intact.
- **bollinger_reversion** buys weakness. It wants a price stretched away from
  its own recent mean, and it exits when the price gets back.

These lose money at different times, which is the only property that makes
combining them worth anything. Whether they *do*, on your data, is a question
for `portfolio.py` — it measures the correlation rather than assuming it.

Adding a strategy: write a `signals` function, declare which parameter fields
it reads, and register it. Everything else — walk-forward, sensitivity, the
portfolio, the deflated Sharpe ratio — works against the registry, so a new
strategy is validated the same way the existing ones are without further work.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from market_data.models import OHLCVBar

from . import indicator_series
from .models import StrategyParams

SignalFn = Callable[[list[OHLCVBar], StrategyParams], list[str]]

# MACD needs 26 + 9 bars before it returns anything but zero, whatever else a
# strategy uses.
MACD_WARMUP = 35


@dataclass(frozen=True)
class Strategy:
    """One trading rule, and the parameters it actually reads."""

    name: str
    description: str
    signals: SignalFn
    param_fields: tuple[str, ...]
    """Which StrategyParams fields this rule looks at.

    Searches collapse every other axis to a single value. Without this, a grid
    that varies the EMA periods would count nine separate 'trials' of a
    mean-reversion rule that never reads them — inflating the trial count with
    configurations that are the same configuration, and deflating the Sharpe
    ratio against a bar built from duplicates.
    """
    warmup: Callable[[StrategyParams], int]

    def warmup_bars(self, params: StrategyParams) -> int:
        return self.warmup(params)

    def label(self, params: StrategyParams) -> str:
        return params.label(self.param_fields)


# ---------------------------------------------------------------------------
# ema_rsi_macd — buy strength
# ---------------------------------------------------------------------------
MOMENTUM_FIELDS = ("ema_fast", "ema_slow", "rsi_buy_min", "rsi_buy_max", "macd_hist_min")


def momentum_signals(bars: list[OHLCVBar], params: StrategyParams) -> list[str]:
    """Dual-EMA trend with RSI and MACD confirmation.

    BUY when the fast EMA is above the slow one, RSI sits in a band that is
    rising but not yet overbought, and the MACD histogram confirms. SELL is the
    mirror image: the RSI band reflects around 50 rather than being set
    independently, which keeps the search from doubling in size to express the
    same idea.
    """
    warmup = momentum_warmup(params)
    closes = [bar.close for bar in bars]
    series = indicator_series.build(closes, params.ema_fast, params.ema_slow)

    signals: list[str] = []
    for i in range(len(bars)):
        if i + 1 < warmup:
            signals.append("HOLD")
            continue

        ema_fast = series.ema_fast[i]
        ema_slow = series.ema_slow[i]
        rsi = series.rsi[i]
        macd_hist = series.macd_hist[i]

        buy = (
            ema_fast > ema_slow
            and params.rsi_buy_min < rsi < params.rsi_buy_max
            and macd_hist > params.macd_hist_min
        )
        sell = (
            ema_fast < ema_slow
            and params.rsi_sell_min < rsi < params.rsi_sell_max
            and macd_hist < -params.macd_hist_min
        )
        signals.append("BUY" if buy else "SELL" if sell else "HOLD")

    return signals


def momentum_warmup(params: StrategyParams) -> int:
    return max(params.ema_slow + 1, MACD_WARMUP)


# ---------------------------------------------------------------------------
# bollinger_reversion — buy weakness
# ---------------------------------------------------------------------------
REVERSION_FIELDS = ("bb_period", "bb_std", "rsi_oversold", "rsi_overbought")


def reversion_signals(bars: list[OHLCVBar], params: StrategyParams) -> list[str]:
    """Buy a price stretched below its own mean; exit when it returns to it.

    BUY when the close is below the lower Bollinger band *and* RSI confirms the
    move is oversold rather than the start of a collapse. Requiring both is the
    difference between fading noise and catching a falling knife — though it is
    a mitigation, not a solution: no price-only rule can tell a temporary
    dislocation from a company in trouble, and this one will buy both.

    SELL on a return to the middle band (the mean it was reverting to) or on a
    push through the upper band. The exit is the *point* of a reversion rule —
    holding past the mean turns it into a momentum bet in the wrong direction.
    """
    warmup = reversion_warmup(params)
    closes = [bar.close for bar in bars]
    bands = indicator_series.bollinger(closes, params.bb_period, params.bb_std)
    rsi = indicator_series._rsi_series(closes)

    signals: list[str] = []
    for i in range(len(bars)):
        if i + 1 < warmup:
            signals.append("HOLD")
            continue

        close = closes[i]
        buy = close < bands.lower[i] and rsi[i] < params.rsi_oversold
        sell = close > bands.middle[i] or rsi[i] > params.rsi_overbought
        signals.append("BUY" if buy else "SELL" if sell else "HOLD")

    return signals


def reversion_warmup(params: StrategyParams) -> int:
    # RSI needs 15 bars; the band needs its own window. MACD is not used here,
    # so this rule warms up faster than the momentum one.
    return max(params.bb_period + 1, 15)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
REGISTRY: dict[str, Strategy] = {
    "ema_rsi_macd": Strategy(
        name="ema_rsi_macd",
        description="Dual-EMA trend with RSI and MACD confirmation. Buys strength.",
        signals=momentum_signals,
        param_fields=MOMENTUM_FIELDS,
        warmup=momentum_warmup,
    ),
    "bollinger_reversion": Strategy(
        name="bollinger_reversion",
        description=(
            "Buys a close below the lower Bollinger band with RSI oversold; "
            "exits on a return to the mean. Buys weakness."
        ),
        signals=reversion_signals,
        param_fields=REVERSION_FIELDS,
        warmup=reversion_warmup,
    ),
}

DEFAULT_STRATEGY = "ema_rsi_macd"


def get_strategy(name: str) -> Strategy:
    """Look up a strategy, naming the alternatives when the lookup fails."""
    try:
        return REGISTRY[name]
    except KeyError:
        raise ValueError(
            f"Unknown strategy {name!r}. Available: {', '.join(sorted(REGISTRY))}"
        ) from None


def strategy_names() -> Sequence[str]:
    return sorted(REGISTRY)
