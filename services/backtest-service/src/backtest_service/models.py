"""Request/response models for backtest service."""

from __future__ import annotations

import itertools
from collections.abc import Sequence
from datetime import datetime

from pydantic import BaseModel, Field, ValidationError, model_validator

# A regular US equity session is 6.5 hours = 390 minutes. Used to convert an
# intraday bar size into periods-per-year for annualising Sharpe.
US_SESSION_MINUTES = 390
TRADING_DAYS_PER_YEAR = 252


class StrategyParams(BaseModel):
    """The knobs of the EMA/RSI/MACD rule.

    These were fixed constants until walk-forward analysis needed to vary them.
    The defaults reproduce the original hardcoded rule exactly, so every result
    produced before this model existed still reproduces.

    The sell band mirrors the buy band around RSI 50 rather than being set
    independently. That is a deliberate reduction of the search space: two more
    free parameters would double the number of trials without describing a
    different idea, and every trial raises the bar the result has to clear.
    """

    # --- ema_rsi_macd (momentum) ----------------------------------------
    ema_fast: int = Field(default=20, ge=2, le=200)
    ema_slow: int = Field(default=50, ge=3, le=400)
    rsi_buy_min: float = Field(default=45.0, ge=0.0, le=100.0)
    rsi_buy_max: float = Field(default=70.0, ge=0.0, le=100.0)
    macd_hist_min: float = Field(default=0.0)
    """Minimum MACD histogram for a BUY. Above 0 demands stronger momentum."""

    # --- bollinger_reversion (mean reversion) ---------------------------
    # Held on the same flat object rather than in a per-strategy subclass.
    # Each strategy declares which fields it reads (Strategy.param_fields), and
    # searches collapse the axes a strategy ignores to a single value — so a
    # flat model costs nothing in trial count and keeps the request schema and
    # the grid one shape instead of a discriminated union.
    bb_period: int = Field(default=20, ge=5, le=200)
    bb_std: float = Field(default=2.0, gt=0.0, le=5.0)
    rsi_oversold: float = Field(default=30.0, ge=0.0, le=100.0)
    rsi_overbought: float = Field(default=70.0, ge=0.0, le=100.0)

    @model_validator(mode="after")
    def _check_ordering(self) -> "StrategyParams":
        if self.ema_fast >= self.ema_slow:
            raise ValueError(
                f"ema_fast ({self.ema_fast}) must be below ema_slow ({self.ema_slow}) "
                "— a crossover rule needs two different speeds"
            )
        if self.rsi_buy_min >= self.rsi_buy_max:
            raise ValueError(
                f"rsi_buy_min ({self.rsi_buy_min}) must be below rsi_buy_max "
                f"({self.rsi_buy_max}) — an empty band never triggers"
            )
        if self.rsi_oversold >= self.rsi_overbought:
            raise ValueError(
                f"rsi_oversold ({self.rsi_oversold}) must be below rsi_overbought "
                f"({self.rsi_overbought})"
            )
        return self

    @property
    def rsi_sell_min(self) -> float:
        return 100.0 - self.rsi_buy_max

    @property
    def rsi_sell_max(self) -> float:
        return 100.0 - self.rsi_buy_min

    @property
    def min_warmup_bars(self) -> int:
        """Bars before any indicator is meaningful, across every strategy.

        MACD needs 26 + 9 = 35 regardless; a slower EMA or a longer Bollinger
        window needs more. Trading before this point is trading on an
        indicator's default value.

        This is the conservative figure covering all strategies. A single
        strategy's own requirement comes from `Strategy.warmup_bars`, which is
        what the engine actually uses.
        """
        return max(self.ema_slow + 1, self.bb_period + 1, 35)

    def label(self, fields: "Sequence[str] | None" = None) -> str:
        """A short identity for this configuration.

        `fields` restricts the label to the parameters a given strategy reads.
        Without it a mean-reversion configuration would be labelled by its EMA
        settings, which it never looks at — and two configurations identical in
        every field that matters would appear to be different trials.
        """
        parts = {
            "ema_fast": lambda: f"ema{self.ema_fast}/{self.ema_slow}",
            "ema_slow": lambda: None,  # folded into ema_fast above
            "rsi_buy_min": lambda: f"rsi{self.rsi_buy_min:g}-{self.rsi_buy_max:g}",
            "rsi_buy_max": lambda: None,
            "macd_hist_min": lambda: f"macd>{self.macd_hist_min:g}",
            "bb_period": lambda: f"bb{self.bb_period}",
            "bb_std": lambda: f"sd{self.bb_std:g}",
            "rsi_oversold": lambda: f"rsi<{self.rsi_oversold:g}",
            "rsi_overbought": lambda: f"rsi>{self.rsi_overbought:g}",
        }
        selected = list(parts) if fields is None else list(fields)
        rendered = [parts[name]() for name in selected if name in parts]
        return " ".join(part for part in rendered if part)


