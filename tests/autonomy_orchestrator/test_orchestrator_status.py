from pathlib import Path

def test_orchestrator_status_endpoint(tmp_path: Path) -> None:
    config_file = tmp_path / "policy.yaml"
    config_file.write_text(
        "weekly_notional_cap_usd: 500\nkill_switch: false\ntrading_mode: demo\nsymbol_allowlist: [AAPL]\n",
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
