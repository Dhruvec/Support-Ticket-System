import pytest
from app.config import Settings
from app.main import create_app
from cryptography.fernet import Fernet
from sqlalchemy.engine import make_url


def production_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "app_env": "production",
        "database_url": (
            "postgresql+psycopg://support_app:strong-password@db.internal:5432/"
            "support_tickets"
        ),
        "redis_url": "rediss://cache.internal:6380/0",
        "operator_api_key": "operator-key-with-at-least-thirty-two-characters",
        "groq_api_key": "groq-key",
        "google_client_id": "google-client-id",
        "google_client_secret": "google-client-secret",
        "google_redirect_uri": "https://support.example.com/auth/google/callback",
        "google_oauth_state_secret": "oauth-state-secret-with-at-least-32-characters",
        "google_pubsub_audience": "https://support.example.com/webhooks/gmail",
        "google_pubsub_service_account": (
            "pubsub-invoker@support-project.iam.gserviceaccount.com"
        ),
        "gmail_credentials_encryption_key": Fernet.generate_key().decode("ascii"),
        "slack_webhook_url": "https://hooks.slack.com/services/example",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_nonproduction_configuration_remains_permissive() -> None:
    settings = Settings(_env_file=None, app_env="test")

    settings.validate_runtime()


def test_production_configuration_reports_all_missing_security_controls() -> None:
    settings = Settings(
        _env_file=None,
        app_env="production",
        database_url=(
            "postgresql+psycopg://postgres:postgres@localhost:5432/support_tickets"
        ),
        redis_url="redis://localhost:6379/0",
    )

    with pytest.raises(ValueError) as exc_info:
        settings.validate_runtime()

    message = str(exc_info.value)
    assert "OPERATOR_API_KEY is required" in message
    assert "GOOGLE_PUBSUB_AUDIENCE is required" in message
    assert "GMAIL_CREDENTIALS_ENCRYPTION_KEY is required" in message
    assert "DATABASE_URL must not use a local development host" in message
    assert "DATABASE_URL must not use the default development credentials" in message
    assert "REDIS_URL must use rediss" in message


def test_production_configuration_accepts_hardened_settings() -> None:
    production_settings().validate_runtime()


def test_production_configuration_accepts_cloud_sql_socket() -> None:
    settings = production_settings(
        database_url=(
            "postgresql+psycopg://support_app:strong-password@/support_tickets"
            "?host=/cloudsql/support-project:asia-south1:support-ticket-db"
        ),
    )

    settings.validate_runtime()


def test_cloud_sql_connection_name_converts_local_url_to_unix_socket() -> None:
    settings = Settings(
        _env_file=None,
        database_url=(
            "postgresql+psycopg://support_app:encoded%40password@127.0.0.1:5432/"
            "support_tickets"
        ),
        cloud_sql_connection_name=(
            "support-ticket-agent-505519:asia-south1:support-ticket-db"
        ),
    )

    database = make_url(settings.database_url)
    assert database.host is None
    assert database.port is None
    assert database.password == "encoded@password"
    assert database.query["host"] == (
        "/cloudsql/support-ticket-agent-505519:asia-south1:support-ticket-db"
    )


def test_cloud_sql_connection_name_rejects_invalid_format() -> None:
    with pytest.raises(ValueError, match="project:region:instance"):
        Settings(
            _env_file=None,
            cloud_sql_connection_name="support-ticket-db",
        )


def test_uppercase_google_oauth_environment_variables_are_mapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-from-secret-manager")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret-from-secret-manager")

    settings = Settings(_env_file=None)

    assert settings.google_client_id == "client-from-secret-manager"
    assert settings.google_client_secret == "secret-from-secret-manager"


def test_production_configuration_rejects_database_url_whitespace() -> None:
    settings = production_settings(
        database_url=(
            "postgresql+psycopg://support_app:strong-password@/support_tickets"
            "?host=/cloudsql/support-project:asia-south1:support-ticket-db\n"
        ),
    )

    with pytest.raises(ValueError, match="leading or trailing whitespace"):
        settings.validate_runtime()


def test_production_configuration_rejects_invalid_gmail_encryption_key() -> None:
    settings = production_settings(
        gmail_credentials_encryption_key="not-a-valid-fernet-key",
    )

    with pytest.raises(ValueError, match="32-byte Fernet key"):
        settings.validate_runtime()


def test_production_configuration_rejects_invalid_pubsub_identity() -> None:
    settings = production_settings(
        google_pubsub_service_account="ordinary-user@example.com",
    )

    with pytest.raises(ValueError, match="Google service-account email"):
        settings.validate_runtime()


def test_create_app_validates_injected_settings_before_infrastructure_setup() -> None:
    settings = production_settings(operator_api_key=None)

    with pytest.raises(ValueError, match="OPERATOR_API_KEY is required"):
        create_app(settings)
