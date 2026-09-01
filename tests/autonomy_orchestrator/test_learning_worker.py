from __future__ import annotations

import json
from pathlib import Path

import pytest
from autonomy_orchestrator.learning_worker import load_strategy_artifact


def _write_artifact(root: Path, **overrides) -> None:
    payload = {
        "strategy_id": "ema_rsi_macd",
        "strategy_version": "v1",
        "timeframe": "1d",
        "parameters": {
            "ema_fast": 20,
            "ema_slow": 50,
            "rsi_buy_min": 45.0,
            "rsi_buy_max": 70.0,
            "macd_hist_min": 0.0,
        },
        **overrides,
    }
    (root / "ema_rsi_macd--v1.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )


def test_strategy_artifact_is_content_addressed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_artifact(tmp_path)
    monkeypatch.setenv("LEARNING_STRATEGY_REGISTRY_PATH", str(tmp_path))
    artifact = load_strategy_artifact("ema_rsi_macd", "v1")
    assert len(artifact.sha256) == 64
    assert artifact.parameters["ema_fast"] == 20


def test_strategy_artifact_rejects_digest_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_artifact(tmp_path)
    monkeypatch.setenv("LEARNING_STRATEGY_REGISTRY_PATH", str(tmp_path))
    monkeypatch.setenv("LEARNING_STRATEGY_SHA256", "0" * 64)
    with pytest.raises(ValueError, match="digest mismatch"):
        load_strategy_artifact("ema_rsi_macd", "v1")


@pytest.mark.parametrize(
    ("strategy_id", "version"),
    [
        ("../escape", "v1"),
        ("ema_rsi_macd", "../../v1"),
        ("bad/name", "v1"),
    ],
)
def test_strategy_artifact_rejects_path_escape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    strategy_id: str,
    version: str,
) -> None:
    monkeypatch.setenv("LEARNING_STRATEGY_REGISTRY_PATH", str(tmp_path))
    with pytest.raises(ValueError, match="unsafe"):
        load_strategy_artifact(strategy_id, version)
