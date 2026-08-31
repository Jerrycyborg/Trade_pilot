![Trade Pilot banner](assets/trade-pilot-banner.svg)

# Trade_pilot — Autonomous Trading Platform

Production-minded AI trading stack: strategy proposes, policy approves, execution fills, and portfolio reconciles. This repo includes autonomous orchestration, approvals, notifications, sentiment, audit logging, and dashboard controls on top of the core trading services.

## Architecture

```text
┌─────────────────────────────────────────────────────────────────┐
│                    Autonomy Orchestrator :8007                  │
│        scheduler → signal fetch → risk → policy → execute      │
└──────┬──────────────┬────────────────┬───────────────┬──────────┘
       │              │                │               │
       ▼              ▼                ▼               ▼
  Risk Engine    Policy Gate      Execution        Audit Logger
  (sizing,       (hard rules,     Service :8002    :8006
   drawdown,      sector conc,    (broker order)   (append-only)
   PDT, sector)   event block)         │
                       │               ▼
                       │         Broker (eToro/Paper)
                       │
              ┌────────┴─────────┐
              ▼                  ▼
     Notification :8009    Approval Gateway :8010
     (webhook, tiered)     (PENDING/APPROVE/REJECT)

── External Data ──────────────────────────────────────────────────
  Strategy Service :8003   (signals, TA, ADX, patterns)
  Portfolio Service :8004  (positions, NAV)
  Research Service :8005   (AI research summaries)
  Sentiment Aggregator :8008 (NewsAPI, AlphaVantage)
  Dashboard :8080          (kill switch UI, approvals, stats)
```

## Service Port Map

| Service | Port | Purpose |
|---------|------|---------|
| policy-service | 8001 | Policy evaluation (hard rules gate) |
| execution-service | 8002 | Order routing to broker |
| strategy-service | 8003 | Signal generation (TA, ADX, patterns, volume) |
| portfolio-service | 8004 | Position tracking and NAV |
| research-service | 8005 | AI-powered research summaries |
| audit-logger | 8006 | Append-only audit trail (SQLite) |
| autonomy-orchestrator | 8007 | Main loop scheduler and decision engine |
| sentiment-aggregator | 8008 | News/sentiment scoring |
| notification-service | 8009 | Webhook notifications (tiered) |
| approval-gateway | 8010 | Human approval flow (PENDING/APPROVE/REJECT) |
| backtest-service | 8011 | Backtesting, walk-forward, parameter sensitivity |
| dashboard | 8080 | Web UI (kill switch, approvals, stats bar) |

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `INTERNAL_API_KEY` | Yes (prod) | Shared secret for service-to-service auth |
| `ADMIN_API_KEY` | Yes (prod) | Extra key for kill switch / live mode endpoints |
| `ETORO_API_KEY` | Yes | eToro public API key |
| `ETORO_USER_KEY` | Yes | eToro user key |
| `ETORO_DEMO` | No | Set `true` for eToro demo account (default true) |
| `ANTHROPIC_API_KEY` | Yes | For AI research summaries |
| `NEWSAPI_KEY` | No | NewsAPI.org key for sentiment |
| `ALPHAVANTAGE_KEY` | No | AlphaVantage key for sentiment |
| `WEBHOOK_URL` | No | Slack/Discord/custom webhook for notifications |
| `BROKER` | No | `etoro` or `paper` (default paper) |
| `WORKER_ENABLED` | No | `true` to enable strategy worker polling |
| `ORCHESTRATOR_INTERVAL_MINUTES` | No | Cycle interval (default 5) |
| `STOP_LOSS_PCT` | No | Stop loss % (default 0.03 = 3%) |
| `TAKE_PROFIT_PCT` | No | Take profit % (default 0.06 = 6%) |
| `MAX_HOLD_HOURS` | No | Max position hold time in hours (default 48) |
| `VOLUME_CONFIRM_ENABLED` | No | Require above-avg volume for BUY (default true) |
| `STRATEGY_WATCHLIST` | No | Comma-separated symbols to trade |

