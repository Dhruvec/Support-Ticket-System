from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database.models import OutboundDelivery, OutboundDeliveryStatus

SOURCE_MESSAGE_ID_FIELD = "_source_message_id"
OPERATION_VERSION_FIELD = "_operation_version"
DEFAULT_OPERATION_VERSION = "outbound_v1"
SUPPORTED_OUTBOUND_JOB_TYPES = frozenset({"gmail_send", "slack_escalation"})


class OutboundDeliveryError(RuntimeError):
    """Base class for errors that have an explicit delivery outcome."""


class OutboundDeliveryConflictError(OutboundDeliveryError):
    """Raised when an idempotency key is reused with different operation data."""


class RetryableDeliveryError(OutboundDeliveryError):
    """Raised when a delivery failed before the provider could receive it."""


class UncertainDeliveryError(OutboundDeliveryError):
    """Raised when provider contact occurred but its final outcome is unknown."""


@dataclass(frozen=True)
class DeliveryPayload:
    source_message_id: str
    operation_version: str
    provider_payload: dict[str, Any]


def split_delivery_payload(payload: Mapping[str, Any]) -> DeliveryPayload:
    """Remove routing metadata before a payload reaches an external provider."""
    provider_payload = dict(payload)
    source_message_id = provider_payload.pop(SOURCE_MESSAGE_ID_FIELD, None)
    operation_version = provider_payload.pop(OPERATION_VERSION_FIELD, DEFAULT_OPERATION_VERSION)
    if not isinstance(source_message_id, str) or not source_message_id.strip():
        raise ValueError(f"{SOURCE_MESSAGE_ID_FIELD} is required")
    if not isinstance(operation_version, str) or not operation_version.strip():
        raise ValueError(f"{OPERATION_VERSION_FIELD} is required")
    return DeliveryPayload(
        source_message_id=source_message_id.strip(),
        operation_version=operation_version.strip(),
        provider_payload=provider_payload,
    )


