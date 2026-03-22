# Architect Plan — Trader Sprint (A+B+C+D)
_2026-03-22 | pipeline: architect → implementer → reviewer_

## Baseline: 108 tests, commit 58e7a1d
## Scenario: $50/month wallet, $10 max loss, $20 profit target, notifications, candle patterns, intraday

---

## SPRINT A — Dollar Risk Controls + Take-Profit

### A1. Monthly P&L tracker (autonomy-orchestrator/state)
Add to `_AppState` (or module-level):
```python
monthly_realized_loss_usd: float = 0.0   # cumulative losses this calendar month
monthly_profit_target_usd: float = 20.0  # stop new trades once reached
monthly_loss_limit_usd: float = 10.0     # stop new trades once exceeded
monthly_reset_date: date = today
```
Reset on first cycle of new calendar month.

### A2. run_cycle() guard
Before processing signals:
```python
if _state.monthly_realized_loss_usd >= settings.monthly_loss_limit_usd:
    logger.warning("Monthly loss limit $%.2f reached — no new trades", settings.monthly_loss_limit_usd)
    return  # skip signal loop, still process approvals
if _state.monthly_profit_target_usd_reached:
    logger.info("Monthly profit target reached — coasting")
    return
```

### A3. Take-profit monitor (new module)
`services/autonomy-orchestrator/src/autonomy_orchestrator/take_profit_monitor.py`
Mirror of StopLossMonitor:
```python
class TakeProfitRecord(BaseModel):
    symbol: str
    entry_price: float
    target_price: float    # entry + target_gain_usd / qty
    position_id: str
    qty: float = 0.0
    target_gain_usd: float = 20.0
    created_at: datetime

class TakeProfitMonitor:
    def register(self, record: TakeProfitRecord) -> None
    async def check_all(self, fetcher) -> list[str]  # returns triggered symbols
    async def _trigger_close(self, record: TakeProfitRecord) -> None
```
Registered after every approved trade alongside stop-loss.
APScheduler job every 5 min (same as stop-loss check).

### A4. Config additions (orchestrator/config.py)
```
MONTHLY_LOSS_LIMIT_USD = 10.0
MONTHLY_PROFIT_TARGET_USD = 20.0
TAKE_PROFIT_TARGET_USD = 20.0   # per-trade target gain in $
WALLET_SIZE_USD = 50.0          # used to compute risk_per_trade_pct
```

### A5. policy-baseline.yaml
```yaml
weekly_notional_cap_usd: 50
risk_per_trade_pct: 0.10   # 10% of $50 = $5 risk/trade → good R/R for $20 target
```

---

## SPRINT B — Candlestick Patterns in Rule Engine

### B1. detect_patterns already exists in indicators.py — wire it
In `rule_engine.evaluate_rules()`, after computing action:
```python
# Pattern confirmation boost
if bars is not None and len(bars) >= 2:
    from market_data.indicators import detect_patterns
    opens  = [b.open for b in bars[-3:]]
    highs  = [b.high for b in bars[-3:]]
    lows   = [b.low  for b in bars[-3:]]
    closes = [b.close for b in bars[-3:]]
    patterns = detect_patterns(opens, highs, lows, closes)
    bullish = {"hammer", "bullish_engulfing", "morning_star"}
    bearish = {"shooting_star", "bearish_engulfing"}
    if action == "BUY" and bullish & set(patterns):
        confidence = min(0.95, confidence + 0.10)
        reasoning += f" | Pattern confirmed: {bullish & set(patterns)}"
    elif action == "SELL" and bearish & set(patterns):
        confidence = min(0.95, confidence + 0.10)
        reasoning += f" | Pattern confirmed: {bearish & set(patterns)}"
```

### B2. Pass bars into evaluate_rules
`evaluate_rules(ta, config, sentiment_score, bars=None)` — add `bars: list | None = None` param.
Callers in ai_pipeline.py pass `bars=bars` when available (bars already fetched for TA).

