# Hardening: before and after

Companion to [DIAGNOSIS.md](DIAGNOSIS.md) (what was wrong) and
[SCHEMA.md](SCHEMA.md) (the shared-state design). This is what changed, what
was verified and how, what is still risky, and how to get back.

**Real-money trading is not enabled by this work and cannot be enabled by
configuration alone.** No sleeve starts anywhere but `candidate`, live mode is
a database row defaulting to false, and `docker-compose.yml` pins `BROKER=paper`.

---

## Execution path

### Before

```text
  orchestrator ──┐
                 ├──▶ POST /v1/orders ──▶ broker  ◀── chosen once at import from
  worker      ───┘         │                          BROKER / ALPACA_API_KEY
   (each checked                                      env vars
    its own cached
    JSON roster)      anything else that could reach the port
                      arrived here having checked nothing
```

The lifecycle "gate" was a boolean in two callers. `execution-service` checked
nothing, and the broker was whatever the environment configured — so with
credentials present, a `candidate` sleeve's order reached the live venue
exactly like a `live` one.

### After

```text
  orchestrator ──┐
                 ├──▶ POST /v1/orders
  worker      ───┘         │
  anything else  ─────────▶│
                           ▼
                  ┌─────────────────────────────────────────┐
                  │ execution-service resolves the route     │
                  │   sleeve state  ← shared Postgres roster │
                  │   live mode     ← operator-set row       │
                  │   halt latch    ← reconciliation state   │
                  │   intent        ← entry | reduce_only    │
                  └───────────────┬─────────────────────────┘
                                  ▼
     ┌──────────┬────────────┬──────────────┬────────────────┐
     ▼          ▼            ▼              ▼                ▼
  BLOCKED    SHADOW      SIMULATED        LIVE          (guard)
  no order   journal     PaperBroker    real broker   assert_not_live
             only        only           only          at the boundary
```

Nothing reaches a live adapter without a `live` sleeve **and** an
operator-enabled live mode **and** a second boundary check agreeing. Blocked
and shadow orders are persisted with an `order.not_placed` event rather than
dropped, because a candidate's recorded decisions are the evidence its
promotion rests on.

## Lifecycle authority

### Before

```text
  orchestrator process          worker process
   ├─ registry (in memory)       ├─ registry (in memory)
   │    loaded once at boot      │    loaded once at boot
   └────────┬───────────┬────────┘
            ▼           ▼
        strategy-lifecycle.json     ← whole file rewritten on every change;
                                      last writer wins over every sleeve;
                                      a failed write was logged and the
                                      caller told the promotion succeeded
```

### After

```text
  orchestrator ──┐                          ┌── reads: no cache, every call
  execution   ───┼──▶ PostgreSQL ◀──────────┤   writes: one transaction,
  worker      ───┘    lifecycle.*           └──  optimistic version check
                       ├─ sleeve (version)
                       ├─ transition (append-only, seq)
                       ├─ evidence_snapshot (immutable, hashed)
                       ├─ validation_artifact
                       ├─ execution_environment (live mode, off by default)
                       ├─ reconciliation_state (latched, survives restart)
                       └─ journal_health
```

A stale write raises `ConcurrentTransitionError` instead of clobbering. A
failed write raises instead of reporting success. Local JSON and SQLite remain
as labelled single-process development stores and are never presented as
shared state.

## Promotion

### Before

```text
  POST /promote  {"deflated_sharpe_ratio": 0.99, "out_of_sample_trades": 500}
        │
        ▼
     gates read exactly what the caller sent ──▶ live
```

### After

```text
  run the validation ──▶ lifecycle.validation_artifact  (written when it ran)
                                    │
  POST /promote {sleeve, artifact_ids}                  journal (scoped:
        │                           │                    strategy, symbol,
        ▼                           ▼                    environment, account)
   server derives ◀─────────────────┴────────────────────────────┘
        │   checks each artifact really describes THIS sleeve
        │   (kind, strategy, symbol, strategy version)
        ▼
   immutable evidence_snapshot (+ artifact hashes) ──▶ gates ──▶ transition
```

---

## What was verified, and how

Run against a real PostgreSQL 16.13 (`initdb` + `pg_ctl` on this machine, not a
mock). Exact commands and results:

