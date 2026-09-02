"""The orchestrator's scheduled safety jobs, and what a dead dependency costs.

Found by the first orchestrator drill (synthetic prices, isolated archive),
within its first minutes:

- Every risk job — stop-loss, take-profit, reconciliation, health sweep — was
  registered as a sync lambda wrapping asyncio.create_task. AsyncIOScheduler
  runs sync callables in its thread-pool executor, where there is no running
  event loop, so every tick of every one of them died with "no running event
  loop" and none had ever executed. The trading cycle placed entries whose
  stops would never be watched.
- One refused connection to the audit logger, hit between `state.running =
  True` and the cycle's try block, wedged the orchestrator as "busy" for the
  rest of the process's life. The audit logger being down disabled trading —
  and the flag made it look like work was in progress.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from autonomy_orchestrator import main as m
from httpx import Response


@pytest.mark.asyncio
async def test_every_scheduled_job_is_a_coroutine_the_loop_can_await() -> None:
    """A sync lambda on AsyncIOScheduler lands in a thread with no loop, so
    `asyncio.create_task` inside it raises on every tick, forever. Handing the
    scheduler the coroutine function itself is the supported shape."""
    m.state.scheduler = None
    m._start_scheduler()
    try:
        assert m.state.scheduler is not None
        jobs = m.state.scheduler.get_jobs()
        assert len(jobs) >= 5, "cycle + four risk jobs"
        for job in jobs:
            assert asyncio.iscoroutinefunction(job.func), (
                f"{job.id} is not a coroutine function: the scheduler would run "
                f"it in a thread where create_task has no loop"
            )
    finally:
        if m.state.scheduler is not None:
            m.state.scheduler.shutdown(wait=False)
        m.state.scheduler = None


@pytest.mark.asyncio
async def test_only_the_configured_entry_owner_gets_a_trading_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRADING_LOOP_OWNER", "strategy")
    m.state.scheduler = None
    m._start_scheduler()
    try:
        assert m.state.scheduler is not None
        assert "autonomy_orchestrator" not in {job.id for job in m.state.scheduler.get_jobs()}
        assert {
            "stop_loss_check",
            "take_profit_check",
            "position_reconciliation",
            "lifecycle_health_sweep",
        }.issubset({job.id for job in m.state.scheduler.get_jobs()})
        assert await m.run_cycle() == {
            "status": "disabled",
            "reason": "trading_loop_owned_by_strategy",
        }
    finally:
        if m.state.scheduler is not None:
            m.state.scheduler.shutdown(wait=False)
        m.state.scheduler = None


def test_intraday_protective_checks_default_to_two_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MARKET_DATA_TIMEFRAME", "intraday")
    monkeypatch.delenv("STOP_LOSS_CHECK_INTERVAL_SECONDS", raising=False)
    monkeypatch.delenv("STOP_LOSS_CHECK_INTERVAL_MINUTES", raising=False)
    monkeypatch.setattr(m.state, "market_settings", None)

    assert m._risk_check_interval_seconds("STOP_LOSS_CHECK_INTERVAL_MINUTES") == 2


def test_protective_check_seconds_override_legacy_minutes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STOP_LOSS_CHECK_INTERVAL_SECONDS", "3")
    monkeypatch.setenv("STOP_LOSS_CHECK_INTERVAL_MINUTES", "1")

    assert m._risk_check_interval_seconds("STOP_LOSS_CHECK_INTERVAL_MINUTES") == 3


def test_invalid_trading_loop_owner_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRADING_LOOP_OWNER", "both")
    with pytest.raises(RuntimeError, match="TRADING_LOOP_OWNER"):
        m._trading_loop_owner()


@pytest.mark.asyncio
async def test_orchestrator_generates_each_allowed_symbol_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    async def fake_post(self, url, **kwargs):
        seen.append(kwargs["json"]["symbol"])
        return Response(
            200,
            json={"signal_id": f"sig-{len(seen)}"},
            request=m.httpx.Request("POST", url),
        )

    monkeypatch.setattr(m.httpx.AsyncClient, "post", fake_post)
    result = await m._generate_signals({"symbol_allowlist": ["aapl", "AAPL", "MSFT", "../bad"]})

    assert sorted(seen) == ["AAPL", "MSFT"]
    assert result == {
        "requested": 2,
        "generated": 2,
        "failed": 0,
        "errors": [],
    }


@pytest.mark.asyncio
async def test_an_unreachable_audit_logger_is_zero_spend_not_an_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-200 already fell back to 0.0; a refused connection must not be a
    harder failure than a broken response."""
    monkeypatch.setattr(m, "settings", replace(m.settings, audit_logger_url="http://127.0.0.1:9"))
    assert await m._weekly_spend() == 0.0


