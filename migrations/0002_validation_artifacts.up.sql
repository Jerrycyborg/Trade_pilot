-- Durable validation artifacts.
--
-- Promotion previously accepted performance numbers in the request body, so
-- anyone able to construct {"deflated_sharpe_ratio": 0.99, ...} could promote a
-- sleeve to live. The gates were real; the inputs were fiction.
--
-- A promotion request may now name artifact IDs, and the server derives its
-- evidence from the rows those IDs point at. The artifact is written when the
-- validation actually runs, not when a promotion is requested.
--
-- Additive: new table only, nothing existing is touched.

CREATE TABLE IF NOT EXISTS lifecycle.validation_artifact (
    id                BIGSERIAL PRIMARY KEY,
    kind              TEXT        NOT NULL,
    strategy_id       TEXT        NOT NULL,
    strategy_version  TEXT        NOT NULL DEFAULT '',
    symbol            TEXT        NOT NULL,
    asset_class       TEXT        NOT NULL DEFAULT 'equity',
    environment       TEXT        NOT NULL,
    account_id        TEXT        NOT NULL DEFAULT 'default',
    window_start      TIMESTAMPTZ NOT NULL,
    window_end        TIMESTAMPTZ NOT NULL,
    data_version      TEXT        NOT NULL DEFAULT '',
    model_version     TEXT        NOT NULL DEFAULT '',
    payload           JSONB       NOT NULL DEFAULT '{}'::jsonb,
    content_hash      TEXT        NOT NULL,
    produced_by       TEXT        NOT NULL DEFAULT '',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT artifact_kind_known
        CHECK (kind IN ('walk_forward', 'portfolio_correlation', 'parameter_sensitivity')),
    CONSTRAINT artifact_environment_known
        CHECK (environment IN ('backtest', 'paper', 'live')),
    CONSTRAINT artifact_window_ordered CHECK (window_end >= window_start)
);

CREATE INDEX IF NOT EXISTS ix_artifact_scope
    ON lifecycle.validation_artifact
       (strategy_id, symbol, environment, kind, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_artifact_hash
    ON lifecycle.validation_artifact (content_hash);
