from __future__ import annotations

from datetime import datetime, timezone

from autonomy_orchestrator.main import _monthly_limits_ok, settings, state


def setup_function() -> None:
    state.monthly_realized_loss_usd = 0.0
    state.monthly_realized_profit_usd = 0.0
    state.monthly_reset_month = datetime.now(timezone.utc).month


def test_limits_ok_by_default() -> None:
    assert _monthly_limits_ok() is True


def test_loss_limit_blocks() -> None:
    state.monthly_realized_loss_usd = settings.monthly_loss_limit_usd
    assert _monthly_limits_ok() is False


def test_profit_target_blocks() -> None:
    state.monthly_realized_profit_usd = settings.monthly_profit_target_usd
    assert _monthly_limits_ok() is False
