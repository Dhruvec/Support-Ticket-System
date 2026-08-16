import base64
import json
from collections.abc import Mapping
from typing import Any

import pytest
from app import security
from app.api import gmail_auth
from fastapi.testclient import TestClient

_OPERATOR_KEY = "operator-key-with-at-least-thirty-two-characters"
_PUBSUB_AUDIENCE = "https://support.example.com/webhooks/gmail"
_PUBSUB_SERVICE_ACCOUNT = "pubsub-invoker@support-project.iam.gserviceaccount.com"


class RecordingGmailProcessor:
    def __init__(self, *, processed_messages: int = 1) -> None:
        self.processed_messages = processed_messages
        self.calls: list[tuple[str, str]] = []

    def process(self, email_address: str, history_id: str) -> int:
        self.calls.append((email_address, history_id))
        return self.processed_messages


def gmail_envelope() -> dict[str, Any]:
    payload = json.dumps(
        {
            "emailAddress": "support@example.com",
            "historyId": "201",
        }
    ).encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return {
        "message": {"data": encoded},
        "subscription": "projects/example/subscriptions/gmail",
    }


def configure_operator(client: TestClient) -> None:
    client.app.state.settings.operator_api_key = _OPERATOR_KEY


def configure_pubsub(client: TestClient) -> None:
    client.app.state.settings.google_pubsub_audience = _PUBSUB_AUDIENCE
    client.app.state.settings.google_pubsub_service_account = _PUBSUB_SERVICE_ACCOUNT


def valid_pubsub_claims(**overrides: object) -> dict[str, object]:
    claims: dict[str, object] = {
        "iss": "https://accounts.google.com",
        "email_verified": True,
        "email": _PUBSUB_SERVICE_ACCOUNT,
    }
    claims.update(overrides)
    return claims


def test_operator_key_is_required_when_configured(client: TestClient) -> None:
    configure_operator(client)

    response = client.get("/tickets")

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid or missing operator credentials"