### Intraday / real-time

| Variable | Default | Description |
|----------|---------|-------------|
| `MARKET_DATA_TIMEFRAME` | `daily` | Set to `intraday` to trade on intraday bars |
| `INTRADAY_MINUTES` | `15` | Bar size. Yahoo serves 1, 2, 5, 15, 30, 60, 90 |
| `INTRADAY_LOOKBACK_DAYS` | `5` | Bar history for indicators (Yahoo caps 1m at 7 days) |
| `MARKET_DATA_PROVIDER` | auto | `yahoo` forces Yahoo; blank uses Alpaca when keys are set |
| `STREAMING_ENABLED` | `false` | Real-time websocket bar stream (Alpaca only) |
| `STREAM_SYMBOLS` | allowlist | Symbols to stream |
| `STREAM_SYMBOL_LIMIT` | `30` | Cap on concurrent subscriptions (free IEX feed) |
| `ALPACA_FEED` | `iex` | `sip` on a paid Alpaca data plan |
| `MAX_PRICE_AGE_SECONDS` | `120` | Older prices are treated as unusable |
| `ORCHESTRATOR_INTERVAL_SECONDS` | — | Cycle cadence; overrides the minutes setting |
| `STOP_LOSS_CHECK_INTERVAL_MINUTES` | 1 intraday / 5 daily | Stop-loss poll interval |
| `TAKE_PROFIT_CHECK_INTERVAL_MINUTES` | 1 intraday / 5 daily | Take-profit poll interval |
| `PAPER_STARTING_CASH` | `100000` | Paper broker opening cash |
| `PAPER_SLIPPAGE_BPS` | `2` | Simulated slippage, always against the trader |
| `PAPER_STATE_PATH` | `./paper-broker-state.json` | Paper position ledger |

### Execution quality

| Variable | Default | Description |
|----------|---------|-------------|
| `USE_LIMIT_ORDERS` | `true` | Send marketable limit + IOC instead of market orders |
| `LIMIT_TOLERANCE_BPS` | `10` | How far through the touch the limit is priced |
| `MAX_ADV_PARTICIPATION` | `0.01` | Cap an order at this share of average daily volume (0 disables) |

## Getting eToro API Keys

1. Log into your eToro account at etoro.com
2. Go to Settings → API (or developer.etoro.com)
3. Create an API key pair — copy the public key and user key
4. Set `ETORO_API_KEY` and `ETORO_USER_KEY` in your `.env`
5. Keep `ETORO_DEMO=true` until you are ready for live trading

## Running in Demo Mode

```bash
cp .env.example .env
# Edit .env — at minimum set ANTHROPIC_API_KEY and ETORO_API_KEY/USER_KEY
# Leave ETORO_DEMO=true and BROKER=paper (or etoro with demo=true)

# Install dependencies
uv sync

# Start all services (each in its own terminal or use a process manager)
uv run uvicorn policy_service.main:app --port 8001
uv run uvicorn execution_service.main:app --port 8002
uv run uvicorn strategy_service.main:app --port 8003
uv run uvicorn portfolio_service.main:app --port 8004
uv run uvicorn research_service.main:app --port 8005
uv run uvicorn audit_logger.main:app --port 8006
uv run uvicorn autonomy_orchestrator.main:app --port 8007
uv run uvicorn sentiment_aggregator.main:app --port 8008
uv run uvicorn notification_service.main:app --port 8009
uv run uvicorn approval_gateway.main:app --port 8010
uv run uvicorn backtest_service.main:app --port 8011

# Open dashboard
open apps/dashboard/index.html
```

## Real-Time Intraday Trading

Two data paths are supported. Both run the same trading loop.

