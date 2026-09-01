"""AI researcher using Claude with web search to analyze trading symbols."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

from contracts import ResearchReport, load_prompt, untrusted_block

from .config import settings

logger = logging.getLogger(__name__)

_NEUTRAL_STUB = {
    "sentiment": "neutral",
    "headline_summary": "Research unavailable — using neutral assumptions.",
    "risk_factors": [],
    "macro_context": "",
    "confidence_modifier": 0.0,
}


class AIResearcher:
    """Runs web-search-backed research on symbols using Claude."""

    def __init__(self) -> None:
        if not settings.anthropic_api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. research-service requires an Anthropic API key."
            )
        self._prompt = load_prompt(settings.prompt_id, settings.prompt_sha256)

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

        user_message = untrusted_block("requested-symbol", symbol, max_chars=64)


        response = await client.messages.create(
            model=settings.claude_model,
            max_tokens=1024,
            system=self._prompt.content,
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
