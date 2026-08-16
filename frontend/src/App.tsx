import { FormEvent, useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
    AlertCircle,
    ArrowLeft,
    Bot,
    CheckCircle2,
    Clock3,
    Inbox,
    KeyRound,
    LoaderCircle,
    LockKeyhole,
    LogOut,
    Mail,
    RefreshCw,
    Search,
    Send,
    ShieldAlert,
    UserRound,
} from "lucide-react";
import {
    addDecision,
    clearOperatorKey,
    getOperatorKey,
    getTicket,
    listTickets,
    operatorAuthenticationRequiredEvent,
    setOperatorKey,
    updateTicket,
    type Ticket,
    type TicketPriority,
    type TicketStatus,
} from "./api";

const statusOptions: Array<{ value: TicketStatus | "all"; label: string }> = [
    { value: "all", label: "All" },
    { value: "open", label: "Open" },
    { value: "in_progress", label: "In progress" },
    { value: "escalated", label: "Escalated" },
    { value: "auto_resolved", label: "Auto resolved" },
    { value: "closed", label: "Closed" },
];

const editableStatuses: Array<{ value: TicketStatus; label: string }> =
    statusOptions.filter(
        (option): option is { value: TicketStatus; label: string } =>
            option.value !== "all",
    );

const priorities: TicketPriority[] = ["low", "medium", "high", "critical"];

