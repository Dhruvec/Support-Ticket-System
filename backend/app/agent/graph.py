from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.agent.nodes import (
    ExternalJobEnqueuer,
    build_classification_node,
    build_faq_search_node,
    build_persistence_node,
    build_routing_node,
    extract_information,
    parse_email,
)
from app.agent.policy import DeterministicRoutingPolicy
from app.agent.state import TicketState
from app.integrations.llm import ClassificationService
from app.knowledge.retriever import FAQSearchService
from app.services.email_ticket_service import EmailTicketService


def build_classification_graph(classifier: ClassificationService) -> CompiledStateGraph:
    graph = StateGraph(TicketState)
    graph.add_node("parse_email", parse_email)
    graph.add_node("classify_email", build_classification_node(classifier))
    graph.add_node("extract_information", extract_information)
    graph.add_edge(START, "parse_email")
    graph.add_edge("parse_email", "classify_email")
    graph.add_edge("classify_email", "extract_information")
    graph.add_edge("extract_information", END)
    return graph.compile()


def build_support_workflow_graph(
    classifier: ClassificationService,
    ticket_service: EmailTicketService,
    faq_retriever: FAQSearchService,
    routing_policy: DeterministicRoutingPolicy,
    enqueue_job: ExternalJobEnqueuer | None = None,
) -> CompiledStateGraph:
    graph = StateGraph(TicketState)
    graph.add_node("parse_email", parse_email)
    graph.add_node("classify_email", build_classification_node(classifier))
    graph.add_node("extract_information", extract_information)
    graph.add_node("resolve_and_persist_ticket", build_persistence_node(ticket_service))
    graph.add_node("search_faq", build_faq_search_node(faq_retriever))
    graph.add_node(
        "route_and_persist",
        build_routing_node(routing_policy, ticket_service, enqueue_job),
    )
    graph.add_edge(START, "parse_email")
    graph.add_edge("parse_email", "classify_email")
    graph.add_edge("classify_email", "extract_information")
    graph.add_edge("extract_information", "resolve_and_persist_ticket")
    graph.add_edge("resolve_and_persist_ticket", "search_faq")
    graph.add_edge("search_faq", "route_and_persist")
    graph.add_edge("route_and_persist", END)
    return graph.compile()


def build_ticket_workflow_graph(
    classifier: ClassificationService,
    ticket_service: EmailTicketService,
) -> CompiledStateGraph:
    graph = StateGraph(TicketState)
    graph.add_node("parse_email", parse_email)
    graph.add_node("classify_email", build_classification_node(classifier))
    graph.add_node("extract_information", extract_information)
    graph.add_node("resolve_and_persist_ticket", build_persistence_node(ticket_service))
    graph.add_edge(START, "parse_email")
    graph.add_edge("parse_email", "classify_email")
    graph.add_edge("classify_email", "extract_information")
    graph.add_edge("extract_information", "resolve_and_persist_ticket")
    graph.add_edge("resolve_and_persist_ticket", END)
    return graph.compile()
