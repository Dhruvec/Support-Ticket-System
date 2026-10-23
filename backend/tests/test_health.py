from collections.abc import Callable, Generator
from unittest.mock import Mock

from fastapi.testclient import TestClient
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.health import get_redis_checker
from app.config import Settings
from app.database.database import get_db
from app.main import create_app


def build_health_client(
    session: Session,
    redis_checker: Callable[[Settings], None],
) -> tuple[TestClient, object]:
    application = create_app(
        Settings(
            app_env="test",
            database_url="sqlite+pysqlite:///:memory:",
            groq_api_key=None,
        )
    )

    def override_get_db() -> Generator[Session, None, None]:
        yield session

    application.dependency_overrides[get_db] = override_get_db
    application.dependency_overrides[get_redis_checker] = lambda: redis_checker
    return TestClient(application), application


def test_root_identifies_service_and_health_routes(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert response.json() == {
        "service": client.app.state.settings.app_name,
        "status": "running",
        "health": "/health",
        "readiness": "/ready",
    }


def test_health_is_dependency_free(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


def test_readiness_reports_connected_dependencies() -> None:
    session = Mock(spec=Session)
    redis_checker = Mock()
    test_client, _ = build_health_client(session, redis_checker)

    with test_client:
        response = test_client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "database": "connected",
        "redis": "connected",
    }
    session.execute.assert_called_once()
    redis_checker.assert_called_once()


def test_readiness_reports_unavailable_database() -> None:
    session = Mock(spec=Session)
    session.execute.side_effect = SQLAlchemyError("database unavailable")
    redis_checker = Mock()
    test_client, _ = build_health_client(session, redis_checker)

    with test_client:
        response = test_client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "database": "unavailable",
        "redis": "connected",
    }
    redis_checker.assert_called_once()


def test_readiness_reports_unavailable_redis() -> None:
    session = Mock(spec=Session)
    redis_checker = Mock(side_effect=RedisError("redis unavailable"))
    test_client, _ = build_health_client(session, redis_checker)

    with test_client:
        response = test_client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "database": "connected",
        "redis": "unavailable",
    }


def test_readiness_reports_all_unavailable_dependencies() -> None:
    session = Mock(spec=Session)
    session.execute.side_effect = SQLAlchemyError("database unavailable")
    redis_checker = Mock(side_effect=OSError("redis unavailable"))
    test_client, _ = build_health_client(session, redis_checker)

    with test_client:
        response = test_client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "database": "unavailable",
        "redis": "unavailable",
    }