class BacktestRequest(BaseModel):
    symbol: str
    strategy: str = "ema_rsi_macd"
    period_days: int = Field(default=180, ge=1, le=730)
    initial_capital: float = Field(default=100_000.0, gt=0)
    risk_per_trade_pct: float = Field(default=0.01, gt=0, le=0.05)
    atr_stop_multiplier: float = Field(default=2.0, gt=0)
    params: StrategyParams = Field(default_factory=StrategyParams)

    # --- Timeframe -------------------------------------------------------
    timeframe: str = Field(default="daily", pattern="^(daily|intraday)$")
    intraday_minutes: int = Field(default=15, ge=1, le=390)

    # --- Trading costs ---------------------------------------------------
    # Intraday strategies trade often, so costs compound fast. They are modelled
    # explicitly rather than folded into one number, because they behave
    # differently: commission may be zero while the spread never is.
    commission_pct: float = Field(default=0.0, ge=0.0)
    """Broker commission as a fraction of notional, per side. 0 for Alpaca."""

    spread_bps: float = Field(default=5.0, ge=0.0)
    """Full quoted bid-ask spread in basis points. A market order crosses half
    of it on entry and half on exit."""

    slippage_bps: float = Field(default=1.0, ge=0.0)
    """Additional adverse fill vs the quote, per side, in basis points."""

    @property
    def is_intraday(self) -> bool:
        return self.timeframe == "intraday"

    @property
    def periods_per_year(self) -> float:
        """Return periods per year for this bar size, for annualising Sharpe."""
        if self.timeframe != "intraday":
            return float(TRADING_DAYS_PER_YEAR)
        bars_per_day = US_SESSION_MINUTES / self.intraday_minutes
        return TRADING_DAYS_PER_YEAR * bars_per_day

    @property
    def cost_per_side_pct(self) -> float:
        """Total one-way cost as a fraction of notional."""
        return self.commission_pct + (self.spread_bps / 2.0 + self.slippage_bps) / 10_000.0


class TradeRecord(BaseModel):
    entry_date: datetime
    exit_date: datetime
    symbol: str
    action: str
    entry_price: float
    exit_price: float
    pnl: float
    pnl_pct: float
    costs: float = 0.0
    """Total commission + spread + slippage paid on this round trip."""
    exit_reason: str = "signal"
    """signal | stop | end_of_data"""
    same_day: bool = False
    """True when entry and exit fall on the same session — a day trade."""


class BacktestResult(BaseModel):
    symbol: str
    strategy: str
    period_days: int
    timeframe: str = "daily"
    intraday_minutes: int = 15
    bars_count: int = 0
    initial_capital: float
    final_value: float
    total_return_pct: float
    gross_return_pct: float = 0.0
    """Return before any trading costs. The gap to total_return_pct is what
    costs took, and on an intraday strategy it is usually the whole edge."""
    total_costs: float = 0.0
    max_drawdown_pct: float
    sharpe_ratio: float
    total_trades: int
    win_rate: float
    profit_factor: float = 0.0
    """Gross profit / gross loss. Below 1.0 means the strategy loses money."""
    avg_trade_pnl: float = 0.0
    day_trades: int = 0
    """Round trips opened and closed in the same session. Relevant to the US
    pattern-day-trader rule, which restricts accounts under $25k equity."""
    max_day_trades_in_5_sessions: int = 0
    trades: list[TradeRecord]
    generated_at: datetime


