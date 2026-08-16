from datetime import datetime
from typing import NotRequired, TypedDict

from app.schemas.email import Category, Sentiment, Urgency


class TicketState(TypedDict):
    email_id: str
    thread_id: str
    sender: str
    recipient: str
    subject: str
    body: str
    timestamp: datetime
    message_id_header: str | None
    in_reply_to: str | None
    references: list[str]

    customer_name: NotRequired[str | None]
    product: NotRequired[str | None]
    summary: NotRequired[str | None]
    category: NotRequired[Category | None]
    urgency: NotRequired[Urgency | None]
    sentiment: NotRequired[Sentiment | None]
    classification_confidence: NotRequired[float | None]

    ticket_id: NotRequired[str | None]
    is_existing_ticket: NotRequired[bool]
    duplicate_message: NotRequired[bool]
    resolution_method: NotRequired[str | None]
    faq_match: NotRequired[str | None]
    faq_confidence: NotRequired[float | None]
    action: NotRequired[str | None]
    decision_reason: NotRequired[str | None]
    response_body: NotRequired[str | None]
    error: NotRequired[str | None]
