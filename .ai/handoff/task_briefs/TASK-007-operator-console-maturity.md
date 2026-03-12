# TASK-007 Operator Console Maturity

## Goal

Turn the refresh-based dashboard into a usable operator console with lifecycle drill-down, client-side filters, summary metrics, and visible approve/review/reject handling while preserving the current service boundaries.

## Context

- `.ai/handoff/manifest.json`
- `.ai/handoff/decisions/ADR-001-execution-boundary.md`
- `.ai/handoff/decisions/ADR-002-portfolio-source-of-truth.md`
- `apps/dashboard/`
- `tests/integration/`
- `tests/dashboard/`

## Deliverables

- Dashboard lifecycle drill-down linked by existing identifiers
- Client-side filters for symbol/id, workflow state, and recent-record limit
- Summary metrics built from current read endpoints only
- Review-path integration coverage and dashboard data-composition tests
- Updated README/dashboard/AAHP trail with no duplicate planning document

## Constraints

- Deterministic behavior where possible
- Keep scope inside the manifest
- Prefer shared contracts over duplicated schemas
- No new backend aggregator, websocket layer, auth, or live broker integration
- Preserve ADR-001 and ADR-002 as the source of truth

## Validation

- Node dashboard core tests
- Integration tests for accepted, rejected, and review flows
- Documentation reflects the operator-console workflow

## Handoff Notes

- Record follow-up decisions or open questions in `.ai/handoff/decisions/`
- Write a concise summary in `.ai/handoff/summaries/`
