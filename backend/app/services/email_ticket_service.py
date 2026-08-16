import re
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database.models import (
    AgentDecision,
    Ticket,
    TicketHistory,
    TicketMessage,
    TicketPriority,
    TicketStatus,
)
from app.database.repositories import TicketRepository
from app.schemas.email import EmailClassification, IncomingEmail

TICKET_NUMBER_PATTERN = re.compile(r"\bTKT-[A-Z0-9]{8}\b", re.IGNORECASE)


class SemanticTicketMatcher(Protocol):
    def find_match(self, email: IncomingEmail) -> Ticket | None: ...


@dataclass(frozen=True)
class EmailTicketResult:
    ticket: Ticket
    created: bool
    duplicate: bool
    resolution_method: str


class EmailTicketService:
    def __init__(
        self,
        db: Session,
        *,
        semantic_matcher: SemanticTicketMatcher | None = None,
    ) -> None:
        self.db = db
        self.repository = TicketRepository(db)
        self.semantic_matcher = semantic_matcher

    def find_duplicate(self, gmail_message_id: str) -> Ticket | None:
        """Return the ticket for an already-persisted Gmail message, if any."""
        return self.repository.get_by_gmail_message_id(gmail_message_id)

    def process(
        self,
        email: IncomingEmail,
        classification: EmailClassification,
        *,
        actor: str = "email_agent",
    ) -> EmailTicketResult:
        duplicate_ticket = self.repository.get_by_gmail_message_id(email.message_id)
        if duplicate_ticket is not None:
            return EmailTicketResult(
                ticket=duplicate_ticket,
                created=False,
                duplicate=True,
                resolution_method="gmail_message_id",
            )

        ticket, resolution_method = self._resolve_ticket(email)
        created = ticket is None
        if ticket is None:
            ticket = self._build_ticket(email, classification)

        try:
            if created:
                self.repository.add(ticket)
                self.repository.add_history(
                    TicketHistory(
                        ticket_id=ticket.id,
                        action="TICKET_CREATED_FROM_EMAIL",
                        old_value=None,
                        new_value=ticket.status.value,
                        actor=actor,
                    )
                )
            else:
                self._apply_latest_classification(ticket, classification)

            self.repository.add_message(
                TicketMessage(
                    ticket_id=ticket.id,
                    gmail_message_id=email.message_id,
                    thread_id=email.thread_id,
                    message_id_header=email.message_id_header,
                    in_reply_to=email.in_reply_to,
                    references=email.references,
                    sender=str(email.sender),
                    subject=email.subject,
                    body=email.body,
                    received_at=email.timestamp,
                )
            )
            self.repository.add_history(
                TicketHistory(
                    ticket_id=ticket.id,
                    action="EMAIL_ATTACHED",
                    old_value=None,
                    new_value=email.message_id,
                    actor=actor,
                )
            )
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            concurrent_ticket = self.repository.get_by_gmail_message_id(email.message_id)
            if concurrent_ticket is None:
                raise
            return EmailTicketResult(
                ticket=concurrent_ticket,
                created=False,
                duplicate=True,
                resolution_method="gmail_message_id",
            )
        except Exception:
            self.db.rollback()
            raise

        self.db.refresh(ticket)
        return EmailTicketResult(
            ticket=ticket,
            created=created,
            duplicate=False,
            resolution_method=resolution_method if not created else "new_ticket",
        )

    def record_routing_decision(
        self,
        ticket_number: str,
        *,
        action: str,
        reason: str,
        confidence: float | None,
        model: str,
        status: TicketStatus,
        actor: str = "email_agent",
    ) -> Ticket:
        ticket = self.repository.get_by_ticket_number(ticket_number)
        if ticket is None:
            raise LookupError(f"Ticket {ticket_number} was not found")

        old_status = ticket.status
        try:
            ticket.status = status
            self.repository.add_decision(
                AgentDecision(
                    ticket_id=ticket.id,
                    decision=action,
                    reason=reason,
                    confidence=confidence,
                    model=model,
                )
            )
            self.repository.add_history(
                TicketHistory(
                    ticket_id=ticket.id,
                    action="AGENT_ROUTING_COMPLETED",
                    old_value=old_status.value,
                    new_value=status.value,
                    actor=actor,
                )
            )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

        self.db.refresh(ticket)
        return ticket

    def _resolve_ticket(self, email: IncomingEmail) -> tuple[Ticket | None, str]:
        ticket = self.repository.get_by_thread_id(email.thread_id)
        if ticket is not None:
            return ticket, "gmail_thread_id"

        ticket_number = self._extract_ticket_number(email.subject, email.body)
        if ticket_number is not None:
            ticket = self.repository.get_by_ticket_number(ticket_number)
            if ticket is not None:
                return ticket, "ticket_number"

        headers = [email.in_reply_to or "", *email.references]
        ticket = self.repository.get_by_message_headers(headers)
        if ticket is not None:
            return ticket, "message_headers"

        if self.semantic_matcher is not None:
            ticket = self.semantic_matcher.find_match(email)
            if ticket is not None:
                return ticket, "semantic_similarity"

        return None, "new_ticket"

    @staticmethod
    def _extract_ticket_number(subject: str, body: str) -> str | None:
        match = TICKET_NUMBER_PATTERN.search(f"{subject}\n{body}")
        return match.group(0).upper() if match else None

    @staticmethod
    def _build_ticket(email: IncomingEmail, classification: EmailClassification) -> Ticket:
        from app.services.ticket_service import TicketService

        return Ticket(
            ticket_number=TicketService._new_ticket_number(),
            customer_email=str(email.sender),
            customer_name=classification.customer_name,
            subject=email.subject,
            description=classification.summary,
            category=classification.category,
            priority=TicketPriority(classification.urgency),
            sentiment=classification.sentiment,
            product=classification.product,
            confidence=classification.confidence,
            status=TicketStatus.OPEN,
        )

    @staticmethod
    def _apply_latest_classification(
        ticket: Ticket,
        classification: EmailClassification,
    ) -> None:
        ticket.customer_name = classification.customer_name or ticket.customer_name
        ticket.category = classification.category
        ticket.priority = TicketPriority(classification.urgency)
        ticket.sentiment = classification.sentiment
        ticket.product = classification.product or ticket.product
        ticket.confidence = classification.confidence
