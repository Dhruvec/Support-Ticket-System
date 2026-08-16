from __future__ import annotations

import secrets
from collections.abc import Mapping
from typing import Annotated, Any

from fastapi import HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import id_token

from app.config import Settings

_OPERATOR_HEADER_NAME = "X-Operator-Key"
_GOOGLE_ISSUERS = frozenset({"accounts.google.com", "https://accounts.google.com"})
_operator_key = APIKeyHeader(name=_OPERATOR_HEADER_NAME, auto_error=False)
_bearer_token = HTTPBearer(auto_error=False)


def require_operator(
    request: Request,
    supplied_key: Annotated[str | None, Security(_operator_key)] = None,
) -> str:
    """Authenticate a support operator when operator authentication is configured."""
    settings: Settings = request.app.state.settings
    expected_key = settings.operator_api_key
    if not expected_key:
        if settings.app_env != "production":
            return "development-operator"
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Operator authentication is not configured",
        )

    if not supplied_key or not secrets.compare_digest(
        supplied_key.encode("utf-8"),
        expected_key.encode("utf-8"),
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing operator credentials",
        )
    return "operator"


def require_google_pubsub_identity(
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Security(_bearer_token),
    ] = None,
) -> Mapping[str, Any]:
    """Verify the Google OIDC identity attached to a Pub/Sub push request."""
    settings: Settings = request.app.state.settings
    audience = settings.google_pubsub_audience
    expected_email = settings.google_pubsub_service_account

    if not audience and not expected_email and settings.app_env != "production":
        return {}
    if not audience or not expected_email:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Google Pub/Sub authentication is not configured",
        )
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _pubsub_authentication_error()

    try:
        claims = _verify_google_token(credentials.credentials, audience)
    except (GoogleAuthError, ValueError) as exc:
        raise _pubsub_authentication_error() from exc

    if claims.get("iss") not in _GOOGLE_ISSUERS:
        raise _pubsub_authentication_error()
    if claims.get("email_verified") is not True:
        raise _pubsub_authentication_error()

    token_email = claims.get("email")
    if not isinstance(token_email, str) or token_email.casefold() != expected_email.casefold():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Pub/Sub caller is not authorized",
        )
    return claims


def _verify_google_token(token: str, audience: str) -> Mapping[str, Any]:
    claims = id_token.verify_oauth2_token(
        token,
        GoogleAuthRequest(),
        audience=audience,
    )
    return claims


def _pubsub_authentication_error() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or missing Pub/Sub identity token",
        headers={"WWW-Authenticate": "Bearer"},
    )
