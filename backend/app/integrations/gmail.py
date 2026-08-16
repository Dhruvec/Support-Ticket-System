import base64
import html
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email import policy
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Any, Protocol

from google.auth.credentials import Credentials
from google.oauth2.credentials import Credentials as OAuthCredentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

from app.schemas.email import IncomingEmail

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]


class HistoryCursorStore(Protocol):
    def get(self, email_address: str) -> str | None: ...

    def set(self, email_address: str, history_id: str) -> None: ...


@dataclass(frozen=True)
class GmailHistoryBatch:
    message_ids: tuple[str, ...]
    history_id: str


class GmailGateway:

    def __init__(self, service: Any, *, user_id: str = "me") -> None:
        self.service = service
        self.user_id = user_id

    def watch(self, topic_name: str) -> dict[str, Any]:
        response = (
            self.service.users()
            .watch(
                userId=self.user_id,
                body={
                    "topicName": topic_name,
                    "labelIds": ["INBOX"],
                    "labelFilterBehavior": "INCLUDE",
                },
            )
            .execute()
        )
        return response

    def list_new_message_ids(self, start_history_id: str) -> GmailHistoryBatch:
        message_ids: list[str] = []
        seen_message_ids: set[str] = set()
        terminal_history_id: str | None = None
        page_token: str | None = None
        while True:
            request = self.service.users().history().list(
                userId=self.user_id,
                startHistoryId=start_history_id,
                historyTypes=["messageAdded"],
                labelId="INBOX",
                pageToken=page_token,
            )
            response = request.execute()
            response_history_id = response.get("historyId")
            if response_history_id is not None:
                candidate = str(response_history_id)
                _history_id_as_int(candidate)
                if (
                    terminal_history_id is None
                    or _history_id_as_int(candidate)
                    > _history_id_as_int(terminal_history_id)
                ):
                    terminal_history_id = candidate
            for history in response.get("history", []):
                for addition in history.get("messagesAdded", []):
                    message_id = addition.get("message", {}).get("id")
                    if message_id and message_id not in seen_message_ids:
                        seen_message_ids.add(message_id)
                        message_ids.append(message_id)
            page_token = response.get("nextPageToken")
            if not page_token:
                if terminal_history_id is None:
                    raise ValueError(
                        "Gmail History response did not include a terminal historyId"
                    )
                return GmailHistoryBatch(
                    message_ids=tuple(message_ids),
                    history_id=terminal_history_id,
                )

    def get_message(self, message_id: str) -> IncomingEmail:
        message = (
            self.service.users()
            .messages()
            .get(userId=self.user_id, id=message_id, format="full")
            .execute()
        )
        return parse_gmail_message(message)

    def send_reply(self, original: IncomingEmail, body: str) -> str:
        message = EmailMessage(policy=policy.SMTP.clone(max_line_length=998))
        message["To"] = str(original.sender)
        message["Subject"] = _reply_subject(original.subject)
        if original.message_id_header:
            message["In-Reply-To"] = original.message_id_header
            references = [*original.references, original.message_id_header]
            message["References"] = " ".join(dict.fromkeys(references))
        message.set_content(body)
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
        response = (
            self.service.users()
            .messages()
            .send(
                userId=self.user_id,
                body={"raw": raw, "threadId": original.thread_id},
            )
            .execute()
        )
        return str(response["id"])


class GmailNotificationProcessor:
    def __init__(
        self,
        gateway: GmailGateway,
        cursor_store: HistoryCursorStore,
        process_email: Callable[[IncomingEmail], None],
    ) -> None:
        self.gateway = gateway
        self.cursor_store = cursor_store
        self.process_email = process_email

    def process(self, email_address: str, history_id: str) -> int:
        notification_history_value = _history_id_as_int(history_id)
        previous_history_id = self.cursor_store.get(email_address)
        if previous_history_id is None:
            self.cursor_store.set(email_address, history_id)
            return 0

        previous_history_value = _history_id_as_int(previous_history_id)
        if notification_history_value <= previous_history_value:
            return 0

        batch = self.gateway.list_new_message_ids(previous_history_id)
        processed_messages = 0
        for message_id in batch.message_ids:
            email = self.gateway.get_message(message_id)
            if _mailbox_identity(email.sender) == _mailbox_identity(email_address):
                continue
            self.process_email(email)
            processed_messages += 1

        terminal_history_id = max(
            (previous_history_id, history_id, batch.history_id),
            key=_history_id_as_int,
        )
        self.cursor_store.set(email_address, terminal_history_id)
        return processed_messages


