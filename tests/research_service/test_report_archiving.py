"""Every freshly generated research report lands in the point-in-time archive.

The service's own table is a delete-and-insert TTL cache — its set() destroys
the previous answer by design — so the append-only journal archive is the only
place the sequence of answers survives, and the only honest input for the
fundamentals specialist.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock


def _report(symbol: str = "NVDA"):
    from contracts import ResearchReport

    return ResearchReport(
        symbol=symbol,
        generated_at=datetime.now(timezone.utc),
        sentiment="bullish",
        headline_summary="strong quarter",
        risk_factors=["valuation", "concentration"],
        confidence_modifier=0.1,
    )


def test_set_archives_the_report_before_overwriting_the_cache() -> None:
    from journal import get_journal
    from research_service.cache import ResearchCache

    ResearchCache().set(_report(), MagicMock())

    rows = get_journal().research_as_of("NVDA", datetime.now(timezone.utc))
    assert len(rows) == 1
    assert rows[0]["sentiment"] == "bullish"
    assert rows[0]["risk_factors"] == ["valuation", "concentration"]


def test_an_unwritable_archive_does_not_block_the_cache_write() -> None:
    from journal import Journal, reset_journal
    from research_service.cache import ResearchCache

    reset_journal(Journal(enabled=False))
    try:
        session = MagicMock()
        ResearchCache().set(_report(), session)
        assert session.add.called, "the serving cache still gets the report"
    finally:
        reset_journal(None)
