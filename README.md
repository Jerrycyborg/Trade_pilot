![Trade Pilot banner](assets/trade-pilot-banner.svg)

# Trade Pilot

Production-minded AI trading stack built around a strict execution boundary: strategy proposes, policy approves, execution persists fills, and portfolio state is derived from fills only.

Suggested GitHub description:
`Production-minded AI trading stack with deterministic execution, fill-driven portfolio reconciliation, and AAHP-based AI handoffs.`

Suggested GitHub topics:
`ai-trading`, `algorithmic-trading`, `fastapi`, `python`, `postgresql`, `portfolio-management`, `risk-management`, `multi-agent`, `aahp`

## Overview

This repository implements a staged trading platform architecture with explicit service boundaries:

- `strategy-service` generates deterministic signals
- `policy-service` applies deterministic risk and approval rules
- `execution-service` handles order persistence, fills, and execution events
- `portfolio-service` derives positions and PnL from execution fills only

The design intentionally separates reasoning, policy, execution, and portfolio state so that trading behavior stays auditable and deterministic.

## Architecture

Core flow:

1. strategy proposes a signal
2. policy approves or rejects it
3. execution persists orders, fills, and events
4. portfolio reconciles from fills only

Key boundary rule:

- `orders` are not portfolio truth
- `fills` are the source of truth for positions
- `execution_events` are the lifecycle and audit stream

## Services

- `libs/contracts`: shared Pydantic contracts
- `services/strategy-service`: fake deterministic signal generation
- `services/policy-service`: deterministic policy gate
- `services/execution-service`: paper execution, fills, idempotency, execution events
- `services/portfolio-service`: derived positions, snapshots, and PnL reconciliation
- `apps/dashboard`: placeholder for future operator UI

## Quick Start

1. Install Python 3.11 and `uv`.
2. Run `make setup`.
3. Copy `.env.example` to `.env` if you need custom database URLs.
4. Start Postgres:

```bash
docker compose up postgres -d
```

5. Run services as needed:

```bash
make run-strategy
make run-policy
make run-execution
make run-portfolio
```

## Development

Commands:

- `make lint`
- `make test`
- `make run-strategy`
- `make run-policy`
- `make run-execution`
- `make run-portfolio`
- `make aahp-validate`
- `make aahp-checksums`

Live Postgres execution integration tests:

1. Start Postgres locally.
2. Set `TEST_EXECUTION_POSTGRES_URL`.
3. Run:

```bash
pytest tests/integration/test_execution_service_postgres.py
pytest tests/integration/test_execution_service_portfolio_boundary.py
```

## Repository Layout

```text
libs/
  contracts/

services/
  strategy-service/
  policy-service/
  execution-service/
  portfolio-service/

apps/
  dashboard/

tests/
.ai/handoff/
```

## AAHP Workflow

The repository includes a lightweight AAHP handoff structure for multi-agent work:

- `.ai/handoff/manifest.json`
- `.ai/handoff/checksums/manifest_checksums.json`
- `.ai/handoff/task_briefs/`
- `.ai/handoff/summaries/`
- `.ai/handoff/decisions/`
- `.ai/handoff/prompts/`

Helper commands:

```bash
python3 tools/aahp.py validate-manifest
python3 tools/aahp.py generate-checksums
python3 tools/aahp.py create-task-brief TASK-001 "Implement dashboard shell"
```

## Status

Implemented:

- shared contracts
- strategy, policy, execution, and portfolio services
- paper broker adapter
- execution-to-portfolio fill boundary
- positions, snapshots, and PnL persistence
- AAHP scaffold and summaries

Out of scope:

- live broker sync
- options or margin logic
- advanced analytics
- background workers
- large-scale LLM orchestration
