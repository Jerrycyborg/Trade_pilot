# TASK-009 Path to a Profitable Strategy

## Status: PROPOSED — awaiting go/no-go on ADR-006

## Goal

Establish whether this system can make money, and if so, on what. Every phase
below is an experiment with a stated kill criterion, not a construction task.
The system is architecturally complete; what is missing is evidence.

## Context

Read these and nothing else. The codebase is large and reading it all is a known
failure mode for this task.

- `.ai/handoff/decisions/ADR-006-where-profitability-comes-from.md` — the four
  decisions this brief executes. Read first.
- `services/backtest-service/src/backtest_service/validation.py` — the existing
  walk-forward, embargo and trial counting. Strong; do not rebuild.
- `services/backtest-service/src/backtest_service/stats.py` — Probabilistic
  Sharpe, expected-max Sharpe, Deflated Sharpe. Already present.
- `services/backtest-service/src/backtest_service/strategies.py` — the research
  copy of the momentum rule.
- `services/strategy-service/src/strategy_service/rule_engine.py` — the live
  copy. Phase 1 removes this duplication.
- `libs/journal/src/journal/store.py` — `record_bars`, `bars_as_of`. Read only
  the docstrings on those two; they explain the observation-log semantics that
  Phase 2 must not violate.

## Cost model

Adopt as backtest defaults. Derivation and sources in ADR-006; treat as priors to
be replaced by measured fills once live.

| Parameter | Large cap | Mid cap ($2-10bn) |
|---|---|---|
| Round-trip base case | **10 bps** | **25 bps** |
| Round-trip stress case | **20 bps** | **45 bps** |
| SEC Section 31 (sell side) | 0.206 bps | 0.206 bps |
| FINRA TAF (sell side) | $0.000195/share, cap $9.79 | same |
| Short borrow, general collateral | ~30 bps/yr; $0 at Alpaca on ETB | 30-100 bps/yr |
| Market impact | not modelled — <0.5 bps at this size | not modelled |

Every result is reported at base **and** stress. A strategy that only clears the
base case has not cleared.

## Deliverables

Phased. Each phase gates the next.

### Phase 1 — Make the existing evidence mean something

1. `libs/strategy` — one signal implementation and one parameter set, imported by
   both `backtest-service` and `strategy-service`. Delete the duplicate in
   `rule_engine.py`.
2. A test asserting research and live produce identical signals on identical bars.
3. Unify sizing: the ATR risk-budget model becomes the single sizing path,
   replacing the three-value `_SIZE_BY_RISK` lookup and the hard-coded
   `$100_000` notional in `policy_service/rules.py:111`.
4. Re-run the champion's walk-forward at the cost model above, multi-year daily.

**Kill criterion:** if the champion's Deflated Sharpe is at or below zero
out-of-sample at the base cost case, it is not promoted, not hardened, and not
defended. Phase 2 proceeds on the assumption it is dead.

**Blocker:** needs multi-year daily bars. This container's egress blocks Yahoo
and Alpaca (403). Resolve by running locally, opening the proxy allowlist, or
supplying CSV.

### Phase 2 — Data foundation for cross-sectional work

1. `reference_bars` store — vendor history, keyed `(symbol, timeframe, bar_ts,
   vendor, vendor_asof)`. Additive migration. **Never** read by `bars_as_of`;
   never stamped into `observed_at`.
2. Survivorship-bias-free universe with point-in-time membership and corporate
   actions. Without this, any cross-sectional result is invalid — this is the
   hard requirement of the phase, not the bar backfill.
3. Backfill CLI: idempotent, resumable, provenance-stamped with vendor and feed.
   `record_bars` is already dedup-and-revision aware; mirror its design.
4. Archive-vs-reference divergence report, as a free data-quality check.

**Kill criterion:** if a survivorship-free universe with point-in-time membership
cannot be obtained at acceptable cost, cross-sectional work stops here and the
programme reverts to single-name with an honest statement of its ceiling.

### Phase 3 — Cross-sectional alpha

1. Universe ranking in `libs/strategy`: rank on features, trade the extremes.
2. Feature set beyond price: liquidity, volatility, and at least one
   non-technical feature.
3. Rebalance frequency chosen against the turnover budget, not by preference.
4. Walk-forward on the wide universe at both cost cases.

**Kill criterion:** Deflated Sharpe at or below zero out-of-sample at the stress
case, across at least 200 closed trades. Below 100 trades, no verdict is claimed
either way.

### Phase 4 — Live portfolio construction

`portfolio-service` allocates rather than only tracking: correlation-aware
weights, gross/net exposure, sector caps, volatility targeting. Move
`backtest_service/portfolio.py`'s correlation machinery to a shared library so
research and live share it, per ADR-006 decision 1.

### Phase 5 — Validation hardening

Add PBO (CSCV) alongside the existing Deflated Sharpe. Smallest gap; last.

## Constraints

- **No new services.** Shared libraries and changes inside existing services.
  `project_spc.md`'s service map is unchanged.
- **No LLM in the primary alpha path.** LLM signals cannot be backtested honestly
  (ADR-006, decision 4). Multiple models are used for research throughput —
  generating and screening hypotheses that the deterministic walk-forward then
  judges — and for the reasoning layer the spec already defines.
- Real-money execution stays disabled. No phase here enables it, promotes a
  sleeve, or grants any automated component transition authority.
- Journal migrations additive and reversible.
- Do not re-read the whole codebase. The Context list above is sufficient;
  delegate wide searches to a subagent and keep the conclusion, not the files.

## Validation

- `uv run pytest -q` green offline from a clean checkout, no env vars.
- `uv run ruff check .` clean.
- Every fix carries a test shown to fail before it.
- Backtest results reported at base and stress cost, with trade count, and with
  Deflated Sharpe alongside raw Sharpe. A raw Sharpe quoted alone is not a result.

## Handoff Notes

- The single most important finding behind this brief: **live and research run
  different strategy code.** `CHAMPION_PARAMETERS` in `rule_engine.py` and
  `MOMENTUM_FIELDS` in `strategies.py` are independent definitions that agree by
  convention. Until Phase 1 lands, no backtest result describes the system that
  trades.
- The validation and attribution layers are the strongest parts of this codebase
  and are ahead of what most retail systems ever build. Do not rebuild them;
  point them at something worth measuring.
- Statistical power drives the universe decision as much as alpha does. Five
  symbols on daily bars cannot produce 100 closed trades in a useful horizon.
- Sources behind the cost table were reached via search extracts, not primary
  fetches (egress). Confirm before treating as settled.
