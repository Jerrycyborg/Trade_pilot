# TASK-002 Hardening Summary

Reviewed the Milestone 1 implementation for contract mismatches, unsafe assumptions, persistence gaps, error handling, and test coverage.

Targeted fixes applied:

- strategy-service now emits a unique `signal_id` per generated signal instead of reusing the same ID for every symbol
- policy-service now persists `policy_version` alongside each evaluation for auditability
- execution-service now persists a broker-side `external_order_id` for each order
- execution-service now handles duplicate insert races around idempotent submissions by resolving the existing order instead of failing unpredictably
- tests now cover unique signal IDs, persisted policy version, and persisted broker order references

These changes keep Milestone 1 scope intact while removing ambiguity in audit records and reducing unsafe assumptions around repeated signal generation and duplicate order submission.
