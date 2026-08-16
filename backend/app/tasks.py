from collections.abc import Callable
from typing import Any
from uuid import uuid4

from celery import Celery

from app.config import get_settings
from app.database.database import SessionLocal
from app.database.gmail_mailbox_store import GmailMailboxStore
from app.database.models import OutboundDeliveryStatus
from app.integrations.gmail import GmailGateway, build_gmail_service
from app.integrations.slack import (
    EscalationNotification,
    SlackTransportError,
    SlackWebhookSender,
)
from app.observability import reset_request_id, set_request_id
from app.schemas.email import IncomingEmail
from app.services.outbound_delivery_service import (
    OutboundDeliveryService,
    RetryableDeliveryError,
    UncertainDeliveryError,
    split_delivery_payload,
)
from app.services.retry_service import RetryService

JobHandler = Callable[[dict[str, Any]], str | None]

JOB_QUEUES = {
    "gmail_send": "gmail_outbound",
    "slack_escalation": "slack_outbound",
}

settings = get_settings()
celery_app = Celery(
    "support_ticket_agent",
    broker=settings.redis_url,
    backend=settings.redis_url,
)
celery_app.conf.update(
    accept_content=["json"],
    task_serializer="json",
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
)

_job_handlers: dict[str, JobHandler] = {}


def register_job_handler(job_type: str, handler: JobHandler) -> None:
    if not job_type:
        raise ValueError("Job type is required")
    _job_handlers[job_type] = handler


def enqueue_external_job(job_type: str, payload: dict[str, Any]) -> str:
    queue = JOB_QUEUES.get(job_type)
    if queue is None:
        raise ValueError(f"No queue is configured for job type {job_type}")

    delivery_payload = split_delivery_payload(payload)
    with SessionLocal() as db:
        delivery_service = OutboundDeliveryService(db)
        delivery = delivery_service.create_or_get(
            job_type=job_type,
            source_message_id=delivery_payload.source_message_id,
            operation_version=delivery_payload.operation_version,
            payload=delivery_payload.provider_payload,
        )
        if delivery.status not in {
            OutboundDeliveryStatus.PENDING.value,
            OutboundDeliveryStatus.ENQUEUE_FAILED.value,
        }:
            return delivery.celery_task_id or delivery.correlation_id

        celery_task_id = str(uuid4())
        if not delivery_service.claim_for_enqueue(
            delivery.correlation_id,
            celery_task_id,
        ):
            current = delivery_service.get(delivery.correlation_id)
            if current is None:
                raise RuntimeError(
                    f"Outbound delivery {delivery.correlation_id} disappeared"
                )
            return current.celery_task_id or current.correlation_id

        try:
            execute_external_job.apply_async(
                args=(delivery.correlation_id,),
                queue=queue,
                task_id=celery_task_id,
            )
        except Exception as exc:
            delivery_service.mark_enqueue_failed(delivery.correlation_id, exc)
            raise
        return celery_task_id


@celery_app.task(bind=True, name="support.execute_external_job")
def execute_external_job(task: Any, correlation_id: str) -> None:
    context_token = set_request_id(correlation_id)
    try:
        _execute_external_job(task, correlation_id)
    finally:
        reset_request_id(context_token)