| Command | Result |
|---|---|
| `uv run pytest -q` | 737 passed, 7 skipped |
| `uv run pytest tests/hardening -q` | 160 passed |
| `uv run pytest tests/hardening/test_shared_lifecycle_state.py -q` | 17 passed |
| `uv run pytest tests/hardening/test_execution_routing.py tests/hardening/test_promotion_evidence.py -q` | 36 passed |
| `uv run ruff check .` | All checks passed |
| `uv sync --frozen --all-packages` | Audited 75 packages |
| `tools/migrate.py up / up / status / down / up` | applied 0001+0002, no-op, listed, reverted 0002, applied 0002 |
| `uv run pytest tests/hardening -q` with `TEST_LIFECYCLE_POSTGRES_URL` unset | 71 passed, 89 skipped |

Re-run at the head of the branch, not copied forward from when the section was
first written. The last row matters: the Postgres tests skip with a stated
reason rather than silently degrading to SQLite, which would prove nothing
about concurrency.

Five defects in this work were found by running it rather than by reading it,
and each is recorded where it belongs rather than summarised away: a blocked
order that could not be persisted on PostgreSQL, a router that latched its
simulated-only fallback at import, a sweep that reported a halt that had not
happened, an ADX sentinel that let a trend filter pass on absent data, and a
decay trigger comparing a per-trade Sharpe with an annualised one.

### Acceptance criteria

| # | Criterion | Where | Verified |
|---|---|---|---|
| 1 | Two processes see the same transition | `TestTwoProcessesShareState` | Yes |
| 2 | A failed write cannot change state | `TestFailedWritesCannotChangeState` | Yes |
| 3 | Restart preserves lifecycle, demotion, halt | `TestRestartPreservesEverything` | Yes |
| 4 | PAPER produces real fills, no live call | `TestPaperSleeveNeverReachesLive` | Yes |
| 5 | CANDIDATE journals, places nothing | `TestCandidateShadowsOnly` | Yes |
| 6 | Evidence cannot be fabricated by request | `TestEvidenceCannotBeFabricated` | Yes |
| 7 | Paper/live/test cannot be mixed | `test_live_and_backtest_records_cannot_pad_a_paper_window` | Yes |
| 8 | Dependency failure halts entries, never exits | `TestExitsSurviveEveryHalt` | Yes |
| 9 | Existing tests still pass | full suite | Yes |
| 10 | No secrets, providers, real orders, destructive ops | see below | Yes |

On (10): the live adapter in the routing tests is a spy that raises on any
call, so "the paper broker was used" cannot pass while the live one was also
touched. No test contacts a broker or a data provider. Every migration is
additive; no existing table is altered, truncated or back-filled.

### Verified on CI

