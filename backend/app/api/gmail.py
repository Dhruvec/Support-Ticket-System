import base64
import binascii
import json
from typing import Any, Protocol

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator

from app.security import require_google_pubsub_identity


class GmailWebhookProcessor(Protocol):
    def process(self, email_address: str, history_id: str) -> int: ...


class PubSubMessage(BaseModel):
    data: str = Field(min_length=1)


class GmailPubSubEnvelope(BaseModel):
    message: PubSubMessage
    subscription: str = Field(min_length=1)


class GmailNotification(BaseModel):
    email_address: str = Field(alias="emailAddress", min_length=1)
    history_id: str | int = Field(alias="historyId")

    @field_validator("history_id", mode="before")
    @classmethod
    def normalize_history_id(cls, value: object) -> str:
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise TypeError("historyId must be a string or integer")
        normalized = str(value)
        if not normalized.isdecimal():
            raise ValueError("historyId must contain only decimal digits")
        return normalized


router = APIRouter(
    prefix="/webhooks",
    tags=["webhooks"],
    dependencies=[Depends(require_google_pubsub_identity)],
)


@router.post("/gmail", status_code=status.HTTP_202_ACCEPTED)
def gmail_webhook(
    envelope: GmailPubSubEnvelope,
    request: Request,
) -> dict[str, Any]:
    try:
        encoded_data = envelope.message.data
        padding = "=" * (-len(encoded_data) % 4)
        decoded = base64.urlsafe_b64decode(encoded_data + padding)
        notification = GmailNotification.model_validate(json.loads(decoded.decode("utf-8")))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid Gmail Pub/Sub message data",
        ) from exc

    email_address = notification.email_address
    history_id = str(notification.history_id)

    processor = getattr(request.app.state, "gmail_processor", None)
    if processor is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Gmail processor is not configured",
        )

    processed = processor.process(email_address, history_id)
    return {"accepted": True, "processed_messages": processed}
