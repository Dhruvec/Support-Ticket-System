from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.database.database import get_db
from app.schemas.failed_job import FailedJobRead, FailedJobReplayRead, FailedJobStatus
from app.security import require_operator
from app.services.failed_job_service import (
    FailedJobNotFoundError,
    FailedJobReplayError,
    FailedJobService,
)

router = APIRouter(
    prefix="/failed-jobs",
    tags=["failed jobs"],
    dependencies=[Depends(require_operator)],
)
DbSession = Annotated[Session, Depends(get_db)]


def publish_replay(correlation_id: str, queue: str, task_id: str) -> None:
    from app.tasks import execute_external_job

    execute_external_job.apply_async(
        args=(correlation_id,),
        queue=queue,
        task_id=task_id,
    )


def get_failed_job_service(db: DbSession) -> FailedJobService:
    return FailedJobService(db, publish_replay)


FailedJobServiceDependency = Annotated[FailedJobService, Depends(get_failed_job_service)]


@router.get("", response_model=list[FailedJobRead])
def list_failed_jobs(
    service: FailedJobServiceDependency,
    job_status: Annotated[FailedJobStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[FailedJobRead]:
    return service.list_jobs(job_status=job_status, limit=limit, offset=offset)


@router.get("/{failed_job_id}", response_model=FailedJobRead)
def get_failed_job(
    failed_job_id: int,
    service: FailedJobServiceDependency,
) -> FailedJobRead:
    try:
        return service.get_job(failed_job_id)
    except FailedJobNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post("/{failed_job_id}/replay", response_model=FailedJobReplayRead)
def replay_failed_job(
    failed_job_id: int,
    service: FailedJobServiceDependency,
) -> FailedJobReplayRead:
    try:
        job, delivery, task_id, queue = service.replay(failed_job_id)
    except FailedJobNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except FailedJobReplayError as exc:
        http_status = (
            status.HTTP_503_SERVICE_UNAVAILABLE
            if "could not be published" in str(exc)
            else status.HTTP_409_CONFLICT
        )
        raise HTTPException(status_code=http_status, detail=str(exc)) from exc

    return FailedJobReplayRead(
        failed_job_id=job.id,
        outbound_delivery_id=delivery.id,
        correlation_id=delivery.correlation_id,
        celery_task_id=task_id,
        queue=queue,
    )
