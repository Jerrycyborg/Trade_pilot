# Hardening diagnosis

Findings against `ad919e2`, before any edit. Each item names the file and line
that produces the behaviour, what actually happens, and why it matters for
long-running paper trading.

Several of these are defects in code written earlier in this same effort. They
are listed the same way as any other finding.

---

## A. Lifecycle state and execution environment are unrelated

**A1 — The broker is chosen from environment variables alone.**
`libs/brokers/src/brokers/__init__.py:15` `get_broker()` reads `BROKER`,
`ALPACA_API_KEY`, `ETORO_API_KEY` and returns an adapter.
`services/execution-service/src/execution_service/broker.py:13` binds the
result to a single process-global `broker`. Nothing about a sleeve's lifecycle
state reaches this decision. If `ALPACA_API_KEY` is set, **every** order —
from a `CANDIDATE`, `PAPER` or `LIVE` sleeve alike — routes to Alpaca.

**A2 — The gate is in the callers, not in execution-service.**
`autonomy_orchestrator/main.py::_lifecycle_gate` and
`strategy_service/worker.py::_lifecycle_gate` check the roster before posting.
`execution-service`'s `POST /v1/orders` checks nothing. Any process that can
reach that endpoint — a script, a stale service, a mistake — bypasses the
roster entirely. A control enforced only by the things it is meant to
constrain is not a control.

**A3 — There is no reduce-only concept.**
`can_trade` is one boolean. `PROBATION` and `RETIRED` return `False`, and the
callers use that to skip *entries*, but nothing marks an order as an exit, and
execution-service could not tell the difference if it wanted to. The property
"exits remain possible when entries are halted" is currently a convention in
two call sites rather than a server-side rule.

**A4 — Live mode and the roster are independent switches with no interlock.**
`/v1/orchestrator/live-mode` and the roster do not consult each other. A sleeve
can be `LIVE` while live mode is off, or live mode can be on with no sleeve
`LIVE`. Neither combination is dangerous today only because A1 means the broker
ignores both.

---

## B. Lifecycle authority is per-process, and writes can fail silently

**B1 — State is loaded once and never re-read.**
`libs/lifecycle/src/lifecycle/registry.py:458` calls `_load()` in `__init__`
and nowhere else. The orchestrator holds one instance
(`main.py::_lifecycle()`), the strategy worker holds another
(`worker.py::_lifecycle()`). After start-up they never see each other's
transitions. Promoting a sleeve in the orchestrator leaves the worker still
refusing it until restart, and demoting one leaves the worker still trading it.
This directly defeats acceptance criterion 1.

**B2 — Concurrent writes clobber each other.**
`_save()` serialises the whole roster and replaces the file. Two processes
writing produce last-writer-wins over *every* sleeve, not just the one that
changed.

**B3 — A failed write still reports success.**
`_save()` wraps everything in `try/except` and only logs
(`registry.py:_save`). `_apply()` has already mutated the in-memory record
before `_save()` runs, and `promote()` returns the mutated record with
`allowed=True`. So a full disk, a permissions error or a read-only mount
produces an API response saying the sleeve is now `live`, an in-memory state
that agrees, and nothing on disk. On restart it silently reverts. This is
acceptance criterion 2, and it currently fails.

**B4 — Reconciliation halt state is in memory only.**
`reconciliation.py::Reconciler._consecutive_breaks` is an instance attribute.
Restarting the orchestrator clears a halt that a human never cleared.

---

## C. Promotion evidence is supplied by the caller

**C1 — The API accepts performance numbers as a request body.**
`POST /v1/orchestrator/lifecycle/promote` takes `Evidence` as JSON. Every gate
then reads exactly what the caller sent. Promotion to `live` is available to
anyone who can construct:

```
{"deflated_sharpe_ratio": 0.99, "out_of_sample_trades": 500, ...}
```

The gates are real; the inputs are fiction. This is the most serious finding in
this document and it defeats acceptance criterion 6.

