from uuid import uuid4

from sqlalchemy.orm import Session

from app.database.models import (
    AgentDecision,
    Ticket,
    TicketHistory,
    TicketPriority,
    TicketStatus,
)
from app.database.repositories import TicketRepository
from app.schemas.ticket import AgentDecisionCreate, TicketCreate, TicketUpdate


class TicketNotFoundError(LookupError):
    pass


class TicketService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.repository = TicketRepository(db)

    def create_ticket(self, payload: TicketCreate, *, actor: str = "system") -> Ticket:
        ticket = Ticket(
            ticket_number=self._new_ticket_number(),
            customer_email=str(payload.customer_email),
            customer_name=payload.customer_name,
            subject=payload.subject,
            description=payload.description,
            category=payload.category,
            priority=payload.priority,
            sentiment=payload.sentiment,
            product=payload.product,
            assignee=payload.assignee,
            confidence=payload.confidence,
            status=TicketStatus.OPEN,
        )
        try:
            self.repository.add(ticket)
            self.repository.add_history(
                TicketHistory(
                    ticket_id=ticket.id,
                    action="TICKET_CREATED",
                    old_value=None,
                    new_value=ticket.status.value,
                    actor=actor,
                )
            )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        self.db.refresh(ticket)
        return ticket

    def list_tickets(
        self, *, status: TicketStatus | None = None, limit: int = 100, offset: int = 0
    ) -> list[Ticket]:
        return list(self.repository.list(status=status, limit=limit, offset=offset))

    def get_ticket(self, ticket_id: int) -> Ticket:
        ticket = self.repository.get(ticket_id)
        if ticket is None:
            raise TicketNotFoundError(f"Ticket {ticket_id} was not found")
        return ticket

    def update_ticket(self, ticket_id: int, payload: TicketUpdate, *, actor: str = "system") -> Ticket:
        ticket = self.get_ticket(ticket_id)
        changes = payload.model_dump(exclude_unset=True)
        try:
            for field, new_value in changes.items():
                old_value = getattr(ticket, field)
                if old_value == new_value:
                    continue
                if isinstance(old_value, (TicketStatus, TicketPriority)):
                    old_value = old_value.value
                history_new_value = (
                    new_value.value if isinstance(new_value, (TicketStatus, TicketPriority)) else new_value
                )
                setattr(ticket, field, new_value)
                self.repository.add_history(
                    TicketHistory(
                        ticket_id=ticket.id,
                        action=f"TICKET_{field.upper()}_UPDATED",
                        old_value=None if old_value is None else str(old_value),
                        new_value=None if history_new_value is None else str(history_new_value),
                        actor=actor,
                    )
                )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        self.db.refresh(ticket)
        return ticket

    def add_decision(
        self, ticket_id: int, payload: AgentDecisionCreate, *, actor: str = "agent"
    ) -> AgentDecision:
        ticket = self.get_ticket(ticket_id)
        decision = AgentDecision(
            ticket_id=ticket.id,
            decision=payload.decision,
            reason=payload.reason,
            confidence=payload.confidence,
            model=payload.model,
        )
        try:
            self.repository.add_decision(decision)
            self.repository.add_history(
                TicketHistory(
                    ticket_id=ticket.id,
                    action="AGENT_DECISION_RECORDED",
                    old_value=None,
                    new_value=payload.decision,
                    actor=actor,
                )
            )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        self.db.refresh(decision)
        return decision

    @staticmethod
    def _new_ticket_number() -> str:
        return f"TKT-{uuid4().hex[:8].upper()}"