def test_operator_rejects_wrong_key_using_constant_time_comparison(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_operator(client)
    comparisons: list[tuple[bytes, bytes]] = []
    compare_digest = security.secrets.compare_digest

    def recording_compare_digest(supplied: bytes, expected: bytes) -> bool:
        comparisons.append((supplied, expected))
        return compare_digest(supplied, expected)

    monkeypatch.setattr(security.secrets, "compare_digest", recording_compare_digest)

    response = client.get(
        "/tickets",
        headers={"X-Operator-Key": "incorrect-operator-key"},
    )

    assert response.status_code == 401
    assert comparisons == [
        (b"incorrect-operator-key", _OPERATOR_KEY.encode("utf-8")),
    ]


def test_operator_key_allows_ticket_access(client: TestClient) -> None:
    configure_operator(client)

    response = client.get(
        "/tickets",
        headers={"X-Operator-Key": _OPERATOR_KEY},
    )

    assert response.status_code == 200
    assert response.json() == []


def test_operator_authentication_fails_closed_in_production(client: TestClient) -> None:
    client.app.state.settings.app_env = "production"
    client.app.state.settings.operator_api_key = None

    response = client.get("/tickets")

    assert response.status_code == 503
    assert response.json()["detail"] == "Operator authentication is not configured"


def test_google_oauth_requires_credential_encryption_key(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_operator(client)
    client.app.state.settings.google_client_id = "client-id"
    client.app.state.settings.google_client_secret = "client-secret"
    client.app.state.settings.google_redirect_uri = "http://testserver/auth/google/callback"
    client.app.state.settings.google_oauth_state_secret = "test-state-secret"
    client.app.state.settings.gmail_credentials_encryption_key = None
    provider_calls = 0

    def unexpected_provider_call(**_: str) -> None:
        nonlocal provider_calls
        provider_calls += 1
        raise AssertionError("OAuth provider setup must not run without encryption")

    monkeypatch.setattr(gmail_auth, "build_oauth_flow", unexpected_provider_call)

    response = client.get(
        "/auth/google",
        params={"email_address": "support@example.com"},
        headers={"X-Operator-Key": _OPERATOR_KEY},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == (
        "Google OAuth is not configured; missing environment variables: "
        "GMAIL_CREDENTIALS_ENCRYPTION_KEY"
    )
    assert provider_calls == 0


def test_google_oauth_reports_all_missing_environment_variables(
    client: TestClient,
) -> None:
    configure_operator(client)
    settings = client.app.state.settings
    settings.google_client_id = None
    settings.google_client_secret = None
    settings.google_redirect_uri = None
    settings.google_oauth_state_secret = None
    settings.gmail_credentials_encryption_key = None

    response = client.get(
        "/auth/google",
        params={"email_address": "support@example.com"},
        headers={"X-Operator-Key": _OPERATOR_KEY},
    )

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "GOOGLE_CLIENT_ID" in detail
    assert "GOOGLE_CLIENT_SECRET" in detail
    assert "GOOGLE_REDIRECT_URI" in detail
    assert "GOOGLE_OAUTH_STATE_SECRET" in detail
    assert "GMAIL_CREDENTIALS_ENCRYPTION_KEY" in detail


def test_auth_login_redirects_to_canonical_google_route(client: TestClient) -> None:
    configure_operator(client)

    response = client.get(
        "/auth/login",
        params={"email_address": "support@example.com"},
        headers={"X-Operator-Key": _OPERATOR_KEY},
        follow_redirects=False,
    )

    assert response.status_code == 307
    assert response.headers["location"] == (
        "/auth/google?email_address=support@example.com"
    )


def test_mailbox_cleanup_deletes_stale_ciphertext_without_decrypting(
    client: TestClient,
    session_factory,
) -> None:
    from app.database.models import GmailMailbox

    configure_operator(client)
    client.app.state.session_factory = session_factory
    with session_factory.begin() as session:
        session.add(
            GmailMailbox(
                email_address="stale@example.com",
                credentials={
                    "__support_credentials__": "gmail_oauth_credentials",
                    "version": 1,
                    "ciphertext": "unrecoverable-with-active-key",
                },
            )
        )

    response = client.delete(
        "/auth/google/mailbox",
        params={"email_address": "stale@example.com"},
        headers={"X-Operator-Key": _OPERATOR_KEY},
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "deleted",
        "email_address": "stale@example.com",
    }


def test_google_oauth_start_rejects_request_before_provider_work(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_operator(client)
    provider_calls = 0

    def unexpected_provider_call(**_: str) -> None:
        nonlocal provider_calls
        provider_calls += 1
        raise AssertionError("OAuth provider setup must not run before authentication")

    monkeypatch.setattr(gmail_auth, "build_oauth_flow", unexpected_provider_call)

    response = client.get(
        "/auth/google",
        params={"email_address": "support@example.com"},
    )

    assert response.status_code == 401
    assert provider_calls == 0


def test_pubsub_rejects_missing_bearer_before_processing(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_pubsub(client)
    processor = RecordingGmailProcessor()
    client.app.state.gmail_processor = processor

    def unexpected_verification(token: str, audience: str) -> Mapping[str, Any]:
        raise AssertionError(f"unexpected verification for {token=} and {audience=}")

    monkeypatch.setattr(security, "_verify_google_token", unexpected_verification)

    response = client.post("/webhooks/gmail", json=gmail_envelope())

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert processor.calls == []


def test_pubsub_rejects_token_when_google_verification_fails(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_pubsub(client)
    processor = RecordingGmailProcessor()
    client.app.state.gmail_processor = processor
    verification_calls: list[tuple[str, str]] = []

    def reject_token(token: str, audience: str) -> Mapping[str, Any]:
        verification_calls.append((token, audience))
        raise ValueError("invalid audience")

    monkeypatch.setattr(security, "_verify_google_token", reject_token)

    response = client.post(
        "/webhooks/gmail",
        json=gmail_envelope(),
        headers={"Authorization": "Bearer wrong-audience-token"},
    )

    assert response.status_code == 401
    assert verification_calls == [("wrong-audience-token", _PUBSUB_AUDIENCE)]
    assert processor.calls == []


@pytest.mark.parametrize(
    "claims",
    [
        valid_pubsub_claims(iss="https://issuer.example.com"),
        valid_pubsub_claims(email_verified=False),
        {
            "iss": "https://accounts.google.com",
            "email": _PUBSUB_SERVICE_ACCOUNT,
        },
    ],
    ids=["wrong-issuer", "unverified-email", "missing-email-verification"],
)
def test_pubsub_rejects_untrusted_google_claims(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    claims: Mapping[str, Any],
) -> None:
    configure_pubsub(client)
    processor = RecordingGmailProcessor()
    client.app.state.gmail_processor = processor
    monkeypatch.setattr(
        security,
        "_verify_google_token",
        lambda token, audience: claims,
    )

    response = client.post(
        "/webhooks/gmail",
        json=gmail_envelope(),
        headers={"Authorization": "Bearer google-token"},
    )

    assert response.status_code == 401
    assert processor.calls == []


def test_pubsub_rejects_wrong_service_account(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_pubsub(client)
    processor = RecordingGmailProcessor()
    client.app.state.gmail_processor = processor
    monkeypatch.setattr(
        security,
        "_verify_google_token",
        lambda token, audience: valid_pubsub_claims(
            email="other-invoker@support-project.iam.gserviceaccount.com"
        ),
    )

    response = client.post(
        "/webhooks/gmail",
        json=gmail_envelope(),
        headers={"Authorization": "Bearer google-token"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Pub/Sub caller is not authorized"
    assert processor.calls == []


def test_pubsub_validates_audience_and_invokes_processor_once(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_pubsub(client)
    processor = RecordingGmailProcessor(processed_messages=2)
    client.app.state.gmail_processor = processor
    verification_calls: list[tuple[str, str]] = []

    def accept_token(token: str, audience: str) -> Mapping[str, Any]:
        verification_calls.append((token, audience))
        return valid_pubsub_claims(email=_PUBSUB_SERVICE_ACCOUNT.upper())

    monkeypatch.setattr(security, "_verify_google_token", accept_token)

    response = client.post(
        "/webhooks/gmail",
        json=gmail_envelope(),
        headers={"Authorization": "Bearer valid-google-token"},
    )

    assert response.status_code == 202
    assert response.json() == {
        "accepted": True,
        "processed_messages": 2,
    }
    assert verification_calls == [("valid-google-token", _PUBSUB_AUDIENCE)]
    assert processor.calls == [("support@example.com", "201")]


def test_pubsub_partial_configuration_fails_closed(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client.app.state.settings.google_pubsub_audience = _PUBSUB_AUDIENCE
    client.app.state.settings.google_pubsub_service_account = None
    processor = RecordingGmailProcessor()
    client.app.state.gmail_processor = processor
    verification_calls = 0

    def unexpected_verification(token: str, audience: str) -> Mapping[str, Any]:
        nonlocal verification_calls
        verification_calls += 1
        return valid_pubsub_claims()

    monkeypatch.setattr(security, "_verify_google_token", unexpected_verification)

    response = client.post(
        "/webhooks/gmail",
        json=gmail_envelope(),
        headers={"Authorization": "Bearer google-token"},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == (
        "Google Pub/Sub authentication is not configured"
    )
    assert verification_calls == 0
    assert processor.calls == []


def test_pubsub_authentication_has_explicit_test_environment_bypass(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client.app.state.settings.google_pubsub_audience = None
    client.app.state.settings.google_pubsub_service_account = None
    processor = RecordingGmailProcessor()
    client.app.state.gmail_processor = processor

    def unexpected_verification(token: str, audience: str) -> Mapping[str, Any]:
        raise AssertionError(f"unexpected verification for {token=} and {audience=}")

    monkeypatch.setattr(security, "_verify_google_token", unexpected_verification)

    response = client.post("/webhooks/gmail", json=gmail_envelope())

    assert response.status_code == 202
    assert processor.calls == [("support@example.com", "201")]