**C2 — Evidence has no scope.**
`Evidence` carries bare numbers. There is no strategy version, symbol, asset
class, environment, broker, account, time window, or data/model version. Two
consequences: a result measured on one symbol can promote another, and paper,
live and test numbers are indistinguishable — acceptance criterion 7 cannot
hold, because there is nothing to separate.

**C3 — No immutable snapshot.**
The evidence is written into the journal's free-form `inputs` on the
transition. There is no artifact reference, no hash, and nothing that lets a
reviewer six months later establish which walk-forward run justified a
promotion, or whether the artifact has changed since.

---

## D. Reconciliation is symbol-only and forgets everything on restart

**D1 — Duplicate positions overwrite instead of aggregating.**
`compare_positions` builds `{symbol: qty}` with a dict comprehension. A broker
returning two lots of `AAPL` (100 and 50) yields 50, not 150 — and then reports
a break against a ledger that correctly says 150. Worse, the failure direction
is arbitrary: it depends on list order.

**D2 — No account, broker, asset class, or environment identity.**
Positions are keyed on symbol alone. Two accounts, or a paper and a live view
of the same symbol, are indistinguishable and would be compared against each
other.

**D3 — A dependency outage never halts.**
`check()` catches the fetch failure and returns `ok=False` *without*
incrementing `_consecutive_breaks`, by deliberate earlier design ("an
unreachable service is not a divergence"). That reasoning is right for a
momentary blip and wrong for a sustained outage: after ten minutes of not being
able to see the broker, continuing to open positions is the unsafe choice.
There is no grace period because there is no timer.

**D4 — A halt clears itself, or by restart.**
`reset()` is an in-process method with no authentication and no audit record,
and `_consecutive_breaks = 0` on the next clean check. Nothing requires a human
to acknowledge a break that occurred.

---

## E. The journal cannot reconstruct what was known

**E1 — Revisions are discarded.**
`record_bars` inserts with `ON CONFLICT DO NOTHING` on
`(symbol, timeframe, bar_ts)`. Providers revise bars. The first version seen
wins permanently, and a corrected bar is dropped without a trace. The archive
therefore cannot answer "what did we see at 14:35?" — only "what did we see
first". That is the one question the archive exists to answer.

**E2 — A missing market timestamp becomes `now()`.**
`store.py:44` `_utc(None)` returns `datetime.now(timezone.utc)`. A bar with no
usable timestamp is archived as though it printed at the moment of the fetch,
silently corrupting the series it lands in. The mission requires rejection.

**E3 — Provenance is one free-text string.**
`source` is the only provider metadata. There is no payload hash, no revision
counter, no provider request identity — so two observations of the same bar
cannot be compared, and tampering or drift cannot be detected.

**E4 — Execution records are thin.**
`ExecutionQuality` has symbol, side, qty, prices, shortfall, filled flag. It has
no strategy or version, environment, account, portfolio, broker, decision id,
order-intent id, requested/submitted timestamps, fees, spread, partial-fill
detail, or cancellation/rejection classification. Post-trade attribution and
any future learning are impossible from this record.

**E5 — There is no notion of journal health.**
Nothing measures completeness, nothing alerts on a gap, and nothing prevents a
promotion whose evidence window contains one.

---

## F. Deployment and CI

**F1 — Half the stack is missing from Compose.**
`docker-compose.yml` defines postgres, policy, execution, portfolio, strategy
and backtest. Absent: autonomy-orchestrator, audit-logger, research-service,
sentiment-aggregator, notification-service, approval-gateway. The orchestrator
is the main loop, so the documented stack cannot actually trade.

**F2 — Services default to per-service SQLite.**
Each service's config falls back to its own `.db` file. Compose overrides some
to Postgres and not others, so the composed stack is a mix.

**F3 — There is no CI at all.**
No `.github/` directory. Nothing verifies the suite, the linter, migrations, or
that the stack starts. Every guarantee in this repository currently depends on
someone running `uv run pytest` by hand and remembering the result.
