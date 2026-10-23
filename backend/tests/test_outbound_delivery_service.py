from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.database.models import OutboundDelivery, OutboundDeliveryStatus
from app.services.outbound_delivery_service import (
    OPERATION_VERSION_FIELD,
    SOURCE_MESSAGE_ID_FIELD,
    OutboundDeliveryConflictError,
    OutboundDeliveryService,
    split_delivery_payload,
)


def provider_payload(ticket_number: str = "TKT-1") -> dict[str, object]:
    return {
        "ticket_number": ticket_number,
        "body": "A grounded support response.",
    }


def create_delivery(
    session: Session,
    *,
    job_type: str = "gmail_send",
    source_message_id: str = "gmail-message-1",
    operation_version: str = "deterministic-policy-v1",
    payload: dict[str, object] | None = None,
) -> OutboundDelivery:
    return OutboundDeliveryService(session).create_or_get(
        job_type=job_type,
        source_message_id=source_message_id,
        operation_version=operation_version,
        payload=payload or provider_payload(),
    )


def test_split_delivery_payload_removes_internal_routing_metadata() -> None:
    payload = {
        SOURCE_MESSAGE_ID_FIELD: " gmail-message-1 ",
        OPERATION_VERSION_FIELD: " deterministic-policy-v1 ",
        "ticket_number": "TKT-1",
        "body": "Reply body",
    }

    split = split_delivery_payload(payload)

    assert split.source_message_id == "gmail-message-1"
    assert split.operation_version == "deterministic-policy-v1"
    assert split.provider_payload == {
        "ticket_number": "TKT-1",
        "body": "Reply body",
    }
    assert payload[SOURCE_MESSAGE_ID_FIELD] == " gmail-message-1 "


def test_create_or_get_reuses_one_deterministic_delivery(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        first = create_delivery(session)
        second = create_delivery(session)
        first_id = first.id
        first_correlation_id = first.correlation_id

    with session_factory() as session:
        assert session.scalar(select(func.count(OutboundDelivery.id))) == 1
        saved = session.get(OutboundDelivery, first_id)
        assert saved is not None
        assert second.id == first_id
        assert saved.correlation_id == first_correlation_id
        assert saved.status == OutboundDeliveryStatus.PENDING.value
        assert saved.attempts == 0


def test_create_or_get_rejects_changed_payload_for_same_operation(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        create_delivery(session)
        service = OutboundDeliveryService(session)

        try:
            service.create_or_get(
                job_type="gmail_send",
                source_message_id="gmail-message-1",
                operation_version="deterministic-policy-v1",
                payload={
                    "ticket_number": "TKT-1",
                    "body": "A different response.",
                },
            )
        except OutboundDeliveryConflictError as exc:
            assert "different outbound data" in str(exc)
        else:
            raise AssertionError("Expected conflicting replay payload to be rejected")


def test_independent_sessions_allow_only_one_enqueue_claim(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        correlation_id = create_delivery(session).correlation_id

    with session_factory() as first_session, session_factory() as second_session:
        first_service = OutboundDeliveryService(first_session)
        second_service = OutboundDeliveryService(second_session)

        assert first_service.claim_for_enqueue(correlation_id, "task-1") is True
        assert second_service.claim_for_enqueue(correlation_id, "task-2") is False

    with session_factory() as session:
        saved = OutboundDeliveryService(session).get(correlation_id)
        assert saved is not None
        assert saved.status == OutboundDeliveryStatus.QUEUED.value
        assert saved.celery_task_id == "task-1"


def test_independent_sessions_allow_only_one_worker_attempt_claim(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        service = OutboundDeliveryService(session)
        correlation_id = create_delivery(session).correlation_id
        assert service.claim_for_enqueue(correlation_id, "task-1") is True

    with session_factory() as first_session, session_factory() as second_session:
        first_service = OutboundDeliveryService(first_session)
        second_service = OutboundDeliveryService(second_session)

        assert (
            first_service.claim_attempt(
                correlation_id,
                "task-1",
                expected_attempts=0,
            )
            is True
        )
        assert (
            second_service.claim_attempt(
                correlation_id,
                "task-1",
                expected_attempts=0,
            )
            is False
        )

    with session_factory() as session:
        saved = OutboundDeliveryService(session).get(correlation_id)
        assert saved is not None
        assert saved.status == OutboundDeliveryStatus.IN_PROGRESS.value
        assert saved.attempts == 1


def test_success_transition_is_terminal_and_idempotent(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        service = OutboundDeliveryService(session)
        correlation_id = create_delivery(session).correlation_id
        assert service.claim_for_enqueue(correlation_id, "task-1") is True
        assert service.claim_attempt(
            correlation_id,
            "task-1",
            expected_attempts=0,
        )
        assert service.mark_succeeded(correlation_id, "provider-message-1") is True
        assert service.mark_succeeded(correlation_id, "provider-message-2") is False
        assert service.claim_attempt(
            correlation_id,
            "task-1",
            expected_attempts=1,
        ) is False

    with session_factory() as session:
        saved = OutboundDeliveryService(session).get(correlation_id)
        assert saved is not None
        assert saved.status == OutboundDeliveryStatus.SUCCEEDED.value
        assert saved.provider_message_id == "provider-message-1"
        assert saved.completed_at is not None
