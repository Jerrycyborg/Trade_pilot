# ADR-006: Where Profitability Comes From

## Status: Proposed

Awaiting a decision. Every prior ADR in this directory records a choice already
made; this one records a choice still open, because it changes what the project
is for.

## Date: 2026-09-03

## Context

The stack is architecturally complete against `project_spc.md`: strategy
proposes, reasoning explains, policy approves, execution places. Twenty-two
defects have been found and fixed by running it. The validation apparatus is
genuinely strong — anchored walk-forward with an embargo, Probabilistic Sharpe,
expected-maximum Sharpe under N trials, and a Deflated Sharpe that prices in the
trial count (`backtest-service/stats.py`, `validation.py`). The audit apparatus
is stronger than most retail systems ever build: a point-in-time observation
archive, round-trip attribution with regime classification and counterfactuals.

None of that is evidence the system can make money, and after a live paper run
and two orchestrator drills there is still no such evidence. Three conditions
must hold before it can. Only one is currently in reach.

**1. What is validated must be what trades.** It is not. The momentum rule
exists twice: `strategy-service/rule_engine.py` holds `CHAMPION_PARAMETERS`
(`ema_fast=20, ema_slow=50, rsi_buy_min=45, rsi_buy_max=70, macd_hist_min=0`)
and its own three-value risk-to-size lookup; `backtest-service/strategies.py`
holds a second implementation with its own `MOMENTUM_FIELDS` and ATR-based
sizing. They agree today by convention and a test asserting the champion
reproduces the original hard-coded rule. Nothing structurally prevents
divergence, and there is no mechanism by which a parameter set the walk-forward
selects becomes the parameter set the worker trades. Everything `validation.py`
proves is proven about code that does not execute.

**2. There must be an edge.** The champion is a dual-EMA/RSI/MACD rule — the
most-tried signal family — applied to five symbols, three of them mega-cap US
equities, which is the most efficient and most crowded corner of the market. It
is long-only, single-asset, single-timeframe, price-only. There is no
cross-sectional ranking anywhere in the codebase.

McLean & Pontiff (Journal of Finance, 2016), studying 97 published
cross-sectional predictors, measure returns **26% lower out-of-sample and 58%
lower post-publication** — and find the surviving returns concentrate in stocks
with *high idiosyncratic risk and low liquidity*. That is a capacity-constrained
niche: institutional size cannot fit into it, which is the one structural
advantage a small account actually has. Competing on mega-cap technical rules
forfeits that advantage entirely.

**3. The edge must survive costs.** The simulator charges commission, half-spread
and slippage per side and includes a break-even-spread ladder, which is better
than most. What it lacks is not a market-impact model — at $10k-$100k an order is
roughly 1e-6 to 1e-3 of ADV, where square-root-law impact is under half a basis
point and is genuinely not the problem. What it lacks is a cost *level* grounded
in what retail actually pays. Schwarz, Barber, Huang, Jorion & Odean (*Journal of
Finance*, Oct 2025) placed 85,000 simultaneous identical market orders across five
brokers and measured round-trip costs of **7 to 46 bps excluding commissions — a
6.5x dispersion for the same trade at the same moment**, not explained by payment
for order flow. Theoretical cost arithmetic for a large cap totals ~3.5 bps, but
that assumes capturing mid, which retail routing does not. The defensible base
case is **10 bps round trip for large caps and 25 bps for mid caps**, with stress
cases at 20 and 45.

Those numbers impose a turnover budget, and the budget is binding: a strategy
turning over once a day at 10 bps burns roughly 25% annualised before it earns
anything. At mid-cap costs it is far worse. Any cross-sectional design that
reaches for less-liquid names to escape crowding must simultaneously *lower* its
rebalance frequency to afford them.

There is a fourth constraint that decides where AI belongs. Recent work on
lookahead bias in LLM forecasts (Gao, Jiang & Yan, 2025-26) shows LLMs reproduce
pre-cutoff financial values close to verbatim; a "lookahead propensity" statistic
is materially positive throughout the in-sample period and **collapses to
approximately zero immediately after the training cutoff**, and forecast accuracy
is amplified exactly where that propensity is high. Masking tickers and
instructing the model to respect historical boundaries do not remove it. An
LLM-generated signal therefore cannot be honestly backtested on data preceding
the model's cutoff — its apparent skill is partly recall.

A further consideration bounds the whole programme: statistical power. Roughly
100 closed trades is the practical minimum for a defensible read on expectancy,
200+ convincing. Five symbols on daily bars produce a handful of trades a month.
At that rate the system cannot learn whether it works within any useful horizon,
independent of whether an edge exists.

## Decision

Four decisions, in dependency order.

