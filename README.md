# AI Trading Stack Bootstrap

This workspace bootstraps Milestone 1 of the trading stack described in `Project_spec2.md` and `Bootstrap.md`.

Included components:

- `libs/contracts`: shared Pydantic contracts
- `services/strategy-service`: deterministic fake signal generator
- `services/policy-service`: deterministic policy evaluation with persistence
- `services/execution-service`: paper broker order flow with idempotency
- `services/portfolio-service`: derived positions and PnL from execution fills
- `apps/dashboard`: placeholder app directory
- `tests`: unit and API-level tests
- `.ai/handoff`: AAHP handoff scaffold
- `tools/aahp.py`: AAHP helper for manifest validation, checksums, and task briefs

## Local setup

1. Install Python 3.11 and [uv](https://docs.astral.sh/uv/).
2. Run `make setup`.
3. Copy `.env.example` to `.env` if you want to customize database URLs.
4. Start Postgres with `docker compose up postgres -d`.
5. Run services with:
   - `make run-strategy`
   - `make run-policy`
   - `make run-execution`
   - `make run-portfolio`

## Development commands

- `make lint`
- `make test`
- `make run-policy`
- `make run-execution`
- `make run-portfolio`
- `make run-strategy`
- `make aahp-validate`
- `make aahp-checksums`

## AAHP workflow

The repo now includes a lightweight AAHP workflow:

- `.ai/handoff/manifest.json` defines the intended context boundary.
- `.ai/handoff/checksums/manifest_checksums.json` records hashes for files inside that boundary.
- `.ai/handoff/task_briefs/TEMPLATE.md` is the standard task brief format.
- `.ai/handoff/prompts/agent_prompt_template.md` is the reusable prompt skeleton.
- `tools/aahp.py` provides:
  - `validate-manifest`
  - `generate-checksums`
  - `create-task-brief <task-id> <title>`

Example:

```bash
python3 tools/aahp.py validate-manifest
python3 tools/aahp.py generate-checksums
python3 tools/aahp.py create-task-brief TASK-001 "Implement dashboard shell"
```

## Milestone 1 status

Implemented:

- shared contracts
- strategy, policy, and execution services
- portfolio service derived from execution fills
- paper broker adapter
- service persistence models
- AAHP bootstrap scaffold

Remaining for Milestone 1:

- dashboard implementation beyond placeholder docs
- service-to-service wiring and local orchestration beyond standalone endpoints
- deeper integration tests against a live Postgres container

## Execution to Portfolio Boundary

The repo treats execution persistence as the upstream boundary for a future `portfolio-service`.

- `fills` are the source of truth for position changes and realized execution.
- `orders` are the source of truth for order intent and lifecycle requests, but not for positions.
- `execution_events` are the audit/event stream for downstream consumers that need lifecycle visibility.

Until `portfolio-service` exists, the execution service owns these records and downstream code should derive positions from fills, not accepted orders alone.

Live Postgres execution integration tests:

- start Postgres locally, for example with `docker compose up postgres -d`
- set `TEST_EXECUTION_POSTGRES_URL`
- run `pytest tests/integration/test_execution_service_postgres.py`
- portfolio-facing read boundary coverage lives in `tests/integration/test_execution_service_portfolio_boundary.py`
