from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.database.models import FailedJob, OutboundDelivery, OutboundDeliveryStatus

ReplayPublisher = Callable[[str, str, str], None]

JOB_QUEUES = {
    "gmail_send": "gmail_outbound",
    "slack_escalation": "slack_outbound",
}


class FailedJobNotFoundError(LookupError):
    pass


class FailedJobReplayError(RuntimeError):
    pass


class FailedJobService:
    def __init__(self, db: Session, publisher: ReplayPublisher) -> None:
        self.db = db
        self.publisher = publisher

    def list_jobs(
        self,
        *,
        job_status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[FailedJob]:
        statement = select(FailedJob).order_by(FailedJob.created_at.desc(), FailedJob.id.desc())
        if job_status is not None:
            statement = statement.where(FailedJob.status == job_status)
        return list(self.db.scalars(statement.limit(limit).offset(offset)))

    def get_job(self, failed_job_id: int) -> FailedJob:
        job = self.db.get(FailedJob, failed_job_id)
        if job is None:
            raise FailedJobNotFoundError(f"Failed job {failed_job_id} was not found")
        return job

    def replay(self, failed_job_id: int) -> tuple[FailedJob, OutboundDelivery, str, str]:
        job = self.get_job(failed_job_id)
        if job.status not in {"failed", "replay_failed"}:
            raise FailedJobReplayError(
                f"Failed job {failed_job_id} is not eligible for replay from status {job.status}"
            )
        if job.outbound_delivery_id is None or job.correlation_id is None:
            raise FailedJobReplayError(
                f"Failed job {failed_job_id} has no correlated outbound delivery"
            )
        queue = JOB_QUEUES.get(job.job_type)
        if queue is None:
            raise FailedJobReplayError(f"No queue is configured for job type {job.job_type}")

        task_id = str(uuid4())
        try:
            delivery_result = self.db.execute(
                update(OutboundDelivery)
                .where(
                    OutboundDelivery.id == job.outbound_delivery_id,
                    OutboundDelivery.correlation_id == job.correlation_id,
                    OutboundDelivery.job_type == job.job_type,
                    OutboundDelivery.status == OutboundDeliveryStatus.FAILED.value,
                )
                .values(
                    status=OutboundDeliveryStatus.QUEUED.value,
                    attempts=0,
                    celery_task_id=task_id,
                    last_error=None,
                    next_retry_at=None,
                    enqueued_at=datetime.now(UTC),
                    completed_at=None,
                )
                .execution_options(synchronize_session=False)
            )
            job_result = self.db.execute(
                update(FailedJob)
                .where(
                    FailedJob.id == failed_job_id,
                    FailedJob.status.in_(("failed", "replay_failed")),
                )
                .values(status="replayed", celery_task_id=task_id, next_retry_at=None)
                .execution_options(synchronize_session=False)
            )
            if delivery_result.rowcount != 1 or job_result.rowcount != 1:
                raise FailedJobReplayError(
                    f"Failed job {failed_job_id} or its delivery changed before replay"
                )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

        try:
            self.publisher(job.correlation_id, queue, task_id)
        except Exception as exc:
            self._record_publish_failure(failed_job_id, job.correlation_id, task_id, exc)
            raise FailedJobReplayError(
                f"Failed job {failed_job_id} could not be published for replay"
            ) from exc

        replayed_job = self.get_job(failed_job_id)
        delivery = self.db.get(OutboundDelivery, job.outbound_delivery_id)
        if delivery is None:
            raise FailedJobReplayError(
                f"Outbound delivery {job.outbound_delivery_id} disappeared after replay"
            )
        return replayed_job, delivery, task_id, queue

    def _record_publish_failure(
        self,
        failed_job_id: int,
        correlation_id: str,
        task_id: str,
        error: Exception,
    ) -> None:
        try:
            self.db.execute(
                update(OutboundDelivery)
                .where(
                    OutboundDelivery.correlation_id == correlation_id,
                    OutboundDelivery.status == OutboundDeliveryStatus.QUEUED.value,
                    OutboundDelivery.celery_task_id == task_id,
                )
                .values(
                    status=OutboundDeliveryStatus.FAILED.value,
                    celery_task_id=None,
                    last_error=str(error),
                    completed_at=None,
                )
                .execution_options(synchronize_session=False)
            )
            self.db.execute(
                update(FailedJob)
                .where(
                    FailedJob.id == failed_job_id,
                    FailedJob.status == "replayed",
                    FailedJob.celery_task_id == task_id,
                )
                .values(
                    status="replay_failed",
                    celery_task_id=None,
                    last_error=str(error),
                )
                .execution_options(synchronize_session=False)
            )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
