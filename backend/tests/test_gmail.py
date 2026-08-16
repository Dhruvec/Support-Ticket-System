import base64
import json
from email import policy
from email.parser import BytesParser

from app.api.gmail import GmailPubSubEnvelope
from app.integrations.gmail import (
    GmailGateway,
    GmailNotificationProcessor,
    parse_gmail_message,
)


def encoded(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).decode()


def gmail_message() -> dict:
    return {
        "id": "gmail-1",
        "threadId": "thread-1",
        "internalDate": "1735689600000",
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [
                {"name": "From", "value": "Customer <customer@example.com>"},
                {"name": "To", "value": "support@example.com"},
                {"name": "Subject", "value": "Cannot log in"},
                {"name": "Message-ID", "value": "<message-1@example.com>"},
                {"name": "In-Reply-To", "value": "<message-0@example.com>"},
                {
                    "name": "References",
                    "value": "<message-root@example.com> <message-0@example.com>",
                },
            ],
            "parts": [
                {"mimeType": "text/plain", "body": {"data": encoded("Please help me log in.")}},
                {"mimeType": "text/html", "body": {"data": encoded("<p>HTML fallback</p>")}},
            ],
        },
    }


def test_parse_gmail_message_prefers_plain_text() -> None:
    email = parse_gmail_message(gmail_message())

    assert email.message_id == "gmail-1"
    assert email.thread_id == "thread-1"
    assert email.sender == "customer@example.com"
    assert email.body == "Please help me log in."
    assert email.references == ["<message-root@example.com>", "<message-0@example.com>"]


class FakeRequest:
    def __init__(self, response: dict) -> None:
        self.response = response

    def execute(self) -> dict:
        return self.response


class FakeHistory:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def list(self, **kwargs: dict) -> FakeRequest:
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            return FakeRequest(
                {
                    "history": [
                        {"messagesAdded": [{"message": {"id": "m-1"}}]},
                        {"messagesAdded": [{"message": {"id": "m-1"}}, {"message": {"id": "m-2"}}]},
                    ],
                    "historyId": "205",
                    "nextPageToken": "next",
                }
            )
        return FakeRequest(
            {
                "history": [{"messagesAdded": [{"message": {"id": "m-3"}}]}],
                "historyId": "207",
            }
        )


class FakeMessages:
    def __init__(self, message: dict) -> None:
        self.message = message
        self.sent_body: dict | None = None

    def get(self, **kwargs: dict) -> FakeRequest:
        return FakeRequest(self.message)

    def send(self, **kwargs: dict) -> FakeRequest:
        self.sent_body = kwargs["body"]
        return FakeRequest({"id": "sent-1"})


class FakeUsers:
    def __init__(self, message: dict) -> None:
        self.history_resource = FakeHistory()
        self.messages_resource = FakeMessages(message)

    def history(self) -> FakeHistory:
        return self.history_resource

    def messages(self) -> FakeMessages:
        return self.messages_resource


class FakeGmailService:
    def __init__(self, message: dict) -> None:
        self.users_resource = FakeUsers(message)

    def users(self) -> FakeUsers:
        return self.users_resource


def test_gateway_lists_paginated_unique_ids_and_sends_threaded_reply() -> None:
    service = FakeGmailService(gmail_message())
    gateway = GmailGateway(service)

    batch = gateway.list_new_message_ids("100")

    assert batch.message_ids == ("m-1", "m-2", "m-3")
    assert batch.history_id == "207"
    assert all(
        call["labelId"] == "INBOX"
        for call in service.users_resource.history_resource.calls
    )
    sent_id = gateway.send_reply(parse_gmail_message(gmail_message()), "Resolved.")

    assert sent_id == "sent-1"
    raw = service.users_resource.messages_resource.sent_body["raw"]
    reply = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(raw))
    assert reply["Subject"] == "Re: Cannot log in"
    assert reply["In-Reply-To"] == "<message-1@example.com>"
    assert reply["References"] == "<message-root@example.com> <message-0@example.com> <message-1@example.com>"
    assert reply.get_content().strip() == "Resolved."


class MemoryCursor:
    def __init__(self) -> None:
        self.value: str | None = None

    def get(self, email_address: str) -> str | None:
        return self.value

    def set(self, email_address: str, history_id: str) -> None:
        self.value = history_id


def test_notification_processor_initializes_cursor_then_processes_changes() -> None:
    cursor = MemoryCursor()
    processed: list[str] = []
    gateway = GmailGateway(FakeGmailService(gmail_message()))
    processor = GmailNotificationProcessor(gateway, cursor, lambda email: processed.append(email.message_id))

    assert processor.process("support@example.com", "200") == 0
    assert processor.process("support@example.com", "201") == 3
    assert processed == ["gmail-1", "gmail-1", "gmail-1"]
    assert cursor.value == "207"


