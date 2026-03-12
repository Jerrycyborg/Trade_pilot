# ADR-005: Research Service Isolation and Caching

## Status: Accepted

## Date: 2026-03-12

## Context

Claude API calls for web research are expensive (latency + cost). Research results for the same symbol are unlikely to change significantly within 30 minutes. The research pipeline must not block the trading loop.

## Decision

`research-service` is a standalone FastAPI service (port 8005) that:
1. Checks a SQLite-backed cache first (keyed by symbol, 30-min TTL)
2. On cache miss, calls Claude with `web_search_20250305` tool
3. Persists results and returns structured `ResearchReport`

**Isolation rationale:**
- Research failures must not crash signal generation
- Strategy-service calls research with a 5-second HTTP timeout; on any failure, a neutral stub (`confidence_modifier=0.0`) is used — signals are generated from TA alone
- Research service can be omitted entirely in zero-config mode (no `ANTHROPIC_API_KEY`)

**Cache design:**
- `expires_at = generated_at + cache_ttl_seconds` stored in DB
- `ResearchCache.get()` queries for unexpired record; returns `None` on miss
- `ResearchCache.set()` deletes old records and inserts fresh (upsert semantics)
- `cached: bool` field on `ResearchReport` tells callers whether result is from cache

## Consequences

- Research is best-effort; trading continues without it
- `research-service` can be scaled independently if needed
- The 30-min cache means Claude API is called at most once per symbol per 30 minutes across all worker cycles
