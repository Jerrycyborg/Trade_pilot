from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

from autonomy_orchestrator.main import _monthly_limits_ok, settings, state


def setup_function() -> None:
    state.monthly_realized_loss_usd = 0.0
    state.monthly_realized_profit_usd = 0.0
    now = datetime.now(timezone.utc)
    state.monthly_reset_month = now.month
    state.monthly_reset_year = now.year


def test_limits_ok_by_default() -> None:
    assert _monthly_limits_ok() is True


def test_loss_limit_blocks() -> None:
    state.monthly_realized_loss_usd = settings.monthly_loss_limit_usd
    assert _monthly_limits_ok() is False


def test_profit_target_blocks() -> None:
    state.monthly_realized_profit_usd = settings.monthly_profit_target_usd
    assert _monthly_limits_ok() is False


def test_monthly_limits_reset_across_year_boundary() -> None:
    state.monthly_realized_loss_usd = settings.monthly_loss_limit_usd
    state.monthly_realized_profit_usd = settings.monthly_profit_target_usd
    state.monthly_reset_month = 3
    state.monthly_reset_year = 2025

    with patch("autonomy_orchestrator.main.datetime") as mock_datetime:
        mock_datetime.now.return_value = datetime(2026, 3, 22, tzinfo=timezone.utc)
        assert _monthly_limits_ok() is True

    assert state.monthly_realized_loss_usd == 0.0
    assert state.monthly_realized_profit_usd == 0.0
    assert state.monthly_reset_month == 3
    assert state.monthly_reset_year == 2026
