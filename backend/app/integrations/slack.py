import json
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class SlackTransportError(RuntimeError):
    """Raised when Slack rejects or cannot receive a webhook request."""


@dataclass(frozen=True)
class EscalationNotification:
    ticket_number: str
    subject: str
    customer_email: str
    reason: str
    urgency: str | None = None
    category: str | None = None
    confidence: float | None = None
    ticket_url: str | None = None


class SlackSender(Protocol):
    def send(self, notification: EscalationNotification) -> None: ...


class SlackWebhookSender:
    """Send escalation notifications through an incoming Slack webhook."""

    def __init__(
        self,
        webhook_url: str,
        *,
        timeout_seconds: float = 10.0,
    ) -> None:
        if not webhook_url:
            raise ValueError("Slack webhook URL is required")
        self.webhook_url = webhook_url
        self.timeout_seconds = timeout_seconds

    def send(self, notification: EscalationNotification) -> None:
        payload = build_slack_payload(notification)
        request = Request(
            self.webhook_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                if not 200 <= response.status < 300:
                    raise SlackTransportError(
                        f"Slack webhook returned HTTP {response.status}"
                    )
        except HTTPError as exc:
            raise SlackTransportError(
                f"Slack webhook returned HTTP {exc.code}"
            ) from exc
        except URLError as exc:
            raise SlackTransportError("Slack webhook request failed") from exc


def build_slack_payload(notification: EscalationNotification) -> dict[str, Any]:
    """Build a readable Slack message with a plain-text notification fallback."""
    fields = [
        {"type": "mrkdwn", "text": f"*Ticket:* `{notification.ticket_number}`"},
        {"type": "mrkdwn", "text": f"*Customer:* {notification.customer_email}"},
    ]
    if notification.category:
        fields.append({"type": "mrkdwn", "text": f"*Category:* {notification.category}"})
    if notification.urgency:
        fields.append({"type": "mrkdwn", "text": f"*Urgency:* {notification.urgency}"})
    if notification.confidence is not None:
        fields.append({"type": "mrkdwn", "text": f"*Confidence:* {notification.confidence:.2f}"})

    title = f"Support ticket escalated: {notification.ticket_number}"
    text = f"{title} | {notification.subject} | {notification.reason}"
    section: dict[str, Any] = {
        "type": "section",
        "text": {"type": "mrkdwn", "text": f"*{title}*\n{notification.reason}"},
        "fields": fields,
    }
    if notification.ticket_url:
        section["accessory"] = {
            "type": "button",
            "text": {"type": "plain_text", "text": "Open ticket"},
            "url": notification.ticket_url,
            "action_id": "open_ticket",
        }

    return {
        "text": text,
        "blocks": [section],
    }