class OutboundDeliveryService:
    """Persist and transition one logical provider delivery at a time."""

    def __init__(self, db: Session) -> None:
        self.db = db

    @staticmethod
    def build_idempotency_key(
        *,
        job_type: str,
        ticket_number: str,
        source_message_id: str,
        operation_version: str,
    ) -> str:
        normalized_job_type = _required_text(job_type, "job_type", max_length=128)
        normalized_ticket_number = _required_text(
            ticket_number,
            "ticket_number",
            max_length=32,
        )
        normalized_source_message_id = _required_text(
            source_message_id,
            "source_message_id",
            max_length=255,
        )
        normalized_operation_version = _required_text(
            operation_version,
            "operation_version",
            max_length=128,
        )
        identity = {
            "job_type": normalized_job_type,
            "operation_version": normalized_operation_version,
            "source_message_id": normalized_source_message_id,
            "ticket_number": normalized_ticket_number,
        }
        canonical = json.dumps(
            identity,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def create_or_get(
        self,
        *,
        job_type: str,
        source_message_id: str,
        operation_version: str,
        payload: dict[str, Any],
    ) -> OutboundDelivery:
        if job_type not in SUPPORTED_OUTBOUND_JOB_TYPES:
            raise ValueError(f"Unsupported outbound job type: {job_type}")
        ticket_number = payload.get("ticket_number")
        idempotency_key = self.build_idempotency_key(
            job_type=job_type,
            ticket_number=ticket_number,
            source_message_id=source_message_id,
            operation_version=operation_version,
        )
        existing = self.db.scalar(
            select(OutboundDelivery).where(
                OutboundDelivery.idempotency_key == idempotency_key
            )
        )
        if existing is not None:
            self._assert_compatible(
                existing,
                job_type=job_type,
                source_message_id=source_message_id,
                operation_version=operation_version,
                payload=payload,
            )
            return existing

        delivery = OutboundDelivery(
            correlation_id=str(uuid4()),
            idempotency_key=idempotency_key,
            job_type=job_type,
            source_message_id=source_message_id,
            operation_version=operation_version,
            payload=payload,
            status=OutboundDeliveryStatus.PENDING.value,
            attempts=0,
        )
        self.db.add(delivery)
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            delivery = self.db.scalar(
                select(OutboundDelivery).where(
                    OutboundDelivery.idempotency_key == idempotency_key
                )
            )
            if delivery is None:
                raise
            self._assert_compatible(
                delivery,
                job_type=job_type,
                source_message_id=source_message_id,
                operation_version=operation_version,
                payload=payload,
            )
            return delivery
        self.db.refresh(delivery)
        return delivery

    def get(self, correlation_id: str) -> OutboundDelivery | None:
        return self.db.scalar(
            select(OutboundDelivery).where(
                OutboundDelivery.correlation_id == correlation_id
            )
        )

    def claim_for_enqueue(
        self,
        correlation_id: str,
        celery_task_id: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        current_time = now or datetime.now(UTC)
        result = self.db.execute(
            update(OutboundDelivery)
            .where(
                OutboundDelivery.correlation_id == correlation_id,
                OutboundDelivery.status.in_(
                    (
                        OutboundDeliveryStatus.PENDING.value,
                        OutboundDeliveryStatus.ENQUEUE_FAILED.value,
                    )
                ),
            )
            .values(
                status=OutboundDeliveryStatus.QUEUED.value,
                celery_task_id=celery_task_id,
                last_error=None,
                next_retry_at=None,
                enqueued_at=current_time,
                completed_at=None,
            )
            .execution_options(synchronize_session=False)
        )
        self.db.commit()
        return result.rowcount == 1

    def mark_enqueue_failed(
        self,
        correlation_id: str,
        error: Exception,
        *,
        now: datetime | None = None,
    ) -> bool:
        current_time = now or datetime.now(UTC)
        result = self.db.execute(
            update(OutboundDelivery)
            .where(
                OutboundDelivery.correlation_id == correlation_id,
                OutboundDelivery.status == OutboundDeliveryStatus.QUEUED.value,
            )
            .values(
                status=OutboundDeliveryStatus.ENQUEUE_FAILED.value,
                celery_task_id=None,
                last_error=str(error),
                next_retry_at=None,
                completed_at=current_time,
            )
            .execution_options(synchronize_session=False)
        )
        self.db.commit()
        return result.rowcount == 1

    def claim_attempt(
        self,
        correlation_id: str,
        celery_task_id: str,
        *,
        expected_attempts: int,
    ) -> bool:
        if expected_attempts < 0:
            raise ValueError("Expected attempts cannot be negative")
        result = self.db.execute(
            update(OutboundDelivery)
            .where(
                OutboundDelivery.correlation_id == correlation_id,
                OutboundDelivery.status.in_(
                    (
                        OutboundDeliveryStatus.QUEUED.value,
                        OutboundDeliveryStatus.RETRYING.value,
                    )
                ),
                OutboundDelivery.attempts == expected_attempts,
            )
            .values(
                status=OutboundDeliveryStatus.IN_PROGRESS.value,
                attempts=OutboundDelivery.attempts + 1,
                celery_task_id=celery_task_id,
                last_error=None,
                next_retry_at=None,
                completed_at=None,
            )
            .execution_options(synchronize_session=False)
        )
        self.db.commit()
        return result.rowcount == 1

    def mark_succeeded(
        self,
        correlation_id: str,
        provider_message_id: str | None = None,
        *,
        now: datetime | None = None,
    ) -> bool:
        current_time = now or datetime.now(UTC)
        result = self.db.execute(
            update(OutboundDelivery)
            .where(
                OutboundDelivery.correlation_id == correlation_id,
                OutboundDelivery.status == OutboundDeliveryStatus.IN_PROGRESS.value,
            )
            .values(
                status=OutboundDeliveryStatus.SUCCEEDED.value,
                provider_message_id=provider_message_id,
                last_error=None,
                next_retry_at=None,
                completed_at=current_time,
            )
            .execution_options(synchronize_session=False)
        )
        self.db.commit()
        return result.rowcount == 1

    def mark_uncertain(
        self,
        correlation_id: str,
        error: Exception,
        *,
        now: datetime | None = None,
    ) -> bool:
        current_time = now or datetime.now(UTC)
        result = self.db.execute(
            update(OutboundDelivery)
            .where(
                OutboundDelivery.correlation_id == correlation_id,
                OutboundDelivery.status == OutboundDeliveryStatus.IN_PROGRESS.value,
            )
            .values(
                status=OutboundDeliveryStatus.UNCERTAIN.value,
                last_error=str(error),
                next_retry_at=None,
                completed_at=current_time,
            )
            .execution_options(synchronize_session=False)
        )
        self.db.commit()
        return result.rowcount == 1

    def mark_retrying(
        self,
        correlation_id: str,
        *,
        attempt: int,
        error: Exception,
        next_retry_at: datetime,
    ) -> bool:
        result = self.db.execute(
            update(OutboundDelivery)
            .where(
                OutboundDelivery.correlation_id == correlation_id,
                OutboundDelivery.status == OutboundDeliveryStatus.IN_PROGRESS.value,
            )
            .values(
                status=OutboundDeliveryStatus.RETRYING.value,
                attempts=attempt,
                last_error=str(error),
                next_retry_at=next_retry_at,
                completed_at=None,
            )
            .execution_options(synchronize_session=False)
        )
        self.db.commit()
        return result.rowcount == 1

    def mark_failed(
        self,
        correlation_id: str,
        *,
        attempts: int,
        error: Exception,
        now: datetime | None = None,
    ) -> bool:
        current_time = now or datetime.now(UTC)
        result = self.db.execute(
            update(OutboundDelivery)
            .where(
                OutboundDelivery.correlation_id == correlation_id,
                OutboundDelivery.status == OutboundDeliveryStatus.IN_PROGRESS.value,
            )
            .values(
                status=OutboundDeliveryStatus.FAILED.value,
                attempts=attempts,
                last_error=str(error),
                next_retry_at=None,
                completed_at=current_time,
            )
            .execution_options(synchronize_session=False)
        )
        self.db.commit()
        return result.rowcount == 1

    def _assert_compatible(
        self,
        delivery: OutboundDelivery,
        *,
        job_type: str,
        source_message_id: str,
        operation_version: str,
        payload: dict[str, Any],
    ) -> None:
        if (
            delivery.job_type != job_type
            or delivery.source_message_id != source_message_id
            or delivery.operation_version != operation_version
            or delivery.payload != payload
        ):
            raise OutboundDeliveryConflictError(
                "Idempotency key is already associated with different outbound data"
            )


def _required_text(value: object, name: str, *, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    normalized = value.strip()
    if len(normalized) > max_length:
        raise ValueError(f"{name} is too long")
    return normalized
