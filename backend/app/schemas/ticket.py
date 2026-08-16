from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

from app.database.models import TicketPriority, TicketStatus


class TicketCreate(BaseModel):
    customer_email: EmailStr
    customer_name: str | None = Field(default=None, max_length=255)
    subject: str = Field(min_length=1, max_length=998)
    description: str = Field(min_length=1)
    category: str | None = Field(default=None, max_length=64)
    priority: TicketPriority | None = None
    sentiment: str | None = Field(default=None, max_length=32)
    product: str | None = Field(default=None, max_length=255)
    assignee: str | None = Field(default=None, max_length=255)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class TicketUpdate(BaseModel):
    customer_name: str | None = Field(default=None, max_length=255)
    subject: str | None = Field(default=None, min_length=1, max_length=998)
    description: str | None = Field(default=None, min_length=1)
    category: str | None = Field(default=None, max_length=64)
    priority: TicketPriority | None = None
    sentiment: str | None = Field(default=None, max_length=32)
    product: str | None = Field(default=None, max_length=255)
    status: TicketStatus | None = None
    assignee: str | None = Field(default=None, max_length=255)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def reject_null_for_required_columns(self) -> "TicketUpdate":
        for field in ("subject", "description", "status"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


class TicketHistoryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    action: str
    old_value: str | None
    new_value: str | None
    actor: str
    created_at: datetime


class AgentDecisionCreate(BaseModel):
    decision: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=1)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    model: str | None = Field(default=None, max_length=128)


class AgentDecisionRead(AgentDecisionCreate):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime


class TicketRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    ticket_number: str
    customer_email: str
    customer_name: str | None
    subject: str
    description: str
    category: str | None
    priority: TicketPriority | None
    sentiment: str | None
    product: str | None
    status: TicketStatus
    assignee: str | None
    confidence: float | None
    created_at: datetime
    updated_at: datetime


class TicketDetail(TicketRead):
    history: list[TicketHistoryRead]
    decisions: list[AgentDecisionRead]
