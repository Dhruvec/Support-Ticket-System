from datetime import UTC, datetime
from typing import Any

import pytest
from app.agent.graph import (
    build_classification_graph,
    build_support_workflow_graph,
    build_ticket_workflow_graph,
)
from app.agent.policy import DeterministicRoutingPolicy
from app.api.dev import get_classification_service
from app.config import Settings
from app.database.models import AgentDecision, Ticket, TicketMessage, TicketStatus
from app.integrations import llm
from app.knowledge.retriever import FAQMatch
from app.main import create_app
from app.schemas.email import EmailClassification, IncomingEmail
from app.services.email_ticket_service import EmailTicketService
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker


class FakeClassifier:
    model_name = "fake-classifier"

    def classify(self, email: IncomingEmail) -> EmailClassification:
        return EmailClassification(
            category="account",
            urgency="medium",
            sentiment="frustrated",
            customer_name="Avery Customer",
            product="Support Portal",
            summary="Customer cannot access the support portal account.",
            confidence=0.94,
        )


class CriticalClassifier(FakeClassifier):
    def classify(self, email: IncomingEmail) -> EmailClassification:
        classification = super().classify(email)
        return classification.model_copy(update={"urgency": "critical"})


class FakeRetriever:
    query = ""
    category = None

    def __init__(self, *, confidence: float = 0.91) -> None:
        self.confidence = confidence

    def search(self, query: str, *, category: str | None = None) -> FAQMatch:
        self.query = query
        self.category = category
        return FAQMatch(
            entry_id="account-password-reset",
            category="account",
            question="How do I reset a forgotten password?",
            answer="Use Forgot password on the sign-in page.",
            confidence=self.confidence,
        )


class RecordingEnqueuer:
    def __init__(self) -> None:
        self.jobs: list[tuple[str, dict]] = []

    def __call__(self, job_type: str, payload: dict) -> str:
        self.jobs.append((job_type, payload))
        return f"job-{len(self.jobs)}"


def initial_state() -> dict:
    return {
        "email_id": "gmail-message-1",
        "thread_id": "gmail-thread-1",
        "sender": "customer@example.com",
        "recipient": "support@example.com",
        "subject": "  Cannot access account  ",
        "body": "  I cannot log in to Support Portal.  ",
        "timestamp": datetime.now(UTC),
        "message_id_header": "<message-1@example.com>",
        "in_reply_to": None,
        "references": [],
    }


def api_payload() -> dict:
    payload = initial_state()
    payload["message_id"] = payload.pop("email_id")
    payload["timestamp"] = payload["timestamp"].isoformat()
    return payload


def test_classification_graph_returns_validated_structured_state() -> None:
    result = build_classification_graph(FakeClassifier()).invoke(initial_state())

    assert result["subject"] == "Cannot access account"
    assert result["body"] == "I cannot log in to Support Portal."
    assert result["category"] == "account"
    assert result["urgency"] == "medium"
    assert result["sentiment"] == "frustrated"
    assert result["classification_confidence"] == 0.94
    assert result["summary"] == "Customer cannot access the support portal account."