| | Alpaca | Yahoo |
|---|---|---|
| API key | free account required | none |
| Intraday bars | real-time | delayed, typically ~15 min |
| Websocket stream | yes | no |
| Market calendar | authoritative (holidays, half-days) | weekday heuristic only |
| Paper fills | real Alpaca paper account | local simulator |

### Yahoo (no signup)

```bash
MARKET_DATA_PROVIDER=yahoo
MARKET_DATA_TIMEFRAME=intraday
INTRADAY_MINUTES=15
BROKER=paper
```

Yahoo's intraday feed is delayed, so this is intraday but not truly real-time.
It is the right setting for validating the pipeline before committing to a data
provider. Prices are resolved by polling.

### Alpaca (real-time)

```bash
ALPACA_API_KEY=...
ALPACA_SECRET_KEY=...
ALPACA_PAPER=true
MARKET_DATA_PROVIDER=          # blank — auto-selects Alpaca
MARKET_DATA_TIMEFRAME=intraday
INTRADAY_MINUTES=5
STREAMING_ENABLED=true
BROKER=alpaca
```

Keys come from the Alpaca dashboard (alpaca.markets → Paper Trading → API
Keys). With `STREAMING_ENABLED=true` the orchestrator subscribes to 1-minute
bars over a websocket and serves prices from an in-memory cache, so stops are
evaluated against a price that is seconds old rather than one HTTP call away.

### Yahoo cannot satisfy the default policy limit

Worth knowing before you pick a provider. Yahoo's intraday feed is delayed by
roughly 15 minutes, while policy-service rejects any decision built on data
older than `POLICY_MAX_DATA_AGE_SECONDS` (default **30 seconds**). Under
defaults, the Yahoo path therefore places **zero live orders** — every signal is
rejected as `stale_data`. That is the system failing closed correctly, not a
bug.

Three ways forward:

| | What you get |
|---|---|
| **Use Alpaca** | Real-time prices; the default limit works as intended |
| **Raise `POLICY_MAX_DATA_AGE_SECONDS`** | Trading on ~15-minute-old prices. Defensible on a 15-minute bar strategy, indefensible on a 1-minute one. Set it deliberately, not to make a warning go away |
| **Use Yahoo for backtesting only** | Historical bars have no freshness requirement, so `run_backtest.py` works fine on Yahoo regardless |

`scripts/verify_intraday.py` checks freshness against the *policy* limit and
fails when it is exceeded, so this shows up before you start rather than as a
run of rejections in the audit log.

### Verify before trading

Unit tests cannot prove that *this host* can reach a data provider. Run the
preflight on the machine that will trade:

```bash
uv run python scripts/verify_intraday.py
uv run python scripts/verify_intraday.py --symbols AAPL,MSFT --stream 30
```

It checks configuration, the market session, that intraday bars really arrive
at the configured resolution, and that prices are fresh enough for the policy
service to accept. It exits non-zero if anything fails.

### Observing the loop

```bash
curl http://localhost:8007/v1/orchestrator/realtime
```

Reports the resolution the loop is actually running at: timeframe, provider,
cycle and risk-check cadence, stream state, and the age of every cached price.
Check this first if trades are being rejected — a `degrading to DAILY bars`
error in the orchestrator log means intraday data could not be fetched and the
strategy is no longer running at intraday resolution.

### How prices are resolved

Each price lookup tries three tiers, freshest first:

1. the websocket stream's in-memory cache (sub-second, Alpaca only)
2. the provider's latest-trade endpoint (one HTTP call)
3. the close of the most recent bar

Every result carries a timestamp. If no tier can supply a price, the strategy
reports the data as **stale** rather than fresh, and the policy service rejects
the trade. The system fails closed: no price means no order.

## Does the Strategy Actually Work?

The live loop will trade a strategy with no edge just as happily as a good one.
Backtest before trusting it with money.

```bash
# 60 days of 15-minute bars across three symbols, with realistic costs
uv run python scripts/run_backtest.py --symbols AAPL,MSFT,NVDA

# how much cost does the edge survive?
uv run python scripts/run_backtest.py --symbols AAPL --sweep
```

