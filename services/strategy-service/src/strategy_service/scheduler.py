"""APScheduler integration for the trade worker."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

_scheduler: Optional[object] = None  # APScheduler instance


def start_scheduler(app) -> None:
    """Start the APScheduler with the trade worker job."""
    global _scheduler
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    from .config import settings
    from .worker import worker_state

    _scheduler = AsyncIOScheduler()

    async def _run_job() -> None:
        from .worker import TradeWorker
        if worker_state.is_running:
            logger.warning("Scheduler: previous cycle still running — skipping")
            return
        if not _is_market_hours():
            logger.debug("Scheduler: outside market hours — skipping")
            return
        logger.info("Scheduler: starting trade cycle")
        worker = TradeWorker()
        await worker.run_cycle()

    _scheduler.add_job(
        _run_job,
        trigger="interval",
        minutes=settings.worker_interval_minutes,
        id="trade_worker",
        max_instances=1,
    )
    _scheduler.start()

    # Update next_run_at after scheduler starts
    try:
        job = _scheduler.get_job("trade_worker")
        if job and job.next_run_time:
            worker_state.next_run_at = job.next_run_time
    except Exception:
        pass

    logger.info(
        "Trade worker scheduler started. Interval: %d min. Worker enabled: %s",
        settings.worker_interval_minutes,
        settings.worker_enabled,
    )


def stop_scheduler() -> None:
    """Shut down the scheduler gracefully."""
    global _scheduler
    if _scheduler is not None:
        try:
            _scheduler.shutdown(wait=False)
        except Exception as exc:
            logger.warning("Scheduler shutdown error: %s", exc)
        _scheduler = None


def _is_market_hours() -> bool:
    """Basic market hours gate: Mon–Fri, 09:30–16:00 US Eastern.
    Crypto symbols (with /) always pass this gate in the worker itself.
    This is a coarse check only; Alpaca clock is used for precision if configured.
    """
    try:
        import zoneinfo

        eastern = zoneinfo.ZoneInfo("America/New_York")
    except Exception:
        return True  # Can't determine — allow

    now_et = datetime.now(eastern)
    if now_et.weekday() >= 5:  # Saturday or Sunday
        return False
    market_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
    return market_open <= now_et <= market_close
