import asyncio
from pathlib import Path


def _main(tmp_path: Path):
    db_file = tmp_path / "strategy.db"
    import strategy_service.config as config
    import strategy_service.database as database
    import strategy_service.main as main

    config.settings = config.StrategySettings(database_url=f"sqlite+pysqlite:///{db_file}")
    database.settings = config.settings
    database.connect_args = {"check_same_thread": False}
    database.engine = database.create_engine(
        config.settings.database_url, future=True, connect_args=database.connect_args
    )
    database.SessionLocal.configure(bind=database.engine)
    database.Base.metadata.create_all(bind=database.engine)
    main.engine = database.engine
    main.SessionLocal = database.SessionLocal
    return main


def test_generate_signal_returns_valid_candidate(tmp_path: Path) -> None:
    main = _main(tmp_path)
    body = asyncio.run(main.generate_signal(main.SignalGenerationRequest(symbol="AAPL"))).model_dump(mode="json")
    assert body["symbol"] == "AAPL"
    assert body["candidate_action"] in {"BUY", "SELL"}
    assert 0.0 <= body["confidence"] <= 1.0


def test_generate_signal_uses_unique_signal_ids(tmp_path: Path) -> None:
    main = _main(tmp_path)
    first = asyncio.run(main.generate_signal(main.SignalGenerationRequest(symbol="AAPL"))).model_dump(mode="json")
    second = asyncio.run(main.generate_signal(main.SignalGenerationRequest(symbol="AAPL"))).model_dump(mode="json")
    assert first["signal_id"] != second["signal_id"]
    assert first["candidate_action"] == second["candidate_action"]
    assert first["confidence"] == second["confidence"]


def test_list_signals_returns_newest_first_and_filters_symbol(tmp_path: Path) -> None:
    main = _main(tmp_path)
    first = asyncio.run(main.generate_signal(main.SignalGenerationRequest(symbol="AAPL"))).model_dump(mode="json")
    second = asyncio.run(main.generate_signal(main.SignalGenerationRequest(symbol="MSFT"))).model_dump(mode="json")
    third = asyncio.run(main.generate_signal(main.SignalGenerationRequest(symbol="AAPL"))).model_dump(mode="json")

    body = [row.model_dump(mode="json") for row in main.list_signals(limit=2)]
    assert len(body) == 2
    assert body[0]["signal_id"] == third["signal_id"]
    assert body[1]["signal_id"] == second["signal_id"]

    filtered_body = [row.model_dump(mode="json") for row in main.list_signals(limit=20, symbol="aapl")]
    assert len(filtered_body) == 2
    assert [row["signal_id"] for row in filtered_body] == [
        third["signal_id"],
        first["signal_id"],
    ]
