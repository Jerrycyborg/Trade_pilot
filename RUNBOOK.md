# Trade_pilot Operational Runbook

## Kill Switch Procedure

**To halt all trading immediately:**
```bash
curl -X POST http://localhost:8007/v1/orchestrator/kill-switch \
  -H "X-Internal-Key: $INTERNAL_API_KEY" \
  -H "X-Admin-Key: $ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"active": true}'
```

Verify: dashboard shows red "KILL SWITCH ACTIVE" banner.
The orchestrator will halt on its next cycle check (within `ORCHESTRATOR_INTERVAL_MINUTES`).
Open positions are NOT auto-closed by the kill switch — close them manually.

**To re-enable trading:**
```bash
curl -X POST http://localhost:8007/v1/orchestrator/kill-switch \
  -H "X-Internal-Key: $INTERNAL_API_KEY" \
  -H "X-Admin-Key: $ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"active": false}'
```

## Adjusting the Weekly Cap

Edit `config/policy-baseline.yaml`:
```yaml
weekly_notional_cap_usd: 5000  # change to desired amount
```
The orchestrator reloads this file on every cycle — no restart needed.

## Adding Symbols to Allowlist

Edit `config/policy-baseline.yaml`:
```yaml
symbol_allowlist:
  - AAPL
  - MSFT
  - NEW_SYMBOL  # add here
```
Then run allowlist validation:
```bash
curl -X POST http://localhost:8007/v1/orchestrator/validate \
  -H "X-Internal-Key: $INTERNAL_API_KEY"
```
Review the response — remove any "invalid" symbols.

## Sector Concentration Control

Edit `config/policy-baseline.yaml`:
```yaml
max_sector_concentration: 2  # max positions per sector
```
Supported sectors (hardcoded): equity_index (SPY/QQQ/IWM), bonds (TLT/BND/SHY), commodities (GLD), tech (AAPL/MSFT/GOOGL/AMZN/NVDA/META).

## Promoting Demo → Live

1. Confirm 30+ days paper trading with audit log review
2. Confirm all approval tiers have been tested
3. Set `ETORO_DEMO=false` in `.env` and restart execution-service
4. Enable live mode (see README.md step-by-step)
5. Monitor first 10 live trades via dashboard and audit log:
   ```bash
   curl http://localhost:8006/v1/audit/summary
   ```

## Approving/Rejecting Tier 2/3 Trades

List pending approvals:
```bash
curl http://localhost:8010/v1/approvals/pending
```

Approve:
```bash
curl -X POST http://localhost:8010/v1/approvals/{approval_id}/approve \
  -H "X-Internal-Key: $INTERNAL_API_KEY"
```

Reject:
```bash
curl -X POST http://localhost:8010/v1/approvals/{approval_id}/reject \
  -H "X-Internal-Key: $INTERNAL_API_KEY"
```

Tier 2 approvals auto-expire after 15 minutes (configurable in policy-baseline.yaml).

## Intraday Troubleshooting

### Check what resolution the loop is actually running at

```bash
curl http://localhost:8007/v1/orchestrator/realtime
```

`"intraday": false` means the loop is on daily bars regardless of what you
intended — check `MARKET_DATA_TIMEFRAME` in `.env` and restart the orchestrator.

### "degrading to DAILY bars" in the orchestrator log

Intraday data could not be fetched from any provider, so indicators and stops
silently changed meaning. The strategy is no longer intraday. Causes, in order
of likelihood:

1. No network route to the provider — run `uv run python scripts/verify_intraday.py`
2. `INTRADAY_MINUTES` set to a resolution Yahoo does not serve (it offers
   1, 2, 5, 15, 30, 60, 90; anything else snaps down to the nearest)
3. `INTRADAY_LOOKBACK_DAYS` beyond the provider's window — Yahoo caps 1-minute
   history at 7 days and most other intraday resolutions at 60
4. Alpaca rate limit or expired keys

### No trades are being placed

Check the audit log for the rejection reason first:

```bash
curl "http://localhost:8006/v1/audit/logs?event_type=signal.rejected&limit=20"
```

- `stale_data` — the price was older than `POLICY_MAX_DATA_AGE_SECONDS`
  (default 30s), or none could be resolved at all. The system is failing closed
  by design. **On the Yahoo provider this is expected and permanent**: its feed
  is ~15 minutes delayed, so nothing it returns can satisfy a 30-second limit.
  Either move to Alpaca, or raise `POLICY_MAX_DATA_AGE_SECONDS` deliberately,
  accepting that you are trading on delayed prices. Check
  `/v1/orchestrator/realtime` for the age of each cached price.
