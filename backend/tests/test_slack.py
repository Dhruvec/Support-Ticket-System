import json
from typing import Self
from urllib.error import URLError

import pytest
from app.integrations import slack
from app.integrations.slack import (
    EscalationNotification,
    SlackTransportError,
    SlackWebhookSender,
    build_slack_payload,
)


def notification() -> EscalationNotification:
    return EscalationNotification(
        ticket_number="TKT-000001",
        subject="Unable to access billing portal",
        customer_email="customer@example.com",
        reason="Classification confidence is below threshold.",
        urgency="high",
        category="billing",
        confidence=0.61,
        ticket_url="https://support.example.com/tickets/TKT-000001",
    )


def test_build_slack_payload_contains_fallback_and_ticket_context() -> None:
    payload = build_slack_payload(notification())

    assert payload["text"] == (
        "Support ticket escalated: TKT-000001 | "
        "Unable to access billing portal | Classification confidence is below threshold."
    )
    block = payload["blocks"][0]
    assert block["type"] == "section"
    assert "TKT-000001" in block["text"]["text"]
    assert block["accessory"]["url"].endswith("TKT-000001")
    assert any("billing" in field["text"] for field in block["fields"])


def test_sender_requires_webhook_url() -> None:
    with pytest.raises(ValueError, match="webhook URL is required"):
        SlackWebhookSender("")


def test_sender_posts_json_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[object, float]] = []

    class FakeResponse:
        status = 200

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    def fake_urlopen(request: object, timeout: float) -> FakeResponse:
        calls.append((request, timeout))
        return FakeResponse()

    monkeypatch.setattr(slack, "urlopen", fake_urlopen)
    SlackWebhookSender("https://hooks.slack.test/example", timeout_seconds=4).send(notification())

    request, timeout = calls[0]
    assert timeout == 4
    assert request.full_url == "https://hooks.slack.test/example"
    assert request.get_header("Content-type") == "application/json"
    assert json.loads(request.data.decode("utf-8"))["text"].startswith("Support ticket escalated")


def test_sender_wraps_network_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(request: object, timeout: float) -> None:
        raise URLError("connection refused")

    monkeypatch.setattr(slack, "urlopen", fake_urlopen)

    with pytest.raises(SlackTransportError, match="request failed"):
        SlackWebhookSender("https://hooks.slack.test/example").send(notification())
