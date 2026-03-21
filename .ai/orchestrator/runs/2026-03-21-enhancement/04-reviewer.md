## Verdict: PASS WITH NOTES

### Checks

- [PASS] correctness — `compute_atr` correctly implements Wilder's ATR: TR = max(H-L, |H-prevC|, |L-prevC|), seeded with SMA then Wilder-smoothed. `evaluate_rules` logic is coherent: dual-EMA trend + RSI bounds + MACD histogram with correct BUY/SELL zone definitions. `check_all` in `StopLossMonitor` correctly fetches latest bar, compares close vs stop_price, triggers `_trigger_exit` via httpx POST to `/v1/orders/close`, and removes symbol after trigger.

- [PASS] look-ahead bias — `compute_atr` operates only on provided input slice. Backtest engine `_compute_signals` uses `bars[:i+1]` (confirmed: engine.py line 24 + comment "no look-ahead"). ATR stop sizing also uses `bars[:i+1]` slice at entry time (engine.py line 82). `test_no_lookahead` validates this with a terminal spike that must not influence prior signals.

- [PASS] safety — `etoro_broker.py` shows 0 diff lines (git diff confirms untouched). `stop_loss_monitor._trigger_exit` calls internal broker service `/v1/orders/close` — not direct eToro API. `StopLossMonitor` is not wired into `run_cycle()` (deferred), so no autonomous live-trading exposure added this sprint.

- [PASS] test coverage — Four new test files confirmed and correctly scoped:
  - `test_rule_engine.py` — BUY/SELL/HOLD branching, overbought/oversold guards, confidence increments
  - `test_stop_loss_monitor.py` — register, overwrite, check_all trigger vs. no-trigger with mocked fetcher
  - `test_alpaca_stream.py` — AlpacaStreamFetcher coverage
  - `backtest-service/tests/test_engine.py` — includes `test_no_lookahead` (spike-at-end), ATR sizing, smoke test
  - 90 passing, 0 new failures.

- [PASS] deferred items — All safely deferrable:
  1. **vectorbt**: pure-Python engine is a valid functional replacement; no blocking gap.
  2. **stop_loss_monitor → run_cycle()**: deferring integration avoids unreviewed live-execution risk; unit-tested in isolation.
  3. **backtest-service Dockerfile**: ops-only; service functional for local/test use.

### Issues

- **Minor**: `_trigger_exit` passes `qty=0` with comment "broker should close full position". If broker endpoint requires explicit qty, the order may fail silently (exception logged but not re-raised). Low risk while deferred, but needs validation at wiring time.
- **Minor**: `check_all` fetches `period_days=1`. If market is closed and no bars returned, the guard logs a warning and skips — acceptable (no false trigger), but relies on broker data freshness.

### Recommended follow-ups

1. When wiring `StopLossMonitor` into `run_cycle()`, validate `qty=0` handling at broker's `/v1/orders/close`, or pass actual position size.
2. Add a `test_compute_atr_exact_values` test with known input/output to guard against future regressions in Wilder smoothing.
3. Strengthen `test_atr_sizing_produces_fractional_positions`: currently only checks `isfinite(sharpe_ratio)` — add assertion that larger risk_per_trade produces larger position size.
4. Add backtest-service Dockerfile before any staging deployment.
