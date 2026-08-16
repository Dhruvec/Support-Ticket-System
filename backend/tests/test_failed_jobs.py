from collections.abc import Callable
from typing import Annotated

from app.api.failed_jobs import get_failed_job_service
from app.database.database import get_db
from app.database.models import FailedJob, OutboundDelivery, OutboundDeliveryStatus
from app.services.failed_job_service import FailedJobService
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

_OPERATOR_KEY = "operator-key-with-at-least-thirty-two-characters"


def create_failed_delivery(
    session_factory: sessionmaker[Session],
    *,
    job_status: str = "failed",
    delivery_status: str = OutboundDeliveryStatus.FAILED.value,
    job_type: str = "gmail_send",
) -> tuple[int, int, str]:
    with session_factory() as session:
        delivery = OutboundDelivery(
            correlation_id="00000000-0000-0000-0000-000000000123",
            idempotency_key="a" * 64,
            job_type=job_type,
            source_message_id="message-123",
            operation_version="reply-v1",
            payload={"ticket_number": "TKT-123"},
            status=delivery_status,
            attempts=4,
            celery_task_id="terminal-task",
            last_error="provider unavailable",
        )
        session.add(delivery)
        session.flush()
        job = FailedJob(
            outbound_delivery_id=delivery.id,
            correlation_id=delivery.correlation_id,
            celery_task_id=delivery.celery_task_id,
            job_type=job_type,
            payload=delivery.payload,
            attempts=4,
            status=job_status,
            last_error=delivery.last_error,
        )
        session.add(job)
        session.commit()
        return job.id, delivery.id, delivery.correlation_id


def authenticate(client: TestClient) -> dict[str, str]:
    client.app.state.settings.operator_api_key = _OPERATOR_KEY
    return {"X-Operator-Key": _OPERATOR_KEY}


def override_publisher(
    client: TestClient,
    publisher: Callable[[str, str, str], None],
) -> None:
    def service(db: Annotated[Session, Depends(get_db)]) -> FailedJobService:
        return FailedJobService(db, publisher)

    client.app.dependency_overrides[get_failed_job_service] = service


def test_failed_job_inspection_requires_operator_authentication(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    create_failed_delivery(session_factory)
    authenticate(client)

    response = client.get("/failed-jobs")

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid or missing operator credentials"


def test_operator_can_list_filter_and_inspect_failed_jobs(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    failed_job_id, delivery_id, correlation_id = create_failed_delivery(session_factory)
    headers = authenticate(client)

    list_response = client.get("/failed-jobs", headers=headers)
    detail_response = client.get(f"/failed-jobs/{failed_job_id}", headers=headers)
    filtered_response = client.get(
        "/failed-jobs",
        params={"status": "uncertain"},
        headers=headers,
    )

    assert list_response.status_code == 200
    assert len(list_response.json()) == 1
    assert detail_response.status_code == 200
    assert detail_response.json() == list_response.json()[0]
    assert detail_response.json()["id"] == failed_job_id
    assert detail_response.json()["outbound_delivery_id"] == delivery_id
    assert detail_response.json()["correlation_id"] == correlation_id
    assert detail_response.json()["last_error"] == "provider unavailable"
    assert filtered_response.status_code == 200
    assert filtered_response.json() == []


def test_operator_can_replay_terminal_failure_once(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    failed_job_id, delivery_id, correlation_id = create_failed_delivery(session_factory)
    headers = authenticate(client)
    publications: list[tuple[str, str, str]] = []
    override_publisher(
        client,
        lambda replay_correlation_id, queue, task_id: publications.append(
            (replay_correlation_id, queue, task_id)
        ),
    )

    response = client.post(f"/failed-jobs/{failed_job_id}/replay", headers=headers)
    duplicate = client.post(f"/failed-jobs/{failed_job_id}/replay", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["failed_job_id"] == failed_job_id
    assert body["outbound_delivery_id"] == delivery_id
    assert body["correlation_id"] == correlation_id
    assert body["queue"] == "gmail_outbound"
    assert body["status"] == "queued"
    assert publications == [(correlation_id, "gmail_outbound", body["celery_task_id"])]
    assert duplicate.status_code == 409
    assert "not eligible for replay from status replayed" in duplicate.json()["detail"]

    with session_factory() as session:
        job = session.get(FailedJob, failed_job_id)
        delivery = session.get(OutboundDelivery, delivery_id)
        assert job is not None
        assert delivery is not None
        assert job.status == "replayed"
        assert job.celery_task_id == body["celery_task_id"]
        assert delivery.status == OutboundDeliveryStatus.QUEUED.value
        assert delivery.attempts == 0
        assert delivery.celery_task_id == body["celery_task_id"]
        assert delivery.last_error is None


def test_replay_rejects_uncertain_provider_outcome(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    failed_job_id, _, _ = create_failed_delivery(
        session_factory,
        job_status="uncertain",
        delivery_status=OutboundDeliveryStatus.UNCERTAIN.value,
    )
    headers = authenticate(client)
    publications: list[tuple[str, str, str]] = []
    override_publisher(
        client,
        lambda correlation_id, queue, task_id: publications.append(
            (correlation_id, queue, task_id)
        ),
    )

    response = client.post(f"/failed-jobs/{failed_job_id}/replay", headers=headers)

    assert response.status_code == 409
    assert "not eligible for replay from status uncertain" in response.json()["detail"]
    assert publications == []


def test_replay_publish_failure_is_persisted_and_can_be_retried(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    failed_job_id, delivery_id, _ = create_failed_delivery(session_factory)
    headers = authenticate(client)

    def unavailable_publisher(correlation_id: str, queue: str, task_id: str) -> None:
        raise ConnectionError("broker unavailable")

    override_publisher(client, unavailable_publisher)
    response = client.post(f"/failed-jobs/{failed_job_id}/replay", headers=headers)

    assert response.status_code == 503
    assert response.json()["detail"] == (
        f"Failed job {failed_job_id} could not be published for replay"
    )
    with session_factory() as session:
        job = session.get(FailedJob, failed_job_id)
        delivery = session.get(OutboundDelivery, delivery_id)
        assert job is not None
        assert delivery is not None
        assert job.status == "replay_failed"
        assert job.celery_task_id is None
        assert job.last_error == "broker unavailable"
        assert delivery.status == OutboundDeliveryStatus.FAILED.value
        assert delivery.celery_task_id is None
        assert delivery.last_error == "broker unavailable"

    publications: list[tuple[str, str, str]] = []
    override_publisher(
        client,
        lambda correlation_id, queue, task_id: publications.append(
            (correlation_id, queue, task_id)
        ),
    )
    retry_response = client.post(
        f"/failed-jobs/{failed_job_id}/replay",
        headers=headers,
    )

    assert retry_response.status_code == 200
    assert len(publications) == 1
