"""Tests for the audit summary endpoint."""
from pathlib import Path

from contracts import AuditEvent


def _setup(tmp_path: Path):
    db_file = tmp_path / "audit-summary.db"
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


def test_audit_summary_empty(tmp_path: Path):
    main = _setup(tmp_path)
    data = main.get_summary()
    assert "total_trades" in data
    assert "approved_today" in data
    assert "rejected_today" in data
    assert "weekly_spend" in data
    assert "top_symbols_traded" in data


def test_audit_summary_after_log(tmp_path: Path):
    main = _setup(tmp_path)
    main.create_log(
        AuditEvent(
            event_type="signal.approved",
            symbol="AAPL",
            decision="APPROVE",
            reasoning="test",
            metadata={},
        )
    )
    data = main.get_summary()
    assert data["approved_today"] >= 1
