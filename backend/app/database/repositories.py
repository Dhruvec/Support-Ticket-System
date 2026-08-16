from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.database.models import (
    AgentDecision,
    Ticket,
    TicketHistory,
    TicketMessage,
    TicketStatus,
)


class TicketRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def list(self, *, status: TicketStatus | None = None, limit: int = 100, offset: int = 0) -> Sequence[Ticket]:
        statement = select(Ticket).order_by(Ticket.created_at.desc()).limit(limit).offset(offset)
        if status is not None:
            statement = statement.where(Ticket.status == status)
        return self.db.scalars(statement).all()

    def get(self, ticket_id: int) -> Ticket | None:
        statement = (
            select(Ticket)
            .options(selectinload(Ticket.history), selectinload(Ticket.decisions))
            .where(Ticket.id == ticket_id)
        )
        return self.db.scalar(statement)

    def get_by_ticket_number(self, ticket_number: str) -> Ticket | None:
        return self.db.scalar(select(Ticket).where(Ticket.ticket_number == ticket_number))

    def get_by_gmail_message_id(self, gmail_message_id: str) -> Ticket | None:
        statement = (
            select(Ticket)
            .join(TicketMessage)
            .where(TicketMessage.gmail_message_id == gmail_message_id)
        )
        return self.db.scalar(statement)

    def get_by_thread_id(self, thread_id: str) -> Ticket | None:
        statement = (
            select(Ticket)
            .join(TicketMessage)
            .where(TicketMessage.thread_id == thread_id)
            .order_by(TicketMessage.received_at.desc())
        )
        return self.db.scalar(statement)

    def get_by_message_headers(self, headers: Sequence[str]) -> Ticket | None:
        normalized = [header.strip() for header in headers if header.strip()]
        if not normalized:
            return None
        statement = (
            select(Ticket)
            .join(TicketMessage)
            .where(TicketMessage.message_id_header.in_(normalized))
            .order_by(TicketMessage.received_at.desc())
        )
        return self.db.scalar(statement)

    def add(self, ticket: Ticket) -> Ticket:
        self.db.add(ticket)
        self.db.flush()
        return ticket

    def add_history(self, history: TicketHistory) -> TicketHistory:
        self.db.add(history)
        self.db.flush()
        return history

    def add_decision(self, decision: AgentDecision) -> AgentDecision:
        self.db.add(decision)
        self.db.flush()
        return decision

    def add_message(self, message: TicketMessage) -> TicketMessage:
        self.db.add(message)
        self.db.flush()
        return message
