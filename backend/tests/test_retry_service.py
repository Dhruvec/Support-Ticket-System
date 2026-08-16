from datetime import UTC, datetime

import pytest
from app.database.models import FailedJob, OutboundDelivery, OutboundDeliveryStatus
from app.services.outbound_delivery_service import OutboundDeliveryService
from app.services.retry_service import RetryService
from sqlalchemy import func, select


def create_in_progress_delivery(session, *, task_id: str = "task-1") -> OutboundDelivery:
    service = OutboundDeliveryService(session)
    delivery = service.create_or_get(
        job_type="gmail_send",
        source_message_id="gmail-message-1",
        operation_version="deterministic-policy-v1",
        payload={"ticket_number": "TKT-1", "body": "Reply body"},
    )
    assert service.claim_for_enqueue(delivery.correlation_id, task_id) is True
    assert service.claim_attempt(
        delivery.correlation_id,
        task_id,
        expected_attempts=0,
    ) is True
    return service.get(delivery.correlation_id) or delivery


def test_retry_service_calculates_exponential_backoff(session_factory) -> None:
    service = RetryService(None, backoff_base_seconds=3)  # type: ignore[arg-type]

    assert service.backoff_seconds(1) == 3
    assert service.backoff_seconds(2) == 6
    assert service.backoff_seconds(4) == 24


def test_retry_service_persists_retry_state(session_factory) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    with session_factory() as session:
        job = RetryService(session, backoff_base_seconds=2).record_retry(
            job_type="slack_escalation",
            payload={"ticket_number": "TKT-1"},
            attempt=2,
            error=RuntimeError("temporary outage"),
            now=now,
        )

    with session_factory() as session:
        saved = session.get(FailedJob, job.id)
        assert saved is not None
        assert saved.status == "retrying"
        assert saved.attempts == 2
        assert saved.last_error == "temporary outage"
        assert saved.next_retry_at is not None
        assert saved.next_retry_at.replace(tzinfo=UTC) == datetime(2026, 1, 1, 0, 0, 4, tzinfo=UTC)


def test_retry_service_persists_terminal_failure(session_factory) -> None:
    with session_factory() as session:
        job = RetryService(session).record_terminal_failure(
            job_type="gmail_send",
            payload={"message_id": "gmail-1"},
            attempts=4,
            error=RuntimeError("permanent outage"),
        )

    with session_factory() as session:
        saved = session.get(FailedJob, job.id)
        assert saved is not None
        assert saved.status == "failed"
        assert saved.attempts == 4
        assert saved.next_retry_at is None
        assert saved.last_error == "permanent outage"


def test_retry_service_rejects_invalid_attempts(session_factory) -> None:
    with session_factory() as session:
        service = RetryService(session)
        try:
            service.backoff_seconds(0)
        except ValueError as exc:
            assert str(exc) == "Attempt must be at least 1"
        else:
            raise AssertionError("Expected invalid attempt to be rejected")


def test_failed_job_table_is_registered(session_factory) -> None:
    with session_factory() as session:
        session.add(FailedJob(job_type="test", payload={}, attempts=0, status="pending"))
        session.commit()
        assert session.query(FailedJob).count() == 1


def test_correlated_retry_updates_delivery_and_retains_event_atomically(
    session_factory,
) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    with session_factory() as session:
        delivery = create_in_progress_delivery(session)
        delivery_id = delivery.id
        correlation_id = delivery.correlation_id
        job = RetryService(session, backoff_base_seconds=2).record_retry(
            job_type=delivery.job_type,
            payload=dict(delivery.payload),
            attempt=1,
            error=RuntimeError("configuration unavailable"),
            now=now,
            outbound_delivery_id=delivery.id,
            correlation_id=delivery.correlation_id,
            celery_task_id="task-1",
        )
        job_id = job.id

    with session_factory() as session:
        saved_delivery = session.get(OutboundDelivery, delivery_id)
        saved_job = session.get(FailedJob, job_id)
        assert saved_delivery is not None
        assert saved_job is not None
        assert saved_delivery.status == OutboundDeliveryStatus.RETRYING.value
        assert saved_delivery.attempts == 1
        assert saved_delivery.last_error == "configuration unavailable"
        assert saved_delivery.next_retry_at is not None
        assert saved_delivery.completed_at is None
        assert saved_job.outbound_delivery_id == delivery_id
        assert saved_job.correlation_id == correlation_id
        assert saved_job.celery_task_id == "task-1"
        assert saved_job.status == "retrying"
        assert saved_job.next_retry_at == saved_delivery.next_retry_at