**1. One strategy definition, shared.** Signal logic and its parameters move to a
single versioned library (`libs/strategy`) that both `backtest-service` and
`strategy-service` import. Promotion writes the validated parameter set to that
one place. A test asserts research and live produce identical signals on
identical bars. No new service — a shared library, in the shape of
`libs/attribution` and `libs/journal`.

**2. Alpha becomes cross-sectional, on a wide and deliberately less-liquid
universe.** Rank a universe on features and trade the extremes, rather than
evaluating each symbol against itself. This is both the alpha decision and the
statistical-power decision: a few hundred names generate an adequate sample in
months rather than years. The universe deliberately extends past mega-caps
toward the capacity-constrained region the McLean-Pontiff result points at.

**3. Research data is a reference series, kept separate from the observation
log.** `bar_observations` records what this system saw, when it saw it;
`observed_at` is the basis of every as-of read. A bulk vendor backfill stamped
with today's `observed_at` is invisible to historical as-of reads, and stamping
it with `bar_ts` instead would fabricate an observation history and destroy the
knowable-by-cutoff guarantee the specialists and the veto depend on. Vendor
history therefore lands in a separate `reference_bars` store, keyed by vendor and
vendor-as-of, which backtests read and `bars_as_of` never does. Where the two
overlap, disagreement is a free data-quality measurement.

**4. LLMs are excluded from the primary alpha path.** Because an LLM signal
cannot be validated by backtest, it cannot be promoted on backtest evidence, and
forward validation costs months per hypothesis. LLMs are used where their output
is cheap to check: generating and screening candidate hypotheses that are then
validated deterministically by the existing walk-forward, and explaining
decisions — which is the role `project_spc.md` already assigns to the reasoning
layer. Any LLM component that gates trades carries forward-only evidence and is
declared as such.

## Consequences

- Everything the validation layer already proves becomes meaningful, because it
  will be proving it about the code that trades. This is a prerequisite, not an
  improvement.
- Cross-sectional work is blocked on a survivorship-bias-free universe with
  point-in-time membership and corporate actions. Today's data layer has neither,
  and Yahoo intraday history is capped near 60 days. The data foundation must
  land before the alpha work, not alongside it.
- `portfolio-service` must allocate, not only track. Ranking a universe produces
  competing positions; correlation, gross/net exposure and sector caps stop being
  optional. The correlation machinery already exists in
  `backtest-service/portfolio.py` and moves to a shared library.
- Cost assumptions rise to 10 bps (large cap) and 25 bps (mid cap) round trip,
  and every result is reported against the stress case as well. Existing
  backtests will look worse. That is the point. Market impact is explicitly *not*
  prioritised: it is immaterial at this account size, and modelling it would be
  precision in the wrong place.
- Turnover becomes a first-class design constraint, not an outcome. A strategy is
  specified with a rebalance frequency it can afford at its universe's cost
  level, and a candidate that only works at daily turnover in mid caps is
  rejected on cost grounds before it is backtested.
- Once live, measured fills against arrival-price mid replace these priors. The
  `ExecutionQuality` record in `libs/journal` already carries requested,
  submitted and filled timestamps plus `spread_bps`, so the measurement needs no
  new storage.
- No new services. Four shared libraries and changes inside existing services,
  consistent with `project_spc.md`'s service map.
- The programme is falsifiable. Each phase carries a kill criterion, and a phase
  that fails its criterion stops the work rather than motivating more hardening
  around it.
- Risk accepted: a wider, less-liquid universe raises per-trade cost and
  borrow/short constraints. This is why the impact model and the cost parameters
  are prerequisites for believing any cross-sectional result.

## References

- McLean, R.D. & Pontiff, J. (2016), "Does Academic Research Destroy Stock Return
  Predictability?", *Journal of Finance* 71(1).
  https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2156623
- Bailey, D.H. & López de Prado, M. (2014), "The Deflated Sharpe Ratio",
  *Journal of Portfolio Management* 40(5): 94-107.
  https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551
- Gao, Z., Jiang, W. & Yan, Y., "Detecting Lookahead Bias in LLM Forecasts".
  https://arxiv.org/abs/2512.23847
- Schwarz, C., Barber, B., Huang, X., Jorion, P. & Odean, T. (2025), "The Actual
  Retail Price of Equity Trades", *Journal of Finance* 80(5).
  https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4189239
- Almgren, R. et al. (2005), "Direct Estimation of Equity Market Impact" — for
  the impact model deliberately *not* adopted at this account size.
- Cost parameters and their derivation: see TASK-009, "Cost model" section.
  Primary sources (Alpaca fee schedule, SEC Section 31 rate, FINRA TAF) were
  reached through search extracts rather than fetched directly, as the research
  environment's egress proxy blocked those domains; they should be confirmed
  against the primary pages before the numbers are treated as settled.
