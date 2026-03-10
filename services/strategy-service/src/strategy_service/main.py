"""Strategy service entrypoint."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from contracts import SignalCandidate
from fastapi import FastAPI
from pydantic import BaseModel


class SignalGenerationRequest(BaseModel):
    symbol: str = "AAPL"


def _build_signal(symbol: str) -> SignalCandidate:
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
        model_version="strategy-m1",
    )


app = FastAPI(title="strategy-service", version="0.1.0")


@app.post("/v1/signals/generate", response_model=SignalCandidate)
def generate_signal(request: SignalGenerationRequest) -> SignalCandidate:
    """Return a deterministic fake signal for the requested symbol."""

    return _build_signal(request.symbol)
