"""AI researcher using Claude with web search to analyze trading symbols."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

from contracts import ResearchReport

from .config import settings

logger = logging.getLogger(__name__)

_NEUTRAL_STUB = {
    "sentiment": "neutral",
    "headline_summary": "Research unavailable — using neutral assumptions.",
    "risk_factors": [],
    "macro_context": "",
    "confidence_modifier": 0.0,
}

_SYSTEM_PROMPT = """\
You are a financial research analyst. Your job is to provide a concise, structured \
research report on a given stock, ETF, crypto, or forex symbol.

Always respond with ONLY a valid JSON object — no markdown, no explanation, just JSON.

The JSON must have exactly these fields:
{
  "sentiment": "bullish" | "bearish" | "neutral",
  "headline_summary": "<2-3 sentence summary of the most important recent news>",
  "risk_factors": ["<risk 1>", "<risk 2>", ...],
  "macro_context": "<1-2 sentences on macro environment affecting this symbol>",
  "confidence_modifier": <float between -0.2 and 0.2>
}

confidence_modifier rules:
  +0.1 to +0.2: strong bullish signals, positive catalysts, low uncertainty
   0.0: neutral, mixed signals, or insufficient data
  -0.1 to -0.2: bearish signals, negative catalysts, high uncertainty or risk
"""


class AIResearcher:
    """Runs web-search-backed research on symbols using Claude."""

    def __init__(self) -> None:
        if not settings.anthropic_api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. research-service requires an Anthropic API key."
            )

    async def research(self, symbol: str) -> ResearchReport:
        """Fetch research for a symbol. Returns neutral stub on any failure."""
        try:
            return await self._do_research(symbol)
        except Exception as exc:
            logger.error("AIResearcher.research failed for %s: %s", symbol, exc)
            return _build_stub(symbol)

    async def _do_research(self, symbol: str) -> ResearchReport:
        import anthropic

        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

        user_message = (
            f"Research the trading symbol: {symbol}\n\n"
            "Use web search to find:\n"
            "1. Recent news headlines from the past 48 hours\n"
            "2. Any upcoming earnings, product launches, or regulatory events\n"
            "3. Macro environment factors affecting this symbol\n"
            "4. Key risk factors traders should know\n\n"
            "Return ONLY a JSON object with the required fields."
        )

        response = await client.messages.create(
            model=settings.claude_model,
            max_tokens=1024,
            system=_SYSTEM_PROMPT,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{"role": "user", "content": user_message}],
        )

        # Extract text from response (may have tool use blocks before final text)
        text = _extract_text(response)
        data = _parse_json(text)

        return ResearchReport(
            symbol=symbol.upper(),
            generated_at=datetime.now(timezone.utc),
            sentiment=data.get("sentiment", "neutral"),
            headline_summary=data.get("headline_summary", ""),
            risk_factors=data.get("risk_factors", []),
            macro_context=data.get("macro_context", ""),
            confidence_modifier=float(data.get("confidence_modifier", 0.0)),
            cached=False,
        )


def _extract_text(response) -> str:
    """Pull the last text block from a Claude response (after any tool use)."""
    text_parts: list[str] = []
    for block in response.content:
        if hasattr(block, "text"):
            text_parts.append(block.text)
    return "\n".join(text_parts).strip()


def _parse_json(text: str) -> dict:
    """Extract and parse JSON from Claude's response text."""
    # Try direct parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try to extract JSON block from markdown or surrounding text
    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    logger.warning("Failed to parse JSON from Claude response: %r", text[:200])
    return _NEUTRAL_STUB.copy()


def _build_stub(symbol: str) -> ResearchReport:
    return ResearchReport(
        symbol=symbol.upper(),
        generated_at=datetime.now(timezone.utc),
        sentiment="neutral",
        headline_summary="Research unavailable — using neutral assumptions.",
        risk_factors=[],
        macro_context="",
        confidence_modifier=0.0,
        cached=False,
    )
