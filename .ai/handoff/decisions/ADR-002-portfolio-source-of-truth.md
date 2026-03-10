# ADR-002: Portfolio Source Of Truth

## Status

Accepted

## Decision

`portfolio-service` derives positions exclusively from execution fills.

`execution-service` remains the owner of:

- `orders` for order intent and lifecycle state
- `fills` for realized execution
- `execution_events` for downstream lifecycle consumption and audit

`portfolio-service` must not infer holdings from order state.

Position updates occur only when a fill is recorded.

## Context

Accepted orders do not imply filled positions.

Orders may be:

- rejected
- cancelled
- partially filled

Inferring positions from `orders` would therefore lead to incorrect portfolio state.

Execution persistence must therefore distinguish between:

- order intent (`orders`)
- execution lifecycle (`execution_events`)
- realized trades (`fills`)

## Position Accounting Rules

Positions are derived using fill records only.

Rules:

- position changes occur only on `fills`
- partial fills update positions incrementally
- rejected or cancelled orders do not affect positions
- average cost method: weighted average
- realized PnL method: average-cost realization
- unrealized PnL: mark-to-market using latest quote from `data-service`
- fallback price: last fill price if quote unavailable

## Data Ownership

`execution-service` owns and persists:

- `orders`
- `fills`
- `execution_events`

`portfolio-service` is a derived-state service and does not modify execution data.

## Access Pattern

`portfolio-service` consumes execution data via:

- execution-service read endpoints or
- a shared read model derived from execution tables.

Direct mutation of execution persistence by portfolio-service is prohibited.

## Required Identifiers

Execution persistence must include identifiers required for reconciliation:

- `order_id`
- `external_order_id`
- `signal_id`
- `symbol`

## Consequences

- execution-service remains the single source of trading truth
- portfolio-service becomes deterministic and reconstructable
- replay and reconciliation become possible from fill history
- downstream consumers may subscribe to `execution_events` for lifecycle awareness without treating them as position truth