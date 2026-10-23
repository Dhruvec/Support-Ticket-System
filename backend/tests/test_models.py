from datetime import UTC, datetime

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.database.models import Ticket, TicketMessage, TicketStatus

EXPECTED_TABLES = {
    "tickets",
    "ticket_messages",
    "agent_decisions",
    "ticket_history",
    "gmail_mailboxes",
    "outbound_deliveries",
    "failed_jobs",
}


def test_required_tables_are_created(session_factory: sessionmaker[Session]) -> None:
    inspector = inspect(session_factory.kw["bind"])

    assert EXPECTED_TABLES.issubset(set(inspector.get_table_names()))


def test_ticket_status_uses_required_lowercase_value(session_factory: sessionmaker[Session]) -> None:
    with session_factory.begin() as session:
        ticket = Ticket(
            ticket_number="TKT-1001",
            customer_email="customer@example.com",
            subject="Cannot access account",
            description="The account login is failing.",
        )
        session.add(ticket)

    with session_factory() as session:
        stored_ticket = session.scalar(select(Ticket).where(Ticket.ticket_number == "TKT-1001"))

    assert stored_ticket is not None
    assert stored_ticket.status == TicketStatus.OPEN
    assert stored_ticket.status.value == "open"


def test_gmail_message_id_is_unique(session_factory: sessionmaker[Session]) -> None:
    with session_factory.begin() as session:
        first_ticket = Ticket(
            ticket_number="TKT-1001",
            customer_email="customer@example.com",
            subject="First delivery",
            description="Original webhook delivery.",
        )
        second_ticket = Ticket(
            ticket_number="TKT-1002",
            customer_email="customer@example.com",
            subject="Duplicate delivery",
            description="Repeated webhook delivery.",
        )
        session.add_all([first_ticket, second_ticket])
        session.flush()
        session.add(
            TicketMessage(
                ticket_id=first_ticket.id,
                gmail_message_id="gmail-message-1",
                thread_id="gmail-thread-1",
                sender="customer@example.com",
                subject="First delivery",
                body="Original webhook delivery.",
                received_at=datetime.now(UTC),
            )
        )

    with pytest.raises(IntegrityError), session_factory.begin() as session:
        second_ticket_id = session.scalar(
            select(Ticket.id).where(Ticket.ticket_number == "TKT-1002")
        )
        session.add(
            TicketMessage(
                ticket_id=second_ticket_id,
                gmail_message_id="gmail-message-1",
                thread_id="gmail-thread-1",
                sender="customer@example.com",
                subject="Duplicate delivery",
                body="Repeated webhook delivery.",
                received_at=datetime.now(UTC),
            )
        )
