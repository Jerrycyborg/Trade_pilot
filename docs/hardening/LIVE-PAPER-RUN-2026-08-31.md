# Live paper run — 2026-08-31

The first run of the paper stack against real market data, and what it found.
Everything below is against the state of the branch at the start of the run;
each defect names the commit that fixed it. **Nothing here touched a real
broker and nothing was configured to be able to** — the execution service ran
with no live adapter, the lifecycle roster held only `paper` sleeves, and the
one challenger in the roster is categorically barred from live routing.

## What ran

Four services on localhost — policy (8001), execution (8002), strategy worker
(8003), portfolio (8004) — against a shared PostgreSQL lifecycle authority and
a per-run journal. The orchestrator (stop-loss, PDT, scheduled sweeps) was
**not** run; see findings.

Roster: three `paper` champions (`ema_rsi_macd` on AAPL, MSFT, NVDA) and one
bounded challenger (`ema_rsi_macd@chal-ab09a1a9aec8`, `rsi_buy_max` 70→80,
campaign `paper-run-2026-08-31`).

### Market data provenance

This sandbox's egress proxy blocks every configured market-data host (Yahoo,
Alpaca, Alpha Vantage) by organization policy, so the run used the
`FileDropFetcher` (`MARKET_DATA_PROVIDER=file`) with the operator refreshing
the drop directory from live sources reachable over MCP:

- **Daily history**: 41 bars per symbol (2026-07-02 → 2026-08-28), written to
  `<SYM>_1d.json` once at setup.
- **Live quotes**: refreshed into `quotes.json` during the run; quote
  timestamps were verified against their source's `updated_at` (within ~1
  minute of wall clock during market hours).

This is real market data on a delay measured in minutes, not a simulation —
but it is also not a streaming feed, which drove one disclosed setting below.

### Disclosed operator settings (not defaults, not bypasses)

| Setting | Default | This run | Why |
|---|---|---|---|
| `POLICY_MAX_DATA_AGE_SECONDS` | 30 | 600 | Quotes arrive by manual refresh, minutes old. The guard fired correctly at 30s (see below); 600s matches the run's actual data cadence. |
| `WORKER_INTERVAL_MINUTES` | 15 | 1, then 5 | 1-minute cycles to observe behaviour quickly; raised to 5 after the position-stacking finding. |
| `VETO_*` (read-back only) | 15m-cadence values | daily-cadence values | The veto's gap/staleness thresholds are written for intraday bars; the read-back battery reads a daily archive. Set via the documented env overrides, after the run, for analysis only. |

## Defects found by the run

Roughly 50 minutes of live running surfaced four defects that 862 passing
tests had missed. Each fix carries a test that fails with the defect
reinstated.

**1. The worker never authenticated its orders** — every order the strategy
worker submitted was rejected 401 by the execution service, because
`_submit_order` sent no `X-Internal-Key`. The integration tests had been
fixed to send the header; the production caller never was. Fixed in
`fix(worker): order submission never authenticated, and rejection was silent`.

**2. The rejection was silent** — the failed submission logged nothing above
debug and appended nothing to the cycle result, so a worker whose every order
bounced reported "0 orders, 0 errors" and looked idle rather than broken.
Fixed in the same commit: non-2xx now logs at error with status and body, and
lands in `result.errors`.

**3. Real fills were journalled unscoped** — the first genuine fill came back
`strategy_id='' environment=paper broker=''`. The service routed the order
*by* `request.strategy_id` and then dropped it when recording the fill, so
every fill in the archive was invisible to attribution, promotion evidence,
and the champion/challenger comparison. Fixed in
`fix(execution): a journalled fill now carries the sleeve it belongs to`.

**4. Attribution was long-only** — the run's one clean scoped trade was a
short (SELL 7 NVDA → BUY 7 to flatten), and `pair_round_trips` hard-coded
BUY-opens/SELL-closes, so L0 reported "no closed round trips" over an archive
that held one. Every downstream model was already direction-aware; the trips
just never reached it. Pairing now nets by direction, FIFO, with the flip
through flat handled. Same commit fixes the specialist archive reading a
hard-coded 15m slice of a daily archive (41 real bars per symbol reported as
zero, and the veto refused subjects the archive covered).

**Worth stating plainly:** defects 1–3 were invisible to a green 862-test
suite and surfaced within the first hour of contact with real data. The
suite's fixtures authenticated correctly, scoped correctly, and traded long.

## What the guards did (correctly)

- **`stale_data`**: the policy service rejected every entry while quotes were
  older than the (default 30s) limit — the first line of defence worked, and
  the limit was raised only by disclosed operator configuration.
- **`sleeve_not_registered`**: a reduce-only order submitted with an empty
  `strategy_id` was rejected by the router. Correct: an unregistered sleeve
  has never been permitted anything and holds nothing to reduce.
- **Fail-closed pricing**: the PaperBroker refused to fill until a live quote
  existed; slippage was always charged against the trader (2bps on every
  fill, exactly).
- **Health sweep**: `checked=0, errors=[]` — no live sleeves exist, so there
  was nothing to demote. Correct and verified once, after the run.

## The trades, and what L0 says about them

Five fills, all NVDA, all paper. Two SELLs predate fix 3 and are unscoped —
they are permanently unattributable, which is the honest cost of the defect
and is left in the archive as evidence. The scoped legs form one closed short
round trip plus an operator flatten of the unscoped rump:

