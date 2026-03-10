# ADR-001: Execution Boundary

## Status

Accepted

## Decision

Only `execution-service` may place or simulate orders.

## Context

Milestone 1 excludes the reasoning service and all live brokers. Strategy may propose signals and policy may approve or reject them, but neither may submit orders.

## Consequences

- service boundaries stay explicit
- policy remains deterministic
- execution can be tested independently with a paper broker adapter
