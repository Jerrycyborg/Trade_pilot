You are bootstrapping a production-minded AI trading stack.

Read only:
- Project_spec2.md

Goal:
Create the initial monorepo skeleton for Milestone 1 only.

Milestone 1 scope:
- libs/contracts
- services/policy-service
- services/execution-service
- services/strategy-service
- apps/dashboard
- tests
- docs
- .ai/handoff

Do not implement:
- reasoning-service
- live brokers
- RL training
- multi-broker support
- options
- portfolio optimization

Requirements:
- Python 3.11
- FastAPI for services
- Pydantic for schemas
- SQLAlchemy for persistence
- Postgres for service databases
- Redis optional only if needed
- pytest for tests
- Dockerfiles for each service
- docker-compose.yml for local development
- Makefile with setup, lint, test, run commands
- .env.example
- README.md
- docs/architecture.md
- .ai/handoff/manifest.json
- .ai/handoff/decisions/ADR-001-execution-boundary.md
- .ai/handoff/task_briefs/
- .ai/handoff/summaries/
- .ai/handoff/prompts/

Contracts to create in libs/contracts:
- SignalCandidate
- PolicyEvaluationRequest
- PolicyDecision
- ExecutionOrderRequest
- ExecutionOrderResponse
- OrderStatus
- FeatureSnapshot

policy-service must include:
- POST /v1/policy/evaluate
- deterministic rule engine
- hard reject and review logic from spec
- unit tests for:
  - stale data rejection
  - max size rejection
  - confidence review
  - approve path

execution-service must include:
- POST /v1/orders
- GET /v1/orders/{order_id}
- Idempotency-Key support
- paper broker adapter only
- order state model:
  NEW, ACCEPTED, PARTIALLY_FILLED, FILLED, REJECTED, CANCELLED
- unit tests for:
  - duplicate submission same payload
  - duplicate submission different payload
  - valid order accepted
  - rejected order path

strategy-service must include:
- POST /v1/signals/generate
- fake deterministic signal generator for MVP
- returns SignalCandidate
- simple unit tests

dashboard app:
- placeholder only
- simple README
- no real frontend logic required yet

Persistence requirements:
- execution-service owns tables:
  - orders
  - fills
  - execution_events
- policy-service owns tables:
  - policy_evaluations
  - policy_rule_hits

Rules:
- do not scan or invent unrelated parts of the repo
- keep code small and clear
- keep service boundaries explicit
- do not add reasoning-service
- do not add broker integrations beyond paper broker
- do not add background jobs unless necessary
- use structured logging
- write concise docstrings
- add TODO markers only where clearly needed

Deliverables:
1. repo skeleton
2. working contracts package
3. working policy-service
4. working execution-service
5. working strategy-service
6. tests
7. docker-compose.yml
8. Makefile
9. README.md with local startup instructions
10. .ai/handoff/summaries/TASK-000-bootstrap.md

When finished, also output:
- files created
- files modified
- what remains for Milestone 1