| Leg | Side/qty | Fill | Shortfall |
|---|---|---|---|
| entry (worker signal) | SELL 7 | 219.4561 | 2.0 bps |
| exit (operator flatten) | BUY 7 | 219.5439 | 7.5 bps |

Attribution over the archive, after fix 4 — one round trip, 100% coverage:

```
From the signal      +0.84      (the short was directionally right)
Entry execution      -0.31
Exit execution       -1.15      (market-order flatten, 7.5 bps)
Realised             -0.61
```

Execution took 173% of what the signal earned. On a $1,536 position the
numbers are small; the *shape* — a right signal turned into a loss by a
market-order exit — is the kind of fact L0 exists to record. Day P&L across
all five fills: **+$0.85** (the unscoped legs happened to profit).

The specialist read-back over the same archive: MSFT in an established
uptrend (ADX 41.4 vs 20), AAPL and NVDA trendless, NVDA volatility agitated
relative to its own range. The technical role stayed silent at 41 bars
against its 60-bar floor — correct, not a defect. Reproducibility: 6/6
re-runs identical.

## Findings that are not yet fixes

- **Position stacking** *(since fixed)*: the worker re-evaluates each cycle
  with no position awareness, so a persistent signal re-enters every cycle
  (NVDA reached −12, then −19, before the interval was raised). Fixed in two
  layers: the worker now reads the sleeve's book from execution-service and
  skips same-direction entries, and execution-service enforces a per-sleeve
  position cap (`EXECUTION_MAX_POSITION_QTY`, default `EXECUTION_MAX_QTY`)
  from its own fill journal — reduce-only orders exempt, an unknowable book
  refusing entries rather than reading as flat.
- **The earnings gate fails open, silently** *(since fixed)*: `earnings_calendar.py`
  reached yfinance directly and swallowed failure by design ("never blocks on
  error") — under this sandbox's egress policy the gate simply wasn't there,
  and nothing said so. Worse, the worker hard-coded
  `event_blackout_active=False` into every policy request, so the policy
  service's hard event-blackout rejection could never fire on the path that
  was actually trading. Fixed: the check returns a verdict that distinguishes
  "no blackout" from "could not ask", an unanswerable calendar warns at
  WARNING (once per outage per symbol), the failure posture is explicit
  validated configuration (`EARNINGS_GATE_FAIL_CLOSED`, default open), and
  the worker now consults the gate instead of asserting False.
- **The orchestrator was not exercised** *(since drilled — 8 defects found
  and fixed)*: a controlled drill (isolated archive/broker/DB, synthetic
  prices via the file feed, disclosed drill-local baseline) exercised the
  entry → policy → stop-loss path end to end for the first time. It found:
  every scheduled risk job (stop-loss, take-profit, reconciliation, health
  sweep) dead on arrival — sync lambdas wrapping `asyncio.create_task` on an
  AsyncIOScheduler run in a loop-less executor thread, so no risk check had
  ever executed; an unguarded audit-logger call that wedged the orchestrator
  as "busy" forever (and sat outside the finally that resets the flag);
  seven more unguarded HTTP calls where one down dependency aborted the whole
  cycle — the worst sitting between the policy verdict and the order; the
  policy service rejecting sizes `>=` the cap that the ATR sizer clamps *to*,
  making the cap unreachable; the singular quote endpoint pricing marketable
  limits from a session-old close (four for four orders cancelled
  `limit_not_marketable` on a 1.1% gap); two PaperBroker instances over one
  state file, so reads answered from the twin that never traded; and the
  orchestrator emitting all its operational logs below the level anything
  printed. After the fixes, the drill ran clean: entry filled, stop
  registered at entry − 2·ATR, reconciliation flagged a ledger break and
  blocked entries while keeping exits, and the stop fired 37 seconds after
  the breach quote and flattened the lot. Two findings remain open: stop
  records are in-memory only, so positions opened before an orchestrator
  restart are unwatched until re-registered; and the monitor and the broker
  can price from different snapshots under lenient freshness limits (the
  drill's stop fired on 185 while the exit filled from a cached 220).
- **Regime classification is empty for daily-cadence runs** *(resolved)*:
  `attribute_trades.py --timeframe 1d` classifies the run's trade as
  ranging/agitated (ADX 16.2, matching the specialist report) — the plumbing
  existed; the default was wrong for this archive. The remaining gap was
  discoverability, fixed below.
- **The veto's defaults are intraday-tuned**: correct behaviour, but a daily
  archive fails its staleness and gap checks until the documented env
  overrides are set. Mitigated: an empty timeframe slice now names what the
  archive *does* hold ("0 archived bars for NVDA at 15m … the archive does
  hold 41 at 1d — was this the intended timeframe?"), so the refusal carries
  its own diagnosis. The thresholds themselves stay explicit configuration.

## Reproducing the read-back

```
JOURNAL_PATH=<run>/state/journal.db uv run python scripts/attribute_trades.py
JOURNAL_PATH=<run>/state/journal.db \
  VETO_EXPECTED_INTERVAL_MINUTES=1440 VETO_MAX_STALE_MINUTES=7200 \
  VETO_WINDOW_HOURS=336 VETO_MIN_ARCHIVED_BARS=30 \
  uv run python scripts/specialist_report.py --symbols AAPL,MSFT,NVDA --timeframe 1d
```
