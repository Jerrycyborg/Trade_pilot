# TASK-006 Live Operator Read Surfaces Summary

Extended the current Milestone 1 stack with live operator read surfaces while preserving the existing execution and portfolio boundaries.

Included:

- strategy signal persistence plus `GET /v1/signals`
- `GET /v1/policy/evaluations` for persisted policy decisions
- `GET /v1/orders` for persisted order history
- CORS-enabled service reads so the static dashboard can fetch across service ports
- dashboard wiring to live backend endpoints through configurable base URLs
- test coverage for list APIs and dashboard-facing integration reads

Preserved by design:

- strategy only proposes
- policy only evaluates
- execution remains the sole order owner
- portfolio still derives state from fills only
- no websockets, auth, reasoning-service, or live broker sync