- `market_closed` / `outside_trading_hours` — expected outside the session.
  Note the Yahoo path uses a weekday heuristic that does not know about market
  holidays; Alpaca's clock does.
- `max_size_exceeded` — `max_position_size_pct` in `config/policy-baseline.yaml`.

### Stops are firing late

The stop-loss and take-profit monitors poll. Under intraday they default to
every 60 seconds; a stop can overshoot by at most one interval. Tighten with
`STOP_LOSS_CHECK_INTERVAL_MINUTES` (fractions are accepted — `0.5` is 30s).
With Alpaca, set `STREAMING_ENABLED=true` so each check reads a cached
streamed price rather than making an HTTP call.

### Paper broker state

Paper positions live in `PAPER_STATE_PATH` (default
`./paper-broker-state.json`) and survive restarts. To start a fresh run:

```bash
rm -f paper-broker-state.json
```

Inspect current paper P&L:

```bash
curl http://localhost:8002/v1/account
```

## Day-Trade Budget (PDT)

Check remaining budget:
```bash
curl http://localhost:8007/v1/orchestrator/day-trades
```

### "New entries paused — pdt_day_trade_limit"

Expected, not a fault: three day trades have been taken inside the rolling
five-business-day window and account equity is under the threshold. Exits still
work; only new entries are blocked. The budget frees up as sessions roll off.

To trade through it, one of:
- Fund the account above `PDT_EQUITY_THRESHOLD_USD` (25,000 by default)
- Set `PDT_ENABLED=false` **only if the rule does not apply to your broker or
  jurisdiction** — confirm with the broker first
- Wait for the window to roll

### Budget looks wrong after a restart

State lives in `PDT_STATE_PATH` (default `./day-trade-state.json`). If it was
deleted the count restarts at zero while the broker still remembers, which can
put the account over the real limit. Reconcile against the broker's own day
trade counter before resuming.

### Holiday weeks

The window is weekday-based and does not know market holidays, so a day trade
can expire one session early in a holiday week. Set `PDT_MAX_DAY_TRADES=2` for
margin if that matters.

## Checking Whether the Strategy Makes Money

```bash
uv run python scripts/run_backtest.py --symbols AAPL,MSFT,NVDA
uv run python scripts/run_backtest.py --symbols AAPL --sweep   # cost sensitivity
```

Read the **cost drag** line first. If gross is positive and net is negative, the
strategy has a signal but not an edge — costs consume it, and running it faster
only loses money quicker.

"No trades taken" is not a pass: it means the entry conditions never all held,
so there is nothing to evaluate. Lengthen `--days` or check the warm-up (EMA-50
needs 51 bars before any signal is possible).

## Deciding Whether an Edge Is Real

Run this before a strategy change reaches paper, and again before paper reaches
live. It is the check that a profitable-looking backtest cannot substitute for.

```bash
uv run python scripts/run_backtest.py --symbols AAPL --walk-forward
```

Three verdicts come back. Act on them in this order:

### [FAIL] Out-of-sample profitable

The strategy made money on the data it was tuned on and lost it on the data
that followed. Nothing else in the report matters. Do not deploy, do not widen
the grid to find a configuration that passes — that is more of the same error.

### [FAIL] Survives the search

Out-of-sample was profitable, but not by more than a search of this size finds
in noise. Options, in descending order of honesty:

1. **Get more data.** The deflated ratio rises with sample length. A 59-day
   intraday window is short; the same result over a year may clear the bar.
   Note that Yahoo caps intraday history (7 days at 1-minute, 60 at most other
   resolutions), so this usually means Alpaca.
2. **Narrow the grid.** Fewer configurations set a lower bar — but only do this
   by removing parameters you had no reason to vary, never by removing the ones
   that happened to lose.
3. **Accept it as unproven.** Paper trade it and collect out-of-sample evidence
   forward in time, which is the only kind that does not cost a trial.

What not to do: re-run with a different seed, symbol or window until one
passes. Each attempt is another trial, and none of them get counted.

### [FAIL] Folds agree on parameters

Each fold picked different "optimal" settings, which means the optimum is a
property of the window rather than of the market. The strategy may still have
an edge — try fixing the parameters at the default and running walk-forward
with a single-point grid. If it survives without being tuned per fold, the
tuning was the problem, not the idea.

### Everything passed

It has cleared one specific way of being wrong. It has not cleared:

- **Multiple symbols.** Running this on twenty symbols and deploying the best
  is the same selection error one level up. Decide the symbol list first.
- **Small samples.** Below 30 out-of-sample trades the report warns, and the
  warning should be read as disqualifying rather than advisory.
- **Assumed costs.** Re-run with the measured figure from
  `/v1/execution/quality` (see the section above) before believing the return.

