"""Structured logging helpers."""

from __future__ import annotations

import json
import logging


logger = logging.getLogger("policy_service")


def log_event(event: str, **fields: object) -> None:
    """Emit a compact JSON log line."""

    logger.info(json.dumps({"event": event, **fields}, sort_keys=True))
