from typing import Protocol

from langchain_groq import ChatGroq

from app.schemas.email import EmailClassification, IncomingEmail


class ClassificationService(Protocol):
    @property
    def model_name(self) -> str: ...

    def classify(self, email: IncomingEmail) -> EmailClassification: ...


class GroqClassificationService:
    def __init__(self, *, api_key: str, model: str) -> None:
        self._model_name = model
        llm = ChatGroq(api_key=api_key, model=model, temperature=0)
        # Groq validates function-call arguments before Pydantic can normalize
        # them. JSON mode keeps schema validation client-side, allowing benign
        # model variations such as a stringified numeric confidence value.
        self._structured_llm = llm.with_structured_output(
            EmailClassification,
            method="json_mode",
        )

    @property
    def model_name(self) -> str:
        return self._model_name

    def classify(self, email: IncomingEmail) -> EmailClassification:
        prompt = (
            "Classify this support email. Return only one JSON object with exactly "
            "these fields: category, urgency, sentiment, customer_name, product, "
            "summary, and confidence. category must be one of bug, billing, "
            "feature_request, account, technical_support, or other. Use account "
            "for sign-in, password-reset, account-access, and profile requests; "
            "reserve technical_support for product or service troubleshooting. "
            "urgency must be one of low, medium, high, or critical. sentiment must be one of "
            "positive, neutral, negative, or frustrated. customer_name and product "
            "must be strings or null. summary must be a non-empty string. confidence "
            "must be a JSON number from 0 through 1 and reflect certainty in the "
            "complete classification. Extract only information supported by the "
            "email. Do not wrap the JSON in Markdown.\n\n"
            f"Sender: {email.sender}\n"
            f"Subject: {email.subject}\n"
            f"Body:\n{email.body}"
        )
        result = self._structured_llm.invoke(prompt)
        if not isinstance(result, EmailClassification):
            return EmailClassification.model_validate(result)
        return result