class CostScenario(BaseModel):
    """One row of a cost sensitivity sweep."""

    spread_bps: float
    commission_pct: float
    total_return_pct: float
    sharpe_ratio: float
    profit_factor: float
    total_trades: int
    total_costs: float


class CostSensitivityResult(BaseModel):
    """Where a strategy stops being profitable as costs rise.

    A strategy that only works at zero cost is not a strategy.
    """

    symbol: str
    timeframe: str
    gross_return_pct: float
    scenarios: list[CostScenario]
    breakeven_spread_bps: float | None = None
    """Highest spread at which the strategy still returns > 0, if any."""


# ---------------------------------------------------------------------------
# Walk-forward analysis and parameter sensitivity
# ---------------------------------------------------------------------------
class ParameterGrid(BaseModel):
    """The configurations a search is allowed to try.

    Kept small on purpose. A wider grid is not a more thorough search — it is a
    more expensive one, because the level the winner must clear to be
    meaningful rises with the number of trials. These axes were chosen to span
    the range over which the momentum idea is still recognisably itself; going
    wider mostly buys more chances for noise to win.
    """

    ema_fast: list[int] = Field(default_factory=lambda: [10, 20, 30])
    ema_slow: list[int] = Field(default_factory=lambda: [40, 50, 60])
    rsi_buy_min: list[float] = Field(default_factory=lambda: [40.0, 45.0, 50.0])
    rsi_buy_max: list[float] = Field(default_factory=lambda: [65.0, 70.0, 75.0])
    macd_hist_min: list[float] = Field(default_factory=lambda: [0.0])
    bb_period: list[int] = Field(default_factory=lambda: [10, 20, 30])
    bb_std: list[float] = Field(default_factory=lambda: [1.5, 2.0, 2.5])
    rsi_oversold: list[float] = Field(default_factory=lambda: [25.0, 30.0, 35.0])
    rsi_overbought: list[float] = Field(default_factory=lambda: [70.0])

    def _axes(self) -> list[tuple[str, list[object]]]:
        return [
            ("ema_fast", list(self.ema_fast)),
            ("ema_slow", list(self.ema_slow)),
            ("rsi_buy_min", list(self.rsi_buy_min)),
            ("rsi_buy_max", list(self.rsi_buy_max)),
            ("macd_hist_min", list(self.macd_hist_min)),
            ("bb_period", list(self.bb_period)),
            ("bb_std", list(self.bb_std)),
            ("rsi_oversold", list(self.rsi_oversold)),
            ("rsi_overbought", list(self.rsi_overbought)),
        ]

    def _live_axes(self, strategy: str | None) -> list[tuple[str, list[object]]]:
        """The axes the given strategy actually reads.

        Every other axis is collapsed to its first value. Varying a parameter a
        strategy never looks at does not produce a different strategy — it
        produces the same one counted many times, which inflates the trial
        count and deflates the Sharpe ratio against a bar built out of
        duplicates.
        """
        axes = self._axes()
        if strategy is None:
            return axes

        from .strategies import get_strategy

        read = set(get_strategy(strategy).param_fields)
        return [
            (name, values if name in read else values[:1])
            for name, values in axes
            if values
        ]

    def combinations(self, strategy: str | None = None) -> list[StrategyParams]:
        """Every valid point in the grid, for the given strategy.

        Invalid points (a fast EMA at or above the slow one, an inverted RSI
        band) are dropped rather than raised on: they are an artefact of taking
        the cross product of independent axes, not a caller error.
        """
        axes = self._live_axes(strategy)
        names = [name for name, _ in axes]
        combos: list[StrategyParams] = []
        for values in itertools.product(*[values for _, values in axes]):
            try:
                combos.append(StrategyParams(**dict(zip(names, values, strict=True))))
            except ValidationError:
                continue
        return combos

    def neighbours(
        self, params: StrategyParams, strategy: str | None = None
    ) -> list[StrategyParams]:
        """Configurations one grid step away in exactly one dimension.

        This is what separates a plateau from a spike. If a result survives
        moving one notch in any single parameter, it is describing something
        about the market; if it does not, it is describing this sample.

        Only dimensions the strategy reads count as steps — moving a parameter
        it ignores produces the identical configuration, which would read as a
        perfectly stable plateau while proving nothing.
        """
        found: list[StrategyParams] = []
        seen = {params.label()}
        for name, values in self._live_axes(strategy):
            current = getattr(params, name)
            if current not in values:
                continue
            index = values.index(current)
            for offset in (-1, 1):
                position = index + offset
                if not 0 <= position < len(values):
                    continue
                try:
                    candidate = params.model_copy(update={name: values[position]})
                    candidate = StrategyParams(**candidate.model_dump())
                except ValidationError:
                    continue
                if candidate.label() not in seen:
                    seen.add(candidate.label())
                    found.append(candidate)
        return found