The report separates **gross** return from **net**, because on an intraday
strategy the gap between them is usually the whole story:

```
  Net return        +2.14%
  Gross (no costs)  +8.90%
  Cost drag         -6.76%  ($6,760, 76% of gross)
```

Costs default to 5bps spread + 1bps slippage + zero commission — roughly a
liquid US large-cap at a commission-free broker. **Set them to match your broker
and your symbols.** A wider spread is the fastest way for an intraday strategy
to stop working, and `--sweep` shows exactly where it breaks:

```
      spread       return    trades
       0.0bps       8.90%        84
       5.0bps       2.14%        84
      10.0bps      -4.61%        84     <- edge gone
```

If the strategy is unprofitable at your real costs, no amount of faster
execution will fix that. Fix the strategy or stop.

### What the backtest does not tell you

- It runs one symbol at a time with no portfolio interaction.
- It assumes every order fills at the next bar's open. Real market orders in
  fast markets do worse.
- Past results do not predict future returns. A strategy tuned until a backtest
  looks good is a strategy fitted to history — which is what the next section
  is for.

## Is the Edge Real, or Fitted?

A backtest over the whole history answers "what would have happened?". It
cannot answer "would it happen again?", and it is trivially gamed: try enough
parameter combinations and one of them fits the sample.

This is not a subtle risk. The suite contains a test that builds **200
strategies out of pure random noise**, picks the best one, and checks the
numbers it produces:

```
best of 200 noise runs:  annualised Sharpe 10.45
naive confidence:        99.8%
deflated Sharpe ratio:    0.53   <- a coin flip
```

Nothing was there. The first two numbers would still get a strategy funded.

Two commands exist to catch this.

### Walk-forward: choose on the past, judge on what followed

```bash
uv run python scripts/run_backtest.py --symbols AAPL --walk-forward
```

The data is split into sequential folds. For each fold, every configuration in
the grid is scored on the bars *before* the test window, the best one is
selected, and only then is it run on the test window. The out-of-sample
segments are stitched together and reported; the in-sample figures appear for
one reason, which is to show the size of the drop.

```
  fold 1  2025-05-07 -> 2025-05-10  ema10/60 rsi40-70 macd>0
          in-sample    3.66   out-of-sample   -0.22   return -0.35%  (5 trades)
  fold 2  2025-05-10 -> 2025-05-14  ema10/60 rsi40-70 macd>0
          in-sample    1.97   out-of-sample    3.26   return +3.71%  (3 trades)

  Out-of-sample Sharpe             5.20
  In-sample Sharpe (mean)          2.75
  Degradation                     -2.44

  [PASS] Out-of-sample profitable     +20.78%
  [FAIL] Survives the search          deflated Sharpe ratio 0.949
  [PASS] Folds agree on parameters    67% picked the same configuration
```

The three checks, and why each is there:

| Check | What it catches |
|---|---|
| Out-of-sample profitable | The strategy only worked on the data it was tuned on |
| Survives the search | The winner is what a search of this size finds in noise |
| Folds agree on parameters | Each fold's "optimal" settings are a property of its window |

**Read `sharpe_degradation` first.** A strategy scoring 2.5 in-sample and 0.1
out-of-sample has not found an edge; it has memorised a sample. A *negative*
degradation means out-of-sample beat in-sample, which is luck rather than
vindication — with few folds it happens often.

Two design points worth knowing, because they are where leakage would hide:

- **The embargo.** Bars immediately before each test window are dropped from
  training. Adjacent bars carry nearly the same information — an EMA at the
  boundary is largely built from bars about to become test data — so optimising
  right up to the edge is close to optimising on the test set. Defaults to the
  indicator warm-up length.
- **Warm-up is not leakage.** The test segment computes its indicators from the
  bars preceding it. That is what live trading does; leakage would be using
  data from *after* the decision. Trades and equity are counted only from the
  test window's first bar.

