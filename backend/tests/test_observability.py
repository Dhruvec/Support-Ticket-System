import json
import logging

from fastapi.testclient import TestClient

from app.main import create_app
from app.observability import (
    JsonFormatter,
    normalize_request_id,
    reset_request_id,
    set_request_id,
)


def test_normalize_request_id_preserves_valid_value() -> None:
    assert normalize_request_id("operator.request-42") == "operator.request-42"


def test_normalize_request_id_generates_value_for_invalid_input() -> None:
    generated = normalize_request_id("contains spaces")

    assert len(generated) == 36
    assert normalize_request_id(None) != generated


def test_request_logging_returns_request_id_and_structured_fields(
    caplog,
) -> None:
    application = create_app()

    with (
        TestClient(application) as client,
        caplog.at_level(logging.INFO, logger="app.request"),
    ):
        response = client.get("/health", headers={"X-Request-ID": "request-123"})

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "request-123"
    request_record = next(
        record for record in caplog.records if record.name == "app.request"
    )
    assert request_record.request_id == "request-123"
    assert request_record.http_method == "GET"
    assert request_record.http_path == "/health"
    assert request_record.http_status == 200
    assert request_record.duration_ms >= 0


def test_request_logging_generates_response_id_for_invalid_header() -> None:
    application = create_app()

    with TestClient(application) as client:
        response = client.get("/health", headers={"X-Request-ID": "bad value"})

    generated = response.headers["X-Request-ID"]
    assert generated != "bad value"
    assert len(generated) == 36


def test_json_formatter_includes_context_and_exception() -> None:
    token = set_request_id("celery-correlation-1")
    try:
        try:
            raise RuntimeError("provider unavailable")
        except RuntimeError:
            record = logging.getLogger("test").makeRecord(
                "test",
                logging.ERROR,
                __file__,
                1,
                "delivery_failed",
                (),
                __import__("sys").exc_info(),
            )
        payload = json.loads(JsonFormatter().format(record))
    finally:
        reset_request_id(token)

    assert payload["request_id"] == "celery-correlation-1"
    assert payload["message"] == "delivery_failed"
    assert "provider unavailable" in payload["exception"]


def test_request_context_is_reset_after_task_context() -> None:
    token = set_request_id("task-correlation-1")
    reset_request_id(token)

    record = logging.getLogger("test").makeRecord(
        "test", logging.INFO, __file__, 1, "after_task", (), None
    )
    payload = json.loads(JsonFormatter().format(record))

    assert "request_id" not in payload
