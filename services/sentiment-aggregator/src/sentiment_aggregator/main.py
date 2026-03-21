from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
from contracts import SentimentScore
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

from .config import settings

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
async def get_batch(symbols: str = Query(description="Comma-separated symbols")) -> list[SentimentScore]:
    return [await _fetch_sentiment(symbol.strip().upper()) for symbol in symbols.split(",") if symbol.strip()]


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
                for item in response.json().get("articles", []):
                    texts.append(" ".join(filter(None, [item.get("title"), item.get("description")])))
                if response.status_code == 200:
                    sources.append("newsapi")
            except Exception:
                pass
        if settings.alphavantage_key:
            try:
                response = await client.get(
                    "https://www.alphavantage.co/query",
                    params={"function": "NEWS_SENTIMENT", "tickers": symbol, "apikey": settings.alphavantage_key},
                )
                for item in response.json().get("feed", [])[:5]:
                    texts.append(" ".join(filter(None, [item.get("title"), item.get("summary")])))
                if response.status_code == 200:
                    sources.append("alphavantage")
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
