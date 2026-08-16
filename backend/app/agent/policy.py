from dataclasses import dataclass
from typing import Literal

from app.agent.state import TicketState

RoutingAction = Literal["auto_respond", "escalate", "ignore_duplicate"]


@dataclass(frozen=True)
class RoutingDecision:
    action: RoutingAction
    reason: str
    status: str | None
    confidence: float | None
    response_body: str | None = None


class DeterministicRoutingPolicy:
    model_name = "deterministic-policy-v1"

    def __init__(
        self,
        *,
        classification_threshold: float,
        faq_threshold: float,
    ) -> None:
        self.classification_threshold = classification_threshold
        self.faq_threshold = faq_threshold

    def evaluate(self, state: TicketState) -> RoutingDecision:
        if state.get("duplicate_message"):
            return RoutingDecision(
                action="ignore_duplicate",
                reason="Gmail message was already processed.",
                status=None,
                confidence=None,
            )

        classification_confidence = state.get("classification_confidence")
        faq_confidence = state.get("faq_confidence")
        faq_answer = state.get("faq_match")
        urgency = state.get("urgency")

        reasons: list[str] = []
        if classification_confidence is None or (
            classification_confidence < self.classification_threshold
        ):
            reasons.append("classification confidence is below threshold")
        if not faq_answer:
            reasons.append("no grounded FAQ answer was found")
        elif faq_confidence is None or faq_confidence < self.faq_threshold:
            reasons.append("FAQ confidence is below threshold")
        if urgency not in {"low", "medium"}:
            reasons.append(
                f"urgency is {urgency}" if urgency else "urgency is unavailable"
            )

        if reasons:
            return RoutingDecision(
                action="escalate",
                reason="Escalated because " + "; ".join(reasons) + ".",
                status="escalated",
                confidence=classification_confidence,
            )

        return RoutingDecision(
            action="auto_respond",
            reason="Classification and FAQ confidence passed policy thresholds.",
            status="auto_resolved",
            confidence=min(classification_confidence, faq_confidence),
            response_body=self._grounded_response(faq_answer),
        )

    @staticmethod
    def _grounded_response(faq_answer: str) -> str:
        return f"Hello,\n\n{faq_answer}\n\nRegards,\nSupport Team"
