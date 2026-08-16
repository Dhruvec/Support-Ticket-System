from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Annotated, Any

from cryptography.fernet import Fernet
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from pydantic import EmailStr, TypeAdapter, ValidationError

from app.config import Settings
from app.database.gmail_mailbox_store import GmailMailboxStore
from app.integrations.gmail import (
    authorization_url,
    build_oauth_flow,
    exchange_authorization_code,
)
from app.security import require_operator

router = APIRouter(prefix="/auth/google", tags=["gmail-auth"])
_email_adapter = TypeAdapter(EmailStr)


def _oauth_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    required = {
        "GOOGLE_CLIENT_ID": settings.google_client_id,
        "GOOGLE_CLIENT_SECRET": settings.google_client_secret,
        "GOOGLE_REDIRECT_URI": settings.google_redirect_uri,
        "GOOGLE_OAUTH_STATE_SECRET": settings.google_oauth_state_secret,
        "GMAIL_CREDENTIALS_ENCRYPTION_KEY": (
            settings.gmail_credentials_encryption_key
        ),
    }
    missing = [name for name, value in required.items() if not value or not value.strip()]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Google OAuth is not configured; missing environment variables: "
                + ", ".join(missing)
            ),
        )
    try:
        Fernet(settings.gmail_credentials_encryption_key.strip().encode("ascii"))
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Gmail credential encryption is not configured",
        ) from exc
    return settings


def _encode_state(email_address: str, secret: str, *, issued_at: int | None = None) -> str:
    payload = json.dumps(
        {"email_address": email_address, "issued_at": issued_at or int(time.time())},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()
    return _urlsafe_encode(payload + signature)


def _decode_state(state: str, secret: str, *, ttl_seconds: int) -> str:
    try:
        signed = _urlsafe_decode(state)
        payload, signature = signed[:-32], signed[-32:]
        expected = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()
        if len(signature) != 32 or not hmac.compare_digest(signature, expected):
            raise ValueError("invalid signature")
        data: dict[str, Any] = json.loads(payload)
        email_address = str(_email_adapter.validate_python(data["email_address"]))
        issued_at = int(data["issued_at"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, ValidationError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid Google OAuth state",
        ) from exc

    age = int(time.time()) - issued_at
    if age < 0 or age > ttl_seconds:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Google OAuth state has expired",
        )
    return email_address


def _urlsafe_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _urlsafe_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


login_router = APIRouter(tags=["gmail-auth"])


@login_router.get("/auth/login", dependencies=[Depends(require_operator)])
def google_oauth_login_alias(
    email_address: Annotated[
        EmailStr,
        Query(description="Support mailbox to associate with the OAuth grant"),
    ],
) -> RedirectResponse:
    return RedirectResponse(
        url=f"/auth/google?email_address={email_address}",
        status_code=status.HTTP_307_TEMPORARY_REDIRECT,
    )


@router.delete("/mailbox", dependencies=[Depends(require_operator)])
def delete_google_mailbox(
    request: Request,
    email_address: Annotated[
        EmailStr,
        Query(description="Mailbox whose stale credentials should be removed"),
    ],
) -> dict[str, str]:
    settings: Settings = request.app.state.settings
    deleted = GmailMailboxStore(
        request.app.state.session_factory,
        encryption_key=settings.gmail_credentials_encryption_key,
    ).delete(str(email_address))
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No Gmail mailbox configured for {email_address}",
        )
    return {"status": "deleted", "email_address": str(email_address)}


@router.get("", dependencies=[Depends(require_operator)])
def begin_google_oauth(
    request: Request,
    email_address: Annotated[
        EmailStr,
        Query(description="Support mailbox to associate with the OAuth grant"),
    ],
) -> dict[str, str]:
    settings = _oauth_settings(request)
    flow = build_oauth_flow(
        client_id=settings.google_client_id or "",
        client_secret=settings.google_client_secret or "",
        redirect_uri=settings.google_redirect_uri or "",
    )
    state = _encode_state(
        str(email_address),
        settings.google_oauth_state_secret or "",
    )
    return {"authorization_url": authorization_url(flow, state=state)}


@router.get("/callback")
def complete_google_oauth(
    request: Request,
    code: str = Query(min_length=1),
    state: str = Query(min_length=1),
) -> dict[str, str]:
    settings = _oauth_settings(request)
    email_address = _decode_state(
        state,
        settings.google_oauth_state_secret or "",
        ttl_seconds=settings.google_oauth_state_ttl_seconds,
    )
    flow = build_oauth_flow(
        client_id=settings.google_client_id or "",
        client_secret=settings.google_client_secret or "",
        redirect_uri=settings.google_redirect_uri or "",
    )
    credentials = exchange_authorization_code(flow, code=code)
    GmailMailboxStore(
        request.app.state.session_factory,
        encryption_key=settings.gmail_credentials_encryption_key,
    ).set_credentials(
        email_address,
        credentials,
    )
    return {"status": "connected", "email_address": email_address}
