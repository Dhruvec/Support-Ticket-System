export type TicketStatus =
    | "open"
    | "in_progress"
    | "auto_resolved"
    | "escalated"
    | "closed";

export type TicketPriority = "low" | "medium" | "high" | "critical";

export interface TicketHistory {
    id: number;
    action: string;
    old_value: string | null;
    new_value: string | null;
    actor: string;
    created_at: string;
}

export interface AgentDecision {
    id: number;
    decision: string;
    reason: string;
    confidence: number | null;
    model: string | null;
    created_at: string;
}

export interface Ticket {
    id: number;
    ticket_number: string;
    customer_email: string;
    customer_name: string | null;
    subject: string;
    description: string;
    category: string | null;
    priority: TicketPriority | null;
    sentiment: string | null;
    product: string | null;
    status: TicketStatus;
    assignee: string | null;
    confidence: number | null;
    created_at: string;
    updated_at: string;
}

export interface TicketDetail extends Ticket {
    history: TicketHistory[];
    decisions: AgentDecision[];
}

export interface TicketUpdate {
    status?: TicketStatus;
    priority?: TicketPriority;
    assignee?: string;
}

export interface DecisionCreate {
    decision: string;
    reason: string;
    confidence?: number;
    model?: string;
}

const apiBase = import.meta.env.VITE_API_BASE_URL ?? "";
const operatorKeyStorageName = "support-operations.operator-key";
export const operatorAuthenticationRequiredEvent =
    "support-operations:authentication-required";

export class ApiError extends Error {
    readonly status: number;

    constructor(message: string, status: number) {
        super(message);
        this.name = "ApiError";
        this.status = status;
    }
}

export function getOperatorKey(): string | null {
    if (typeof window === "undefined") return null;
    return window.sessionStorage.getItem(operatorKeyStorageName);
}

export function setOperatorKey(operatorKey: string): void {
    if (typeof window === "undefined") return;
    window.sessionStorage.setItem(operatorKeyStorageName, operatorKey);
}

export function clearOperatorKey(): void {
    if (typeof window === "undefined") return;
    window.sessionStorage.removeItem(operatorKeyStorageName);
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
    const headers = new Headers(options?.headers);
    const operatorKey = getOperatorKey();
    if (options?.body != null && !headers.has("Content-Type")) {
        headers.set("Content-Type", "application/json");
    }
    if (operatorKey) {
        headers.set("X-Operator-Key", operatorKey);
    }

    const response = await fetch(`${apiBase}${path}`, {
        ...options,
        headers,
    });

    if (!response.ok) {
        const body = (await response.json().catch(() => null)) as
            | { detail?: string }
            | null;
        const error = new ApiError(
            body?.detail ?? `Request failed with status ${response.status}`,
            response.status,
        );
        if (response.status === 401 && typeof window !== "undefined") {
            clearOperatorKey();
            window.dispatchEvent(new Event(operatorAuthenticationRequiredEvent));
        }
        throw error;
    }

    return (await response.json()) as T;
}

export function listTickets(status?: TicketStatus): Promise<Ticket[]> {
    const query = status ? `?status=${encodeURIComponent(status)}` : "";
    return request<Ticket[]>(`/tickets${query}`);
}

export function getTicket(ticketId: number): Promise<TicketDetail> {
    return request<TicketDetail>(`/tickets/${ticketId}`);
}

export function updateTicket(
    ticketId: number,
    update: TicketUpdate,
): Promise<Ticket> {
    return request<Ticket>(`/tickets/${ticketId}`, {
        method: "PATCH",
        body: JSON.stringify(update),
    });
}

export function addDecision(
    ticketId: number,
    decision: DecisionCreate,
): Promise<AgentDecision> {
    return request<AgentDecision>(`/tickets/${ticketId}/decisions`, {
        method: "POST",
        body: JSON.stringify(decision),
    });
}