@pytest.mark.parametrize(
    ("method_name", "expected_status", "error_message"),
    [
        ("record_terminal_failure", OutboundDeliveryStatus.FAILED.value, "attempts exhausted"),
        ("record_uncertain", OutboundDeliveryStatus.UNCERTAIN.value, "provider outcome unknown"),
    ],
)
def test_correlated_terminal_events_complete_same_delivery(
    session_factory,
    method_name: str,
    expected_status: str,
    error_message: str,
) -> None:
    with session_factory() as session:
        delivery = create_in_progress_delivery(session)
        delivery_id = delivery.id
        correlation_id = delivery.correlation_id
        method = getattr(RetryService(session), method_name)
        job = method(
            job_type=delivery.job_type,
            payload=dict(delivery.payload),
            attempts=1,
            error=RuntimeError(error_message),
            outbound_delivery_id=delivery.id,
            correlation_id=delivery.correlation_id,
            celery_task_id="task-1",
        )
        job_id = job.id

    with session_factory() as session:
        saved_delivery = session.get(OutboundDelivery, delivery_id)
        saved_job = session.get(FailedJob, job_id)
        assert saved_delivery is not None
        assert saved_job is not None
        assert saved_delivery.correlation_id == correlation_id
        assert saved_delivery.status == expected_status
        assert saved_delivery.last_error == error_message
        assert saved_delivery.next_retry_at is None
        assert saved_delivery.completed_at is not None
        assert saved_job.status == expected_status
        assert saved_job.outbound_delivery_id == delivery_id
        assert saved_job.correlation_id == correlation_id


def test_correlated_event_rejects_stale_delivery_state_without_inserting_job(
    session_factory,
) -> None:
    with session_factory() as session:
        delivery = create_in_progress_delivery(session)
        delivery_id = delivery.id
        correlation_id = delivery.correlation_id
        assert OutboundDeliveryService(session).mark_succeeded(correlation_id) is True

    with (
        session_factory() as session,
        pytest.raises(RuntimeError, match="expected in-progress state"),
    ):
        RetryService(session).record_terminal_failure(
            job_type="gmail_send",
            payload={"ticket_number": "TKT-1", "body": "Reply body"},
            attempts=1,
            error=RuntimeError("late failure"),
            outbound_delivery_id=delivery_id,
            correlation_id=correlation_id,
            celery_task_id="task-1",
        )

    with session_factory() as session:
        saved = session.get(OutboundDelivery, delivery_id)
        assert saved is not None
        assert saved.status == OutboundDeliveryStatus.SUCCEEDED.value
        assert session.scalar(select(func.count(FailedJob.id))) == 0


def test_correlated_transition_rolls_back_when_event_commit_fails(
    monkeypatch: pytest.MonkeyPatch,
    session_factory,
) -> None:
    with session_factory() as session:
        delivery = create_in_progress_delivery(session)
        delivery_id = delivery.id
        correlation_id = delivery.correlation_id

        def fail_commit() -> None:
            raise RuntimeError("injected commit failure")

        monkeypatch.setattr(session, "commit", fail_commit)
        with pytest.raises(RuntimeError, match="injected commit failure"):
            RetryService(session).record_terminal_failure(
                job_type=delivery.job_type,
                payload=dict(delivery.payload),
                attempts=1,
                error=RuntimeError("terminal error"),
                outbound_delivery_id=delivery.id,
                correlation_id=delivery.correlation_id,
                celery_task_id="task-1",
            )

    with session_factory() as session:
        saved = session.get(OutboundDelivery, delivery_id)
        assert saved is not None
        assert saved.correlation_id == correlation_id
        assert saved.status == OutboundDeliveryStatus.IN_PROGRESS.value
        assert saved.last_error is None
        assert saved.completed_at is None
        assert session.scalar(select(func.count(FailedJob.id))) == 0
