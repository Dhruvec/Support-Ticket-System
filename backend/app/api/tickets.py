from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.database.database import get_db
from app.database.models import TicketStatus
from app.schemas.ticket import (
    AgentDecisionCreate,
    AgentDecisionRead,
    TicketCreate,
    TicketDetail,
    TicketHistoryRead,
    TicketRead,
    TicketUpdate,
)
from app.security import require_operator
from app.services.ticket_service import TicketNotFoundError, TicketService

router = APIRouter(
    prefix="/tickets",
    tags=["tickets"],
    dependencies=[Depends(require_operator)],
)
DbSession = Annotated[Session, Depends(get_db)]


def get_ticket_service(db: DbSession) -> TicketService:
    return TicketService(db)


TicketServiceDependency = Annotated[TicketService, Depends(get_ticket_service)]


@router.post("", response_model=TicketRead, status_code=status.HTTP_201_CREATED)
def create_ticket(payload: TicketCreate, service: TicketServiceDependency) -> TicketRead:
    return service.create_ticket(payload)


@router.get("", response_model=list[TicketRead])
def list_tickets(
    service: TicketServiceDependency,
    ticket_status: Annotated[TicketStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[TicketRead]:
    return service.list_tickets(status=ticket_status, limit=limit, offset=offset)


@router.get("/{ticket_id}", response_model=TicketDetail)
def get_ticket(ticket_id: int, service: TicketServiceDependency) -> TicketDetail:
    try:
        return service.get_ticket(ticket_id)
    except TicketNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.patch("/{ticket_id}", response_model=TicketRead)
def update_ticket(ticket_id: int, payload: TicketUpdate, service: TicketServiceDependency) -> TicketRead:
    try:
        return service.update_ticket(ticket_id, payload)
    except TicketNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post("/{ticket_id}/decisions", response_model=AgentDecisionRead, status_code=status.HTTP_201_CREATED)
def add_agent_decision(
    ticket_id: int,
    payload: AgentDecisionCreate,
    service: TicketServiceDependency,
) -> AgentDecisionRead:
    try:
        return service.add_decision(ticket_id, payload)
    except TicketNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.get("/{ticket_id}/history", response_model=list[TicketHistoryRead])
def get_ticket_history(ticket_id: int, service: TicketServiceDependency) -> list[TicketHistoryRead]:
    try:
        return service.get_ticket(ticket_id).history
    except TicketNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
