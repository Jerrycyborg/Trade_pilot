# Architect Plan — Alpha Edge Sprint
_2026-03-22 | pipeline: architect → implementer → reviewer_

## Objective
Two highest-ROI signal quality improvements:
1. Sentiment hard gate — override BUY→HOLD if score < -0.3
2. Earnings blackout — auto-populate event_blackout_active from yfinance calendar

## Baseline
- 98 tests passing, commit 41c4963
- yfinance already installed; sentiment service already wired for confidence weighting (not gating)

---

## A. Sentiment Gate

### rule_engine.py
Add `sentiment_score: float | None = None` param to `evaluate_rules()`.
After computing action:
```python
GATE = float((config or {}).get("sentiment_block_threshold", -0.3))
if action == "BUY" and sentiment_score is not None and sentiment_score < GATE:
    action = "HOLD"
    reasoning += f" | Sentiment gate (score={sentiment_score:.2f})"
    confidence = min(confidence, 0.55)
```

### ai_pipeline.py
Pass `sentiment_score=sentiment.score if sentiment else None` into all `evaluate_rules()` calls.

---

## B. Earnings Blackout

### earnings_calendar.py (NEW — strategy-service)
```python
def is_earnings_blackout(symbol: str, blackout_days: int = 2) -> bool:
    """True if today within blackout_days of earnings. Fails open (returns False on error)."""
```
Use `yf.Ticker(symbol).calendar` — parse nearest earnings date, check |delta| <= blackout_days.

### strategy-service/main.py
Import and call `is_earnings_blackout(symbol)` in `generate_signal()`.
Expose result as log + pass to orchestrator via signal metadata if field exists.

### autonomy-orchestrator/main.py
When building policy payload in run_cycle: call `is_earnings_blackout(signal.symbol)`,
set `market_context["event_blackout_active"] = True` if so.

---

## C. Config additions (strategy-service/config.py)
```
SENTIMENT_BLOCK_THRESHOLD = -0.3
EARNINGS_BLACKOUT_DAYS = 2
```
Document both in .env.example.

---

## D. Tests
- tests/strategy_service/test_sentiment_gate.py — 4 tests (blocked, neutral, no-sentiment, sell-unaffected)
- tests/strategy_service/test_earnings_blackout.py — 4 tests (bool return, fail-open, far=False, near=True)

---

## Handoff state
files_create: [earnings_calendar.py, test_sentiment_gate.py, test_earnings_blackout.py]
files_modify: [rule_engine.py, ai_pipeline.py, strategy/main.py, orchestrator/main.py, strategy/config.py, .env.example]
must_pass: 98 existing + 8 new = 106 tests
