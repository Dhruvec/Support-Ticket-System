import pytest

from app.agent.policy import DeterministicRoutingPolicy


@pytest.fixture
def policy() -> DeterministicRoutingPolicy:
    return DeterministicRoutingPolicy(
        classification_threshold=0.85,
        faq_threshold=0.85,
    )


def safe_state() -> dict:
    return {
        "classification_confidence": 0.94,
        "faq_confidence": 0.91,
        "faq_match": "Reset your password from the sign-in page.",
        "urgency": "medium",
        "duplicate_message": False,
    }


def test_safe_grounded_case_auto_responds(
    policy: DeterministicRoutingPolicy,
) -> None:
    decision = policy.evaluate(safe_state())

    assert decision.action == "auto_respond"
    assert decision.status == "auto_resolved"
    assert decision.confidence == 0.91
    assert decision.response_body == (
        "Hello,\n\nReset your password from the sign-in page.\n\n"
        "Regards,\nSupport Team"
    )


@pytest.mark.parametrize(
    ("updates", "expected_reason"),
    [
        ({"classification_confidence": 0.84}, "classification confidence"),
        ({"faq_confidence": 0.84}, "FAQ confidence"),
        ({"faq_match": None}, "no grounded FAQ answer"),
        ({"urgency": "high"}, "urgency is high"),
        ({"urgency": "critical"}, "urgency is critical"),
    ],
)
def test_unsafe_cases_escalate(
    policy: DeterministicRoutingPolicy,
    updates: dict,
    expected_reason: str,
) -> None:
    state = safe_state()
    state.update(updates)

    decision = policy.evaluate(state)

    assert decision.action == "escalate"
    assert decision.status == "escalated"
    assert expected_reason in decision.reason
    assert decision.response_body is None


def test_duplicate_message_is_ignored(
    policy: DeterministicRoutingPolicy,
) -> None:
    state = safe_state()
    state["duplicate_message"] = True

    decision = policy.evaluate(state)

    assert decision.action == "ignore_duplicate"
    assert decision.status is None
    assert decision.response_body is None
