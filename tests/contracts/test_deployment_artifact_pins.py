from __future__ import annotations

import hashlib
from pathlib import Path

import yaml


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_compose_pins_deployed_prompt_and_strategy_artifacts() -> None:
    root = Path(__file__).resolve().parents[2]
    compose = yaml.safe_load((root / "docker-compose.yml").read_text(encoding="utf-8"))
    environment = compose["x-common-env"]

    assert environment["RESEARCH_PROMPT_SHA256"] == _sha256(
        root / "config" / "prompts" / "research-system-v1.txt"
    )
    assert environment["STRATEGY_PROMPT_SHA256"] == _sha256(
        root / "config" / "prompts" / "strategy-system-v1.txt"
    )
    assert environment["LEARNING_STRATEGY_SHA256"] == _sha256(
        root / "config" / "strategies" / "ema_rsi_macd--v1.json"
    )