def parse_gmail_message(message: dict[str, Any]) -> IncomingEmail:
    payload = message.get("payload", {})
    headers = {
        item["name"].lower(): item.get("value", "")
        for item in payload.get("headers", [])
        if item.get("name")
    }
    sender = parseaddr(headers.get("from", ""))[1]
    recipient = parseaddr(headers.get("to", ""))[1]
    body = _extract_body(payload)
    references = headers.get("references", "").split()
    internal_date = int(message.get("internalDate", "0"))

    return IncomingEmail(
        message_id=str(message["id"]),
        thread_id=str(message["threadId"]),
        sender=sender,
        recipient=recipient,
        subject=headers.get("subject") or "(no subject)",
        body=body,
        timestamp=datetime.fromtimestamp(internal_date / 1000, tz=UTC),
        message_id_header=headers.get("message-id") or None,
        in_reply_to=headers.get("in-reply-to") or None,
        references=references,
    )


def build_oauth_flow(
    *,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
) -> Flow:
    client_config = {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [redirect_uri],
        }
    }
    flow = Flow.from_client_config(client_config, scopes=GMAIL_SCOPES)
    flow.redirect_uri = redirect_uri
    return flow


def authorization_url(flow: Flow, *, state: str) -> str:
    url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        state=state,
    )
    return url


def exchange_authorization_code(flow: Flow, *, code: str) -> dict[str, Any]:
    flow.fetch_token(code=code)
    credentials = flow.credentials
    return {
        "token": credentials.token,
        "refresh_token": credentials.refresh_token,
        "token_uri": credentials.token_uri,
        "client_id": credentials.client_id,
        "client_secret": credentials.client_secret,
        "scopes": list(credentials.scopes or GMAIL_SCOPES),
    }


def credentials_from_json(data: dict[str, Any]) -> Credentials:
    """Rebuild refreshable Google credentials from persisted OAuth JSON."""
    required = ("token_uri", "client_id", "client_secret")
    missing = [key for key in required if not data.get(key)]
    if missing:
        raise ValueError(f"Stored Gmail credentials are missing: {', '.join(missing)}")
    return OAuthCredentials(
        token=data.get("token"),
        refresh_token=data.get("refresh_token"),
        token_uri=str(data["token_uri"]),
        client_id=str(data["client_id"]),
        client_secret=str(data["client_secret"]),
        scopes=list(data.get("scopes") or GMAIL_SCOPES),
    )


def build_gmail_service(credentials_data: dict[str, Any]) -> Any:
    """Create a Gmail API client using credentials loaded at execution time."""
    credentials = credentials_from_json(credentials_data)
    return build("gmail", "v1", credentials=credentials, cache_discovery=False)


def _extract_body(payload: dict[str, Any]) -> str:
    plain_parts: list[str] = []
    html_parts: list[str] = []

    def visit(part: dict[str, Any]) -> None:
        mime_type = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if data:
            text = _decode(data)
            if mime_type == "text/plain":
                plain_parts.append(text)
            elif mime_type == "text/html":
                html_parts.append(text)
        for child in part.get("parts", []):
            visit(child)

    visit(payload)
    if plain_parts:
        return "\n".join(part.strip() for part in plain_parts if part.strip())
    if html_parts:
        rendered = "\n".join(html_parts)
        return " ".join(html.unescape(_strip_html(rendered)).split())
    raise ValueError("Gmail message does not contain a supported text body")


def _decode(data: str) -> str:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding).decode("utf-8", errors="replace")


def _strip_html(value: str) -> str:
    inside_tag = False
    output: list[str] = []
    for character in value:
        if character == "<":
            inside_tag = True
        elif character == ">":
            inside_tag = False
            output.append(" ")
        elif not inside_tag:
            output.append(character)
    return "".join(output)


def _reply_subject(subject: str) -> str:
    return subject if subject.lower().startswith("re:") else f"Re: {subject}"


def _history_id_as_int(history_id: object) -> int:
    value = str(history_id)
    if not value.isdecimal():
        raise ValueError("Gmail history ID must contain only decimal digits")
    return int(value)


def _mailbox_identity(address: object) -> str:
    """Normalize Gmail plus-addresses to the watched mailbox identity."""
    local_part, separator, domain = str(address).casefold().partition("@")
    if not separator:
        return str(address).casefold()
    if domain in {"gmail.com", "googlemail.com"}:
        local_part = local_part.split("+", 1)[0].replace(".", "")
        domain = "gmail.com"
    return f"{local_part}@{domain}"