### B3. TASummary — expose raw bars
`build_ta_summary()` already has bars. Pass them through or fetch separately in pipeline.
Simplest: pass bars directly from ai_pipeline where they're already in scope.

---

## SPRINT C — Intraday Support (Alpaca 15-min bars)

### C1. AlpacaFetcher — add intraday method
`libs/market_data/src/market_data/fetcher.py`:
```python
def fetch_intraday(self, symbol: str, period_days: int = 5, timeframe_minutes: int = 15) -> list[OHLCVBar]:
    """Fetch intraday bars (15-min default). Requires Alpaca API key."""
```
Use `StockBarsRequest(timeframe=TimeFrame.Minute(15), ...)` for stocks,
`CryptoBarsRequest(timeframe=TimeFrame.Minute(15), ...)` for crypto.

### C2. MarketDataSettings — add timeframe config
```python
timeframe: str = field(default_factory=lambda: os.getenv("MARKET_DATA_TIMEFRAME", "daily"))
# "daily" → Yahoo daily bars (no key needed)
# "intraday" → Alpaca 15-min bars (requires ALPACA_API_KEY)
intraday_minutes: int = field(default_factory=lambda: int(os.getenv("INTRADAY_MINUTES", "15")))
```

### C3. get_fetcher() — respect timeframe
If `settings.timeframe == "intraday"` and Alpaca keys set → use `AlpacaFetcher.fetch_intraday`.
If `settings.timeframe == "intraday"` and no Alpaca keys → log warning, fall back to daily Yahoo.

### C4. Multi-timeframe confirmation in rule_engine
If intraday bars available AND daily bars available:
- Daily trend must agree with intraday signal (same direction)
- If they conflict → HOLD
- Add `daily_bars: list | None = None` param to `evaluate_rules()`

---

## SPRINT D — Smart Notifications

### D1. Wire notification calls in orchestrator/main.py

Notify on these events (POST to notification-service /v1/notify):

| Event | tier | message |
|-------|------|---------|
| Stop-loss triggered | 2 | "⛔ Stop-loss fired: {symbol} closed at loss ~${loss:.2f}" |
| Take-profit triggered | 1 | "✅ Take-profit hit: {symbol} closed at +${gain:.2f}" |
| Monthly loss warning ($7 of $10 reached) | 2 | "⚠️ Monthly loss $7 of $10 limit reached — trading cautious" |
| Monthly loss limit hit ($10) | 3 | "🛑 Monthly $10 loss limit reached — all trading paused" |
| Monthly target hit ($20) | 1 | "🎯 Monthly $20 profit target reached — coasting" |
| Signal blocked (sentiment/earnings) | 0 | debug only — no push |
| Trade approved + executed | 1 | "📈 Trade: BUY {qty}x {symbol} @ ${price:.2f}" |

### D2. Notification helper
Add `_notify_event(title, body, tier)` helper in orchestrator/main.py that wraps existing `_notify()` pattern.

---

## Tests needed
- tests/autonomy_orchestrator/test_take_profit_monitor.py (4 tests)
- tests/autonomy_orchestrator/test_monthly_limits.py (3 tests: loss limit, profit target, reset)
- tests/strategy_service/test_pattern_boost.py (3 tests: hammer boosts BUY, shooting_star boosts SELL, no pattern no change)
- tests/market_data/test_intraday_fetcher.py (2 tests: mock Alpaca intraday, fallback when no key)

Total target: 108 + ~12 = 120 tests

---

## Handoff state
baseline_commit: 58e7a1d
files_create: [take_profit_monitor.py, test_take_profit_monitor.py, test_monthly_limits.py, test_pattern_boost.py, test_intraday_fetcher.py]
files_modify: [orchestrator/config.py, orchestrator/main.py, rule_engine.py, ai_pipeline.py, fetcher.py, market_data/config.py, policy-baseline.yaml, .env.example]
must_pass: 120 tests, 7 skipped, 0 failed