def test_notification_processor_skips_messages_sent_by_watched_mailbox() -> None:
    cursor = MemoryCursor()
    cursor.value = "200"
    message = gmail_message()
    message["payload"]["headers"][0]["value"] = "Support <SUPPORT@example.com>"
    processed: list[str] = []
    processor = GmailNotificationProcessor(
        GmailGateway(FakeGmailService(message)),
        cursor,
        lambda email: processed.append(email.message_id),
    )

    assert processor.process("support@example.com", "201") == 0
    assert processed == []
    assert cursor.value == "207"


def test_notification_processor_ignores_duplicate_notification_before_history_call() -> None:
    cursor = MemoryCursor()
    cursor.value = "201"
    service = FakeGmailService(gmail_message())
    processor = GmailNotificationProcessor(
        GmailGateway(service),
        cursor,
        lambda email: None,
    )

    assert processor.process("support@example.com", "201") == 0
    assert service.users_resource.history_resource.calls == []
    assert cursor.value == "201"


def test_notification_processor_ignores_out_of_order_notification_before_history_call() -> None:
    cursor = MemoryCursor()
    cursor.value = "205"
    service = FakeGmailService(gmail_message())
    processor = GmailNotificationProcessor(GmailGateway(service), cursor, lambda email: None)

    assert processor.process("support@example.com", "203") == 0
    assert service.users_resource.history_resource.calls == []
    assert cursor.value == "205"


def test_notification_processor_skips_gmail_plus_alias_from_watched_mailbox() -> None:
    cursor = MemoryCursor()
    cursor.value = "200"
    message = gmail_message()
    message["payload"]["headers"][0]["value"] = (
        "Support <supp.ort+automated@googlemail.com>"
    )
    processed: list[str] = []
    processor = GmailNotificationProcessor(
        GmailGateway(FakeGmailService(message)),
        cursor,
        lambda email: processed.append(email.message_id),
    )

    assert processor.process("support@gmail.com", "201") == 0
    assert processed == []
    assert cursor.value == "207"


def test_pubsub_envelope_schema_requires_data_and_subscription() -> None:
    payload = {"emailAddress": "support@example.com", "historyId": "201"}
    envelope = GmailPubSubEnvelope(
        message={"data": encoded(json.dumps(payload))},
        subscription="projects/example/subscriptions/gmail",
    )

    assert envelope.subscription.endswith("gmail")


def test_gmail_webhook_invokes_configured_processor(client) -> None:
    class FakeProcessor:
        def process(self, email_address: str, history_id: str) -> int:
            assert email_address == "support@example.com"
            assert history_id == "201"
            return 2

    client.app.state.gmail_processor = FakeProcessor()
    notification = encoded(
        json.dumps(
            {
                "emailAddress": "support@example.com",
                "historyId": "201",
            }
        )
    )

    response = client.post(
        "/webhooks/gmail",
        json={
            "message": {"data": notification},
            "subscription": "projects/example/subscriptions/gmail",
        },
    )

    assert response.status_code == 202
    assert response.json() == {
        "accepted": True,
        "processed_messages": 2,
    }


def test_gmail_webhook_normalizes_numeric_history_id(client) -> None:
    class FakeProcessor:
        def process(self, email_address: str, history_id: str) -> int:
            assert email_address == "support@example.com"
            assert history_id == "123456789"
            return 0

    client.app.state.gmail_processor = FakeProcessor()
    notification = encoded(
        json.dumps(
            {
                "emailAddress": "support@example.com",
                "historyId": 123456789,
            }
        )
    )

    response = client.post(
        "/webhooks/gmail",
        json={
            "message": {"data": notification},
            "subscription": "projects/example/subscriptions/gmail",
        },
    )

    assert response.status_code == 202
    assert response.json() == {"accepted": True, "processed_messages": 0}


def test_gmail_webhook_rejects_invalid_data(client) -> None:
    response = client.post(
        "/webhooks/gmail",
        json={
            "message": {"data": "not-base64"},
            "subscription": "projects/example/subscriptions/gmail",
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid Gmail Pub/Sub message data"


def test_gmail_webhook_rejects_non_numeric_history_id(client) -> None:
    notification = encoded(
        json.dumps(
            {
                "emailAddress": "support@example.com",
                "historyId": "not-a-history-id",
            }
        )
    )

    response = client.post(
        "/webhooks/gmail",
        json={
            "message": {"data": notification},
            "subscription": "projects/example/subscriptions/gmail",
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid Gmail Pub/Sub message data"


def test_gmail_webhook_requires_processor(client) -> None:
    notification = encoded(
        json.dumps(
            {
                "emailAddress": "support@example.com",
                "historyId": "201",
            }
        )
    )

    response = client.post(
        "/webhooks/gmail",
        json={
            "message": {"data": notification},
            "subscription": "projects/example/subscriptions/gmail",
        },
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "Gmail processor is not configured"