class ParamScore(BaseModel):
    """One configuration's result over a window."""

    params: StrategyParams
    label: str = ""
    """The configuration, rendered with only the parameters this strategy
    reads. The full label would list EMA settings for a mean-reversion rule
    that never looks at them, implying they were part of the result."""
    sharpe_ratio: float
    """Annualised."""
    total_return_pct: float
    total_trades: int
    profit_factor: float


class FoldResult(BaseModel):
    """One walk-forward split: chosen on the training window, judged on what followed."""

    fold: int
    train_bars: int
    test_bars: int
    train_end: datetime
    test_start: datetime
    test_end: datetime
    selected_params: StrategyParams
    selected_label: str = ""
    """Only the parameters this strategy reads — see ParamScore.label."""
    in_sample_sharpe: float
    in_sample_trades: int
    out_of_sample_sharpe: float
    out_of_sample_return_pct: float
    out_of_sample_trades: int
    out_of_sample_profit_factor: float


class WalkForwardResult(BaseModel):
    """Out-of-sample performance, and how far it fell from the in-sample figure."""

    symbol: str
    timeframe: str
    bars_count: int
    n_folds: int
    n_trials: int
    """Configurations that competed for selection. Sets the bar the deflated
    Sharpe ratio has to clear."""
    objective: str
    embargo_bars: int

    folds: list[FoldResult]

    in_sample_sharpe: float
    """Mean annualised Sharpe of the selected configuration on its own training
    window. Reported only so the drop below is visible — it is not a result."""
    out_of_sample_sharpe: float
    """Annualised Sharpe over the stitched out-of-sample segments. This is the
    only performance figure here that means anything."""
    out_of_sample_return_pct: float
    out_of_sample_max_drawdown_pct: float
    out_of_sample_trades: int
    sharpe_degradation: float
    """in_sample_sharpe - out_of_sample_sharpe. The size of the lie the
    in-sample number was telling."""

    trial_sharpes: list[float] = Field(default_factory=list)
    """Per-period Sharpe of every configuration that competed, un-annualised.

    Exposed so a *campaign* of several walk-forwards can pool them. Deflating
    each run against only its own grid would let someone run twenty searches
    and report the best one's deflated ratio as though nineteen never happened,
    which is the way this statistic becomes decorative."""

    out_of_sample_returns: list[float] = Field(default_factory=list)
    """The stitched out-of-sample per-bar returns of the selected
    configuration. Needed to recompute deflation against a pooled trial set."""

    probabilistic_sharpe_ratio: float | None = None
    """P(true Sharpe > 0), correcting for skew, kurtosis and sample length.
    None when the sample is too short or degenerate to support a claim."""
    deflated_sharpe_ratio: float | None = None
    """P(the out-of-sample Sharpe beats the best a search of n_trials would find
    by luck). Below 0.95 the result is not distinguishable from a lucky search.

    Precisely what is computed: the *out-of-sample* record is tested against a
    benchmark derived from the dispersion of the configurations' *in-sample*
    Sharpe ratios. The textbook deflated Sharpe ratio tests the in-sample
    winner against that same benchmark; holding the out-of-sample record to it
    is the stricter of the two, because in-sample dispersion across a grid is
    normally the wider distribution. It is stated here rather than left
    implicit, since the two are not the same quantity."""
    trial_sharpe_dispersion: float = 0.0
    """Annualised spread of Sharpe across the grid. Wide dispersion means the
    search had more room to get lucky, and raises the deflation bar."""
    parameter_stability: float = 0.0
    """Share of folds that picked the same configuration. Low means the folds
    disagree, which is what fitting to noise looks like."""

    warnings: list[str] = Field(default_factory=list)


