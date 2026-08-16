from __future__ import annotations

import json
import logging
import re
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from time import perf_counter
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request, Response

_REQUEST_ID_HEADER = "X-Request-ID"
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_JSON_HANDLER_NAME = "support-ticket-json"
_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)


class JsonFormatter(logging.Formatter):
    """Serialize application logs as one JSON object per line."""

    _standard_attributes = frozenset(logging.makeLogRecord({}).__dict__)

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = getattr(record, "request_id", None) or _request_id.get()
        if request_id:
            payload["request_id"] = request_id

        for name, value in record.__dict__.items():
            if name not in self._standard_attributes and name not in {
                "message",
                "asctime",
                "request_id",
            }:
                payload[name] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, separators=(",", ":"))


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.set_name(_JSON_HANDLER_NAME)
    handler.setFormatter(JsonFormatter())
    root_logger = logging.getLogger()
    root_logger.handlers = [
        existing
        for existing in root_logger.handlers
        if existing.get_name() != _JSON_HANDLER_NAME
    ]
    root_logger.addHandler(handler)
    root_logger.setLevel(level.upper())


def install_request_logging(application: FastAPI) -> None:
    logger = logging.getLogger("app.request")

    @application.middleware("http")
    async def request_observability(request: Request, call_next: Any) -> Response:
        request_id = normalize_request_id(request.headers.get(_REQUEST_ID_HEADER))
        token = _request_id.set(request_id)
        started_at = perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            logger.exception(
                "request_failed",
                extra={
                    "request_id": request_id,
                    "http_method": request.method,
                    "http_path": request.url.path,
                    "duration_ms": round((perf_counter() - started_at) * 1000, 2),
                },
            )
            raise
        else:
            response.headers[_REQUEST_ID_HEADER] = request_id
            logger.info(
                "request_completed",
                extra={
                    "request_id": request_id,
                    "http_method": request.method,
                    "http_path": request.url.path,
                    "http_status": response.status_code,
                    "duration_ms": round((perf_counter() - started_at) * 1000, 2),
                },
            )
            return response
        finally:
            _request_id.reset(token)


def normalize_request_id(value: str | None) -> str:
    if value and _REQUEST_ID_PATTERN.fullmatch(value):
        return value
    return str(uuid4())


def set_request_id(value: str) -> Token[str | None]:
    """Set correlation context for non-HTTP execution such as Celery tasks."""
    return _request_id.set(normalize_request_id(value))


def reset_request_id(token: Token[str | None]) -> None:
    _request_id.reset(token)
