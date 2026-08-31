#!/usr/bin/env python3
"""Apply the additive lifecycle migrations.

Numbered SQL pairs rather than Alembic: a dozen explicit files are easier to
review and to smoke-test than a generated revision graph, and this schema is
new rather than evolving.

Every migration runs inside a transaction and is recorded in
``lifecycle.schema_migrations``, so re-running is a no-op and drift shows up as
a checksum mismatch rather than a silent difference.

    uv run python tools/migrate.py --url postgresql+psycopg://... up
    uv run python tools/migrate.py --url ... status
    uv run python tools/migrate.py --url ... down --steps 1

No migration here alters, truncates or back-fills an existing table, so
applying or rolling back cannot lose user trading data.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
VERSION_PATTERN = re.compile(r"^(\d+)_(.+)\.(up|down)\.sql$")

# The bookkeeping table lives inside the migration itself (0001 creates it), so
# this is only needed to *read* state before the first migration has run.
_TABLE_EXISTS = text(
    "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
    "WHERE table_schema = 'lifecycle' AND table_name = 'schema_migrations')"
)


@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    up: Path
    down: Path | None

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.up.read_bytes()).hexdigest()[:16]


def discover(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    """Every migration on disk, in version order."""
    ups: dict[str, tuple[str, Path]] = {}
    downs: dict[str, Path] = {}
    for path in sorted(directory.glob("*.sql")):
        match = VERSION_PATTERN.match(path.name)
        if not match:
            continue
        version, name, direction = match.groups()
        if direction == "up":
            ups[version] = (name, path)
        else:
            downs[version] = path

    return [
        Migration(version=v, name=name, up=path, down=downs.get(v))
        for v, (name, path) in sorted(ups.items())
    ]


def applied_versions(engine: Engine) -> dict[str, str]:
    """Versions already recorded, mapped to their stored checksum."""
    with engine.connect() as conn:
        if not conn.execute(_TABLE_EXISTS).scalar():
            return {}
        rows = conn.execute(
            text("SELECT version, checksum FROM lifecycle.schema_migrations")
        ).all()
    return {row[0]: row[1] for row in rows}


def _execute_script(conn, sql: str) -> None:
    """Run a migration script as one statement batch.

    ``exec_driver_sql`` passes the text through to the driver rather than
    letting SQLAlchemy parse it, so the ``$$``-quoted and multi-statement forms
    a schema migration needs survive intact.
    """
    conn.exec_driver_sql(sql)


def up(engine: Engine, migrations: list[Migration] | None = None) -> list[str]:
    """Apply every pending migration. Returns the versions applied."""
    migrations = migrations if migrations is not None else discover()
    already = applied_versions(engine)
    applied: list[str] = []

    for migration in migrations:
        if migration.version in already:
            stored = already[migration.version]
            if stored and stored != migration.checksum:
                raise RuntimeError(
                    f"Migration {migration.version} has changed since it was applied "
                    f"(recorded {stored}, on disk {migration.checksum}). Migrations "
                    "are immutable once applied — add a new one instead."
                )
            continue

        # One transaction per migration: a failure leaves the database exactly
        # as it was, rather than half-migrated.
        with engine.begin() as conn:
            _execute_script(conn, migration.up.read_text(encoding="utf-8"))
            conn.execute(
                text(
                    "INSERT INTO lifecycle.schema_migrations (version, checksum) "
                    "VALUES (:v, :c) ON CONFLICT (version) DO NOTHING"
                ),
                {"v": migration.version, "c": migration.checksum},
            )
        applied.append(migration.version)

    return applied


def down(engine: Engine, steps: int = 1) -> list[str]:
    """Roll back the most recent `steps` migrations, newest first."""
    migrations = {m.version: m for m in discover()}
    already = sorted(applied_versions(engine), reverse=True)
    reverted: list[str] = []

    for version in already[:steps]:
        migration = migrations.get(version)
        if migration is None or migration.down is None:
            raise RuntimeError(f"No down script for migration {version}")

        with engine.begin() as conn:
            # Remove the record first: the down script may drop the table it
            # lives in, and doing it in this order keeps both cases correct.
            conn.execute(
                text("DELETE FROM lifecycle.schema_migrations WHERE version = :v"),
                {"v": version},
            )
            _execute_script(conn, migration.down.read_text(encoding="utf-8"))
        reverted.append(version)

    return reverted


def status(engine: Engine) -> list[tuple[str, str, bool]]:
    already = applied_versions(engine)
    return [(m.version, m.name, m.version in already) for m in discover()]


def _url(explicit: str | None) -> str:
    url = explicit or os.getenv("LIFECYCLE_DATABASE_URL", "")
    if not url:
        raise SystemExit(
            "No database URL. Pass --url or set LIFECYCLE_DATABASE_URL "
            "(e.g. postgresql+psycopg://postgres@localhost:5432/trading_stack)."
        )
    return url


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("up", "down", "status"))
    parser.add_argument("--url", default=None)
    parser.add_argument("--steps", type=int, default=1, help="for down")
    args = parser.parse_args(argv)

    engine = create_engine(_url(args.url), future=True)

    if args.command == "status":
        for version, name, done in status(engine):
            print(f"  [{'x' if done else ' '}] {version}  {name}")
        return 0

    if args.command == "up":
        applied = up(engine)
        print(f"applied: {', '.join(applied) if applied else 'nothing pending'}")
        return 0

    reverted = down(engine, steps=args.steps)
    print(f"reverted: {', '.join(reverted) if reverted else 'nothing to revert'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
