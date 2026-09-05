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


def test_ci_uses_least_privilege_and_a_pinned_toolchain() -> None:
    root = Path(__file__).resolve().parents[2]
    workflow = yaml.safe_load(
        (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    )

    assert workflow["permissions"] == {"contents": "read"}

    steps = [
        step
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if "uses" in step
    ]
    checkout_steps = [
        step for step in steps if step["uses"].startswith("actions/checkout@")
    ]
    setup_uv_steps = [
        step for step in steps if step["uses"].startswith("astral-sh/setup-uv@")
    ]

    assert len(checkout_steps) == 3
    assert all(step.get("with", {}).get("persist-credentials") is False for step in checkout_steps)

    assert len(setup_uv_steps) == 2
    for step in setup_uv_steps:
        options = step.get("with", {})
        assert options["version"] == "0.12.4"
        assert options["python-version"] == "3.11"
        assert options["enable-cache"] is True
        assert options["prune-cache"] is True
