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


def compute_adx(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float:
    """Compute Average Directional Index. Returns 25.0 (neutral) if insufficient data."""
    if len(highs) < period + 2 or len(lows) < period + 2 or len(closes) < period + 2:
        return 25.0

    tr_list: list[float] = []
    plus_dm_list: list[float] = []
    minus_dm_list: list[float] = []

    for i in range(1, len(closes)):
        high, low, prev_close = highs[i], lows[i], closes[i - 1]
        prev_high, prev_low = highs[i - 1], lows[i - 1]

        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        tr_list.append(tr)

        up_move = high - prev_high
        down_move = prev_low - low
        plus_dm = up_move if (up_move > down_move and up_move > 0) else 0.0
        minus_dm = down_move if (down_move > up_move and down_move > 0) else 0.0
        plus_dm_list.append(plus_dm)
        minus_dm_list.append(minus_dm)

    if len(tr_list) < period:
        return 25.0

    # Wilder smoothing
    atr = sum(tr_list[:period])
    plus_di_smooth = sum(plus_dm_list[:period])
    minus_di_smooth = sum(minus_dm_list[:period])

    dx_list: list[float] = []
    for i in range(period, len(tr_list)):
        atr = atr - atr / period + tr_list[i]
        plus_di_smooth = plus_di_smooth - plus_di_smooth / period + plus_dm_list[i]
        minus_di_smooth = minus_di_smooth - minus_di_smooth / period + minus_dm_list[i]

        plus_di = 100.0 * plus_di_smooth / atr if atr > 0 else 0.0
        minus_di = 100.0 * minus_di_smooth / atr if atr > 0 else 0.0
        di_sum = plus_di + minus_di
        dx = 100.0 * abs(plus_di - minus_di) / di_sum if di_sum > 0 else 0.0
        dx_list.append(dx)

    if not dx_list:
        return 25.0

    # ADX = Wilder-smoothed DX
    adx = sum(dx_list[:period]) / period if len(dx_list) >= period else sum(dx_list) / len(dx_list)
    for dx in dx_list[period:]:
        adx = (adx * (period - 1) + dx) / period
    return adx


def detect_patterns(opens: list[float], highs: list[float], lows: list[float], closes: list[float]) -> list[str]:
    """Detect candlestick patterns. Returns list of pattern names present in the last 2 bars."""
    patterns: list[str] = []
    if len(opens) < 2 or len(highs) < 2 or len(lows) < 2 or len(closes) < 2:
        return patterns

    # Current bar (index -1)
    o, h, l, c = opens[-1], highs[-1], lows[-1], closes[-1]
    body = abs(c - o)
    candle_range = h - l if h != l else 1e-9
    upper_shadow = h - max(o, c)
    lower_shadow = min(o, c) - l

    # Doji: body <= 10% of range
    if body <= 0.1 * candle_range:
        patterns.append("doji")

    # Hammer: small body at top, long lower shadow (>= 2x body), small upper shadow
    if lower_shadow >= 2 * body and upper_shadow <= body and body > 0:
        patterns.append("hammer")

    # Shooting star: small body at bottom, long upper shadow (>= 2x body), small lower shadow
    if upper_shadow >= 2 * body and lower_shadow <= body and body > 0:
        patterns.append("shooting_star")

    # Previous bar
    po, ph, pl, pc = opens[-2], highs[-2], lows[-2], closes[-2]

    # Bullish engulfing: prev bar bearish, current bar bullish and engulfs prior body
    if pc < po and c > o and c >= po and o <= pc:
        patterns.append("bullish_engulfing")

    # Bearish engulfing: prev bar bullish, current bar bearish and engulfs prior body
    if pc > po and c < o and c <= po and o >= pc:
        patterns.append("bearish_engulfing")

    return patterns


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
            adx=25.0,
            patterns=[],
            signal_tags=[],
            trend_direction="neutral",
            data_source=data_source,
            current_price=None,
        )

    opens = [b.open for b in bars]
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    closes = [b.close for b in bars]
    current_price = closes[-1]
    as_of = bars[-1].timestamp

    rsi = compute_rsi(closes)
    macd_line, macd_sig, macd_hist = compute_macd(closes)
    bb_upper, bb_middle, bb_lower = compute_bollinger(closes)
    ema_20 = compute_ema(closes, 20)
    ema_50 = compute_ema(closes, 50)
    adx = compute_adx(highs, lows, closes)
    patterns = detect_patterns(opens, highs, lows, closes)
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
        adx=round(adx, 4),
        patterns=patterns,
        signal_tags=tags,
        trend_direction=trend,
        data_source=data_source,
        current_price=round(current_price, 4),
    )
