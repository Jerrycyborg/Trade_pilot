"""Versioned, content-addressed runtime prompt registry."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

_PROMPT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_LABEL = re.compile(r"[^a-z0-9-]+")
_MAX_PROMPT_BYTES = 64 * 1024


@dataclass(frozen=True)
class PromptArtifact:
    prompt_id: str
    content: str
    sha256: str


def _registry_root() -> Path:
    configured = os.environ.get("PROMPT_REGISTRY_PATH", "config/prompts")
    root = Path(configured).expanduser().resolve()
    if not root.is_dir():
        raise RuntimeError(f"Prompt registry does not exist: {root}")
    return root


def load_prompt(prompt_id: str, expected_sha256: str = "") -> PromptArtifact:
    """Load a named immutable prompt and optionally enforce its pinned digest."""
    if not _PROMPT_ID.fullmatch(prompt_id):
        raise ValueError("Invalid prompt id")
    root = _registry_root()
    path = (root / f"{prompt_id}.txt").resolve()
    if path.parent != root:
        raise ValueError("Prompt path escapes registry")
    raw = path.read_bytes()
    if not raw or len(raw) > _MAX_PROMPT_BYTES:
        raise RuntimeError("Prompt is empty or exceeds the size limit")
    digest = hashlib.sha256(raw).hexdigest()
    if expected_sha256 and not hmac.compare_digest(digest, expected_sha256.lower()):
        raise RuntimeError(
            f"Prompt digest mismatch for {prompt_id}: expected {expected_sha256}, got {digest}"
        )
    return PromptArtifact(
        prompt_id=prompt_id,
        content=raw.decode("utf-8"),
        sha256=digest,
    )


def untrusted_block(label: str, value: object, *, max_chars: int = 8_000) -> str:
    """Serialize external data so it cannot forge prompt control delimiters."""
    safe_label = _LABEL.sub("-", label.strip().lower()).strip("-") or "data"
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    serialized = serialized.replace("<", "\\u003c").replace(">", "\\u003e")
    if len(serialized) > max_chars:
        serialized = serialized[:max_chars] + "...[truncated]"
    return (
        f"<untrusted-{safe_label}>\n"
        f"{serialized}\n"
        f"</untrusted-{safe_label}>"
    )
