# TASK-006 Live Operator Read Surfaces

## Goal

Implement live dashboard-facing read surfaces without changing the core Milestone 1 execution and portfolio boundaries.

## Context

- `.ai/handoff/manifest.json`
- `.ai/handoff/decisions/ADR-001-execution-boundary.md`
- `.ai/handoff/decisions/ADR-002-portfolio-source-of-truth.md`
- `libs/contracts/`
- `services/strategy-service/`
- `services/policy-service/`
- `services/execution-service/`
- `services/portfolio-service/`
- `apps/dashboard/`
- `tests/strategy_service/`
- `tests/policy_service/`
- `tests/execution_service/`
- `tests/integration/`

## Deliverables

- Persist generated strategy signals and expose `GET /v1/signals`
- Expose `GET /v1/policy/evaluations`
- Expose `GET /v1/orders`
- Wire the dashboard to live service endpoints with configurable base URLs
- Update docs and handoff summary to reflect the live read surfaces

## Constraints

- Deterministic behavior where possible
- Keep scope inside the manifest
- Prefer shared contracts over duplicated schemas
- Preserve ADR-001 and ADR-002 as the source of truth
- No websockets, auth, live brokers, or reasoning-service

## Validation

- Service tests for strategy, policy, and execution list APIs
- Integration coverage for dashboard-facing read surfaces after persisted writes
- Documentation reflects live dashboard behavior

## Handoff Notes

- Record follow-up decisions or open questions in `.ai/handoff/decisions/`
- Write a concise summary in `.ai/handoff/summaries/`
