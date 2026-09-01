from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx
from contracts import SentimentScore
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

from .config import settings

logger = logging.getLogger(__name__)

POSITIVE_WORDS = {"beat", "surge", "gain", "bullish", "growth", "record", "upside", "strong"}
NEGATIVE_WORDS = {"miss", "drop", "loss", "bearish", "fraud", "weak", "downside", "lawsuit"}
_cache: dict[str, SentimentScore] = {}
_cache_expiry: dict[str, datetime] = {}

app = FastAPI(title="sentiment-aggregator", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "sentiment-aggregator"}


@app.get("/v1/sentiment/{symbol}", response_model=SentimentScore)
async def get_sentiment(symbol: str) -> SentimentScore:
    return await _fetch_sentiment(symbol.upper())


@app.get("/v1/sentiment/batch")
async def get_batch(
    symbols: str = Query(description="Comma-separated symbols"),
) -> list[SentimentScore]:
    return [
        await _fetch_sentiment(symbol.strip().upper())
        for symbol in symbols.split(",")
        if symbol.strip()
    ]


def _archive_headlines(symbol: str, items: list[dict], source: str) -> None:
    """Write fetched headlines to the point-in-time archive, on fetch.

    Headlines used to flow straight into a score and vanish, so nothing could
    ask what was in the news about a symbol at a past moment — the news
    role's blocker. Provider timestamps are kept when parseable; the archive
    stamps its own observed_at either way. Best-effort: an unwritable archive
    never blocks scoring.
    """
    try:
        from journal import get_journal

        cleaned = []
        for item in items:
            published = item.get("published_at")
            if isinstance(published, str):
                try:
                    published = datetime.fromisoformat(published.replace("Z", "+00:00"))
                except ValueError:
                    published = None
            cleaned.append({"headline": item.get("headline"), "published_at": published})
        get_journal().record_headlines(symbol, cleaned, source=source)
    except Exception as exc:
        logger.warning("Headline archive write failed for %s: %s", symbol, exc)


async def _fetch_sentiment(symbol: str) -> SentimentScore:
    now = datetime.now(timezone.utc)
    if symbol in _cache and _cache_expiry[symbol] > now:
        return _cache[symbol]

    texts: list[str] = []
    sources: list[str] = []
    async with httpx.AsyncClient(timeout=5.0) as client:
        if settings.newsapi_key:
            try:
                response = await client.get(
                    "https://newsapi.org/v2/everything",
                    params={"q": symbol, "sortBy": "publishedAt", "pageSize": 5},
                    headers={"x-api-key": settings.newsapi_key},
                )
                articles = response.json().get("articles", [])
                for item in articles:
                    texts.append(
                        " ".join(filter(None, [item.get("title"), item.get("description")]))
                    )
                if response.status_code == 200:
                    sources.append("newsapi")
                    _archive_headlines(
                        symbol,
                        [
                            {"headline": a.get("title"), "published_at": a.get("publishedAt")}
                            for a in articles
                        ],
                        source="newsapi",
                    )
            except Exception:
                pass
        if settings.alphavantage_key:
            try:
                response = await client.get(
                    "https://www.alphavantage.co/query",
                    params={
                        "function": "NEWS_SENTIMENT",
                        "tickers": symbol,
                        "apikey": settings.alphavantage_key,
                    },
                )
                feed = response.json().get("feed", [])[:5]
                for item in feed:
                    texts.append(" ".join(filter(None, [item.get("title"), item.get("summary")])))
                if response.status_code == 200:
                    sources.append("alphavantage")
                    _archive_headlines(
                        symbol,
                        [{"headline": f.get("title")} for f in feed],
                        source="alphavantage",
                    )
            except Exception:
                pass
        if settings.etoro_api_key and settings.etoro_user_key:
            try:
                response = await client.get(
                    f"https://public-api.etoro.com/api/v1/feeds/instrument/{symbol}",
                    headers={
                        "x-api-key": settings.etoro_api_key,
                        "x-user-key": settings.etoro_user_key,
                    },
                )
                body = response.json()
                feed_items = body.get("items") or body.get("posts") or []
                for item in feed_items[:5]:
                    texts.append(str(item.get("message") or item.get("text") or ""))
                if response.status_code == 200:
                    sources.append("etoro_social")
            except Exception:
                pass

    score = _score_texts(texts)
    confidence = min(1.0, 0.25 + 0.15 * len(sources) + 0.05 * min(len(texts), 5))
    sentiment = SentimentScore(
        symbol=symbol,
        score=score,
        confidence=round(confidence, 2),
        sources_used=sources,
        cached_at=now,
    )
    _cache[symbol] = sentiment
    _cache_expiry[symbol] = now + timedelta(seconds=settings.cache_ttl_seconds)
    # Every computed score lands in the point-in-time archive. The TTL cache
    # above holds only the current answer — a new score overwrites it and
    # expiry deletes it — so before this line no past sentiment could be
    # recovered and the sentiment specialist role had nothing to read.
    # Best-effort: an unwritable archive must not block serving the score.
    try:
        from journal import get_journal

        get_journal().record_sentiment(
            symbol, score=score, confidence=confidence, sources=sources
        )
    except Exception as exc:
        logger.warning("Sentiment archive write failed for %s: %s", symbol, exc)
    return sentiment


def _score_texts(texts: list[str]) -> float:
    pos = 0
    neg = 0
    for text in texts:
        words = {word.strip(".,:;!?()[]{}\"'").lower() for word in text.split()}
        pos += len(words & POSITIVE_WORDS)
        neg += len(words & NEGATIVE_WORDS)
    total = pos + neg
    if total == 0:
        return 0.0
    return round(max(-1.0, min(1.0, (pos - neg) / total)), 3)
