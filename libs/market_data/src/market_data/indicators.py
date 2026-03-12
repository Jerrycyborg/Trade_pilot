"""Pure technical indicator functions — no external TA library dependency."""

from __future__ import annotations

from datetime import timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

from .models import OHLCVBar, TASummary, TechnicalIndicators


def compute_rsi(closes: list[float], period: int = 14) -> float:
    """Compute RSI using Wilder's smoothing method. Returns 50.0 if insufficient data."""
    if len(closes) < period + 1:
        return 50.0

    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [abs(min(d, 0.0)) for d in deltas]

    # Initial average
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    # Wilder's smoothing
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0.0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def compute_ema(closes: list[float], period: int) -> float:
    """Compute exponential moving average. Returns last close if insufficient data."""
    if not closes:
        return 0.0
    if len(closes) < period:
        return closes[-1]

    multiplier = 2.0 / (period + 1)
    ema = sum(closes[:period]) / period  # SMA seed
    for price in closes[period:]:
        ema = (price - ema) * multiplier + ema
    return ema


def compute_macd(
    closes: list[float],
    fast: int = 12,
    slow: int = 26,
    signal_period: int = 9,
) -> tuple[float, float, float]:
    """Returns (macd_line, signal_line, histogram). Returns (0,0,0) if insufficient data."""
    if len(closes) < slow + signal_period:
        return 0.0, 0.0, 0.0

    # Build EMA series for fast and slow
    mult_fast = 2.0 / (fast + 1)
    mult_slow = 2.0 / (slow + 1)

    ema_fast = sum(closes[:fast]) / fast
    ema_slow = sum(closes[:slow]) / slow

    macd_series: list[float] = []
    for i in range(slow, len(closes)):
        price = closes[i]
        ema_fast = (price - ema_fast) * mult_fast + ema_fast
        ema_slow = (price - ema_slow) * mult_slow + ema_slow
        macd_series.append(ema_fast - ema_slow)

    if len(macd_series) < signal_period:
        return macd_series[-1] if macd_series else 0.0, 0.0, 0.0

    # Signal line = EMA of MACD series
    mult_sig = 2.0 / (signal_period + 1)
    sig = sum(macd_series[:signal_period]) / signal_period
    for val in macd_series[signal_period:]:
        sig = (val - sig) * mult_sig + sig

    line = macd_series[-1]
    histogram = line - sig
    return line, sig, histogram


def compute_bollinger(
    closes: list[float],
    period: int = 20,
    std_dev: float = 2.0,
) -> tuple[float, float, float]:
    """Returns (upper, middle, lower). Returns (0,0,0) if insufficient data."""
    if len(closes) < period:
        return 0.0, 0.0, 0.0

    window = closes[-period:]
    middle = sum(window) / period
    variance = sum((p - middle) ** 2 for p in window) / period
    std = variance**0.5
    upper = middle + std_dev * std
    lower = middle - std_dev * std
    return upper, middle, lower


def _bb_position(price: float, upper: float, lower: float) -> float:
    """Position of price within Bollinger Bands [0=lower, 1=upper]."""
    band_width = upper - lower
    if band_width <= 0:
        return 0.5
    return max(0.0, min(1.0, (price - lower) / band_width))


def _derive_signal_tags(
    price: float,
    rsi: float,
    macd_hist: float,
    ema_20: float,
    ema_50: float,
    bb_pos: float,
) -> list[str]:
    tags: list[str] = []
    if rsi > 70:
        tags.append("overbought")
    elif rsi < 30:
        tags.append("oversold")
    if macd_hist > 0:
        tags.append("macd_bullish")
    elif macd_hist < 0:
        tags.append("macd_bearish")
    if price > ema_20:
        tags.append("above_ema20")
    if price > ema_50:
        tags.append("above_ema50")
    if bb_pos > 0.8:
        tags.append("near_upper_band")
    elif bb_pos < 0.2:
        tags.append("near_lower_band")
    return tags


def _derive_trend(rsi: float, macd_hist: float, ema_20: float, ema_50: float, price: float) -> str:
    bullish_signals = sum([
        price > ema_20,
        price > ema_50,
        ema_20 > ema_50,
        macd_hist > 0,
        rsi > 55,
    ])
    bearish_signals = sum([
        price < ema_20,
        price < ema_50,
        ema_20 < ema_50,
        macd_hist < 0,
        rsi < 45,
    ])
    if bullish_signals >= 3:
        return "bullish"
    if bearish_signals >= 3:
        return "bearish"
    return "neutral"


def build_ta_summary(symbol: str, bars: list[OHLCVBar], data_source: str = "unknown") -> TASummary:
    """Build a complete TASummary from a list of OHLCV bars."""
    from datetime import datetime

    if not bars:
        return TASummary(
            symbol=symbol,
            as_of=datetime.now(timezone.utc),
            bars_count=0,
            indicators=TechnicalIndicators(),
            signal_tags=[],
            trend_direction="neutral",
            data_source=data_source,
            current_price=None,
        )

    closes = [b.close for b in bars]
    current_price = closes[-1]
    as_of = bars[-1].timestamp

    rsi = compute_rsi(closes)
    macd_line, macd_sig, macd_hist = compute_macd(closes)
    bb_upper, bb_middle, bb_lower = compute_bollinger(closes)
    ema_20 = compute_ema(closes, 20)
    ema_50 = compute_ema(closes, 50)
    bb_pos = _bb_position(current_price, bb_upper, bb_lower)

    indicators = TechnicalIndicators(
        rsi_14=round(rsi, 4),
        macd_line=round(macd_line, 6),
        macd_signal=round(macd_sig, 6),
        macd_histogram=round(macd_hist, 6),
        bb_upper=round(bb_upper, 4),
        bb_middle=round(bb_middle, 4),
        bb_lower=round(bb_lower, 4),
        bb_position=round(bb_pos, 4),
        ema_20=round(ema_20, 4),
        ema_50=round(ema_50, 4),
    )

    tags = _derive_signal_tags(current_price, rsi, macd_hist, ema_20, ema_50, bb_pos)
    trend = _derive_trend(rsi, macd_hist, ema_20, ema_50, current_price)

    return TASummary(
        symbol=symbol,
        as_of=as_of,
        bars_count=len(bars),
        indicators=indicators,
        signal_tags=tags,
        trend_direction=trend,
        data_source=data_source,
        current_price=round(current_price, 4),
    )