def _execute_external_job(task: Any, correlation_id: str) -> None:
    task_id = str(getattr(task.request, "id", None) or correlation_id)
    expected_attempts = int(task.request.retries)
    with SessionLocal() as db:
        delivery_service = OutboundDeliveryService(db)
        delivery = delivery_service.get(correlation_id)
        if delivery is None:
            raise ValueError(f"Outbound delivery {correlation_id} was not found")
        job_type = delivery.job_type
        handler = _job_handlers.get(job_type)
        if handler is None:
            raise ValueError(f"No handler is registered for job type {job_type}")
        if delivery.status == OutboundDeliveryStatus.SUCCEEDED.value:
            return
        if delivery.status in {
            OutboundDeliveryStatus.FAILED.value,
            OutboundDeliveryStatus.UNCERTAIN.value,
            OutboundDeliveryStatus.ENQUEUE_FAILED.value,
        }:
            return
        if not delivery_service.claim_attempt(
            correlation_id,
            task_id,
            expected_attempts=expected_attempts,
        ):
            current = delivery_service.get(correlation_id)
            if current is not None and current.status == OutboundDeliveryStatus.SUCCEEDED.value:
                return
            return
        payload = dict(delivery.payload)

    try:
        provider_message_id = handler(payload)
    except UncertainDeliveryError as exc:
        with SessionLocal() as db:
            RetryService(db).record_uncertain(
                job_type=job_type,
                payload=payload,
                attempts=expected_attempts + 1,
                error=exc,
                outbound_delivery_id=delivery.id,
                correlation_id=correlation_id,
                celery_task_id=task_id,
            )
        return
    except Exception as exc:
        attempt = expected_attempts + 1
        with SessionLocal() as db:
            retry_service = RetryService(
                db,
                backoff_base_seconds=settings.retry_backoff_base_seconds,
            )
            if attempt >= settings.max_retry_attempts:
                retry_service.record_terminal_failure(
                    job_type=job_type,
                    payload=payload,
                    attempts=attempt,
                    error=exc,
                    outbound_delivery_id=delivery.id,
                    correlation_id=correlation_id,
                    celery_task_id=task_id,
                )
                raise

            retry_service.record_retry(
                job_type=job_type,
                payload=payload,
                attempt=attempt,
                error=exc,
                outbound_delivery_id=delivery.id,
                correlation_id=correlation_id,
                celery_task_id=task_id,
            )
            raise task.retry(
                exc=exc,
                countdown=retry_service.backoff_seconds(attempt),
                max_retries=settings.max_retry_attempts - 1,
            ) from exc

    with SessionLocal() as db:
        if not OutboundDeliveryService(db).mark_succeeded(
            correlation_id,
            provider_message_id,
        ):
            current = OutboundDeliveryService(db).get(correlation_id)
            if current is None or current.status != OutboundDeliveryStatus.SUCCEEDED.value:
                raise RuntimeError(
                    f"Outbound delivery {correlation_id} could not be completed"
                )


def _send_gmail_reply(payload: dict[str, Any]) -> str:
    ticket_number = payload.get("ticket_number")
    original_data = payload.get("original")
    body = payload.get("body")
    if not isinstance(ticket_number, str) or not ticket_number:
        raise RetryableDeliveryError("gmail_send payload requires ticket_number")
    if not isinstance(original_data, dict):
        raise RetryableDeliveryError("gmail_send payload requires original email data")
    if not isinstance(body, str) or not body:
        raise RetryableDeliveryError("gmail_send payload requires a non-empty body")

    try:
        original = IncomingEmail.model_validate(original_data)
        mailbox_address = str(original.recipient)
        mailbox_store = GmailMailboxStore(
            SessionLocal,
            encryption_key=settings.gmail_credentials_encryption_key,
        )
        credentials = mailbox_store.get_credentials(mailbox_address)
        if credentials is None:
            raise RuntimeError(
                f"No Gmail credentials are configured for {mailbox_address}"
            )
        gateway = GmailGateway(build_gmail_service(credentials))
    except RetryableDeliveryError:
        raise
    except Exception as exc:
        raise RetryableDeliveryError(
            "Gmail reply could not be prepared before provider contact"
        ) from exc

    try:
        return gateway.send_reply(original, body)
    except Exception as exc:
        raise UncertainDeliveryError(
            "Gmail reply provider outcome is uncertain"
        ) from exc


def _send_slack_escalation(payload: dict[str, Any]) -> None:
    if not settings.slack_webhook_url:
        raise RetryableDeliveryError("Slack webhook URL is not configured")
    try:
        notification = EscalationNotification(**payload)
    except Exception as exc:
        raise RetryableDeliveryError(
            "Slack escalation could not be prepared before provider contact"
        ) from exc

    try:
        SlackWebhookSender(settings.slack_webhook_url).send(notification)
    except SlackTransportError as exc:
        raise UncertainDeliveryError(
            "Slack escalation provider outcome is uncertain"
        ) from exc


register_job_handler("gmail_send", _send_gmail_reply)
register_job_handler("slack_escalation", _send_slack_escalation)
