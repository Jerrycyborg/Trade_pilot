CREATE TABLE lifecycle.learning_cycle (
    id BIGSERIAL PRIMARY KEY,
    campaign_id TEXT NOT NULL UNIQUE,
    account_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    base_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN (
            'VETOED',
            'VETO_INCOMPLETE',
            'INSUFFICIENT_PAPER_EVIDENCE',
            'NO_CHALLENGERS',
            'RECORDED',
            'EVALUATED_UNRECORDED'
        )
    ),
    as_of TIMESTAMPTZ NOT NULL,
    report JSONB NOT NULL,
    content_hash TEXT NOT NULL,
    deployment_authority BOOLEAN NOT NULL DEFAULT FALSE
        CHECK (deployment_authority = FALSE),
    promotion_authority BOOLEAN NOT NULL DEFAULT FALSE
        CHECK (promotion_authority = FALSE),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX ix_learning_cycle_scope
ON lifecycle.learning_cycle (account_id, strategy_id, symbol, as_of DESC);
