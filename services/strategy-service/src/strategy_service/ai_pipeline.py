"""AI-driven signal generation pipeline using Claude + market data + research."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

import httpx

from contracts import ResearchReport, SentimentScore, SignalCandidate, TechnicalSummaryContract
from market_data import MarketDataSettings, build_ta_summary, get_fetcher
from market_data.fetcher import DataUnavailableError

from .config import settings
from .rule_engine import evaluate_rules

logger = logging.getLogger(__name__)

_SIZE_BY_RISK = {"LOW": 0.02, "MEDIUM": 0.015, "HIGH": 0.005}

_SYSTEM_PROMPT = """\
You are a quantitative technical analyst. Based on the provided technical indicators \
and research context, decide whether to BUY, SELL, or HOLD the given symbol.

Respond with ONLY a valid JSON object — no markdown, no explanation, just JSON:
{
  "action": "BUY" | "SELL" | "HOLD",
  "confidence": <float 0.0-0.95>,
  "risk_score": "LOW" | "MEDIUM" | "HIGH",
  "reasoning": "<1-2 sentences>"
}

Guidelines:
- confidence: how confident you are in the signal (0.60+ to trade, below 0.60 means HOLD)
- risk_score LOW: clear trend, low volatility, strong indicator alignment
- risk_score MEDIUM: mixed signals or moderate uncertainty
- risk_score HIGH: conflicting signals, high volatility, or concerning news
- When uncertain, prefer HOLD over LOW confidence BUY/SELL
"""


class AISignalPipeline:
    """Generates trading signals using Claude for TA analysis + research integration."""

    def __init__(self) -> None:
        self._market_settings = MarketDataSettings()

    async def generate(self, symbol: str) -> SignalCandidate:
        """Generate an AI-driven signal for a symbol. Falls back to deterministic on failure."""
        try:
            return await self._do_generate(symbol)
        except Exception as exc:
            logger.error("AISignalPipeline.generate failed for %s: %s", symbol, exc)
            if settings.fallback_to_deterministic:
                logger.info("Falling back to deterministic signal for %s", symbol)
                ta = None
                bars = None
                try:
                    fetcher = get_fetcher(self._market_settings)
                    bars = fetcher.fetch(symbol, period_days=self._market_settings.default_lookback_days)
                    if bars:
                        from market_data import build_ta_summary
                        ta = build_ta_summary(symbol, bars)
                except Exception:
                    pass
                # Best-effort sentiment fetch for fallback path
                fallback_sentiment_score: float | None = None
                try:
                    import asyncio
                    from .config import settings as _s
                    import httpx
                    resp = httpx.get(f"{_s.sentiment_service_url}/v1/sentiment/{symbol.upper()}", timeout=2.0)
                    if resp.status_code == 200:
                        fallback_sentiment_score = resp.json().get("score")
                except Exception:
                    pass
                return _build_deterministic_signal(symbol, ta, sentiment_score=fallback_sentiment_score, bars=bars)
            raise

    async def _do_generate(self, symbol: str) -> SignalCandidate:
        # 1. Fetch market data + compute TA
        ta_summary, bars = await self._get_ta_summary(symbol)

        # 2. Fetch research report (best-effort, 5s timeout)
        research = await self._get_research(symbol)
        sentiment = await self._get_sentiment(symbol)

        # 2b. Pre-Claude deterministic check: if rule signal is high-confidence, skip Claude
        if ta_summary is not None and getattr(settings, "prefer_deterministic", False):
            rule_config = {"sentiment_block_threshold": settings.sentiment_block_threshold}
            rule_signal = evaluate_rules(
                ta_summary,
                config=rule_config,
                sentiment_score=sentiment.score if sentiment else None,
                bars=bars,
            )
            if rule_signal.confidence >= 0.75:
                ta_contract_pre: Optional[TechnicalSummaryContract] = None
                if ta_summary:
                    ind_pre = ta_summary.indicators
                    ta_contract_pre = TechnicalSummaryContract(
                        symbol=symbol.upper(),
                        trend_direction=ta_summary.trend_direction,
                        signal_tags=ta_summary.signal_tags,
                        rsi_14=ind_pre.rsi_14,
                        macd_histogram=ind_pre.macd_histogram,
                        bb_position=ind_pre.bb_position,
                        data_source=ta_summary.data_source,
                        as_of=ta_summary.as_of,
                    )
                return SignalCandidate(
                    signal_id=str(uuid4()),
                    symbol=symbol.upper(),
                    ts=datetime.now(timezone.utc),
                    candidate_action=rule_signal.action,
                    confidence=rule_signal.confidence,
                    size_pct=rule_signal.size_pct,
                    model_version="strategy-rule-v1",
                    risk_score=rule_signal.risk_score,
                    ta_summary=ta_contract_pre,
                )

        # 3. Build Claude prompt and get signal
        ai_result = await self._call_claude(symbol, ta_summary, research)

        action = ai_result.get("action", "HOLD")
        raw_confidence = float(ai_result.get("confidence", 0.6))
        risk_score = ai_result.get("risk_score", "MEDIUM")

        # Apply research confidence modifier
        modifier = research.confidence_modifier if research else 0.0
        technical_confidence = max(0.0, min(0.95, raw_confidence + modifier))
        confidence = technical_confidence
        if sentiment:
            sentiment_component = max(0.0, min(1.0, (sentiment.score + 1.0) / 2.0))
            weight = max(0.0, min(1.0, settings.sentiment_weight))
            confidence = max(
                0.0,
                min(0.95, ((1.0 - weight) * technical_confidence) + (weight * sentiment_component)),
            )

        size_pct = _SIZE_BY_RISK.get(risk_score, 0.015)

        # Build TA contract for persistence
        ta_contract: Optional[TechnicalSummaryContract] = None
        if ta_summary:
            ta_contract = TechnicalSummaryContract(
                symbol=symbol.upper(),
                trend_direction=ta_summary.trend_direction,
                signal_tags=ta_summary.signal_tags,
                rsi_14=ta_summary.indicators.rsi_14,
                macd_histogram=ta_summary.indicators.macd_histogram,
                bb_position=ta_summary.indicators.bb_position,
                data_source=ta_summary.data_source,
                as_of=ta_summary.as_of,
            )

        research_summary = (
            research.headline_summary[:200] if research and research.headline_summary else None
        )

        return SignalCandidate(
            signal_id=str(uuid4()),
            symbol=symbol.upper(),
            ts=datetime.now(timezone.utc),
            candidate_action=action,
            confidence=round(confidence, 4),
            size_pct=size_pct,
            model_version=f"strategy-ai-v1/{settings.claude_model}",
            risk_score=risk_score,
            ta_summary=ta_contract,
            research_summary=research_summary,
        )

    async def _get_ta_summary(self, symbol: str):
        try:
            fetcher = get_fetcher(self._market_settings)
            bars = fetcher.fetch(symbol, period_days=self._market_settings.default_lookback_days)
            if not bars:
                logger.warning("No market data bars returned for %s", symbol)
                return None, None
            return build_ta_summary(symbol, bars, data_source=type(fetcher).__name__), bars
        except DataUnavailableError as exc:
            logger.warning("Market data unavailable for %s: %s", symbol, exc)
            return None, None
        except Exception as exc:
            logger.error("TA summary error for %s: %s", symbol, exc)
            return None, None

    async def _get_research(self, symbol: str) -> Optional[ResearchReport]:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    f"{settings.research_service_url}/v1/research/report/{symbol.upper()}"
                )
                if resp.status_code == 200:
                    return ResearchReport.model_validate(resp.json())
        except Exception as exc:
            logger.debug("Research fetch failed for %s: %s", symbol, exc)
        return None

    async def _call_claude(self, symbol: str, ta_summary, research) -> dict:
        import anthropic

        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

        # Build context block
        context_parts: list[str] = [f"Symbol: {symbol}"]

        if ta_summary:
            ind = ta_summary.indicators
            context_parts.append(
                f"\nTechnical Analysis:\n"
                f"  Trend: {ta_summary.trend_direction}\n"
                f"  Signal tags: {', '.join(ta_summary.signal_tags) or 'none'}\n"
                f"  RSI(14): {ind.rsi_14:.1f}\n"
                f"  MACD histogram: {ind.macd_histogram:.6f}\n"
                f"  Bollinger position (0=lower,1=upper): {ind.bb_position:.2f}\n"
                f"  EMA20: {ind.ema_20:.2f}, EMA50: {ind.ema_50:.2f}\n"
                f"  Current price: {ta_summary.current_price}"
            )
        else:
            context_parts.append("\nTechnical Analysis: Unavailable")

        if research:
            context_parts.append(
                f"\nResearch Context:\n"
                f"  Sentiment: {research.sentiment}\n"
                f"  Headlines: {research.headline_summary}\n"
                f"  Risk factors: {'; '.join(research.risk_factors[:3]) or 'none'}\n"
                f"  Macro: {research.macro_context}"
            )
        else:
            context_parts.append("\nResearch Context: Unavailable")

        user_message = "\n".join(context_parts)

        response = await client.messages.create(
            model=settings.claude_model,
            max_tokens=256,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )

        text = ""
        for block in response.content:
            if hasattr(block, "text"):
                text += block.text

        return _parse_json(text)

    async def _get_sentiment(self, symbol: str) -> Optional[SentimentScore]:
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(
                    f"{settings.sentiment_service_url}/v1/sentiment/{symbol.upper()}"
                )
                if resp.status_code == 200:
                    return SentimentScore.model_validate(resp.json())
        except httpx.HTTPError:
            logger.warning("sentiment service unreachable, using sentiment_weight=0")
            return SentimentScore(symbol=symbol.upper(), score=0.0, confidence=0.0)
        except Exception:
            logger.warning("sentiment service unreachable, using sentiment_weight=0")
            return SentimentScore(symbol=symbol.upper(), score=0.0, confidence=0.0)
        return SentimentScore(symbol=symbol.upper(), score=0.0, confidence=0.0)


def _parse_json(text: str) -> dict:
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    logger.warning("Failed to parse AI signal JSON: %r", text[:200])
    return {"action": "HOLD", "confidence": 0.5, "risk_score": "MEDIUM", "reasoning": "parse error"}


def _build_deterministic_signal(
    symbol: str,
    ta_summary=None,
    sentiment_score: float | None = None,
    bars: list | None = None,
) -> SignalCandidate:
    """Deterministic rule-based signal using EMA/RSI/MACD strategy."""
    if ta_summary is not None:
        rule_signal = evaluate_rules(
            ta_summary,
            config={"sentiment_block_threshold": settings.sentiment_block_threshold},
            sentiment_score=sentiment_score,
            bars=bars,
        )
        return SignalCandidate(
            signal_id=str(uuid4()),
            symbol=symbol.upper(),
            ts=datetime.now(timezone.utc),
            candidate_action=rule_signal.action,
            confidence=rule_signal.confidence,
            size_pct=rule_signal.size_pct,
            model_version="strategy-rule-v1",
            risk_score=rule_signal.risk_score,
        )
    # Fallback: hash-based when no TA data available
    basis = sum(ord(char) for char in symbol.upper())
    action = "BUY" if basis % 2 == 0 else "SELL"
    confidence = round(0.6 + (basis % 20) / 100, 2)
    size_pct = round(0.01 + (basis % 2) * 0.005, 3)
    return SignalCandidate(
        signal_id=str(uuid4()),
        symbol=symbol.upper(),
        ts=datetime.now(timezone.utc),
        candidate_action=action,
        confidence=min(confidence, 0.95),
        size_pct=size_pct,
        model_version="strategy-m1-deterministic",
        risk_score="MEDIUM",
    )
