from __future__ import annotations

from collections import deque

import httpx
from contracts import NotificationEvent
from contracts.auth import verify_internal_key
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings

app = FastAPI(title="notification-service", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

_history: deque[dict[str, object]] = deque(maxlen=50)
_pending_approvals: deque[dict[str, object]] = deque(maxlen=50)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "notification-service"}


@app.post("/v1/notify")
async def notify(event: NotificationEvent, _: None = Depends(verify_internal_key)) -> dict[str, object]:
    payload = event.model_dump(mode="json")
    _history.appendleft(payload)
    if event.tier >= 2:
        _pending_approvals.appendleft(payload)
    if settings.webhook_url:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(settings.webhook_url, json=payload)
        except Exception:
            payload["webhook_status"] = "failed"
    return {"status": "queued", "tier": event.tier}


@app.get("/v1/notify/history")
def history() -> list[dict[str, object]]:
    return list(_history)


@app.get("/v1/notify/pending")
def pending() -> list[dict[str, object]]:
    return list(_pending_approvals)
