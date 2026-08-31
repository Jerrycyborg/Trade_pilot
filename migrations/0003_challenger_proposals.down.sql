-- Reverses 0003. Dropping the proposal table discards the record of what was
-- proposed; that is acceptable because no trading data lives here and nothing
-- reads it to make a decision. The sleeve column goes back to not existing,
-- which restores the pre-0003 behaviour exactly: without it, promotion has no
-- origin to refuse on, so roll this back only if L4 is being removed whole.

DROP INDEX IF EXISTS lifecycle.challenger_proposal_scope;
DROP TABLE IF EXISTS lifecycle.challenger_proposal;

ALTER TABLE lifecycle.sleeve DROP CONSTRAINT IF EXISTS sleeve_origin_known;
ALTER TABLE lifecycle.sleeve DROP COLUMN IF EXISTS origin;
