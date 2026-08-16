from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from app.agent.graph import build_support_workflow_graph
from app.agent.policy import DeterministicRoutingPolicy
from app.config import Settings
from app.database.gmail_mailbox_store import GmailMailboxStore
from app.integrations.gmail import (
    GmailGateway,
    GmailNotificationProcessor,
    build_gmail_service,
)
from app.integrations.llm import GroqClassificationService
from app.knowledge.retriever import FAQRetriever, HuggingFaceEmbeddingProvider
from app.schemas.email import IncomingEmail
from app.services.email_ticket_service import EmailTicketService


class RuntimeGmailProcessor:
    """Compose mailbox-specific Gmail access with the support workflow."""

    def __init__(
        self,
        settings: Settings,
        session_factory: sessionmaker[Session],
        enqueue_job: Callable[[str, dict[str, Any]], str],
    ) -> None:
        if not settings.groq_api_key:
            raise ValueError("GROQ_API_KEY is required to configure Gmail processing")
        self.session_factory = session_factory
        self.mailbox_store = GmailMailboxStore(
            session_factory,
            encryption_key=settings.gmail_credentials_encryption_key,
        )
        self.enqueue_job = enqueue_job
        self.classifier = GroqClassificationService(
            api_key=settings.groq_api_key,
            model=settings.groq_model,
        )
        self.faq_retriever = FAQRetriever(
            source_path=settings.faq_source_path,
            index_path=settings.faq_index_path,
            embedding_provider=HuggingFaceEmbeddingProvider(settings.embedding_model),
        )
        self.routing_policy = DeterministicRoutingPolicy(
            classification_threshold=settings.classification_confidence_threshold,
            faq_threshold=settings.faq_similarity_threshold,
        )

    def process(self, email_address: str, history_id: str) -> int:
        credentials = self.mailbox_store.get_credentials(email_address)
        if credentials is None:
            raise RuntimeError(f"No Gmail credentials are configured for {email_address}")
        gateway = GmailGateway(build_gmail_service(credentials))
        processor = GmailNotificationProcessor(
            gateway,
            self.mailbox_store,
            self._process_email,
        )
        return processor.process(email_address, history_id)

    def _process_email(self, email: IncomingEmail) -> None:
        with self.session_factory() as session:
            ticket_service = EmailTicketService(session)
            # Replayed Pub/Sub deliveries should not depend on Groq, the
            # embedding model, or outbound services once this Gmail message is
            # durably persisted. The database uniqueness constraint remains the
            # final guard against concurrent deliveries.
            if ticket_service.find_duplicate(email.message_id) is not None:
                return
            graph = build_support_workflow_graph(
                self.classifier,
                ticket_service,
                self.faq_retriever,
                self.routing_policy,
                self.enqueue_job,
            )
            graph.invoke(
                {
                    "email_id": email.message_id,
                    "thread_id": email.thread_id,
                    "sender": str(email.sender),
                    "recipient": str(email.recipient),
                    "subject": email.subject,
                    "body": email.body,
                    "timestamp": email.timestamp,
                    "message_id_header": email.message_id_header,
                    "in_reply_to": email.in_reply_to,
                    "references": email.references,
                }
            )
