from pathlib import Path

def _setup(tmp_path: Path):
    db_file = tmp_path / "audit.db"
    import audit_logger.config as config
    import audit_logger.database as database
    import audit_logger.main as main

    config.settings = config.AuditLoggerSettings(database_url=f"sqlite+pysqlite:///{db_file}")
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


def test_audit_logger_round_trip(tmp_path: Path) -> None:
    from contracts import AuditEvent

    main = _setup(tmp_path)
    created = main.create_log(
        AuditEvent(
            event_type="trade.executed",
            symbol="AAPL",
            signal_id="sig-1",
            decision="APPROVE",
            reasoning="smoke",
            metadata={"amount_usd": 100.0},
        )
    )
    event_id = created.event_id

    listed = main.list_logs(symbol="AAPL", event_type="trade.executed", limit=100)
    assert listed[0].event_id == event_id

    fetched = main.get_log(event_id)
    assert fetched.metadata["amount_usd"] == 100.0
