from datetime import UTC, datetime

import pytest
from app import tasks
from app.api.gmail_auth import _decode_state, _encode_state
from app.config import Settings
from app.database.gmail_mailbox_store import GmailMailboxStore
from app.integrations import gmail, gmail_runtime
from app.main import create_app
from app.schemas.email import IncomingEmail
from cryptography.fernet import Fernet
from fastapi import HTTPException
from fastapi.testclient import TestClient


def oauth_settings(database_url: str, encryption_key: str) -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        database_url=database_url,
        google_client_id="client-id",
        google_client_secret="client-secret",
        google_redirect_uri="http://testserver/auth/google/callback",
        google_oauth_state_secret="test-state-secret",
        gmail_credentials_encryption_key=encryption_key,
    )


def test_credentials_from_json_builds_refreshable_credentials() -> None:
    credentials = gmail.credentials_from_json(
        {
            "token": "access-token",
            "refresh_token": "refresh-token",
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "client-id",
            "client_secret": "client-secret",
            "scopes": ["scope-a"],
        }
    )

    assert credentials.token == "access-token"
    assert credentials.refresh_token == "refresh-token"
    assert credentials.scopes == ["scope-a"]


def test_credentials_from_json_rejects_incomplete_data() -> None:
    with pytest.raises(ValueError, match="client_secret"):
        gmail.credentials_from_json(
            {
                "token_uri": "https://oauth2.googleapis.com/token",
                "client_id": "client-id",
            }
        )


def test_build_gmail_service_disables_discovery_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, object, bool]] = []

    def fake_build(
        service_name: str,
        version: str,
        *,
        credentials: object,
        cache_discovery: bool,
    ) -> str:
        calls.append((service_name, version, credentials, cache_discovery))
        return "gmail-service"

    monkeypatch.setattr(gmail, "build", fake_build)
    service = gmail.build_gmail_service(
        {
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "client-id",
            "client_secret": "client-secret",
        }
    )

    assert service == "gmail-service"
    assert calls[0][0:2] == ("gmail", "v1")
    assert calls[0][3] is False


def test_oauth_state_is_signed_and_rejects_tampering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.api.gmail_auth.time.time", lambda: 1_100)
    state = _encode_state("support@example.com", "secret", issued_at=1_000)

    assert _decode_state(state, "secret", ttl_seconds=600) == "support@example.com"
    with pytest.raises(HTTPException, match="Invalid Google OAuth state"):
        _decode_state(state, "wrong-secret", ttl_seconds=600)


def test_oauth_callback_persists_credentials(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'oauth.db'}"
    encryption_key = Fernet.generate_key().decode("ascii")
    settings = oauth_settings(database_url, encryption_key)
    application = create_app(settings)
    from app.database import models  # noqa: F401
    from app.database.database import Base

    Base.metadata.create_all(application.state.session_factory.kw["bind"])
    monkeypatch.setattr(
        "app.api.gmail_auth.build_oauth_flow",
        lambda **kwargs: object(),
    )
    monkeypatch.setattr(
        "app.api.gmail_auth.exchange_authorization_code",
        lambda flow, *, code: {
            "token": "token",
            "refresh_token": "refresh",
            "token_uri": "token-uri",
            "client_id": "client-id",
            "client_secret": "client-secret",
            "scopes": ["scope"],
        },
    )
    state = _encode_state("support@example.com", "test-state-secret")

    with TestClient(application) as client:
        response = client.get(
            "/auth/google/callback",
            params={"code": "authorization-code", "state": state},
        )

    assert response.status_code == 200
    store = GmailMailboxStore(
        application.state.session_factory,
        encryption_key=encryption_key,
    )
    assert store.get_credentials("support@example.com")["refresh_token"] == "refresh"


def test_runtime_processor_short_circuits_persisted_message_before_graph(
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    incoming = IncomingEmail(
        message_id="gmail-1",
        thread_id="thread-1",
        sender="customer@example.com",
        recipient="support@example.com",
        subject="Help",
        body="Please help",
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
    )

    class StubTicketService:
        def __init__(self, session: object) -> None:
            self.session = session

        def find_duplicate(self, gmail_message_id: str) -> object:
            assert gmail_message_id == "gmail-1"
            return object()

    def fail_if_graph_is_built(*args: object, **kwargs: object) -> object:
        raise AssertionError("duplicate replay must not initialize the workflow graph")

    monkeypatch.setattr(gmail_runtime, "EmailTicketService", StubTicketService)
    monkeypatch.setattr(gmail_runtime, "build_support_workflow_graph", fail_if_graph_is_built)
    processor = object.__new__(gmail_runtime.RuntimeGmailProcessor)
    processor.session_factory = session_factory

    processor._process_email(incoming)


def test_send_gmail_reply_loads_credentials_and_sends(
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    encryption_key = Fernet.generate_key().decode("ascii")
    GmailMailboxStore(
        session_factory,
        encryption_key=encryption_key,
    ).set_credentials(
        "support@example.com",
        {
            "token_uri": "token-uri",
            "client_id": "client-id",
            "client_secret": "client-secret",
        },
    )
    sent: list[tuple[IncomingEmail, str]] = []

    class FakeGateway:
        def __init__(self, service: object) -> None:
            assert service == "service"

        def send_reply(self, original: IncomingEmail, body: str) -> str:
            sent.append((original, body))
            return "sent-id"

    monkeypatch.setattr(tasks, "SessionLocal", session_factory)
    monkeypatch.setattr(
        tasks.settings,
        "gmail_credentials_encryption_key",
        encryption_key,
    )
    monkeypatch.setattr(tasks, "build_gmail_service", lambda credentials: "service")
    monkeypatch.setattr(tasks, "GmailGateway", FakeGateway)
    original = IncomingEmail(
        message_id="gmail-1",
        thread_id="thread-1",
        sender="customer@example.com",
        recipient="support@example.com",
        subject="Help",
        body="Please help",
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
    )

    tasks._send_gmail_reply(
        {
            "ticket_number": "TKT-12345678",
            "original": original.model_dump(mode="json"),
            "body": "Resolved",
        }
    )

    assert sent == [(original, "Resolved")]
