# Milestone 1 Architecture

Milestone 1 implements a deterministic paper-trading core with explicit service boundaries.

Flow:

1. `strategy-service` emits a fake `SignalCandidate`.
2. `policy-service` evaluates that candidate against hard reject and review rules.
3. `execution-service` submits approved orders to a paper broker adapter.
4. The paper broker returns deterministic acceptance or rejection.
5. Execution state is persisted for later dashboard and portfolio work.
6. Future portfolio logic must treat `fills` as the source of truth for positions and `execution_events` as the lifecycle audit feed.

Non-goals for this milestone:

- no reasoning service
- no live brokers
- no RL or portfolio optimization
- no background workers

Only `execution-service` may place orders. All other services stop at proposal or evaluation boundaries.

Execution-to-portfolio boundary:

- `orders`: order intent and lifecycle record owned by `execution-service`
- `fills`: source of truth for portfolio position changes
- `execution_events`: downstream event/audit feed for portfolio and monitoring consumers
- read interfaces exposed by `execution-service`: `/v1/orders/{order_id}/fills`, `/v1/fills`, `/v1/execution/events`

## Alpha Edge Features (2026-03-22)

### Sentiment Gate
`evaluate_rules()` accepts `sentiment_score`. If BUY and score < `SENTIMENT_BLOCK_THRESHOLD` (default -0.3),
action overrides to HOLD. Configured via env var. Wired in ai_pipeline and deterministic path.

### Earnings Blackout
`earnings_calendar.is_earnings_blackout(symbol)` uses yfinance to detect proximity to earnings.
Fails open (never blocks on error). BUY signals suppressed to HOLD within `EARNINGS_BLACKOUT_DAYS` (default 2).
Wired in strategy-service generate_signal() and autonomy-orchestrator policy payload.
