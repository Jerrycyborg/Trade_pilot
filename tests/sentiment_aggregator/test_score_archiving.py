"""Every computed sentiment score lands in the point-in-time archive.

The aggregator's TTL cache holds only the current answer — a new score
overwrites it and expiry deletes it — so before this write no past sentiment
could be recovered and the sentiment specialist role had nothing honest to
read. The write is best-effort: an unwritable archive must not block serving
the score, and says so at WARNING rather than silently.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest


@pytest.mark.asyncio
async def test_a_computed_score_is_archived_with_its_moment(monkeypatch) -> None:
    from journal import get_journal
    from sentiment_aggregator import main as m

    # All upstream text sources are per-source guarded; offline they
    # contribute nothing and the score computes from an empty corpus.
    m._cache.clear()
    m._cache_expiry.clear()
    result = await m._fetch_sentiment("NVDA")

    rows = get_journal().sentiment_as_of("NVDA", datetime.now(timezone.utc))
    assert len(rows) == 1
    assert rows[0]["score"] == result.score
    assert rows[0]["observed_at"].tzinfo is not None


@pytest.mark.asyncio
async def test_a_cache_hit_is_not_re_archived(monkeypatch) -> None:
    """The archive records computations, not reads: serving the cached score
    twice a minute must not fabricate a denser observation series."""
    from journal import get_journal
    from sentiment_aggregator import main as m

    m._cache.clear()
    m._cache_expiry.clear()
    await m._fetch_sentiment("NVDA")
    await m._fetch_sentiment("NVDA")  # served from the TTL cache

    rows = get_journal().sentiment_as_of("NVDA", datetime.now(timezone.utc))
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_an_unwritable_archive_does_not_block_the_score(monkeypatch) -> None:
    from journal import Journal, reset_journal
    from sentiment_aggregator import main as m

    reset_journal(Journal(enabled=False))
    try:
        m._cache.clear()
        m._cache_expiry.clear()
        result = await m._fetch_sentiment("NVDA")
        assert result.symbol == "NVDA"
    finally:
        reset_journal(None)


def test_fetched_headlines_are_archived_with_provider_stamps() -> None:
    """_archive_headlines is the write-on-fetch seam for the news archive:
    provider timestamps are kept when parseable, garbage stamps degrade to
    None rather than dropping the headline."""
    from datetime import datetime, timezone

    from journal import get_journal
    from sentiment_aggregator.main import _archive_headlines

    _archive_headlines(
        "NVDA",
        [
            {"headline": "NVDA beats estimates", "published_at": "2026-08-31T14:00:00Z"},
            {"headline": "NVDA raises guidance", "published_at": "not a date"},
        ],
        source="newsapi",
    )
    rows = get_journal().headlines_as_of("NVDA", datetime.now(timezone.utc))
    assert [r["headline"] for r in rows] == [
        "NVDA beats estimates", "NVDA raises guidance",
    ]
    assert rows[0]["published_at"] is not None
    assert rows[1]["published_at"] is None
