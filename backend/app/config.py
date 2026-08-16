from functools import lru_cache
from urllib.parse import urlparse

from cryptography.fernet import Fernet
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL, make_url


class Settings(BaseSettings):
    """Application configuration loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Support Ticket Agent"
    app_env: str = "development"
    log_level: str = "INFO"
    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/support_tickets"
    cloud_sql_connection_name: str | None = None
    redis_url: str = "redis://localhost:6379/0"
    operator_api_key: str | None = None

    groq_api_key: str | None = None
    groq_model: str = "llama-3.3-70b-versatile"
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    faq_source_path: str = "app/knowledge/faq.json"
    faq_index_path: str = "data/faq_index"
    google_client_id: str | None = None
    google_client_secret: str | None = None
    google_redirect_uri: str | None = None
    google_oauth_state_secret: str | None = None
    google_oauth_state_ttl_seconds: int = Field(default=600, ge=60, le=3600)
    google_pubsub_audience: str | None = None
    google_pubsub_service_account: str | None = None
    gmail_credentials_encryption_key: str | None = None
    slack_webhook_url: str | None = None

    classification_confidence_threshold: float = Field(default=0.85, ge=0.0, le=1.0)
    faq_similarity_threshold: float = Field(default=0.85, ge=0.0, le=1.0)
    max_retry_attempts: int = Field(default=4, ge=1)
    retry_backoff_base_seconds: int = Field(default=2, ge=1)

    @model_validator(mode="after")
    def use_cloud_sql_socket(self) -> "Settings":
        """Route PostgreSQL through Cloud Run's mounted Cloud SQL Unix socket."""
        connection_name = (self.cloud_sql_connection_name or "").strip()
        if not connection_name:
            return self
        if "/" in connection_name or connection_name.count(":") != 2:
            raise ValueError(
                "CLOUD_SQL_CONNECTION_NAME must use project:region:instance format"
            )

        try:
            database = make_url(self.database_url.strip())
        except ValueError:
            return self
        if not database.drivername.startswith("postgresql"):
            return self

        socket_path = f"/cloudsql/{connection_name}"
        query = dict(database.query)
        query["host"] = socket_path
        self.database_url = URL.create(
            drivername=database.drivername,
            username=database.username,
            password=database.password,
            database=database.database,
            query=query,
        ).render_as_string(hide_password=False)
        return self

    def validate_runtime(self) -> None:
        """Reject unsafe or incomplete settings before a production process starts."""
        if self.app_env != "production":
            return

        errors: list[str] = []
        required_secrets = {
            "OPERATOR_API_KEY": self.operator_api_key,
            "GROQ_API_KEY": self.groq_api_key,
            "GOOGLE_CLIENT_ID": self.google_client_id,
            "GOOGLE_CLIENT_SECRET": self.google_client_secret,
            "GOOGLE_OAUTH_STATE_SECRET": self.google_oauth_state_secret,
            "GOOGLE_PUBSUB_AUDIENCE": self.google_pubsub_audience,
            "GOOGLE_PUBSUB_SERVICE_ACCOUNT": self.google_pubsub_service_account,
            "GMAIL_CREDENTIALS_ENCRYPTION_KEY": self.gmail_credentials_encryption_key,
            "SLACK_WEBHOOK_URL": self.slack_webhook_url,
        }
        errors.extend(
            f"{name} is required in production"
            for name, value in required_secrets.items()
            if not value or not value.strip()
        )

        for name, value in (
            ("OPERATOR_API_KEY", self.operator_api_key),
            ("GOOGLE_OAUTH_STATE_SECRET", self.google_oauth_state_secret),
        ):
            if value and len(value.strip()) < 32:
                errors.append(f"{name} must contain at least 32 characters")

        self._validate_gmail_encryption_key(errors)
        self._validate_production_database(errors)
        self._validate_secure_url(
            errors,
            "REDIS_URL",
            self.redis_url,
            schemes={"rediss"},
        )
        self._validate_secure_url(
            errors,
            "GOOGLE_REDIRECT_URI",
            self.google_redirect_uri,
            schemes={"https"},
        )
        self._validate_secure_url(
            errors,
            "GOOGLE_PUBSUB_AUDIENCE",
            self.google_pubsub_audience,
            schemes={"https"},
        )
        self._validate_secure_url(
            errors,
            "SLACK_WEBHOOK_URL",
            self.slack_webhook_url,
            schemes={"https"},
        )

        service_account = self.google_pubsub_service_account or ""
        if service_account and (
            "@" not in service_account
            or not service_account.lower().endswith(".iam.gserviceaccount.com")
        ):
            errors.append(
                "GOOGLE_PUBSUB_SERVICE_ACCOUNT must be a Google service-account email"
            )

        if errors:
            formatted = "; ".join(dict.fromkeys(errors))
            raise ValueError(f"Invalid production configuration: {formatted}")

    def _validate_gmail_encryption_key(self, errors: list[str]) -> None:
        value = self.gmail_credentials_encryption_key
        if not value or not value.strip():
            return
        try:
            Fernet(value.strip().encode("ascii"))
        except (TypeError, ValueError, UnicodeEncodeError):
            errors.append(
                "GMAIL_CREDENTIALS_ENCRYPTION_KEY must be a URL-safe "
                "base64-encoded 32-byte Fernet key"
            )

    def _validate_production_database(self, errors: list[str]) -> None:
        if self.database_url != self.database_url.strip():
            errors.append("DATABASE_URL must not contain leading or trailing whitespace")
            return

        try:
            database = make_url(self.database_url)
        except ValueError:
            errors.append("DATABASE_URL must be a valid SQLAlchemy URL")
            return

        if not database.drivername.startswith("postgresql"):
            errors.append("DATABASE_URL must use PostgreSQL in production")

        socket_host = database.query.get("host")
        effective_host = database.host or socket_host or ""
        is_cloud_sql_socket = (
            database.host is None
            and isinstance(socket_host, str)
            and socket_host.startswith("/cloudsql/")
        )
        if not is_cloud_sql_socket and effective_host.lower() in {
            "",
            "localhost",
            "127.0.0.1",
            "postgres",
        }:
            errors.append("DATABASE_URL must not use a local development host")
        if database.username == "postgres" and database.password == "postgres":
            errors.append("DATABASE_URL must not use the default development credentials")

    @staticmethod
    def _validate_secure_url(
        errors: list[str],
        name: str,
        value: str | None,
        *,
        schemes: set[str],
    ) -> None:
        if not value:
            return
        parsed = urlparse(value)
        if parsed.scheme.lower() not in schemes or not parsed.hostname:
            expected = " or ".join(sorted(schemes))
            errors.append(f"{name} must use {expected} with a valid host")
            return
        if parsed.hostname.lower() in {"localhost", "127.0.0.1"}:
            errors.append(f"{name} must not use a local development host")


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.validate_runtime()
    return settings
