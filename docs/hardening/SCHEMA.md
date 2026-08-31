# Shared-state schema and migration plan

Authoritative lifecycle, evidence, reconciliation and journal-health state move
from per-process files into the PostgreSQL service that already exists in
`docker-compose.yml`.

## Principles

**Additive and reversible.** Every migration creates new objects. Nothing
drops, truncates, rewrites or back-fills an existing table, and no existing
user trading data is touched. Each migration ships a `down` that removes only
what its `up` created.

**Failure is visible.** Every write happens inside a transaction. A write that
does not commit must surface as a failed operation to the caller — never as a
success with lost state (diagnosis B3).

**Concurrency is explicit.** Sleeves carry a monotonic `version`. A transition
reads the version, and the `UPDATE` asserts it is unchanged; a mismatch means
another process moved first and the caller is told so rather than clobbering it
(diagnosis B2).

**Local files are development-only.** The JSON and SQLite paths remain, clearly
labelled as single-process development stores. They are never presented as
shared production state.

## Tables

All in schema `lifecycle`, so nothing collides with existing per-service
tables.

### `lifecycle.sleeve`

The roster. Identity is the trading unit, not the environment: a sleeve has one
lifecycle state, and the environment it may execute in is *derived* from that
state plus the global switch.

| column | type | notes |
|---|---|---|
| `id` | bigserial PK | |
| `strategy_id` | text | e.g. `ema_rsi_macd` |
| `strategy_version` | text | pinned at registration |
| `symbol` | text | |
| `asset_class` | text | `equity`, `crypto`, … |
| `account_id` | text | |
| `state` | text | `candidate`/`paper`/`live`/`probation`/`retired` |
| `version` | bigint | optimistic lock, bumped every transition |
| `since` | timestamptz | when the current state began |
| `reason` | text | |
| `probation_count` | int | |
| `created_at`, `updated_at` | timestamptz | |

Unique on `(strategy_id, symbol, account_id)`. Check constraint on `state`.

### `lifecycle.transition`

Append-only history. One row per state change, never updated or deleted.

`id`, `sleeve_id` FK, `seq` (per-sleeve monotonic), `from_state`, `to_state`,
`reason`, `actor` (who/what), `evidence_snapshot_id` FK nullable,
`created_at`. Unique on `(sleeve_id, seq)`.

### `lifecycle.evidence_snapshot`

Immutable. Written by the *server* from durable records, never from a request
body (diagnosis C1). No `UPDATE` path exists in the code.

Scope columns, all mandatory: `strategy_id`, `strategy_version`, `symbol`,
`asset_class`, `environment` (`paper`/`live`/`backtest`), `broker`,
`account_id`, `portfolio_id`, `window_start`, `window_end`, `data_version`,
`model_version`.

Payload: `metrics` jsonb (the derived numbers), `source_artifacts` jsonb (the
records the metrics were derived from), `content_hash` text (sha256 over the
canonicalised scope + metrics + artifact hashes), `created_at`.

The `environment` column is what makes acceptance criterion 7 enforceable: a
query for live evidence cannot return paper rows, because they are different
rows with a different scope.

### `lifecycle.execution_environment`

Operator-controlled global switch, one row per `account_id`.
`live_mode_enabled` bool default **false**, `updated_by`, `updated_at`,
`reason`. Real-money execution stays off by default; enabling it is an audited
row change, not an env var.

### `lifecycle.reconciliation_state`

Survives restarts (diagnosis B4/D4). Keyed on
`(account_id, broker, environment)`.

`halted` bool, `consecutive_breaks` int, `first_failure_at`, `last_ok_at`,
`last_checked_at`, `last_error`, `halt_reason`, `cleared_by`, `cleared_at`.
`first_failure_at` is what makes a bounded grace period possible (diagnosis
D3): a dependency that has been unavailable since a timestamp can be compared
against a configured window.

### `lifecycle.journal_health`

`scope_key` (strategy/symbol/environment), `window_start`, `window_end`,
`expected_observations`, `actual_observations`, `gap_count`,
`eligible_for_learning` bool, `last_gap_at`, `status`, `updated_at`.

### `lifecycle.schema_migrations`

`version` text PK, `applied_at`, `checksum`. The runner records what it applied
so re-running is a no-op and drift is detectable.

## Migration mechanism

Numbered SQL pairs under `migrations/`:

```
migrations/0001_lifecycle_core.up.sql
migrations/0001_lifecycle_core.down.sql
```

Applied by a small runner (`tools/migrate.py`) that takes a database URL,
applies pending `up` files in order inside a transaction each, and records them
in `schema_migrations`. `--down` reverses the most recent. Alembic was not used
deliberately: a dozen explicit SQL files are easier to review and to smoke-test
than generated revision graphs, and this schema is new rather than evolving.

The runner is idempotent, so the CI smoke test is: apply to an empty database,
apply again (no-op), roll back one, re-apply.

## Rollback

Because every migration is additive, rolling back the application code alone is
sufficient — the new tables are simply unused, and the JSON/SQLite development
stores still work. To remove the schema entirely, run the `down` files in
reverse order; no existing table is touched, so there is nothing to restore.
