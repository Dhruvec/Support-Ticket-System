from datetime import datetime
from typing import Literal

from pydantic import BaseModel, EmailStr, Field

Category = Literal["bug", "billing", "feature_request", "account", "technical_support", "other"]
Urgency = Literal["low", "medium", "high", "critical"]
Sentiment = Literal["positive", "neutral", "negative", "frustrated"]


class IncomingEmail(BaseModel):
    message_id: str = Field(min_length=1, max_length=255)
    thread_id: str = Field(min_length=1, max_length=255)
    sender: EmailStr
    recipient: EmailStr
    subject: str = Field(default="(no subject)", max_length=998)
    body: str = Field(min_length=1)
    timestamp: datetime
    message_id_header: str | None = Field(default=None, max_length=998)
    in_reply_to: str | None = Field(default=None, max_length=998)
    references: list[str] = Field(default_factory=list)


class EmailClassification(BaseModel):
    category: Category
    urgency: Urgency
    sentiment: Sentiment
    customer_name: str | None = Field(default=None, max_length=255)
    product: str | None = Field(default=None, max_length=255)
    summary: str = Field(min_length=1, max_length=2000)
    confidence: float = Field(ge=0.0, le=1.0)
