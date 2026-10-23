from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.database.models import AgentDecision, Ticket, TicketHistory

TICKET_PAYLOAD = {
    "customer_email": "customer@example.com",
    "customer_name": "Avery Customer",
    "subject": "Cannot access account",
    "description": "The account login is failing.",
    "category": "account",
    "priority": "medium",
    "sentiment": "negative",
    "product": "Support Portal",
    "confidence": 0.93,
}


def create_ticket(client: TestClient) -> dict:
    response = client.post("/tickets", json=TICKET_PAYLOAD)
    assert response.status_code == 201
    return response.json()


def test_create_ticket_persists_ticket_and_history(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    created = create_ticket(client)

    assert created["ticket_number"].startswith("TKT-")
    assert created["status"] == "open"
    assert created["customer_email"] == TICKET_PAYLOAD["customer_email"]

    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Ticket)) == 1
        history = session.scalar(select(TicketHistory))

    assert history is not None
    assert history.action == "TICKET_CREATED"
    assert history.new_value == "open"
    assert history.actor == "system"


def test_list_and_filter_tickets(client: TestClient) -> None:
    created = create_ticket(client)

    update_response = client.patch(f"/tickets/{created['id']}", json={"status": "escalated"})
    assert update_response.status_code == 200

    escalated_response = client.get("/tickets", params={"status": "escalated"})
    open_response = client.get("/tickets", params={"status": "open"})

    assert [ticket["id"] for ticket in escalated_response.json()] == [created["id"]]
    assert open_response.json() == []


def test_update_ticket_records_each_changed_field(client: TestClient) -> None:
    created = create_ticket(client)

    response = client.patch(
        f"/tickets/{created['id']}",
        json={"status": "in_progress", "assignee": "support@example.com"},
    )
    history_response = client.get(f"/tickets/{created['id']}/history")

    assert response.status_code == 200
    assert response.json()["status"] == "in_progress"
    assert response.json()["assignee"] == "support@example.com"
    actions = {entry["action"] for entry in history_response.json()}
    assert actions == {
        "TICKET_CREATED",
        "TICKET_STATUS_UPDATED",
        "TICKET_ASSIGNEE_UPDATED",
    }


def test_detail_includes_agent_decision_and_audit_history(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    created = create_ticket(client)
    decision_payload = {
        "decision": "ESCALATE",
        "reason": "Classification confidence below threshold",
        "confidence": 0.72,
        "model": "test-model",
    }

    decision_response = client.post(f"/tickets/{created['id']}/decisions", json=decision_payload)
    detail_response = client.get(f"/tickets/{created['id']}")

    assert decision_response.status_code == 201
    assert decision_response.json()["decision"] == "ESCALATE"
    detail = detail_response.json()
    assert detail["decisions"][0]["reason"] == decision_payload["reason"]
    assert "AGENT_DECISION_RECORDED" in {entry["action"] for entry in detail["history"]}

    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(AgentDecision)) == 1


def test_required_update_fields_reject_explicit_null(client: TestClient) -> None:
    created = create_ticket(client)

    response = client.patch(f"/tickets/{created['id']}", json={"status": None})

    assert response.status_code == 422


def test_missing_ticket_returns_not_found(client: TestClient) -> None:
    assert client.get("/tickets/999").status_code == 404
    assert client.patch("/tickets/999", json={"status": "closed"}).status_code == 404
    assert (
        client.post(
            "/tickets/999/decisions",
            json={"decision": "ESCALATE", "reason": "Ticket missing"},
        ).status_code
        == 404
    )