All three jobs green on run 3
([33405778790](https://github.com/Jerrycyborg/Trade_pilot/actions/runs/33405778790)),
including `compose-paper-trading` — the stack builds, applies its migrations,
refuses an order for an unregistered sleeve, and has live mode off. That job
could not be run in the development sandbox (docker client, no daemon) and was
shipped as unproven; it is proven now.

Getting there took three runs, and each failure was real rather than a CI
configuration problem:

| Run | Result | What it found |
|---|---|---|
| [1](https://github.com/Jerrycyborg/Trade_pilot/actions/runs/33397057895) | lint ok, tests failed, compose failed | Two integration tests only passed because `INTERNAL_API_KEY` was unset locally; `build_router` latched the simulated-only fallback at import; the compose curl raced the container |
| [2](https://github.com/Jerrycyborg/Trade_pilot/actions/runs/33405209428) | lint ok, tests ok, compose failed | A blocked order could not be persisted on PostgreSQL — `external_order_id` is NOT NULL and UNIQUE, and the insert passed None |
| [3](https://github.com/Jerrycyborg/Trade_pilot/actions/runs/33405778790) | **all green** | — |

The second and third findings were defects in this hardening work, not in the
pre-existing code. Both were invisible locally: the suite runs on SQLite by
default and every execution-service test used a simulated-only router, so the
blocked-order path never touched a database at all. That gap is now covered by
`tests/hardening/test_unplaced_orders_persist.py`, parametrised over both
backends.

---

## Remaining risks

**The reconciliation grace period is a guess.** `RECONCILE_DEPENDENCY_GRACE_SECONDS`
defaults to 600. Too short and a container restart halts trading; too long and
the system opens positions it cannot verify. Ten minutes is defensible and is
not derived from anything. Watch it and tune.

*(The three gaps below were closed after this document was first written; they
are kept here with their resolution rather than deleted, because what a system
did not do is part of its record.)*

**~~Journal health is computed, not scheduled.~~ Closed.** A health sweep runs
every `HEALTH_SWEEP_INTERVAL_SECONDS`, demotes live sleeves that breached or
decayed, records journal health per sleeve, and halts entries on a gap past its
grace period. Two defects surfaced while wiring it: the sweep reported a halt
that had not happened (a gap was routed through a counter needing two passes),
and completeness was derived from a wall-clock expected count that would have
condemned every window crossing an overnight close.

**~~Evidence is supplied, not harvested.~~ Closed.**
`run_backtest.py --walk-forward` records its own artifact and prints the
promote call to copy.

**~~The orchestrator and worker still hold their own JSON registries.~~
Closed.** The JSON registry is deleted. A second implementation of the roster
is exactly the failure the roster prevents, and both callers now read the
shared authority. With none configured, nothing attempts an entry.

**~~Live decay metrics are thin.~~ Closed.** `_live_metrics` now pairs realised
round trips through `libs/attribution` and compares live performance against
the out-of-sample figure recorded in the sleeve's own promotion snapshot —
against what was actually claimed, rather than a number someone remembers.

Wiring it exposed a hole in the triggers. Both of the original two can go quiet
on the worst possible record: a sleeve that only ever loses never establishes a
positive peak, so its drawdown is not measurable as a percentage, and one whose
losses are identical has no variance and therefore no computable Sharpe. The
first of those was reporting `0.0`, which reads as "no drawdown" for the
clearest failure there is; it now reports `None`, and a third trigger asks the
blunt question — losing money, over enough trades, at a low win rate —
directly. It still waits for a sample, and a losing sleeve that wins half its
trades is deliberately left to the other two, being a sizing or exit problem
rather than a broken strategy.

**~~Regime attribution is not implemented.~~ Closed.** The decomposition
separated signal from execution and could not say whether the signal was wrong
or merely applied in conditions it was not built for — two findings implying
completely different fixes. `attribution.regime` classifies the regime at each
end of a trade from bars knowable at that moment, and the report groups results
by the regime each trade was *entered* into.

Wiring it found a live defect. `compute_adx` returns 25.0 when the series is
too short to measure one, and 25.0 sits **above** the strategy worker's own
trend threshold of 20 — so the filter that exists to keep trend entries out of
a range was passing on a fabricated number precisely when nothing was known
about the regime, including when there was no market snapshot at all. The
worker now refuses a trend entry it cannot measure the regime for, and
`market_data` exposes `adx_is_computable` so no caller has to know that 25.0
is sometimes a reading and sometimes an absence. Two tests fail without the
fix, one of them by sending a real order on six bars of history.

The classifier's own thresholds are conventions and are named as such: ADX 20
because that is what the live filter uses, and a volatility band measured
against the symbol's own recent range rather than an absolute number that
cannot hold across a $3 stock and a $900 one. Regime stays a diagnostic and
never enters the identity — a label with a threshold in it is an opinion, and
the three price components have to add up regardless.

**~~The decay trigger compared two incommensurable numbers.~~ Fixed.** Found
while trying to derive the thresholds below. `out_of_sample_sharpe` is
`annualise(per_period_sharpe, periods_per_year)` — annualised and bar-based —
and `performance_from_trades` returns a raw per-trade ratio. The health check
subtracted one from the other.

The error is not small and it points the wrong way. A per-trade 0.20 at roughly
250 trades a year is an annualised 3.16, so a sleeve comfortably beating a
validated 2.50 read as 2.30 *below* it and was demoted for outperforming.
Systematically, sleeves validated with a high annualised Sharpe were the most
likely to be demoted for it.

`performance_from_trades` now also reports `sharpe_annualised`, scaled by the
trade frequency the sleeve actually ran at — measured from its own exits, not
assumed — and the comparison is annualised against annualised. When the live
record is too short to measure a frequency from, the annualised figure is
`None` and the trigger stays quiet rather than falling back: a wrong scaling
produces a number that looks comparable and is not. The drawdown and
losing-outright triggers still cover the worst case there, and neither needs a
scaling to be true. Four tests fail if the raw ratio is restored.

**~~The drawdown limit was a fraction of the wrong thing.~~ Fixed.** Found by
running the end-to-end loop again after the change above: the sweep demoted a
sleeve for a *"live drawdown of 940.0%"*.

`max_drawdown_pct` divided the peak-to-trough fall of cumulative realised P&L
by the running peak **of that same curve**. That is not a capital base and it
is often tiny — a sleeve that won $20 early and then bled to −$168 reports
940%, and against a limit labelled 15% the trigger fires on nearly any losing
sleeve that had one good trade first. The same $168 on a $100,000 account is
0.17%.

A drawdown percentage is a fraction of the money at risk, and the journal
records fills rather than account sizes, so the denominator has to be declared:
`LIFECYCLE_CAPITAL_BASE_USD`, or `LIFECYCLE_MAX_LIVE_DRAWDOWN_USD` for an
absolute limit instead. There is no default account size — demoting a live
sleeve against a guessed denominator is worse than declining to check.
`max_drawdown_amount`, in currency, is always available and needs no
denominator.

With neither declared the trigger does not run, and it does not pass quietly
either: `HealthCheck.warnings` and `SweepResult.warnings` carry the fact,
deduplicated to one entry per sweep rather than one per sleeve. A check that
did not run reported as "healthy" is how a safety control becomes decoration —
which is what this one had become, firing constantly on a meaningless number.

**One threshold is now derived; the rest are conventions.** The absolute
`max_sharpe_decay: 2.0` is gone, replaced by a band of
`sharpe_decay_sigmas × SE(Sharpe)`, where the standard error is Lo (2002)'s
`sqrt((1 + SR²/2)/n)` on the same annualised footing. The same shortfall is
evidence over 500 trades and noise over 20, and a constant cannot express that.

The sigma count defaults to **1.0, not the conventional 2.0**, because the two
errors do not cost the same: a false demotion parks a working sleeve in
probation and is reversible, while a false clean bill leaves a broken one
trading real money. At two sigmas a sleeve running at an annualised −2.4
against a validated +2.5 still passed, which is not a health check.

Still conventions, and named as such: 15% drawdown, a 20% win rate, 20 trades,
a 60-minute journal-gap grace, the 0.5 absolute floor under the sigma band, and
the five-day minimum span before a trade frequency is worth annualising. Each
is defensible and none is derived.

`LIFECYCLE_MAX_SHARPE_DECAY` is deleted rather than rebound to the sigma count:
a variable that quietly stops meaning what its name says is worse than one that
is gone. `HealthThresholds.from_env` now reads defaults from the fields
themselves rather than keeping a second copy — which is how the sigma default
was changed in one place and silently not the other while this was written —
and refuses an unparseable value instead of falling back to a default the
operator believes they replaced.

**Correlation is measured on a calm sample.** Unchanged from before, and worth
restating: correlations rise in a selloff, which is exactly when the
`max_correlation_with_live` gate is supposed to protect you.

**No load or failover testing.** Optimistic locking is proven correct under a
deliberate two-writer race in a test. It has not been run under real
concurrency, connection-pool exhaustion, or a database failover.

---

## Rollback

Every migration is additive, so rolling back the code is sufficient on its own
— the new tables simply go unused and no existing data was ever modified.

**Code only** (fastest, keeps the data):

```bash
git revert --no-commit <hardening commits>   # or deploy the previous image
```

`execution-service` falls back to simulated-only when
`LIFECYCLE_DATABASE_URL` is unset, so a partial rollback fails toward paper
trading rather than toward unrouted live orders.

**Schema as well** (only if you want the tables gone):

```bash
uv run python tools/migrate.py --url "$LIFECYCLE_DATABASE_URL" down --steps 2
```

That drops `lifecycle.*` and nothing else — the `DROP SCHEMA ... RESTRICT` at
the end of `0001` fails loudly rather than taking anything unexpected with it.
No pre-existing table is referenced by either down script, so there is nothing
to restore afterwards.

**What you lose by rolling back the schema:** the roster, the transition
history, and every evidence snapshot. Nothing that was trading data. If you
might want the audit trail later, roll back the code and leave the schema.