def test_support_workflow_graph_adds_grounded_faq_match(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        retriever = FakeRetriever()
        enqueuer = RecordingEnqueuer()
        result = build_support_workflow_graph(
            FakeClassifier(),
            EmailTicketService(session),
            retriever,
            DeterministicRoutingPolicy(
                classification_threshold=0.85,
                faq_threshold=0.85,
            ),
            enqueuer,
        ).invoke(initial_state())

        assert retriever.category == "account"
        assert "Cannot access account" in retriever.query
        assert result["faq_match"] == "Use Forgot password on the sign-in page."
        assert result["faq_confidence"] == 0.91
        assert result["action"] == "auto_respond"
        assert result["response_body"] == (
            "Hello,\n\nUse Forgot password on the sign-in page.\n\n"
            "Regards,\nSupport Team"
        )

        ticket = session.scalar(select(Ticket))
        assert ticket is not None
        assert ticket.status == TicketStatus.AUTO_RESOLVED
        decision = session.scalar(select(AgentDecision))
        assert decision is not None
        assert decision.decision == "auto_respond"

        assert len(enqueuer.jobs) == 1
        job_type, payload = enqueuer.jobs[0]
        assert job_type == "gmail_send"
        assert payload["_source_message_id"] == "gmail-message-1"
        assert payload["_operation_version"] == "deterministic-policy-v1"
        assert payload["ticket_number"] == ticket.ticket_number
        assert payload["body"] == result["response_body"]
        assert payload["original"]["message_id"] == "gmail-message-1"
        assert payload["original"]["timestamp"].endswith("Z")


def test_support_workflow_enqueues_escalation_for_unsafe_case(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        enqueuer = RecordingEnqueuer()
        state = initial_state()
        state["subject"] = "Production outage"
        result = build_support_workflow_graph(
            CriticalClassifier(),
            EmailTicketService(session),
            FakeRetriever(confidence=0.91),
            DeterministicRoutingPolicy(
                classification_threshold=0.85,
                faq_threshold=0.85,
            ),
            enqueuer,
        ).invoke(state)

        assert result["action"] == "escalate"
        assert len(enqueuer.jobs) == 1
        job_type, payload = enqueuer.jobs[0]
        assert job_type == "slack_escalation"
        assert payload["_source_message_id"] == "gmail-message-1"
        assert payload["_operation_version"] == "deterministic-policy-v1"
        assert payload["subject"] == "Production outage"
        assert payload["customer_email"] == "customer@example.com"
        assert payload["urgency"] == "critical"
        assert payload["category"] == "account"
        assert "urgency is critical" in payload["reason"]


def test_support_workflow_does_not_enqueue_duplicate_message(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        enqueuer = RecordingEnqueuer()
        graph = build_support_workflow_graph(
            FakeClassifier(),
            EmailTicketService(session),
            FakeRetriever(),
            DeterministicRoutingPolicy(
                classification_threshold=0.85,
                faq_threshold=0.85,
            ),
            enqueuer,
        )

        first = graph.invoke(initial_state())
        enqueuer.jobs.clear()
        duplicate = graph.invoke(initial_state())

        assert first["action"] == "auto_respond"
        assert duplicate["action"] == "ignore_duplicate"
        assert enqueuer.jobs == []


def test_ticket_workflow_graph_persists_idempotently(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        graph = build_ticket_workflow_graph(
            FakeClassifier(),
            EmailTicketService(session),
        )
        first = graph.invoke(initial_state())
        duplicate = graph.invoke(initial_state())

        assert first["ticket_id"].startswith("TKT-")
        assert first["is_existing_ticket"] is False
        assert first["duplicate_message"] is False
        assert duplicate["ticket_id"] == first["ticket_id"]
        assert duplicate["is_existing_ticket"] is True
        assert duplicate["duplicate_message"] is True
        assert duplicate["resolution_method"] == "gmail_message_id"
        assert session.scalar(select(func.count()).select_from(TicketMessage)) == 1


def test_groq_classifier_uses_json_mode_and_normalizes_numeric_strings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, Any] = {}

    class FakeStructuredModel:
        def invoke(self, prompt: str) -> dict[str, object]:
            calls["prompt"] = prompt
            return {
                "category": "other",
                "urgency": "low",
                "sentiment": "neutral",
                "customer_name": None,
                "product": None,
                "summary": "Customer is asking about job matching.",
                "confidence": "0.2",
            }

    class FakeChatGroq:
        def __init__(self, **kwargs: object) -> None:
            calls["init"] = kwargs

        def with_structured_output(
            self,
            schema: type[EmailClassification],
            *,
            method: str,
        ) -> FakeStructuredModel:
            calls["schema"] = schema
            calls["method"] = method
            return FakeStructuredModel()

    monkeypatch.setattr(llm, "ChatGroq", FakeChatGroq)
    classifier = llm.GroqClassificationService(api_key="test-key", model="test-model")

    result = classifier.classify(IncomingEmail.model_validate(api_payload()))

    assert calls["method"] == "json_mode"
    assert calls["schema"] is EmailClassification
    assert "confidence must be a JSON number" in calls["prompt"]
    assert "Use account for sign-in, password-reset" in calls["prompt"]
    assert result.confidence == 0.2


def test_invalid_classification_output_is_rejected() -> None:
    with pytest.raises(ValidationError):
        EmailClassification(
            category="unsupported",
            urgency="medium",
            sentiment="neutral",
            summary="Invalid category",
            confidence=0.9,
        )

    with pytest.raises(ValidationError):
        EmailClassification(
            category="other",
            urgency="low",
            sentiment="neutral",
            summary="Invalid confidence",
            confidence=1.5,
        )


def test_dev_test_email_uses_injected_classifier() -> None:
    application = create_app()
    application.dependency_overrides[get_classification_service] = FakeClassifier

    with TestClient(application) as client:
        response = client.post("/dev/test-email", json=api_payload())

    assert response.status_code == 200
    assert response.json()["category"] == "account"
    assert response.json()["classification_confidence"] == 0.94


def test_dev_test_email_requires_groq_configuration() -> None:
    application = create_app(
        Settings(
            app_env="test",
            database_url="sqlite+pysqlite:///:memory:",
            groq_api_key=None,
        )
    )

    with TestClient(application) as client:
        response = client.post("/dev/test-email", json=api_payload())

    assert response.status_code == 503
    assert response.json()["detail"] == "GROQ_API_KEY is required for classification"
