from __future__ import annotations

import logging
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, inspect, text

from alembic import command
from app.database import models  # noqa: F401
from app.database.database import Base
from app.database.migrate import (
    LegacySchemaError,
    build_alembic_config,
    migrate_database,
)

_EXPECTED_TABLES = {
    "agent_decisions",
    "failed_jobs",
    "gmail_mailboxes",
    "outbound_deliveries",
    "ticket_history",
    "ticket_messages",
    "tickets",
}


def sqlite_engine(tmp_path: Path, filename: str) -> Engine:
    return create_engine(f"sqlite+pysqlite:///{tmp_path / filename}")


def revision(engine: Engine) -> str:
    with engine.connect() as connection:
        return connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one()


def test_migrations_create_expected_schema_and_detect_no_drift(
    tmp_path: Path,
) -> None:
    engine = sqlite_engine(tmp_path, "migration.db")
    try:
        migrate_database(engine)

        assert set(inspect(engine).get_table_names()) == _EXPECTED_TABLES | {
            "alembic_version"
        }
        assert revision(engine) == "0001"
        assert logging.getLogger("app.request").disabled is False

        config = build_alembic_config(str(engine.url))
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.check(config)
    finally:
        engine.dispose()


def test_migrations_adopt_exact_legacy_schema_without_losing_data(
    tmp_path: Path,
) -> None:
    engine = sqlite_engine(tmp_path, "legacy.db")
    try:
        Base.metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO tickets "
                    "(ticket_number, customer_email, subject, description, status) "
                    "VALUES ('TKT-LEGACY', 'legacy@example.com', 'Legacy', "
                    "'Preserve me', 'open')"
                )
            )

        migrate_database(engine)

        assert revision(engine) == "0001"
        with engine.connect() as connection:
            assert connection.execute(
                text(
                    "SELECT description FROM tickets "
                    "WHERE ticket_number = 'TKT-LEGACY'"
                )
            ).scalar_one() == "Preserve me"
    finally:
        engine.dispose()


def test_migrations_reject_partial_unmanaged_schema(tmp_path: Path) -> None:
    engine = sqlite_engine(tmp_path, "partial.db")
    try:
        Base.metadata.tables["tickets"].create(engine)

        with pytest.raises(LegacySchemaError, match="tables do not match"):
            migrate_database(engine)

        assert "alembic_version" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_migrations_reject_drifted_unmanaged_schema(tmp_path: Path) -> None:
    engine = sqlite_engine(tmp_path, "drifted.db")
    try:
        Base.metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE tickets ADD COLUMN unmanaged TEXT"))

        with pytest.raises(LegacySchemaError, match="schema differs"):
            migrate_database(engine)

        assert "alembic_version" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_migrations_can_downgrade_empty_database(tmp_path: Path) -> None:
    engine = sqlite_engine(tmp_path, "rollback.db")
    try:
        config = build_alembic_config(str(engine.url))
        command.upgrade(config, "head")
        command.downgrade(config, "base")

        assert set(inspect(engine).get_table_names()) == {"alembic_version"}
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one_or_none() is None
        assert set(Base.metadata.tables) == _EXPECTED_TABLES
    finally:
        engine.dispose()
