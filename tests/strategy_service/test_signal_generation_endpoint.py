"""The signal-generation endpoint: the rule must see the market.

Found by the post-fix orchestrator drill: with a synthetic uptrend on file and
the file provider serving it, POST /v1/signals/generate (non-AI path) returned
SELL at 0.77 for NVDA — twice, identically. The endpoint called the
deterministic builder bare, so every request landed in its no-data fallback,
which fabricates a direction from the symbol name's character sum. The same
defect was fixed on the worker's path months of fixes ago; the HTTP path had
kept the old shape, and nothing that trades through the orchestrator ever saw
the rule engine.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from market_data.fetcher import DataUnavailableError
from strategy_service import main as m


def _uptrend_bars(n: int = 60) -> list[SimpleNamespace]:
    now = datetime.now(timezone.utc)
    bars = []
    for i in range(n):
        close = 150.0 + i * 0.6
        bars.append(
            SimpleNamespace(
                timestamp=now - timedelta(days=n - i),
                open=close - 0.4,
                high=close + 0.8,
                low=close - 1.2,
                close=close,
                volume=40_000_000.0,
            )
        )
    return bars


class TestGenerateEndpointReadsTheMarket:
    @pytest.mark.asyncio
    async def test_the_deterministic_path_consults_the_rule_not_the_hash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(m, "fetch_bars", lambda symbol, settings: _uptrend_bars())
        signal = await m.generate_signal(
            m.SignalGenerationRequest(symbol="NVDA", use_ai=False)
        )
        assert signal.model_version == "strategy-rule-v1", (
            "the observed market never reached the rule — the hash fallback answered"
        )

    @pytest.mark.asyncio
    async def test_an_unobservable_market_still_reaches_the_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The no-data fallback is documented continuity behaviour for the
        champion; it must remain reachable when the market genuinely cannot
        be observed — and only then."""

        def boom(symbol: str, settings) -> list:
            raise DataUnavailableError("feeder dead")

        monkeypatch.setattr(m, "fetch_bars", boom)
        signal = await m.generate_signal(
            m.SignalGenerationRequest(symbol="NVDA", use_ai=False)
        )
        assert signal.model_version == "strategy-rule-v1/no-data"
        assert signal.candidate_action.value == "HOLD"
        assert signal.confidence == 0.0
