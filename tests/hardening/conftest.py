"""Shared fixtures for the hardening tests.

The lifecycle authority is PostgreSQL by design, so the tests that exercise it
need a real database. They skip with a clear reason when one is not configured
rather than silently testing something weaker — a concurrency test that runs on
SQLite proves nothing about the concurrency it is meant to cover.

CI provides Postgres as a service container. Locally:

    export TEST_LIFECYCLE_POSTGRES_URL=postgresql+psycopg://postgres@localhost:5432/trading_stack
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

POSTGRES_URL = os.getenv("TEST_LIFECYCLE_POSTGRES_URL", "")
REPO_ROOT = Path(__file__).resolve().parents[2]

@pytest.fixture(scope="session")
def postgres_url() -> str:
    if not POSTGRES_URL:
        pytest.skip("TEST_LIFECYCLE_POSTGRES_URL is not set")
    return POSTGRES_URL


@pytest.fixture
def migrated_db(postgres_url: str) -> str:
    """A freshly migrated schema for each test.

    Rolled all the way down and back up rather than truncated: it exercises the
    migration on every run, so a broken `down` shows up here instead of in
    production.
    """
    import sys

    sys.path.insert(0, str(REPO_ROOT / "tools"))
    import migrate  # noqa: PLC0415

    engine = create_engine(postgres_url, future=True)
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP SCHEMA IF EXISTS lifecycle CASCADE")
    migrate.up(engine)
    yield postgres_url
    engine.dispose()


@pytest.fixture
def store(migrated_db: str):
    from lifecycle.store import PostgresLifecycleStore, StoreSettings

    return PostgresLifecycleStore(StoreSettings(url=migrated_db))


def table_exists(url: str, name: str) -> bool:
    engine = create_engine(url, future=True)
    with engine.connect() as conn:
        return bool(
            conn.execute(
                text(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema='lifecycle' AND table_name=:n)"
                ),
                {"n": name},
            ).scalar()
        )
