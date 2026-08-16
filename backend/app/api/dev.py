from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.agent.graph import build_classification_graph
from app.config import Settings, get_settings
from app.database.gmail_mailbox_store import GmailMailboxStore
from app.integrations.gmail import GmailGateway, build_gmail_service
from app.integrations.llm import ClassificationService, GroqClassificationService
from app.schemas.email import IncomingEmail

router = APIRouter(prefix="/dev", tags=["development"])


def get_classification_service(settings: Annotated[Settings, Depends(get_settings)]) -> ClassificationService:
    if not settings.groq_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="GROQ_API_KEY is required for classification",
        )
    return GroqClassificationService(api_key=settings.groq_api_key, model=settings.groq_model)


ClassifierDependency = Annotated[ClassificationService, Depends(get_classification_service)]

@router.post("/watch-gmail")
def start_gmail_watch(request: Request, email_address: str) -> dict:
    topic_name = "projects/support-ticket-agent-505519/topics/gmail-watch"
    settings: Settings = request.app.state.settings

    mailbox_store = GmailMailboxStore(
        request.app.state.session_factory,
        encryption_key=settings.gmail_credentials_encryption_key,
    )

    credentials = mailbox_store.get_credentials(email_address)

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No Gmail credentials configured for {email_address}",
        )

    try:
        service = build_gmail_service(credentials)
        gateway = GmailGateway(service)

        result = gateway.watch(topic_name)

        history_id = result.get("historyId")
        if not history_id:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Gmail Watch did not return a historyId",
            )

        mailbox_store.set(email_address, str(history_id))

        return {
            "status": "watch_started",
            "email_address": email_address,
            "history_id": str(history_id),
            "expiration": result.get("expiration"),
            "topic_name": topic_name,
        }

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to start Gmail Watch: {exc}",
        ) from exc

@router.post("/test-email")
def process_test_email(email: IncomingEmail, classifier: ClassifierDependency) -> dict:
    graph = build_classification_graph(classifier)
    initial_state = {
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
    return graph.invoke(initial_state)
