-- Reverses 0001_lifecycle_core.up.sql and nothing else.
--
-- Only objects that migration created are dropped. No pre-existing table is
-- referenced, so rolling back cannot remove trading data.

DROP TABLE IF EXISTS lifecycle.journal_health;
DROP TABLE IF EXISTS lifecycle.reconciliation_state;
DROP TABLE IF EXISTS lifecycle.execution_environment;
DROP TABLE IF EXISTS lifecycle.transition;
DROP TABLE IF EXISTS lifecycle.sleeve;
DROP TABLE IF EXISTS lifecycle.evidence_snapshot;
DROP TABLE IF EXISTS lifecycle.schema_migrations;

-- Dropped only when empty: if anything else has put objects in this schema,
-- RESTRICT makes the rollback fail loudly rather than taking them with it.
DROP SCHEMA IF EXISTS lifecycle RESTRICT;