### Parameter sensitivity

```bash
uv run python scripts/run_backtest.py --symbols AAPL --sensitivity
```

Use it to understand *why* a walk-forward result came out the way it did, not
as a pass/fail on its own. A spike — the best configuration far above its
immediate neighbours — explains a failed walk-forward. A plateau does not
prove anything on its own: on a sample that happened to trend, every momentum
configuration profits and they form a plateau together.

### "Not enough data for N walk-forward folds"

Each fold needs an initial training window plus a test window, on top of the
indicator warm-up. Either raise `--days`, drop `--splits` to 2 or 3, or move to
a smaller `--minutes` so the same calendar window yields more bars.

### The run is slow

The grid is 81 configurations by default and the walk-forward runs it once per
fold. A year of 1-minute bars is a large job. Narrow the grid via the service's
`grid` parameter rather than reducing folds — folds are the part doing the
actual validating.

## Checking What Execution Is Costing You

```bash
curl http://localhost:8002/v1/execution/quality
```

Read three numbers, in this order:

1. **`fill_rate`** — the share of orders that actually filled. A rate well below
   1.0 means the strategy you are running is not the strategy you backtested:
   some of its trades never happened. Widen `LIMIT_TOLERANCE_BPS`, or accept
   that those signals were the expensive ones.
2. **`mean_shortfall_bps`** — average cost per fill, positive being a cost.
   This is the honest input to the backtest's slippage assumption.
3. **`worst_shortfall_bps`** and **`mean_shortfall_by_symbol`** — where the cost
   actually lives. One symbol paying several times the rest is a liquidity
   problem in that symbol; consider dropping it from the watchlist rather than
   widening the tolerance for everything.

Then close the loop — re-run the backtest with the measured cost rather than
the default guess:

```bash
uv run python scripts/run_backtest.py --symbols AAPL,MSFT --slippage-bps 2.5
```

If the strategy was net positive at the assumed cost and net negative at the
measured one, the edge was in the assumption. That is a finding, not a
setback — it is exactly what this measurement exists to catch, and it is
cheaper to learn here than from the account balance.

### Fill rate has collapsed

Every order coming back `limit_not_marketable` usually means one of:

- **The tolerance is too tight for the symbol's spread.** 10bps on a stock with
  a 30bps spread will essentially never fill. Raise `LIMIT_TOLERANCE_BPS` or
  trade something tighter.
- **The decision price is stale.** Check `/v1/orchestrator/realtime` for data
  age. A limit priced off a two-minute-old quote is priced off a market that
  has moved on.
- **The market is moving fast.** Legitimate: this is the protection working,
  and the fills you are missing are the ones that would have been worst.

To confirm the mechanism rather than the market, set `USE_LIMIT_ORDERS=false`
for one session and compare `mean_shortfall_bps` with limits on and off.

### Orders are smaller than expected

Sizing is capped at `MAX_ADV_PARTICIPATION` of average daily volume. The
orchestrator logs the trim (`Order for X trimmed 500 -> 40 shares`). If a
symbol is consistently trimmed hard it is too thin for the size you are
trading — reduce the position size or drop the symbol. Raising the cap does not
make the liquidity appear; it moves the cost from the trim into the fill price.

## Adding or Combining Strategies

### Seeing what is available

```bash
curl http://localhost:8011/backtest/strategies
```

### Before adding a strategy to the portfolio

Run it on its own first, then in combination. A strategy that fails
walk-forward alone does not become viable by being averaged with others.

```bash
uv run python scripts/run_backtest.py --symbols AAPL --walk-forward
uv run python scripts/run_backtest.py --symbols AAPL,MSFT --portfolio
```

Then check one thing above all: `max_correlation`. If the new strategy
correlates above 0.7 with one already in the portfolio, it is not a new
strategy — it is the existing one at a different size, and adding it doubles
the cost without changing the risk.

### "[FAIL] Sleeves actually diversify"

Diversification ratio near 1.0. The sleeves move together. Causes, in order:

1. **Same idea, different parameters.** Two trend rules will correlate whatever
   their lookbacks. Check `/backtest/strategies` — the two shipped rules read
   disjoint parameters precisely so this cannot happen by accident.
2. **Same symbol.** Two strategies on one symbol share its moves. Spread across
   symbols before adding strategies.
3. **Correlated symbols.** Three large-cap US tech names are close to one
   position. The report cannot know that from prices alone over a short window
   — you have to.

### "[FAIL] Beats its best sleeve"

The combination did worse than one of its parts. This is not automatically a
reason to drop the others: the best sleeve is only knowable in hindsight, and
choosing it after the fact is the selection error this whole section exists to
prevent. What it *is* a reason to do:

