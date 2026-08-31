-- Shared lifecycle authority.
--
-- Additive only: every object here is new. No existing table is altered,
-- truncated or back-filled, so applying this to a running system cannot lose
-- trading data. 0001_lifecycle_core.down.sql removes exactly what this creates.

CREATE SCHEMA IF NOT EXISTS lifecycle;

CREATE TABLE IF NOT EXISTS lifecycle.schema_migrations (
    version     TEXT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    checksum    TEXT NOT NULL DEFAULT ''
);

-- Immutable. Written by the server from durable records; there is no UPDATE
-- path in the application. The scope columns are mandatory so that paper, live
-- and backtest evidence can never be mixed in one calculation.
CREATE TABLE IF NOT EXISTS lifecycle.evidence_snapshot (
    id                BIGSERIAL PRIMARY KEY,
    strategy_id       TEXT        NOT NULL,
    strategy_version  TEXT        NOT NULL,
    symbol            TEXT        NOT NULL,
    asset_class       TEXT        NOT NULL,
    environment       TEXT        NOT NULL,
    broker            TEXT        NOT NULL,
    account_id        TEXT        NOT NULL,
    portfolio_id      TEXT        NOT NULL,
    window_start      TIMESTAMPTZ NOT NULL,
    window_end        TIMESTAMPTZ NOT NULL,
    data_version      TEXT        NOT NULL DEFAULT '',
    model_version     TEXT        NOT NULL DEFAULT '',
    metrics           JSONB       NOT NULL DEFAULT '{}'::jsonb,
    source_artifacts  JSONB       NOT NULL DEFAULT '[]'::jsonb,
    content_hash      TEXT        NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT evidence_environment_known
        CHECK (environment IN ('backtest', 'paper', 'live')),
    CONSTRAINT evidence_window_ordered
        CHECK (window_end >= window_start)
);

CREATE INDEX IF NOT EXISTS ix_evidence_scope
    ON lifecycle.evidence_snapshot
       (strategy_id, symbol, account_id, environment, window_end DESC);
CREATE INDEX IF NOT EXISTS ix_evidence_hash
    ON lifecycle.evidence_snapshot (content_hash);

-- The roster. Identity is the trading unit; the execution environment a sleeve
-- may reach is derived from its state plus the global switch, not stored here.
CREATE TABLE IF NOT EXISTS lifecycle.sleeve (
    id                BIGSERIAL PRIMARY KEY,
    strategy_id       TEXT        NOT NULL,
    strategy_version  TEXT        NOT NULL DEFAULT '',
    symbol            TEXT        NOT NULL,
    asset_class       TEXT        NOT NULL DEFAULT 'equity',
    account_id        TEXT        NOT NULL DEFAULT 'default',
    state             TEXT        NOT NULL DEFAULT 'candidate',
    version           BIGINT      NOT NULL DEFAULT 1,
    since             TIMESTAMPTZ NOT NULL DEFAULT now(),
    reason            TEXT        NOT NULL DEFAULT 'registered',
    probation_count   INTEGER     NOT NULL DEFAULT 0,
    -- Where this sleeve's positions live. Set when it enters paper or live and
    -- kept afterwards, so a demoted sleeve still knows which broker to send a
    -- reduce-only exit to. 'none' means it has never executed anywhere.
    position_environment TEXT     NOT NULL DEFAULT 'none',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT sleeve_state_known
        CHECK (state IN ('candidate', 'paper', 'live', 'probation', 'retired')),
    CONSTRAINT sleeve_position_environment_known
        CHECK (position_environment IN ('none', 'simulated', 'live')),
    CONSTRAINT sleeve_identity UNIQUE (strategy_id, symbol, account_id)
);

CREATE INDEX IF NOT EXISTS ix_sleeve_state ON lifecycle.sleeve (state);

-- Append-only. Never updated, never deleted.
CREATE TABLE IF NOT EXISTS lifecycle.transition (
    id                    BIGSERIAL PRIMARY KEY,
    sleeve_id             BIGINT      NOT NULL
                          REFERENCES lifecycle.sleeve (id) ON DELETE CASCADE,
    seq                   BIGINT      NOT NULL,
    from_state            TEXT        NOT NULL,
    to_state              TEXT        NOT NULL,
    reason                TEXT        NOT NULL DEFAULT '',
    actor                 TEXT        NOT NULL DEFAULT 'system',
    evidence_snapshot_id  BIGINT      REFERENCES lifecycle.evidence_snapshot (id),
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT transition_seq_unique UNIQUE (sleeve_id, seq)
);

CREATE INDEX IF NOT EXISTS ix_transition_sleeve
    ON lifecycle.transition (sleeve_id, seq DESC);

-- Operator-controlled global switch. Real money is off by default; turning it
-- on is an audited row change rather than an environment variable.
CREATE TABLE IF NOT EXISTS lifecycle.execution_environment (
    account_id         TEXT        PRIMARY KEY,
    live_mode_enabled  BOOLEAN     NOT NULL DEFAULT FALSE,
    updated_by         TEXT        NOT NULL DEFAULT 'system',
    reason             TEXT        NOT NULL DEFAULT '',
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Survives restarts. first_failure_at is what makes a bounded grace period
-- possible for an unavailable dependency.
CREATE TABLE IF NOT EXISTS lifecycle.reconciliation_state (
    account_id         TEXT        NOT NULL,
    broker             TEXT        NOT NULL,
    environment        TEXT        NOT NULL,
    halted             BOOLEAN     NOT NULL DEFAULT FALSE,
    consecutive_breaks INTEGER     NOT NULL DEFAULT 0,
    first_failure_at   TIMESTAMPTZ,
    last_ok_at         TIMESTAMPTZ,
    last_checked_at    TIMESTAMPTZ,
    last_error         TEXT        NOT NULL DEFAULT '',
    halt_reason        TEXT        NOT NULL DEFAULT '',
    cleared_by         TEXT        NOT NULL DEFAULT '',
    cleared_at         TIMESTAMPTZ,
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, broker, environment)
);

CREATE TABLE IF NOT EXISTS lifecycle.journal_health (
    id                    BIGSERIAL PRIMARY KEY,
    scope_key             TEXT        NOT NULL,
    strategy_id           TEXT        NOT NULL DEFAULT '',
    symbol                TEXT        NOT NULL DEFAULT '',
    environment           TEXT        NOT NULL DEFAULT 'paper',
    window_start          TIMESTAMPTZ NOT NULL,
    window_end            TIMESTAMPTZ NOT NULL,
    expected_observations INTEGER     NOT NULL DEFAULT 0,
    actual_observations   INTEGER     NOT NULL DEFAULT 0,
    gap_count             INTEGER     NOT NULL DEFAULT 0,
    eligible_for_learning BOOLEAN     NOT NULL DEFAULT TRUE,
    last_gap_at           TIMESTAMPTZ,
    status                TEXT        NOT NULL DEFAULT 'ok',
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT journal_health_scope UNIQUE (scope_key, window_start, window_end)
);

CREATE INDEX IF NOT EXISTS ix_journal_health_scope
    ON lifecycle.journal_health (scope_key, window_end DESC);