function formatLabel(value: string | null): string {
    if (!value) return "Unspecified";
    return value.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function formatDate(value: string): string {
    return new Intl.DateTimeFormat(undefined, {
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit",
    }).format(new Date(value));
}

function initials(name: string | null, email: string): string {
    const source = name?.trim() || email.split("@")[0];
    return source
        .split(/[\s._-]+/)
        .slice(0, 2)
        .map((part) => part[0]?.toUpperCase())
        .join("");
}

function StatusBadge({ status }: { status: TicketStatus }) {
    return <span className={`status-badge status-${status}`}>{formatLabel(status)}</span>;
}

function TicketRow({
    ticket,
    selected,
    onSelect,
}: {
    ticket: Ticket;
    selected: boolean;
    onSelect: () => void;
}) {
    return (
        <button
            type="button"
            className={`ticket-row${selected ? " selected" : ""}`}
            onClick={onSelect}
            aria-pressed={selected}
        >
            <span className="avatar" aria-hidden="true">
                {initials(ticket.customer_name, ticket.customer_email)}
            </span>
            <span className="ticket-summary">
                <span className="ticket-row-topline">
                    <strong>{ticket.subject}</strong>
                    <time dateTime={ticket.updated_at}>{formatDate(ticket.updated_at)}</time>
                </span>
                <span className="customer-line">
                    {ticket.customer_name || ticket.customer_email}
                    <span aria-hidden="true">·</span>
                    {ticket.ticket_number}
                </span>
                <span className="ticket-preview">{ticket.description}</span>
                <span className="ticket-tags">
                    <StatusBadge status={ticket.status} />
                    {ticket.priority && (
                        <span className={`priority priority-${ticket.priority}`}>
                            {formatLabel(ticket.priority)}
                        </span>
                    )}
                    {ticket.category && <span className="category">{formatLabel(ticket.category)}</span>}
                </span>
            </span>
        </button>
    );
}

function EmptyQueue({ filtered }: { filtered: boolean }) {
    return (
        <div className="empty-state">
            <Inbox size={30} strokeWidth={1.5} />
            <strong>{filtered ? "No matching tickets" : "Queue is clear"}</strong>
            <span>
                {filtered
                    ? "Adjust the status filter or search terms."
                    : "New support requests will appear here."}
            </span>
        </div>
    );
}

function DetailPanel({ ticketId, onClose }: { ticketId: number; onClose: () => void }) {
    const queryClient = useQueryClient();
    const [decisionReason, setDecisionReason] = useState("");
    const detailQuery = useQuery({
        queryKey: ["ticket", ticketId],
        queryFn: () => getTicket(ticketId),
    });

    const refreshTicket = async () => {
        await Promise.all([
            queryClient.invalidateQueries({ queryKey: ["tickets"] }),
            queryClient.invalidateQueries({ queryKey: ["ticket", ticketId] }),
        ]);
    };

    const updateMutation = useMutation({
        mutationFn: (update: Parameters<typeof updateTicket>[1]) =>
            updateTicket(ticketId, update),
        onSuccess: refreshTicket,
    });

    const decisionMutation = useMutation({
        mutationFn: (reason: string) =>
            addDecision(ticketId, {
                decision: "agent_reviewed",
                reason,
                model: "human-operator",
            }),
        onSuccess: async () => {
            setDecisionReason("");
            await refreshTicket();
        },
    });

    const submitDecision = (event: FormEvent) => {
        event.preventDefault();
        const reason = decisionReason.trim();
        if (reason) decisionMutation.mutate(reason);
    };

    if (detailQuery.isPending) {
        return (
            <aside className="detail-panel detail-loading" aria-live="polite">
                <LoaderCircle className="spin" />
                <span>Loading ticket</span>
            </aside>
        );
    }

    if (detailQuery.isError) {
        return (
            <aside className="detail-panel detail-loading">
                <AlertCircle />
                <strong>Ticket unavailable</strong>
                <span>{detailQuery.error.message}</span>
                <button className="button secondary" onClick={() => detailQuery.refetch()}>
                    <RefreshCw size={16} /> Retry
                </button>
            </aside>
        );
    }

    const ticket = detailQuery.data;
    const mutationError = updateMutation.error ?? decisionMutation.error;

    return (
        <aside className="detail-panel" aria-label={`Ticket ${ticket.ticket_number}`}>
            <header className="detail-header">
                <button className="icon-button mobile-back" onClick={onClose} title="Back to queue">
                    <ArrowLeft size={19} />
                </button>
                <div>
                    <span className="eyebrow">{ticket.ticket_number}</span>
                    <h2>{ticket.subject}</h2>
                </div>
                <StatusBadge status={ticket.status} />
            </header>

            <div className="detail-scroll">
                <section className="customer-strip">
                    <span className="avatar avatar-large" aria-hidden="true">
                        {initials(ticket.customer_name, ticket.customer_email)}
                    </span>
                    <div>
                        <strong>{ticket.customer_name || "Unknown customer"}</strong>
                        <a href={`mailto:${ticket.customer_email}`}>{ticket.customer_email}</a>
                    </div>
                    <time dateTime={ticket.created_at}>{formatDate(ticket.created_at)}</time>
                </section>

                <section className="message-body" aria-label="Customer message">
                    <div className="section-heading">
                        <Mail size={17} />
                        <h3>Customer message</h3>
                    </div>
                    <p>{ticket.description}</p>
                </section>

                <section className="classification-band" aria-label="AI classification">
                    <div>
                        <span>Category</span>
                        <strong>{formatLabel(ticket.category)}</strong>
                    </div>
                    <div>
                        <span>Sentiment</span>
                        <strong>{formatLabel(ticket.sentiment)}</strong>
                    </div>
                    <div>
                        <span>Product</span>
                        <strong>{ticket.product || "Unspecified"}</strong>
                    </div>
                    <div>
                        <span>Confidence</span>
                        <strong>{ticket.confidence == null ? "N/A" : `${Math.round(ticket.confidence * 100)}%`}</strong>
                    </div>
                </section>

                <section className="control-section">
                    <div className="section-heading">
                        <UserRound size={17} />
                        <h3>Ownership and state</h3>
                    </div>
                    <div className="control-grid">
                        <label>
                            Status
                            <select
                                value={ticket.status}
                                disabled={updateMutation.isPending}
                                onChange={(event) =>
                                    updateMutation.mutate({ status: event.target.value as TicketStatus })
                                }
                            >
                                {editableStatuses.map((option) => (
                                    <option key={option.value} value={option.value}>{option.label}</option>
                                ))}
                            </select>
                        </label>
                        <label>
                            Priority
                            <select
                                value={ticket.priority ?? "medium"}
                                disabled={updateMutation.isPending}
                                onChange={(event) =>
                                    updateMutation.mutate({ priority: event.target.value as TicketPriority })
                                }
                            >
                                {priorities.map((priority) => (
                                    <option key={priority} value={priority}>{formatLabel(priority)}</option>
                                ))}
                            </select>
                        </label>
                        <label className="assignee-field">
                            Assignee
                            <input
                                defaultValue={ticket.assignee ?? ""}
                                placeholder="Unassigned"
                                onBlur={(event) => {
                                    const assignee = event.target.value.trim();
                                    if (assignee !== (ticket.assignee ?? "")) {
                                        updateMutation.mutate({ assignee });
                                    }
                                }}
                            />
                        </label>
                    </div>
                    {updateMutation.isPending && <span className="saving"><LoaderCircle className="spin" size={14} /> Saving</span>}
                </section>

                <section className="decision-section">
                    <div className="section-heading">
                        <ShieldAlert size={17} />
                        <h3>Agent decision</h3>
                    </div>
                    <form onSubmit={submitDecision}>
                        <textarea
                            value={decisionReason}
                            onChange={(event) => setDecisionReason(event.target.value)}
                            placeholder="Record the reason for your review or action"
                            rows={3}
                            required
                        />
                        <button className="button primary" disabled={decisionMutation.isPending || !decisionReason.trim()}>
                            {decisionMutation.isPending ? <LoaderCircle className="spin" size={16} /> : <Send size={16} />}
                            Record decision
                        </button>
                    </form>
                </section>

                {mutationError && <div className="inline-error"><AlertCircle size={16} />{mutationError.message}</div>}

                <section className="timeline-section">
                    <div className="section-heading">
                        <Clock3 size={17} />
                        <h3>Activity</h3>
                        <span>{ticket.history.length + ticket.decisions.length}</span>
                    </div>
                    <div className="timeline">
                        {[...ticket.history.map((item) => ({
                            id: `history-${item.id}`,
                            title: formatLabel(item.action),
                            detail: item.new_value || item.old_value || "Ticket updated",
                            actor: item.actor,
                            date: item.created_at,
                            automated: item.actor !== "system" ? false : true,
                        })), ...ticket.decisions.map((item) => ({
                            id: `decision-${item.id}`,
                            title: formatLabel(item.decision),
                            detail: item.reason,
                            actor: item.model || "agent",
                            date: item.created_at,
                            automated: item.model !== "human-operator",
                        }))]
                            .sort((a, b) => new Date(b.date).getTime() - new Date(a.date).getTime())
                            .map((item) => (
                                <article className="timeline-item" key={item.id}>
                                    <span className="timeline-icon" aria-hidden="true">
                                        {item.automated ? <Bot size={15} /> : <CheckCircle2 size={15} />}
                                    </span>
                                    <div>
                                        <strong>{item.title}</strong>
                                        <p>{item.detail}</p>
                                        <span>{item.actor} · {formatDate(item.date)}</span>
                                    </div>
                                </article>
                            ))}
                    </div>
                </section>
            </div>
        </aside>
    );
}

function OperatorKeyGate({ onUnlock }: { onUnlock: () => void }) {
    const [operatorKey, updateOperatorKey] = useState("");

    const submit = (event: FormEvent<HTMLFormElement>) => {
        event.preventDefault();
        const normalized = operatorKey.trim();
        if (!normalized) return;
        setOperatorKey(normalized);
        onUnlock();
    };

    return (
        <main className="authentication-shell">
            <section className="authentication-card" aria-labelledby="operator-access-title">
                <span className="authentication-icon" aria-hidden="true">
                    <LockKeyhole size={25} />
                </span>
                <span className="eyebrow">Restricted console</span>
                <h1 id="operator-access-title">Operator access</h1>
                <p>
                    Enter the operator key issued by your administrator. It is kept only for
                    this browser session and is never included in the dashboard build.
                </p>
                <form onSubmit={submit}>
                    <label htmlFor="operator-key">Operator key</label>
                    <div className="key-field">
                        <KeyRound size={17} aria-hidden="true" />
                        <input
                            id="operator-key"
                            name="operator-key"
                            type="password"
                            value={operatorKey}
                            onChange={(event) => updateOperatorKey(event.target.value)}
                            autoComplete="current-password"
                            autoFocus
                            required
                        />
                    </div>
                    <button className="button primary" disabled={!operatorKey.trim()}>
                        Unlock dashboard
                    </button>
                </form>
            </section>
        </main>
    );
}

export default function App() {
    const queryClient = useQueryClient();
    const [authenticated, setAuthenticated] = useState(() => Boolean(getOperatorKey()));
    const [status, setStatus] = useState<TicketStatus | "all">("all");
    const [search, setSearch] = useState("");
    const [selectedId, setSelectedId] = useState<number | null>(null);

    useEffect(() => {
        const requireAuthentication = () => {
            setAuthenticated(false);
            setSelectedId(null);
            queryClient.clear();
        };
        window.addEventListener(
            operatorAuthenticationRequiredEvent,
            requireAuthentication,
        );
        return () => {
            window.removeEventListener(
                operatorAuthenticationRequiredEvent,
                requireAuthentication,
            );
        };
    }, [queryClient]);

    const ticketsQuery = useQuery({
        queryKey: ["tickets", status],
        queryFn: () => listTickets(status === "all" ? undefined : status),
        refetchInterval: authenticated ? 30_000 : false,
        enabled: authenticated,
    });

    const filteredTickets = useMemo(() => {
        const query = search.trim().toLowerCase();
        if (!query) return ticketsQuery.data ?? [];
        return (ticketsQuery.data ?? []).filter((ticket) =>
            [ticket.subject, ticket.ticket_number, ticket.customer_email, ticket.customer_name, ticket.category]
                .filter(Boolean)
                .some((value) => value!.toLowerCase().includes(query)),
        );
    }, [search, ticketsQuery.data]);

    const visibleSelectedId =
        selectedId && (ticketsQuery.data ?? []).some((ticket) => ticket.id === selectedId)
            ? selectedId
            : null;
    const escalatedCount = (ticketsQuery.data ?? []).filter((ticket) => ticket.status === "escalated").length;

    const unlock = () => {
        setAuthenticated(true);
        void queryClient.invalidateQueries({ queryKey: ["tickets"] });
    };

    const lock = () => {
        clearOperatorKey();
        setAuthenticated(false);
        setSelectedId(null);
        queryClient.clear();
    };

    if (!authenticated) {
        return <OperatorKeyGate onUnlock={unlock} />;
    }

    return (
        <div className="app-shell">
            <header className="topbar">
                <div className="brand-mark"><Inbox size={21} /></div>
                <div className="brand-copy">
                    <strong>Support Operations</strong>
                    <span>Autonomous ticket queue</span>
                </div>
                <div className="topbar-status">
                    <span className="live-dot" />
                    Operator authenticated
                </div>
                <button
                    className="icon-button topbar-refresh"
                    onClick={() => ticketsQuery.refetch()}
                    disabled={ticketsQuery.isFetching}
                    title="Refresh queue"
                    aria-label="Refresh queue"
                >
                    <RefreshCw size={18} className={ticketsQuery.isFetching ? "spin" : ""} />
                </button>
                <button
                    className="icon-button"
                    onClick={lock}
                    title="Lock dashboard"
                    aria-label="Lock dashboard"
                >
                    <LogOut size={18} />
                </button>
            </header>

            <main className={`workspace${visibleSelectedId ? " has-selection" : ""}`}>
                <section className="queue-panel" aria-label="Ticket queue">
                    <div className="queue-header">
                        <div>
                            <span className="eyebrow">Operations queue</span>
                            <h1>Support inbox</h1>
                        </div>
                        <div className="queue-metrics">
                            <span><strong>{ticketsQuery.data?.length ?? 0}</strong> tickets</span>
                            <span className={escalatedCount ? "metric-alert" : ""}><strong>{escalatedCount}</strong> escalated</span>
                        </div>
                    </div>

                    <div className="queue-tools">
                        <label className="search-field">
                            <Search size={17} aria-hidden="true" />
                            <span className="sr-only">Search tickets</span>
                            <input
                                type="search"
                                value={search}
                                onChange={(event) => setSearch(event.target.value)}
                                placeholder="Search subject, customer, or ticket"
                            />
                        </label>
                        <div className="status-tabs" role="tablist" aria-label="Filter by status">
                            {statusOptions.map((option) => (
                                <button
                                    key={option.value}
                                    type="button"
                                    role="tab"
                                    aria-selected={status === option.value}
                                    className={status === option.value ? "active" : ""}
                                    onClick={() => setStatus(option.value)}
                                >
                                    {option.label}
                                </button>
                            ))}
                        </div>
                    </div>

                    <div className="ticket-list" aria-live="polite">
                        {ticketsQuery.isPending ? (
                            <div className="queue-loading"><LoaderCircle className="spin" /><span>Loading queue</span></div>
                        ) : ticketsQuery.isError ? (
                            <div className="empty-state error-state">
                                <AlertCircle size={30} />
                                <strong>Queue unavailable</strong>
                                <span>{ticketsQuery.error.message}</span>
                                <button className="button secondary" onClick={() => ticketsQuery.refetch()}>
                                    <RefreshCw size={16} /> Retry
                                </button>
                            </div>
                        ) : filteredTickets.length === 0 ? (
                            <EmptyQueue filtered={Boolean(search || status !== "all")} />
                        ) : (
                            filteredTickets.map((ticket) => (
                                <TicketRow
                                    key={ticket.id}
                                    ticket={ticket}
                                    selected={ticket.id === visibleSelectedId}
                                    onSelect={() => setSelectedId(ticket.id)}
                                />
                            ))
                        )}
                    </div>
                </section>

                {visibleSelectedId ? (
                    <DetailPanel ticketId={visibleSelectedId} onClose={() => setSelectedId(null)} />
                ) : (
                    <aside className="detail-panel detail-empty">
                        <Mail size={34} strokeWidth={1.4} />
                        <strong>Select a ticket</strong>
                        <span>Review the customer message, classification, ownership, and full activity history.</span>
                    </aside>
                )}
            </main>
        </div>
    );
}