### The deflated Sharpe ratio

The probability that the result beats what the best of N random configurations
would produce by luck alone. Below 0.95 it does not.

It rises with sample length and falls with the number of configurations tried
and how much they differ from each other — so a wider grid is not a more
thorough search, it is a more expensive one. It also corrects for skew and
kurtosis, which matters because "wins small and often, loses catastrophically"
has a flattering Sharpe and a real risk of ruin.

What is compared, precisely: the *out-of-sample* record is tested against a
benchmark built from the spread of the configurations' *in-sample* Sharpe
ratios. The textbook version tests the in-sample winner against that benchmark;
holding the out-of-sample record to it is the stricter of the two. Worth
knowing so the number is not read as the canonical statistic.

**The caveat no formula can fix:** the trial count is the configurations *this
run* evaluated. Every parameter you tried by hand beforehand, every strategy
variant abandoned along the way, and every symbol swapped in and out is also a
trial, and none of them are counted. The true multiple-testing burden is higher
than what is reported. Treat the number as an upper bound on your confidence,
not a measurement of it.

### Parameter sensitivity: plateau or spike?

```bash
uv run python scripts/run_backtest.py --symbols AAPL --sensitivity
```

Scores every configuration in the grid and looks at the shape of the surface
rather than its peak. A real effect is a plateau — the neighbours of the best
configuration work nearly as well. A fitted one is a spike, alone above
configurations that lose money.

```
  Profitable configurations    81/81 (100%)
  Neighbours of the best       4.35 Sharpe vs its 4.81 (90% retained)
  [PASS] Plateau, not a spike  the result survives one step in any parameter
```

**A plateau is necessary but not sufficient.** In a sample that happened to
trend, every momentum configuration profits and they form a plateau together —
this is reproducible on synthetic random-walk data. Sensitivity is in-sample by
design and reads as a companion to `--walk-forward`, never as a substitute.

### Via the service

```bash
curl -X POST http://localhost:8011/backtest/walk-forward \
  -H "Content-Type: application/json" \
  -d '{"symbol":"AAPL","timeframe":"intraday","period_days":59,"n_splits":4}'

curl -X POST http://localhost:8011/backtest/parameter-sensitivity \
  -H "Content-Type: application/json" -d '{"symbol":"AAPL"}'
```

Both accept a `grid` object to narrow or widen the search, and walk-forward
accepts `n_splits`, `embargo_bars` and `objective` (`sharpe`, `return` or
`profit_factor`).

### What this still does not prove

Being explicit, because these checks are easy to over-read:

- **It is one symbol at a time.** Passing on AAPL says nothing about the
  portfolio, and running the same test on twenty symbols and reporting the best
  is the same multiple-testing error at a different level.
- **The out-of-sample sample is small.** Walk-forward over a 60-day intraday
  window produces a handful of round trips per fold. A Sharpe from nine trades
  is an anecdote; the report says so when the count is below 30.
- **Costs are still assumed.** Feed the measured `mean_shortfall_bps` from
  `/v1/execution/quality` into `--slippage-bps` so this is validating the
  strategy you would actually run.
- **Passing is not a green light.** It removes one specific way of being wrong.

## Pattern Day Trader (PDT) Protection

Intraday trading in the US runs into a rule that automated systems breach
quickly: **four or more day trades in five business days, with account equity
under $25,000**, gets an account designated a pattern day trader and restricted.

The orchestrator tracks day trades in a rolling five-business-day window and
blocks new **entries** once the budget is spent. It never blocks an exit —
holding a losing position past its stop to avoid a compliance flag trades a
regulatory problem for a financial one.

```bash
curl http://localhost:8007/v1/orchestrator/day-trades
```

```json
{"enabled": true, "day_trades_used": 2, "max_day_trades": 3,
 "day_trades_remaining": 1, "equity_threshold_usd": 25000.0}
```

