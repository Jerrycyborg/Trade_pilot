import asyncio
from pathlib import Path


def test_orchestrator_status_endpoint(tmp_path: Path) -> None:
    config_file = tmp_path / "policy.yaml"
    config_file.write_text(
        "weekly_notional_cap_usd: 500\nkill_switch: false\n"
        "trading_mode: demo\nsymbol_allowlist: [AAPL]\n",
        encoding="utf-8",
    )

    import autonomy_orchestrator.config as config
    import autonomy_orchestrator.main as main

    config.settings = config.OrchestratorSettings(
        orchestrator_interval_minutes=60,
        policy_config_path=config_file,
    )
    main.settings = config.settings
    main.state.weekly_notional_used = 123.45

    body = main.status()
    assert body["weekly_notional_used"] == 123.45
    assert body["trading_mode"] == "demo"


def test_orchestrator_validation_endpoint(tmp_path: Path, monkeypatch) -> None:
    config_file = tmp_path / "policy.yaml"
    config_file.write_text(
        "weekly_notional_cap_usd: 500\nkill_switch: false\n"
        "trading_mode: demo\nsymbol_allowlist: [AAPL, BAD]\n",
        encoding="utf-8",
    )

    import autonomy_orchestrator.config as config
    import autonomy_orchestrator.main as main

    config.settings = config.OrchestratorSettings(
        orchestrator_interval_minutes=60,
        policy_config_path=config_file,
    )
    main.settings = config.settings

    audited = {}

    async def fake_validate() -> dict[str, list[str]]:
        return {"valid": ["AAPL"], "invalid": ["BAD"], "unknown": []}

    async def fake_audit(event) -> None:
        audited["event_type"] = event.event_type
        audited["metadata"] = event.metadata

    monkeypatch.setattr(main, "_validate_allowlist_symbols", fake_validate)
    monkeypatch.setattr(main, "_audit", fake_audit)

    response = asyncio.run(main.validate_allowlist())

    assert response == {"valid": ["AAPL"], "invalid": ["BAD"], "unknown": []}
    assert audited["event_type"] == "orchestrator.validation"
    assert audited["metadata"]["invalid"] == ["BAD"]
