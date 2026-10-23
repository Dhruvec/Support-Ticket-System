from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.database.models import Ticket, TicketHistory, TicketMessage
from app.schemas.email import EmailClassification, IncomingEmail
from app.services.email_ticket_service import EmailTicketService


def email(
    message_id: str,
    *,
    thread_id: str = "thread-1",
    subject: str = "Cannot access account",
    body: str = "I cannot access the portal.",
    message_id_header: str | None = None,
    in_reply_to: str | None = None,
    references: list[str] | None = None,
) -> IncomingEmail:
    return IncomingEmail(
        message_id=message_id,
        thread_id=thread_id,
        sender="customer@example.com",
        recipient="support@example.com",
        subject=subject,
        body=body,
        timestamp=datetime.now(UTC),
        message_id_header=message_id_header,
        in_reply_to=in_reply_to,
        references=references or [],
    )


def classification() -> EmailClassification:
    return EmailClassification(
        category="account",
        urgency="high",
        sentiment="frustrated",
        customer_name="Avery Customer",
        product="Support Portal",
        summary="Customer cannot access the support portal account.",
        confidence=0.94,
    )


def test_new_email_creates_ticket_message_and_history(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        result = EmailTicketService(session).process(
            email("message-1", message_id_header="<message-1@example.com>"),
            classification(),
        )

        assert result.created is True
        assert result.duplicate is False
        assert result.resolution_method == "new_ticket"
        assert result.ticket.priority.value == "high"
        assert session.scalar(select(func.count()).select_from(Ticket)) == 1
        assert session.scalar(select(func.count()).select_from(TicketMessage)) == 1
        actions = set(session.scalars(select(TicketHistory.action)).all())
        assert actions == {"TICKET_CREATED_FROM_EMAIL", "EMAIL_ATTACHED"}


def test_duplicate_gmail_message_is_idempotent(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        service = EmailTicketService(session)
        first = service.process(email("message-1"), classification())
        duplicate = service.process(email("message-1"), classification())

        assert service.find_duplicate("message-1").id == first.ticket.id
        assert service.find_duplicate("unknown-message") is None
        assert duplicate.ticket.id == first.ticket.id
        assert duplicate.created is False
        assert duplicate.duplicate is True
        assert duplicate.resolution_method == "gmail_message_id"
        assert session.scalar(select(func.count()).select_from(TicketMessage)) == 1


def test_existing_ticket_is_resolved_by_thread_before_other_signals(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        service = EmailTicketService(session)
        first = service.process(email("message-1", thread_id="same-thread"), classification())
        reply = service.process(
            email(
                "message-2",
                thread_id="same-thread",
                subject="TKT-NOTREAL unrelated reference",
            ),
            classification(),
        )

        assert reply.ticket.id == first.ticket.id
        assert reply.created is False
        assert reply.resolution_method == "gmail_thread_id"
        assert session.scalar(select(func.count()).select_from(TicketMessage)) == 2


def test_existing_ticket_is_resolved_by_ticket_number(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        service = EmailTicketService(session)
        first = service.process(email("message-1", thread_id="thread-1"), classification())
        reply = service.process(
            email(
                "message-2",
                thread_id="thread-2",
                subject=f"Re: [{first.ticket.ticket_number.lower()}] account issue",
            ),
            classification(),
        )

        assert reply.ticket.id == first.ticket.id
        assert reply.resolution_method == "ticket_number"


def test_existing_ticket_is_resolved_by_message_headers(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        service = EmailTicketService(session)
        first = service.process(
            email(
                "message-1",
                thread_id="thread-1",
                message_id_header="<original@example.com>",
            ),
            classification(),
        )
        reply = service.process(
            email(
                "message-2",
                thread_id="thread-2",
                in_reply_to="<original@example.com>",
            ),
            classification(),
        )

        assert reply.ticket.id == first.ticket.id
        assert reply.resolution_method == "message_headers"


def test_semantic_matcher_is_only_used_as_final_fallback(
    session_factory: sessionmaker[Session],
) -> None:
    class Matcher:
        calls = 0

        def __init__(self, ticket: Ticket) -> None:
            self.ticket = ticket

        def find_match(self, incoming: IncomingEmail) -> Ticket | None:
            self.calls += 1
            return self.ticket

    with session_factory() as session:
        first = EmailTicketService(session).process(email("message-1"), classification())
        matcher = Matcher(first.ticket)
        result = EmailTicketService(session, semantic_matcher=matcher).process(
            email("message-2", thread_id="unrelated-thread"),
            classification(),
        )

        assert matcher.calls == 1
        assert result.ticket.id == first.ticket.id
        assert result.resolution_method == "semantic_similarity"