| Variable | Default | Description |
|----------|---------|-------------|
| `PDT_ENABLED` | `true` | Set `false` if the rule does not apply to you |
| `PDT_EQUITY_THRESHOLD_USD` | `25000` | Above this the rule does not bite |
| `PDT_MAX_DAY_TRADES` | `3` | The 4th triggers designation; use `2` for margin |
| `PDT_STATE_PATH` | `./day-trade-state.json` | Persisted; the window spans 5 days |

**This is a US margin-account rule.** Cash accounts face settlement constraints
instead, and non-US brokers have their own regimes. Confirm which applies to you
with your broker rather than trusting this default — being flagged is
disruptive to undo. Two known limitations: market holidays are not known, so in
a holiday week a day trade can expire one session early (set
`PDT_MAX_DAY_TRADES=2` for margin); and crypto round trips are counted even
though FINRA's rule covers securities, which is conservative rather than unsafe.

## The Point-in-Time Archive

Every bar, every price and every decision the system makes is recorded to a
single SQLite file (`JOURNAL_PATH`, default `./journal.db`). Three tables:

| Table | Records | Deduplicated? |
|---|---|---|
| `bar_observations` | OHLCV as delivered by a provider | Yes, on (symbol, timeframe, bar time) |
| `price_observations` | Every price resolved — **including ones refused as stale** | No |
| `decisions` | Each pipeline stage, with the inputs that produced it | No |

The distinction that matters is between **when the market printed a value** and
**when this system learned of it** (`bar_ts` vs `recorded_at`, `price_ts` vs
`observed_at`). Research that conflates the two is research contaminated by
hindsight, and you cannot recover the difference after the fact — which is why
this is worth capturing from the first day rather than the first loss.

Refused prices are archived deliberately: a price rejected as stale explains a
trade the system did *not* make, and a post-mortem that only sees fills cannot
account for it.

```bash
curl http://localhost:8007/v1/orchestrator/journal        # coverage + recent decisions
```

The file is plain SQLite — open it with any client, or load it into pandas:

```python
import pandas as pd, sqlite3
bars = pd.read_sql("SELECT * FROM bar_observations", sqlite3.connect("journal.db"))
```

Journalling never blocks trading. Every write is best-effort: a full disk
degrades research, it does not halt the loop or raise mid-order.

## Execution Quality

A strategy that looks profitable on close prices can lose money once it has to
trade. The gap between the two is execution cost, and until you measure it you
are guessing at the single number that decides whether an intraday edge
survives contact with the market.

### Marketable limit orders

Orders go out as **marketable limits with immediate-or-cancel**, not market
orders. A market order accepts whatever price the book offers — on a thin
symbol or during a spike that can be well away from what the strategy assumed.
A marketable limit is priced `LIMIT_TOLERANCE_BPS` through the reference price
in the paying direction (a buy at 200.00 with 10bps sends a 200.20 limit), so
it fills immediately under normal conditions but refuses a fill beyond the
tolerance. IOC means it fills now or cancels: nothing is left working that
would later need managing or cancelling.

The trade-off is explicit and it is a real one:

| | Narrow tolerance | Wide tolerance |
|---|---|---|
| Fill rate | Lower | Higher |
| Price paid when filled | Better | Worse |
| Failure mode | Signals missed | Bad fills accepted |

Neither failure is free. Missing the fills your backtest assumed changes the
strategy you are actually running, which is why misses are recorded as
carefully as fills.

Set `USE_LIMIT_ORDERS=false` to fall back to market orders — worth doing once,
side by side, to see what the limits are costing or saving you.

### Implementation shortfall

Every order carries the price the decision was based on (`decision_price`).
When it fills, the difference against the fill price is recorded as
implementation shortfall, in basis points, **signed so that positive is always
a cost** — a buy filled high and a sell filled low both count as costs. Without
that convention, averaging buys and sells together cancels real costs to zero
and the system reports free trading.

