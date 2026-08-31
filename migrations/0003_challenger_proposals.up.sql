-- L4: champion/challenger in paper.
--
-- Two changes, both additive.
--
-- 1. `sleeve.origin` records whether a roster entry was put there by a person
--    or derived from a challenger proposal. It exists to carry a *structural*
--    barrier: a challenger-origin sleeve is refused promotion to live, by the
--    store, regardless of evidence. L3's safety rested on a proposal having
--    nowhere to go; the moment one becomes a sleeve that stops being true, and
--    "the ordinary gates will catch it" is a weaker guarantee than one the
--    learner cannot satisfy at all. Adopting a challenger is a named human
--    action that flips this column and is recorded as a transition.
--
--    Existing rows default to 'human', which is what they are.
--
-- 2. `challenger_proposal` is where a proposal is persisted. Deliberately not
--    `validation_artifact`: a challenger is a *proposal*, an artifact is a
--    *measurement*, and promotion reads artifacts. Storing proposals there
--    would put something a generator produced into the table the promotion
--    gate trusts, which is the one place it must never appear.
--
--    Append-only. A proposal is a historical fact about what was suggested and
--    on what evidence; editing one would rewrite the record a later reviewer
--    needs.

ALTER TABLE lifecycle.sleeve
    ADD COLUMN IF NOT EXISTS origin TEXT NOT NULL DEFAULT 'human';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'sleeve_origin_known'
    ) THEN
        ALTER TABLE lifecycle.sleeve
            ADD CONSTRAINT sleeve_origin_known
            CHECK (origin IN ('human', 'challenger'));
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS lifecycle.challenger_proposal (
    id                 BIGSERIAL PRIMARY KEY,
    challenger_id      TEXT        NOT NULL,
    campaign_id        TEXT        NOT NULL,
    strategy_id        TEXT        NOT NULL,
    symbol             TEXT        NOT NULL,
    base_version       TEXT        NOT NULL DEFAULT '',
    account_id         TEXT        NOT NULL DEFAULT 'default',

    parameters         JSONB       NOT NULL,
    rationale          TEXT        NOT NULL,
    clamped            JSONB       NOT NULL DEFAULT '[]'::jsonb,
    bounds_version     TEXT        NOT NULL DEFAULT '',
    generator          TEXT        NOT NULL DEFAULT '',

    -- Campaign-level evaluation. The pooled figure is stored and the per-run
    -- one beside it, because a reviewer reading this row months later needs to
    -- see that the two differ and by how much.
    deflated_sharpe_campaign   DOUBLE PRECISION,
    deflated_sharpe_own_search DOUBLE PRECISION,
    pooled_trials      INTEGER     NOT NULL DEFAULT 0,
    out_of_sample_sharpe DOUBLE PRECISION,
    survived           BOOLEAN     NOT NULL DEFAULT FALSE,

    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT challenger_proposal_unique UNIQUE (campaign_id, challenger_id)
);

CREATE INDEX IF NOT EXISTS challenger_proposal_scope
    ON lifecycle.challenger_proposal (strategy_id, symbol, account_id, created_at DESC);
