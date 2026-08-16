from collections.abc import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import Settings, get_settings


class Base(DeclarativeBase):
    pass


def build_engine(database_url: str) -> Engine:
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    return create_engine(
        database_url,
        connect_args=connect_args,
        pool_pre_ping=True,
    )


settings = get_settings()
engine = build_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        yield session


def check_database_connection(db: Session) -> None:
    db.execute(text("SELECT 1"))


def create_database_schema(target_engine: Engine | None = None) -> None:
    # Model import registers table metadata before schema creation.
    from app.database import models  # noqa: F401

    Base.metadata.create_all(bind=target_engine or engine)


def create_session_factory(settings_override: Settings) -> sessionmaker[Session]:
    return sessionmaker(
        bind=build_engine(settings_override.database_url),
        autoflush=False,
        expire_on_commit=False,
    )
