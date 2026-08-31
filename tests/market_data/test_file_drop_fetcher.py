"""The file-drop provider: bars and quotes from a directory a feeder writes.

Exists for environments where the process may not open a connection to a
market-data provider at all — an egress-restricted network, an airgapped rig, a
deterministic replay. The property that matters is fail-closed: a missing or
malformed file must look like an unreachable provider, never like a quiet
default, because everything downstream treats served data as observed market
truth and archives it as such.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from market_data.fetcher import DataUnavailableError, FileDropFetcher, get_fetcher


def _write_bars(directory, symbol="AAPL", n=5, timeframe="1d"):
    now = datetime.now(timezone.utc)
    bars = [
        {
            "timestamp": (now - timedelta(days=n - i)).isoformat(),
            "open": 100.0 + i, "high": 101.0 + i, "low": 99.0 + i,
            "close": 100.5 + i, "volume": 1000,
        }
        for i in range(n)
    ]
    (directory / f"{symbol}_{timeframe}.json").write_text(json.dumps({"bars": bars}))
    return bars


class TestSelection:
    def test_provider_file_selects_the_file_fetcher(self, tmp_path, monkeypatch) -> None:
        from market_data import MarketDataSettings

        monkeypatch.setenv("MARKET_DATA_PROVIDER", "file")
        monkeypatch.setenv("MARKET_DATA_FILE_DIR", str(tmp_path))
        assert isinstance(get_fetcher(MarketDataSettings()), FileDropFetcher)

    def test_a_missing_directory_setting_is_refused_not_defaulted(
        self, monkeypatch
    ) -> None:
        """A file provider pointed at an accidental directory serves nothing
        and looks like an outage."""
        from market_data import MarketDataSettings

        monkeypatch.setenv("MARKET_DATA_PROVIDER", "file")
        monkeypatch.delenv("MARKET_DATA_FILE_DIR", raising=False)
        with pytest.raises(DataUnavailableError, match="MARKET_DATA_FILE_DIR"):
            get_fetcher(MarketDataSettings())


class TestFailClosed:
    def test_a_missing_bar_file_is_a_data_outage(self, tmp_path) -> None:
        with pytest.raises(DataUnavailableError, match="feeder has not written"):
            FileDropFetcher(tmp_path).fetch("AAPL")

    def test_a_malformed_row_poisons_the_whole_file(self, tmp_path) -> None:
        """Silently dropping one row would serve a series with an invisible
        hole."""
        (tmp_path / "AAPL_1d.json").write_text(
            json.dumps({"bars": [
                {"timestamp": "2026-08-28T20:00:00+00:00", "open": 1, "high": 2,
                 "low": 0.5, "close": 1.5, "volume": 1},
                {"timestamp": "2026-08-29T20:00:00+00:00", "open": 1, "high": 2},
            ]})
        )
        with pytest.raises(DataUnavailableError, match="Malformed bar"):
            FileDropFetcher(tmp_path).fetch("AAPL")

    def test_an_empty_file_is_an_outage_not_an_empty_series(self, tmp_path) -> None:
        (tmp_path / "AAPL_1d.json").write_text(json.dumps({"bars": []}))
        with pytest.raises(DataUnavailableError, match="empty"):
            FileDropFetcher(tmp_path).fetch("AAPL")

    def test_an_undated_quote_is_not_served(self, tmp_path) -> None:
        """The price-age guard downstream can only reject what it can date, so
        serving an undated quote would disable it."""
        (tmp_path / "quotes.json").write_text(
            json.dumps({"quotes": {"AAPL": {"price": 314.58}}})
        )
        assert FileDropFetcher(tmp_path).latest_price("AAPL") is None

    def test_missing_quotes_fall_through_rather_than_raising(self, tmp_path) -> None:
        """None sends the realtime resolver to its last-bar tier, exactly as a
        provider with no trade endpoint does."""
        assert FileDropFetcher(tmp_path).latest_price("AAPL") is None


class TestServing:
    def test_bars_come_back_sorted_and_typed(self, tmp_path) -> None:
        _write_bars(tmp_path)
        bars = FileDropFetcher(tmp_path).fetch("AAPL", period_days=30)
        assert len(bars) == 5
        assert bars == sorted(bars, key=lambda b: b.timestamp)
        assert bars[-1].close == 104.5

    def test_a_quote_carries_the_feeders_timestamp(self, tmp_path) -> None:
        stamp = datetime.now(timezone.utc).isoformat()
        (tmp_path / "quotes.json").write_text(
            json.dumps({"quotes": {"AAPL": {"price": 314.58, "ts": stamp}}})
        )
        snapshot = FileDropFetcher(tmp_path).latest_price("AAPL")
        assert snapshot.price == 314.58
        assert snapshot.source == "file_quote"
        assert snapshot.timestamp.isoformat() == stamp

    def test_the_timestamp_spelling_is_also_accepted(self, tmp_path) -> None:
        """A feeder wrote {"timestamp": ...} where the layout says {"ts": ...},
        and the mismatch did not fail — it silently degraded every live price
        to the last bar close, a session old. Either spelling now serves;
        undated quotes are still refused (the test above this suite keeps
        that)."""
        (tmp_path / "quotes.json").write_text(
            json.dumps(
                {"quotes": {"AAPL": {
                    "price": 316.85, "timestamp": "2026-08-31T20:23:03Z",
                }}}
            )
        )
        snapshot = FileDropFetcher(tmp_path).latest_price("AAPL")
        assert snapshot is not None
        assert snapshot.price == 316.85
        assert snapshot.timestamp.tzinfo is not None

    @pytest.mark.real_price_source
    def test_the_price_resolver_reads_file_quotes_end_to_end(
        self, tmp_path, monkeypatch
    ) -> None:
        """The tier the paper broker fills from. Marked real_price_source to
        bypass the offline stub: the file provider never reaches the network,
        which is its entire point."""
        from market_data import MarketDataSettings, RealtimePriceSource

        monkeypatch.setenv("MARKET_DATA_PROVIDER", "file")
        monkeypatch.setenv("MARKET_DATA_FILE_DIR", str(tmp_path))
        _write_bars(tmp_path)
        (tmp_path / "quotes.json").write_text(
            json.dumps({"quotes": {"AAPL": {
                "price": 314.58, "ts": datetime.now(timezone.utc).isoformat(),
            }}})
        )
        assert RealtimePriceSource(MarketDataSettings()).get_price("AAPL") == 314.58
