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
        bars = _uptrend_bars()
        monkeypatch.setattr(m, "fetch_bars", lambda symbol, settings: bars)
        signal = await m.generate_signal(m.SignalGenerationRequest(symbol="NVDA", use_ai=False))
        assert signal.model_version == "strategy-rule-v1", (
            "the observed market never reached the rule — the hash fallback answered"
        )
        assert signal.ta_summary is not None
        assert signal.ta_summary.as_of == bars[-1].timestamp

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
        signal = await m.generate_signal(m.SignalGenerationRequest(symbol="NVDA", use_ai=False))
        assert signal.model_version == "strategy-rule-v1/no-data"
        assert signal.candidate_action.value == "HOLD"
        assert signal.confidence == 0.0


class TestPaperChallengerSignals:
    def test_registered_challenger_keeps_identity_and_market_timestamp(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        bars = _uptrend_bars()
        from market_data import build_ta_summary

        ta = build_ta_summary("AAPL", bars, data_source="test")
        champion = m._build_deterministic_signal("AAPL", ta_summary=ta, bars=bars)

        class Lifecycle:
            def paper_challengers(self, symbol):
                assert symbol == "AAPL"
                return [
                    SimpleNamespace(
                        strategy_id="ema_rsi_macd@chal-0123456789ab",
                        strategy_version="chal-0123456789ab",
                    )
                ]

            def challenger_parameters(self, challenger_id):
                assert challenger_id == "chal-0123456789ab"
                return {
                    "ema_fast": 20.0,
                    "ema_slow": 50.0,
                    "rsi_buy_min": 45.0,
                    "rsi_buy_max": 70.0,
                    "macd_hist_min": 0.0,
                }

        monkeypatch.setattr(
            "lifecycle.service.get_lifecycle_service",
            lambda: Lifecycle(),
        )
        signals = m._paper_challenger_signals(
            "AAPL",
            ta,
            bars,
            ta_contract=champion.ta_summary,
        )

        assert len(signals) == 1
        assert signals[0].strategy == "ema_rsi_macd@chal-0123456789ab"
        assert signals[0].model_version == "challenger:chal-0123456789ab"
        assert signals[0].ta_summary is not None
        assert signals[0].ta_summary.as_of == bars[-1].timestamp

    def test_persisted_challenger_identity_is_restored_without_schema_change(
        self,
    ) -> None:
        row = SimpleNamespace(
            signal_id="sig",
            symbol="AAPL",
            ts=datetime.now(timezone.utc),
            candidate_action="BUY",
            confidence=0.8,
            size_pct=0.01,
            horizon="intraday",
            source="strategy-service",
            model_version="challenger:chal-0123456789ab",
            risk_score="LOW",
            ta_summary_json=None,
            research_summary=None,
            acted_on=False,
        )

        signal = m._to_candidate(row)

        assert signal.strategy == "ema_rsi_macd@chal-0123456789ab"


class TestSignalQueueOrdering:
    def test_oldest_entry_signals_are_returned_without_exit_rows(self) -> None:
        symbol = "QUEUEPROBE"
        now = datetime.now(timezone.utc)
        with m.SessionLocal() as session:
            session.query(m.SignalRecord).filter(m.SignalRecord.symbol == symbol).delete(
                synchronize_session=False
            )
            session.add_all(
                [
                    m.SignalRecord(
                        signal_id="queue-old-entry",
                        symbol=symbol,
                        ts=now - timedelta(minutes=3),
                        candidate_action="BUY",
                        confidence=0.7,
                        size_pct=0.01,
                        horizon="intraday",
                        source="test",
                        model_version="test",
                    ),
                    m.SignalRecord(
                        signal_id="queue-exit",
                        symbol=symbol,
                        ts=now - timedelta(minutes=2),
                        candidate_action="EXIT",
                        confidence=1.0,
                        size_pct=0.0,
                        horizon="intraday",
                        source="test",
                        model_version="test",
                    ),
                    m.SignalRecord(
                        signal_id="queue-new-entry",
                        symbol=symbol,
                        ts=now - timedelta(minutes=1),
                        candidate_action="SELL",
                        confidence=0.7,
                        size_pct=0.01,
                        horizon="intraday",
                        source="test",
                        model_version="test",
                    ),
                ]
            )
            session.commit()

        rows = m.list_signals(
            limit=100,
            symbol=symbol,
            acted_on=False,
            candidate_action=None,
            entry_only=True,
            oldest_first=True,
        )

        assert [row.signal_id for row in rows] == [
            "queue-old-entry",
            "queue-new-entry",
        ]
