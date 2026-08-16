from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

FailedJobStatus = Literal["failed", "retrying", "uncertain", "replayed", "replay_failed"]


class FailedJobRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    outbound_delivery_id: int | None
    correlation_id: str | None
    celery_task_id: str | None
    job_type: str
    attempts: int
    status: str
    last_error: str | None
    next_retry_at: datetime | None
    created_at: datetime


class FailedJobReplayRead(BaseModel):
    failed_job_id: int
    outbound_delivery_id: int
    correlation_id: str
    celery_task_id: str
    queue: str
    status: Literal["queued"] = "queued"