Orders that did not fill are recorded too, with `filled = 0`. A fill rate read
only from fills is 100% by construction.

```bash
curl http://localhost:8002/v1/execution/quality
```

```json
{
  "orders": 3,
  "filled": 2,
  "fill_rate": 0.6667,
  "mean_shortfall_bps": 2.5,
  "worst_shortfall_bps": 5.0,
  "mean_shortfall_by_symbol": {"AAPL": 1.0, "THIN": 5.0}
}
```

**Feed `mean_shortfall_bps` back into the backtest.** The backtest's slippage
assumption is otherwise a guess; this is a measurement from your own orders, on
your own symbols, at your own size. Run the strategy in paper mode long enough
to accumulate fills, then re-run the backtest with the measured figure:

```bash
uv run python scripts/run_backtest.py --slippage-bps <measured mean>
```

Read `worst_shortfall_bps` and the per-symbol breakdown as well as the mean. A
tolerable average with one symbol paying five times the rest is a liquidity
problem in that symbol, not a pricing problem in the strategy.

### Volume-aware sizing

An order that is a large fraction of a symbol's volume moves the price against
itself, so its cost becomes a function of its own size. Orders are therefore
capped at `MAX_ADV_PARTICIPATION` of average daily volume, inferred from the
bars already being fetched (intraday bar volumes are scaled up by the number of
bars in a session).

At the default 1% this never binds on mega-caps and binds hard on thin names —
which is the intent. A symbol too thin to support even one share at the cap is
skipped rather than rounded up to a size that would pay for its own impact.

Caveat, stated plainly: ADV inferred from a short intraday lookback is a rough
estimate, and it says nothing about the spread or the depth at the touch. It
is a guard against the obviously oversized order, not a market-impact model.

## Position Reconciliation

**The broker is the source of truth.** `portfolio-service` derives holdings from
our own fill history, which makes it a cache — and a cache that silently
diverges will eventually trade against a position that does not exist, or leave
a real position with nothing watching it.

A scheduled job compares the two and reports three kinds of break:

| Kind | Meaning |
|---|---|
| `untracked_position` | The broker holds something we do not know about |
| `phantom_position` | We think we hold something the broker does not — a stop watching nothing |
| `quantity_mismatch` | Both know the position, sizes disagree |

```bash
curl "http://localhost:8007/v1/orchestrator/reconciliation?refresh=true"
```

Two design choices worth knowing:

- **A single mismatch does not halt anything.** A fill in flight legitimately
  appears at the broker before it appears in our fills. Only a break that
  survives `RECONCILE_BREAKS_BEFORE_HALT` consecutive checks is believed.
- **Only entries are blocked, never exits.** Refusing to close a position you
  cannot account for is strictly worse than closing it.

An unreachable service is not treated as a divergence — otherwise every
container restart would stop trading.

## Enabling Live Mode (Step-by-Step)

⚠️ Only proceed after at least 30 days of demo/paper trading with no policy violations.

1. Set `ETORO_DEMO=false` and `BROKER=etoro` in `.env`
2. Set `ADMIN_API_KEY` to a strong random secret in `.env`
3. Restart all services
4. Verify kill switch is OFF in the dashboard
5. Set weekly cap in `config/policy-baseline.yaml` (`weekly_notional_cap_usd`)
6. Call the live-mode endpoint with admin credentials:
   ```bash
   curl -X POST http://localhost:8007/v1/orchestrator/live-mode \
     -H "X-Internal-Key: $INTERNAL_API_KEY" \
     -H "X-Admin-Key: $ADMIN_API_KEY" \
     -H "Content-Type: application/json" \
     -d '{"enable": true, "confirmation": "I CONFIRM LIVE TRADING"}'
   ```
7. Monitor the first 10 trades manually via the dashboard

## Running Tests

```bash
uv run pytest tests/ -x -q --ignore=tests/integration
```
