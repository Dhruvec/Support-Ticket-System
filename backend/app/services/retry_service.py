from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.database.models import FailedJob, OutboundDelivery, OutboundDeliveryStatus


class RetryService:
    """Persist retry lifecycle events and correlated delivery state atomically."""

    def __init__(self, db: Session, *, backoff_base_seconds: int = 2) -> None:
        if backoff_base_seconds < 1:
            raise ValueError("Backoff base seconds must be at least 1")
        self.db = db
        self.backoff_base_seconds = backoff_base_seconds

    def record_retry(
        self,
        *,
        job_type: str,
        payload: dict[str, Any],
        attempt: int,
        error: Exception,
        now: datetime | None = None,
        outbound_delivery_id: int | None = None,
        correlation_id: str | None = None,
        celery_task_id: str | None = None,
    ) -> FailedJob:
        if attempt < 1:
            raise ValueError("Attempt must be at least 1")
        current_time = now or datetime.now(UTC)
        next_retry_at = current_time + timedelta(seconds=self.backoff_seconds(attempt))
        job = self._build_job(
            job_type=job_type,
            payload=payload,
            attempts=attempt,
            status="retrying",
            error=error,
            next_retry_at=next_retry_at,
            outbound_delivery_id=outbound_delivery_id,
            correlation_id=correlation_id,
            celery_task_id=celery_task_id,
        )
        if outbound_delivery_id is None:
            return self._persist(job)
        return self._persist_correlated(
            job,
            outbound_delivery_id=outbound_delivery_id,
            correlation_id=correlation_id,
            status=OutboundDeliveryStatus.RETRYING.value,
            attempts=attempt,
            error=error,
            next_retry_at=next_retry_at,
        )

    def record_terminal_failure(
        self,
        *,
        job_type: str,
        payload: dict[str, Any],
        attempts: int,
        error: Exception,
        outbound_delivery_id: int | None = None,
        correlation_id: str | None = None,
        celery_task_id: str | None = None,
    ) -> FailedJob:
        if attempts < 1:
            raise ValueError("Attempts must be at least 1")
        job = self._build_job(
            job_type=job_type,
            payload=payload,
            attempts=attempts,
            status="failed",
            error=error,
            next_retry_at=None,
            outbound_delivery_id=outbound_delivery_id,
            correlation_id=correlation_id,
            celery_task_id=celery_task_id,
        )
        if outbound_delivery_id is None:
            return self._persist(job)
        return self._persist_correlated(
            job,
            outbound_delivery_id=outbound_delivery_id,
            correlation_id=correlation_id,
            status=OutboundDeliveryStatus.FAILED.value,
            attempts=attempts,
            error=error,
            next_retry_at=None,
        )

    def record_uncertain(
        self,
        *,
        job_type: str,
        payload: dict[str, Any],
        attempts: int,
        error: Exception,
        outbound_delivery_id: int,
        correlation_id: str,
        celery_task_id: str | None = None,
    ) -> FailedJob:
        if attempts < 1:
            raise ValueError("Attempts must be at least 1")
        job = self._build_job(
            job_type=job_type,
            payload=payload,
            attempts=attempts,
            status="uncertain",
            error=error,
            next_retry_at=None,
            outbound_delivery_id=outbound_delivery_id,
            correlation_id=correlation_id,
            celery_task_id=celery_task_id,
        )
        return self._persist_correlated(
            job,
            outbound_delivery_id=outbound_delivery_id,
            correlation_id=correlation_id,
            status=OutboundDeliveryStatus.UNCERTAIN.value,
            attempts=attempts,
            error=error,
            next_retry_at=None,
        )

    def backoff_seconds(self, attempt: int) -> int:
        if attempt < 1:
            raise ValueError("Attempt must be at least 1")
        return self.backoff_base_seconds * (2 ** (attempt - 1))

    @staticmethod
    def _build_job(
        *,
        job_type: str,
        payload: dict[str, Any],
        attempts: int,
        status: str,
        error: Exception,
        next_retry_at: datetime | None,
        outbound_delivery_id: int | None,
        correlation_id: str | None,
        celery_task_id: str | None,
    ) -> FailedJob:
        return FailedJob(
            outbound_delivery_id=outbound_delivery_id,
            correlation_id=correlation_id,
            celery_task_id=celery_task_id,
            job_type=job_type,
            payload=payload,
            attempts=attempts,
            status=status,
            last_error=str(error),
            next_retry_at=next_retry_at,
        )

    def _persist_correlated(
        self,
        job: FailedJob,
        *,
        outbound_delivery_id: int,
        correlation_id: str | None,
        status: str,
        attempts: int,
        error: Exception,
        next_retry_at: datetime | None,
    ) -> FailedJob:
        conditions = [
            OutboundDelivery.id == outbound_delivery_id,
            OutboundDelivery.status == OutboundDeliveryStatus.IN_PROGRESS.value,
            OutboundDelivery.attempts == attempts,
        ]
        if correlation_id is not None:
            conditions.append(OutboundDelivery.correlation_id == correlation_id)
        try:
            result = self.db.execute(
                update(OutboundDelivery)
                .where(*conditions)
                .values(
                    status=status,
                    attempts=attempts,
                    last_error=str(error),
                    next_retry_at=next_retry_at,
                    completed_at=(
                        None
                        if status == OutboundDeliveryStatus.RETRYING.value
                        else datetime.now(UTC)
                    ),
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                raise RuntimeError(
                    f"Outbound delivery {outbound_delivery_id} was not in the "
                    "expected in-progress state"
                )
            self.db.add(job)
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        self.db.refresh(job)
        return job

    def _persist(self, job: FailedJob) -> FailedJob:
        try:
            self.db.add(job)
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        self.db.refresh(job)
        return job
