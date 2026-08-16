from collections.abc import Callable
from typing import Any

from app.agent.policy import DeterministicRoutingPolicy
from app.agent.state import TicketState
from app.database.models import TicketStatus
from app.integrations.llm import ClassificationService
from app.knowledge.retriever import FAQSearchService
from app.schemas.email import EmailClassification, IncomingEmail
from app.services.email_ticket_service import EmailTicketService

ClassificationNode = Callable[[TicketState], dict]
PersistenceNode = Callable[[TicketState], dict]
FAQSearchNode = Callable[[TicketState], dict]


def parse_email(state: TicketState) -> dict:
    email = IncomingEmail(
        message_id=state["email_id"],
        thread_id=state["thread_id"],
        sender=state["sender"],
        recipient=state["recipient"],
        subject=state["subject"],
        body=state["body"],
        timestamp=state["timestamp"],
        message_id_header=state["message_id_header"],
        in_reply_to=state["in_reply_to"],
        references=state["references"],
    )
    return {
        "sender": str(email.sender),
        "recipient": str(email.recipient),
        "subject": email.subject.strip() or "(no subject)",
        "body": email.body.strip(),
        "error": None,
    }


def build_classification_node(classifier: ClassificationService) -> ClassificationNode:
    def classify_email(state: TicketState) -> dict:
        email = IncomingEmail(
            message_id=state["email_id"],
            thread_id=state["thread_id"],
            sender=state["sender"],
            recipient=state["recipient"],
            subject=state["subject"],
            body=state["body"],
            timestamp=state["timestamp"],
            message_id_header=state["message_id_header"],
            in_reply_to=state["in_reply_to"],
            references=state["references"],
        )
        classification = classifier.classify(email)
        return {
            "category": classification.category,
            "urgency": classification.urgency,
            "sentiment": classification.sentiment,
            "classification_confidence": classification.confidence,
            "customer_name": classification.customer_name,
            "product": classification.product,
            "summary": classification.summary,
        }

    return classify_email


def extract_information(state: TicketState) -> dict:
    classification = EmailClassification(
        category=state["category"],
        urgency=state["urgency"],
        sentiment=state["sentiment"],
        customer_name=state.get("customer_name"),
        product=state.get("product"),
        summary=state["summary"],
        confidence=state["classification_confidence"],
    )
    return {
        "customer_name": classification.customer_name,
        "product": classification.product,
        "summary": classification.summary.strip(),
    }


def build_persistence_node(service: EmailTicketService) -> PersistenceNode:
    def resolve_and_persist_ticket(state: TicketState) -> dict:
        email = IncomingEmail(
            message_id=state["email_id"],
            thread_id=state["thread_id"],
            sender=state["sender"],
            recipient=state["recipient"],
            subject=state["subject"],
            body=state["body"],
            timestamp=state["timestamp"],
            message_id_header=state["message_id_header"],
            in_reply_to=state["in_reply_to"],
            references=state["references"],
        )
        classification = EmailClassification(
            category=state["category"],
            urgency=state["urgency"],
            sentiment=state["sentiment"],
            customer_name=state.get("customer_name"),
            product=state.get("product"),
            summary=state["summary"],
            confidence=state["classification_confidence"],
        )
        result = service.process(email, classification)
        return {
            "ticket_id": result.ticket.ticket_number,
            "is_existing_ticket": not result.created,
            "duplicate_message": result.duplicate,
            "resolution_method": result.resolution_method,
        }

    return resolve_and_persist_ticket


def build_faq_search_node(retriever: FAQSearchService) -> FAQSearchNode:
    def search_faq(state: TicketState) -> dict:
        query = f"{state.get('summary') or ''}\n{state['subject']}\n{state['body']}"
        match = retriever.search(query, category=state.get("category"))
        if match is None:
            return {"faq_match": None, "faq_confidence": None}
        return {
            "faq_match": match.answer,
            "faq_confidence": match.confidence,
        }

    return search_faq


RoutingNode = Callable[[TicketState], dict]
ExternalJobEnqueuer = Callable[[str, dict[str, Any]], str]


def build_routing_node(
    policy: DeterministicRoutingPolicy,
    service: EmailTicketService,
    enqueue_job: ExternalJobEnqueuer | None = None,
) -> RoutingNode:
    def route_and_persist(state: TicketState) -> dict:
        decision = policy.evaluate(state)
        result = {
            "action": decision.action,
            "decision_reason": decision.reason,
            "response_body": decision.response_body,
        }
        if decision.status is None:
            return result

        ticket_number = state.get("ticket_id")
        if not ticket_number:
            raise ValueError("ticket_id is required before routing")
        service.record_routing_decision(
            ticket_number,
            action=decision.action,
            reason=decision.reason,
            confidence=decision.confidence,
            model=policy.model_name,
            status=TicketStatus(decision.status),
        )
        if enqueue_job is None:
            return result

        if decision.action == "auto_respond":
            if not decision.response_body:
                raise ValueError("response_body is required for an automatic response")
            original = IncomingEmail(
                message_id=state["email_id"],
                thread_id=state["thread_id"],
                sender=state["sender"],
                recipient=state["recipient"],
                subject=state["subject"],
                body=state["body"],
                timestamp=state["timestamp"],
                message_id_header=state["message_id_header"],
                in_reply_to=state["in_reply_to"],
                references=state["references"],
            )
            enqueue_job(
                "gmail_send",
                {
                    "_source_message_id": state["email_id"],
                    "_operation_version": policy.model_name,
                    "ticket_number": ticket_number,
                    "original": original.model_dump(mode="json"),
                    "body": decision.response_body,
                },
            )
        elif decision.action == "escalate":
            enqueue_job(
                "slack_escalation",
                {
                    "_source_message_id": state["email_id"],
                    "_operation_version": policy.model_name,
                    "ticket_number": ticket_number,
                    "subject": state["subject"],
                    "customer_email": state["sender"],
                    "reason": decision.reason,
                    "urgency": state.get("urgency"),
                    "category": state.get("category"),
                    "confidence": decision.confidence,
                },
            )
        return result

    return route_and_persist
