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
| `uv run pytest -q` | 694 passed, 7 skipped |
| `uv run pytest tests/hardening -q` | 34 passed |
| `uv run pytest tests/hardening/test_shared_lifecycle_state.py -q` | 17 passed |
| `uv run pytest ... -k "Restart or survives" -q` | 5 passed, 12 deselected |
| `uv run pytest tests/hardening/test_execution_routing.py tests/hardening/test_promotion_evidence.py -q` | 32 passed |
| `uv run ruff check .` | All checks passed |
| `uv sync --frozen --all-packages` | Audited 74 packages |
| `tools/migrate.py up / up / status / down / up` | applied 0001+0002, no-op, listed, reverted 0002, applied 0002 |
| `uv run pytest tests/hardening -q` with `TEST_LIFECYCLE_POSTGRES_URL` unset | 17 passed, 17 skipped |

The last row matters: the Postgres tests skip with a stated reason rather than
silently degrading to SQLite, which would prove nothing about concurrency.

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

**Journal health is computed, not scheduled.** `Journal.completeness` and
`store.record_journal_health` exist and are used by evidence derivation, but
nothing runs them on a timer, so "block new entries after a configured safety
grace period" on a journal gap is enforced at promotion and not yet in the
live loop. This is the largest remaining gap against requirement E.

**Health checks are called, not scheduled.** Same shape: the demotion triggers
are implemented and tested, but something has to invoke them with current live
figures. Until that is wired to the orchestrator's cycle, decay detection is
manual.

**Evidence is supplied, not harvested.** A promotion cites artifact IDs, and
those artifacts are written when a validation runs — but nothing yet writes one
automatically from `run_backtest.py --walk-forward`. Today an operator records
the artifact. That is honest but manual.

**The orchestrator and worker still hold their own JSON registries.** The
shared store is wired into `execution-service`, which is the component that
actually reaches a broker, so the safety property holds. But the two callers
still consult the old per-process roster for their pre-flight gate, which means
they can disagree with the authority about what to *attempt*. They will be
refused correctly; the log will just be noisier than it needs to be. Migrating
those two call sites is follow-up work, not a safety hole.

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
