from collections.abc import Callable
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel
from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database.database import check_database_connection, get_db

router = APIRouter(tags=["health"])


class RootResponse(BaseModel):
    service: str
    status: Literal["running"]
    health: Literal["/health"]
    readiness: Literal["/ready"]


class LivenessResponse(BaseModel):
    status: Literal["healthy"]


class ReadinessResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    database: Literal["connected", "unavailable"]
    redis: Literal["connected", "unavailable"]


def check_redis_connection(settings: Settings) -> None:
    client = Redis.from_url(
        settings.redis_url,
        socket_connect_timeout=1,
        socket_timeout=1,
    )
    try:
        client.ping()
    finally:
        client.close()


def get_redis_checker() -> Callable[[Settings], None]:
    return check_redis_connection


@router.get("/", response_model=RootResponse)
def root(settings: Annotated[Settings, Depends(get_settings)]) -> RootResponse:
    return RootResponse(
        service=settings.app_name,
        status="running",
        health="/health",
        readiness="/ready",
    )


@router.get("/health", response_model=LivenessResponse)
def health_check() -> LivenessResponse:
    return LivenessResponse(status="healthy")


@router.get("/ready", response_model=ReadinessResponse)
def readiness_check(
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    redis_checker: Annotated[Callable[[Settings], None], Depends(get_redis_checker)],
) -> ReadinessResponse:
    database_status: Literal["connected", "unavailable"] = "connected"
    redis_status: Literal["connected", "unavailable"] = "connected"

    try:
        check_database_connection(db)
    except SQLAlchemyError:
        database_status = "unavailable"

    try:
        redis_checker(settings)
    except (RedisError, OSError):
        redis_status = "unavailable"

    if database_status == "unavailable" or redis_status == "unavailable":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        readiness_status: Literal["ready", "not_ready"] = "not_ready"
    else:
        readiness_status = "ready"

    return ReadinessResponse(
        status=readiness_status,
        database=database_status,
        redis=redis_status,
    )
