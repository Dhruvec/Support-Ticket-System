from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import Engine, inspect, text
from sqlalchemy.engine import Connection

from app.config import get_settings
from app.database import models  # noqa: F401
from app.database.database import Base, build_engine

_BASELINE_REVISION = "0001"
_VERSION_TABLE = "alembic_version"
_POSTGRESQL_MIGRATION_LOCK_ID = 8_102_024_001


class LegacySchemaError(RuntimeError):
    """Raised when an unmanaged database cannot be safely adopted."""


def build_alembic_config(database_url: str | None = None) -> Config:
    """Build Alembic configuration with an explicit, validated database URL."""
    backend_root = Path(__file__).resolve().parents[2]
    config = Config(str(backend_root / "alembic.ini"))
    resolved_database_url = database_url or get_settings().database_url
    config.set_main_option(
        "sqlalchemy.url",
        resolved_database_url.replace("%", "%%"),
    )
    return config


def migrate_database(engine: Engine | None = None) -> None:
    """Adopt a verified legacy schema, upgrade to head, and reject drift."""
    owned_engine = engine is None
    target_engine = engine or build_engine(get_settings().database_url)

    try:
        with target_engine.begin() as connection:
            _lock_migrations(connection)
            config = build_alembic_config(str(target_engine.url))
            config.attributes["connection"] = connection

            _adopt_legacy_schema_if_safe(connection, config)
            command.upgrade(config, "head")
            command.check(config)
    finally:
        if owned_engine:
            target_engine.dispose()


def _lock_migrations(connection: Connection) -> None:
    if connection.dialect.name == "postgresql":
        connection.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": _POSTGRESQL_MIGRATION_LOCK_ID},
        )


def _adopt_legacy_schema_if_safe(connection: Connection, config: Config) -> None:
    table_names = set(inspect(connection).get_table_names())
    if _VERSION_TABLE in table_names or not table_names:
        return

    expected_tables = set(Base.metadata.tables)
    if table_names != expected_tables:
        missing = sorted(expected_tables - table_names)
        unexpected = sorted(table_names - expected_tables)
        raise LegacySchemaError(
            "Refusing to stamp unmanaged database because its tables do not match "
            f"revision {_BASELINE_REVISION}; missing={missing}, unexpected={unexpected}"
        )

    migration_context = MigrationContext.configure(
        connection,
        opts={"compare_type": True},
    )
    differences = compare_metadata(migration_context, Base.metadata)
    if differences:
        raise LegacySchemaError(
            "Refusing to stamp unmanaged database because its schema differs from "
            f"revision {_BASELINE_REVISION}: {differences!r}"
        )

    command.stamp(config, _BASELINE_REVISION)


def main() -> None:
    migrate_database()


if __name__ == "__main__":
    main()
