from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app import tasks
from app.database.models import OutboundDelivery, OutboundDeliveryStatus
from app.services.outbound_delivery_service import OutboundDeliveryService


@pytest.mark.parametrize(
    ("job_type", "queue", "ticket_number"),
    [
        ("gmail_send", "gmail_outbound", "TKT-1"),
        ("slack_escalation", "slack_outbound", "TKT-2"),
    ],
)
def test_enqueue_external_job_persists_provider_safe_delivery_and_isolated_queue(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
    job_type: str,
    queue: str,
    ticket_number: str,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_apply_async(
        *,
        args: tuple[str],
        queue: str,
        task_id: str,
    ) -> None:
        calls.append({"args": args, "queue": queue, "task_id": task_id})

    monkeypatch.setattr(tasks, "SessionLocal", session_factory)
    monkeypatch.setattr(tasks.execute_external_job, "apply_async", fake_apply_async)
    payload = {
        "_source_message_id": f"{job_type}-message-1",
        "_operation_version": "deterministic-policy-v1",
        "ticket_number": ticket_number,
        "body": "Provider-visible content",
    }

    returned_task_id = tasks.enqueue_external_job(job_type, payload)

    assert len(calls) == 1
    call = calls[0]
    assert call["queue"] == queue
    assert call["task_id"] == returned_task_id
    correlation_id = call["args"][0]
    assert call["args"] == (correlation_id,)

    with session_factory() as session:
        delivery = session.scalar(select(OutboundDelivery))
        assert delivery is not None
        assert delivery.correlation_id == correlation_id
        assert delivery.celery_task_id == returned_task_id
        assert delivery.job_type == job_type
        assert delivery.source_message_id == f"{job_type}-message-1"
        assert delivery.operation_version == "deterministic-policy-v1"
        assert delivery.payload == {
            "ticket_number": ticket_number,
            "body": "Provider-visible content",
        }
        assert delivery.status == OutboundDeliveryStatus.QUEUED.value


def test_enqueue_external_job_replay_returns_existing_task_without_republishing(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_apply_async(
        *,
        args: tuple[str],
        queue: str,
        task_id: str,
    ) -> None:
        calls.append({"args": args, "queue": queue, "task_id": task_id})

    monkeypatch.setattr(tasks, "SessionLocal", session_factory)
    monkeypatch.setattr(tasks.execute_external_job, "apply_async", fake_apply_async)
    payload = {
        "_source_message_id": "gmail-message-1",
        "_operation_version": "deterministic-policy-v1",
        "ticket_number": "TKT-1",
        "body": "A grounded response",
    }

    first_task_id = tasks.enqueue_external_job("gmail_send", payload)
    replay_task_id = tasks.enqueue_external_job("gmail_send", payload)

    assert replay_task_id == first_task_id
    assert len(calls) == 1
    with session_factory() as session:
        assert session.scalar(select(func.count(OutboundDelivery.id))) == 1


def test_enqueue_external_job_can_republish_after_broker_failure(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
) -> None:
    publish_attempts: list[str] = []

    def fake_apply_async(
        *,
        args: tuple[str],
        queue: str,
        task_id: str,
    ) -> None:
        publish_attempts.append(task_id)
        if len(publish_attempts) == 1:
            raise ConnectionError("broker unavailable")

    monkeypatch.setattr(tasks, "SessionLocal", session_factory)
    monkeypatch.setattr(tasks.execute_external_job, "apply_async", fake_apply_async)
    payload = {
        "_source_message_id": "gmail-message-1",
        "_operation_version": "deterministic-policy-v1",
        "ticket_number": "TKT-1",
    }

    with pytest.raises(ConnectionError, match="broker unavailable"):
        tasks.enqueue_external_job("gmail_send", payload)

    with session_factory() as session:
        failed = session.scalar(select(OutboundDelivery))
        assert failed is not None
        correlation_id = failed.correlation_id
        assert failed.status == OutboundDeliveryStatus.ENQUEUE_FAILED.value
        assert failed.celery_task_id is None

    returned_task_id = tasks.enqueue_external_job("gmail_send", payload)

    assert len(publish_attempts) == 2
    assert returned_task_id == publish_attempts[1]
    assert publish_attempts[1] != publish_attempts[0]
    with session_factory() as session:
        delivery = OutboundDeliveryService(session).get(correlation_id)
        assert delivery is not None
        assert delivery.status == OutboundDeliveryStatus.QUEUED.value
        assert delivery.celery_task_id == returned_task_id


def test_worker_loads_durable_payload_and_completes_delivery(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
) -> None:
    handled_payloads: list[dict[str, Any]] = []

    def handler(payload: dict[str, Any]) -> str:
        handled_payloads.append(payload)
        return "provider-message-1"

    monkeypatch.setattr(tasks, "SessionLocal", session_factory)
    monkeypatch.setitem(tasks._job_handlers, "gmail_send", handler)
    with session_factory() as session:
        service = OutboundDeliveryService(session)
        delivery = service.create_or_get(
            job_type="gmail_send",
            source_message_id="gmail-message-1",
            operation_version="deterministic-policy-v1",
            payload={"ticket_number": "TKT-1", "body": "Reply body"},
        )
        correlation_id = delivery.correlation_id
        assert service.claim_for_enqueue(correlation_id, "task-1") is True

    tasks.execute_external_job.run(correlation_id)
    tasks.execute_external_job.run(correlation_id)

    assert handled_payloads == [{"ticket_number": "TKT-1", "body": "Reply body"}]
    with session_factory() as session:
        saved = OutboundDeliveryService(session).get(correlation_id)
        assert saved is not None
        assert saved.status == OutboundDeliveryStatus.SUCCEEDED.value
        assert saved.attempts == 1
        assert saved.provider_message_id == "provider-message-1"


def test_duplicate_worker_claim_does_not_invoke_or_mutate_active_delivery(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
) -> None:
    handler_calls = 0

    def handler(payload: dict[str, Any]) -> None:
        nonlocal handler_calls
        handler_calls += 1

    monkeypatch.setattr(tasks, "SessionLocal", session_factory)
    monkeypatch.setitem(tasks._job_handlers, "gmail_send", handler)
    with session_factory() as session:
        service = OutboundDeliveryService(session)
        delivery = service.create_or_get(
            job_type="gmail_send",
            source_message_id="gmail-message-1",
            operation_version="deterministic-policy-v1",
            payload={"ticket_number": "TKT-1"},
        )
        correlation_id = delivery.correlation_id
        assert service.claim_for_enqueue(correlation_id, "task-1") is True
        assert service.claim_attempt(
            correlation_id,
            "task-1",
            expected_attempts=0,
        ) is True

    tasks.execute_external_job.run(correlation_id)

    assert handler_calls == 0
    with session_factory() as session:
        saved = OutboundDeliveryService(session).get(correlation_id)
        assert saved is not None
        assert saved.status == OutboundDeliveryStatus.IN_PROGRESS.value
        assert saved.attempts == 1


def test_enqueue_external_job_rejects_unknown_job_type() -> None:
    with pytest.raises(ValueError, match="No queue is configured"):
        tasks.enqueue_external_job("unknown", {})
