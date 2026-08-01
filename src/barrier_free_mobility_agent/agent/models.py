from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from barrier_free_mobility_agent.domain.mobility import SlotSource
from barrier_free_mobility_agent.domain.models import (
    AnswerOutcome,
    ConversationContextView,
    FacilityQuestionKind,
    FacilityType,
)
from barrier_free_mobility_agent.domain.routing import AgentIntent
from barrier_free_mobility_agent.domain.usage import RunUsage


class TripContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    origin_query: str | None = None
    destination_query: str | None = None
    origin_station: str | None = None
    destination_station: str | None = None
    origin_line: str | None = None
    destination_line: str | None = None


class FacilityContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target_query: str | None = None
    target_station: str | None = None
    target_line: str | None = None
    target_source: SlotSource | None = None
    facility_types: tuple[FacilityType, ...] = ()
    facility_types_source: SlotSource | None = None
    question_kind: FacilityQuestionKind | None = None
    accessible_restroom_confirmation_pending: bool = False


class ClarificationKind(StrEnum):
    REQUIRED_FIELDS = "required_fields"
    STATION_SELECTION = "station_selection"
    MCP_CLARIFICATION = "mcp_clarification"
    UNSUPPORTED_INTENT = "unsupported_intent"


class PendingPrompt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    question: str
    fields: tuple[str, ...] = ()


class PendingClarification(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: ClarificationKind
    intent: AgentIntent | None = None
    fields: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()
    questions: tuple[str, ...]
    prompts: tuple[PendingPrompt, ...] = ()
    message: str
    candidates: tuple[dict[str, str | None], ...] = ()


class GraphTurnStatus(StrEnum):
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    FAILED = "failed"


class ResumeMode(StrEnum):
    NONE = "none"
    COMMAND = "command"
    NEW_TURN = "new_turn"


class GraphTurn(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: GraphTurnStatus
    resume_mode: ResumeMode
    outcome: AnswerOutcome
    context: ConversationContextView = Field(default_factory=ConversationContextView)
    state: dict[str, Any]
    usage: RunUsage = Field(default_factory=RunUsage)
    interrupt_id: str | None = None


class ExecutionLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_graph_steps: int = Field(ge=1)
    max_tool_calls: int = Field(ge=1)
    max_model_calls: int = Field(ge=0)
    max_parallel_tools: int = Field(ge=1)
    max_clarification_cycles: int = Field(ge=1)