- Check whether the losing sleeves failed walk-forward individually. If so,
  they should not have been in the portfolio.
- Check the trade counts. A sleeve with four trades has not demonstrated
  anything either way.

### "[FAIL] Survives the search"

Same meaning as in walk-forward, and here the trial count is usually the
problem: it defaults to the number of sleeves. If you screened a watchlist and
kept the best performers, pass `--considered <how many you looked at>` and read
the result again. It will be worse, and it will be correct.

### A strategy was dropped from the run

The report names it and why — either no bars were supplied for the symbol, or
there were fewer bars than the strategy's warm-up needs. `bollinger_reversion`
warms up faster than `ema_rsi_macd` (no MACD), so a short window can produce a
portfolio where only one strategy ran. Check the sleeve list matches what you
asked for before reading any of the numbers.

## Position Break — "New entries paused"

The ledger and the broker disagree about what is held. Exits still work; only
new entries are blocked.

```bash
curl "http://localhost:8007/v1/orchestrator/reconciliation?refresh=true"
```

Act on the `kind` field:

- **`phantom_position`** — we think we hold something the broker does not. Most
  urgent: a stop-loss is watching a position that is not there. Usually a close
  that succeeded at the broker without a fill being recorded. Check
  `/v1/fills`, then re-run `POST /v1/portfolio/reconcile`.
- **`untracked_position`** — the broker holds something we do not know about.
  Either a manual trade placed outside the system, or an order that filled
  after our record of it failed. **Decide manually whether to keep or close
  it** — do not let the system adopt it silently.
- **`quantity_mismatch`** — usually a partial fill. Compare `/v1/fills` for the
  symbol against the broker's own quantity.

Once resolved, the next check clears the halt automatically. To clear a break
you have judged to be spurious, restart the orchestrator — the counter is
in-memory by design, so a halt never outlives an operator decision.

If reconciliation reports `ok: false` with an `error` rather than breaks, a
service is unreachable. That is not a divergence and does not halt trading.

## Reading the Archive

```bash
curl http://localhost:8007/v1/orchestrator/journal          # coverage + decisions
curl "http://localhost:8007/v1/orchestrator/journal?symbol=AAPL&limit=50"
```

To ask why a specific trade happened, find its `correlation_id` (the signal id)
and read every stage of that signal's journey:

```sql
SELECT ts, stage, outcome, reason, inputs_json
FROM decisions WHERE correlation_id = '<signal-id>' ORDER BY ts;
```

To find what the system refused and why:

```sql
SELECT symbol, COUNT(*), reason FROM decisions
WHERE outcome = 'rejected' GROUP BY symbol, reason ORDER BY 2 DESC;
```

Stale-price refusals live in `price_observations` with `accepted = 0`. A run of
those explains a quiet day better than any log will.

### The archive is growing too fast

At minute resolution across a large allowlist the file grows steadily. Bars
deduplicate, so growth is bounded by real market time, not by cycle frequency.
Prices and decisions are append-only. To trim, delete old rows rather than the
file — losing history costs you the research you are capturing it for:

```sql
DELETE FROM price_observations WHERE observed_at < date('now', '-90 days');
VACUUM;
```

## Emergency Procedures

### Service crash
Check service logs. Restart the crashed service. The orchestrator will resume on next cycle.
Kill switch is persisted in `policy-baseline.yaml` — it survives restarts.

### Unexpected position opened
1. Activate kill switch immediately (see above)
2. Manually close the position via eToro web/app
3. Review audit log: `curl http://localhost:8006/v1/audit/logs?limit=50`
4. Identify the signal that triggered it and fix the policy config

### Weekly cap exhausted
The system will reject all new BUY signals until Monday (cap resets weekly).
To override for an emergency: increase `weekly_notional_cap_usd` in policy-baseline.yaml.

### Drawdown circuit breaker triggered
If portfolio drops >5% from peak, all new entries are paused.
Review positions, decide if you want to override: increase `drawdown_circuit_breaker_pct` in policy-baseline.yaml temporarily.

## Audit Log Review

```bash
# Last 50 events
curl "http://localhost:8006/v1/audit/logs?limit=50"

# Only rejections
curl "http://localhost:8006/v1/audit/logs?event_type=signal.rejected"

# Summary stats
curl "http://localhost:8006/v1/audit/summary"
```

## Health Check

```bash
# All services
for port in 8001 8002 8003 8004 8005 8006 8007 8008 8009 8010 8011; do
  echo -n "Port $port: "
  curl -s http://localhost:$port/health | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('status','?'))"
done

# Dependency status from orchestrator
curl http://localhost:8007/v1/orchestrator/health/deps
```