@pytest.mark.asyncio
async def test_a_cycle_with_every_dependency_dead_still_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One down service must cost exactly its own feature, never the cycle.
    The drill found four separate call sites where a refused connection
    aborted everything after it — including the exit pass and, worst, the
    notification call sitting between the policy verdict and the order."""
    dead = "http://127.0.0.1:9"
    monkeypatch.setattr(
        m,
        "settings",
        replace(
            m.settings,
            strategy_service_url=dead,
            policy_service_url=dead,
            execution_service_url=dead,
            portfolio_service_url=dead,
            audit_logger_url=dead,
            approval_gateway_url=dead,
            notification_service_url=dead,
            broker_url=dead,
        ),
    )
    m.state.running = False
    summary = await m.run_cycle()
    assert summary["status"] in ("completed", "halted")
    assert m.state.running is False


@pytest.mark.asyncio
async def test_a_failed_cycle_never_leaves_the_orchestrator_busy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """state.running guards against overlapping cycles; a dependency failure
    before the try block used to leave it latched True, so every later cycle
    — scheduled or manually triggered — was refused as 'busy' until restart."""

    async def boom() -> float:
        raise ValueError("dependency exploded")

    monkeypatch.setattr(m, "_weekly_spend_safe", boom)
    m.state.running = False
    with pytest.raises(ValueError):
        await m.run_cycle()
    assert m.state.running is False, "a dead dependency must not wedge the loop"


@pytest.mark.asyncio
async def test_the_earnings_posture_survives_a_broken_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blanket `except: False` around the earnings gate silently failed OPEN
    for an operator who configured EARNINGS_GATE_FAIL_CLOSED=true — the module
    import missing from this container, or any error before the gate's own
    posture applies, discarded the configured posture entirely."""

    def boom(_symbol: str):
        raise RuntimeError("gate unreachable")

    monkeypatch.setattr("strategy_service.earnings_calendar.check_earnings_blackout", boom)
    monkeypatch.setenv("EARNINGS_GATE_FAIL_CLOSED", "true")
    assert m._earnings_blackout_for("NVDA") is True

    monkeypatch.setenv("EARNINGS_GATE_FAIL_CLOSED", "false")
    assert m._earnings_blackout_for("NVDA") is False