class ParameterSensitivityResult(BaseModel):
    """The shape of the result surface, not just its peak."""

    symbol: str
    timeframe: str
    grid_size: int
    best: ParamScore
    worst: ParamScore
    scores: list[ParamScore]
    profitable_count: int
    profitable_fraction: float
    neighbour_count: int
    neighbour_mean_sharpe: float | None = None
    plateau_ratio: float | None = None
    """Neighbour mean Sharpe / best Sharpe. Near 1.0 is a plateau and weak
    evidence of something real; near 0 is a spike and evidence of a fit."""
    sharpe_dispersion: float = 0.0
    warnings: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Strategy portfolio
# ---------------------------------------------------------------------------
class SleeveResult(BaseModel):
    """One (strategy, symbol, parameters) triple, run on its own."""

    label: str
    symbol: str
    strategy: str
    params: StrategyParams
    total_return_pct: float
    sharpe_ratio: float
    """Annualised."""
    max_drawdown_pct: float
    total_trades: int
    profit_factor: float
    volatility: float
    """Annualised standard deviation of per-bar returns."""


class CorrelationPair(BaseModel):
    """How closely two sleeves move together."""

    left: str
    right: str
    correlation: float
    """Pearson, on returns aligned by timestamp. Near 1.0 means the two sleeves
    are one sleeve paying two sets of costs."""


class PortfolioResult(BaseModel):
    """Several sleeves combined, and whether combining them helped."""

    timeframe: str
    allocation: str
    sleeves: list[SleeveResult]
    weights: list[float]
    aligned_bars: int
    """Bars in the combined timeline — the union of every sleeve's bars."""

    total_return_pct: float
    sharpe_ratio: float
    max_drawdown_pct: float

    best_sleeve_label: str
    best_sleeve_sharpe: float
    """The comparison that matters: would running just this one have been
    better? A portfolio that loses to its own best sleeve is paying for
    diversification it is not getting — though picking that sleeve in
    hindsight is its own error."""
    weighted_sleeve_sharpe: float
    """What the combination would score if the sleeves moved as one. The gap up
    to sharpe_ratio is what low correlation bought."""

    diversification_ratio: float | None = None
    """Weighted average sleeve volatility / portfolio volatility. Above 1.0
    means the combination is less volatile than its parts. At 1.0 the sleeves
    are the same bet."""
    correlations: list[CorrelationPair] = Field(default_factory=list)
    max_correlation: float | None = None

    probabilistic_sharpe_ratio: float | None = None
    deflated_sharpe_ratio: float | None = None
    n_trials: int = 0
    """Configurations that competed. For a portfolio this must include the
    symbols and strategies that were considered and dropped, not just the
    sleeves that made it in — otherwise the bar is set from the survivors."""

    warnings: list[str] = Field(default_factory=list)