@pytest.mark.asyncio
async def test_the_earnings_gate_runs_off_the_cycles_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate reaches yfinance synchronously. Called inline from the async
    policy pass, a slow calendar froze the whole event loop — the cycle, the
    stop-loss ticks, everything — for up to a socket timeout per symbol."""
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from contracts import SignalCandidate

    seen: dict[str, bool] = {}

    def probe(_symbol: str) -> bool:
        try:
            asyncio.get_running_loop()
            seen["on_loop"] = True
        except RuntimeError:
            seen["on_loop"] = False
        return False

    monkeypatch.setattr(m, "_earnings_blackout_for", probe)
    monkeypatch.setattr(m, "settings", replace(m.settings, policy_service_url="http://127.0.0.1:9"))
    signal = SignalCandidate(
        signal_id="sig-loop-probe",
        symbol="NVDA",
        ts=datetime.now(timezone.utc),
        candidate_action="BUY",
        confidence=0.9,
        size_pct=0.05,
        model_version="test",
    )
    # The dead policy service fails the evaluation closed — expected; the
    # gate has already been consulted by then, which is what this observes.
    with pytest.raises(RuntimeError, match="Policy service unreachable"):
        await m._policy_evaluate(
            signal,
            SimpleNamespace(adjusted_size_pct=0.05),
            {"symbol_allowlist": ["NVDA"]},
            {"positions": []},
        )
    assert seen["on_loop"] is False, "the calendar lookup ran on the event loop"


@pytest.mark.asyncio
async def test_a_garbage_gate_config_is_still_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate refuses an unparseable EARNINGS_GATE_FAIL_CLOSED on the first
    call; the orchestrator's wrapper must not swallow that refusal into a
    silent fail-open."""
    monkeypatch.setenv("EARNINGS_GATE_FAIL_CLOSED", "yes please")
    with pytest.raises(ValueError, match="EARNINGS_GATE_FAIL_CLOSED"):
        m._earnings_blackout_for("NVDA")


def _queued_signal():
    return m.SignalCandidate(
        signal_id="queued-entry",
        symbol="AAPL",
        ts=m.datetime.now(m.timezone.utc),
        candidate_action="BUY",
        confidence=0.8,
        size_pct=0.01,
        model_version="test",
    )


def _stub_cycle_until_portfolio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def weekly() -> float:
        return 0.0

    async def approvals() -> None:
        return None

    async def audit(_event) -> None:
        return None

    async def stop_after_queue():
        raise RuntimeError("queue probe complete")

    monkeypatch.setenv("TRADING_LOOP_OWNER", "orchestrator")
    monkeypatch.setattr(m, "load_policy_config", lambda _path: {"kill_switch": False})
    monkeypatch.setattr(m, "_weekly_spend_safe", weekly)
    monkeypatch.setattr(m, "_process_approvals", approvals)
    monkeypatch.setattr(m, "_monthly_limits_ok", lambda: True)
    monkeypatch.setattr(m, "_portfolio_state", stop_after_queue)
    monkeypatch.setattr(m, "_audit", audit)
    m.state.allowlist_validation_ran = True
    m.state.running = False


@pytest.mark.asyncio
async def test_cycle_drains_existing_backlog_before_generating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_cycle_until_portfolio(monkeypatch)

    async def pending():
        return [_queued_signal()]

    async def generation(_config):
        raise AssertionError("generation must wait until the durable queue drains")

    monkeypatch.setattr(m, "_pending_signals", pending)
    monkeypatch.setattr(m, "_generate_signals", generation)

    summary = await m.run_cycle()

    assert summary["generation"] == {
        "status": "skipped_pending_backlog",
        "pending": 1,
    }


@pytest.mark.asyncio
async def test_cycle_does_not_generate_when_queue_state_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_cycle_until_portfolio(monkeypatch)

    async def pending():
        return None

    async def generation(_config):
        raise AssertionError("an unreadable queue is not an empty queue")

    monkeypatch.setattr(m, "_pending_signals", pending)
    monkeypatch.setattr(m, "_generate_signals", generation)

    summary = await m.run_cycle()

    assert summary["generation"] == {"status": "skipped_signal_queue_unavailable"}


@pytest.mark.asyncio
async def test_cycle_generates_only_when_backlog_is_empty_then_refetches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_cycle_until_portfolio(monkeypatch)
    reads = 0
    generations = 0

    async def pending():
        nonlocal reads
        reads += 1
        return [] if reads == 1 else [_queued_signal()]

    async def generation(_config):
        nonlocal generations
        generations += 1
        return {"requested": 1, "generated": 1, "failed": 0, "errors": []}

    monkeypatch.setattr(m, "_pending_signals", pending)
    monkeypatch.setattr(m, "_generate_signals", generation)

    summary = await m.run_cycle()

    assert generations == 1
    assert reads == 2
    assert summary["generation"]["generated"] == 1
