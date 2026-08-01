from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from barrier_free_mobility_agent.domain.models import ResponseStatus
from barrier_free_mobility_agent.domain.routing import ToolCapability

type SafeIdentifier = Annotated[
    str,
    Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._:-]+$"),
]
type SafeRequestId = Annotated[
    str,
    Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$"),
]


class RunExecutionPhase(StrEnum):
    RUNNING = "running"
    INTERRUPTED = "interrupted"
    COMPLETED = "completed"
    FAILED = "failed"


class RunEventType(StrEnum):
    RUN_STARTED = "run.started"
    RUN_RESUME_STARTED = "run.resume_started"
    RUN_INTERRUPTED = "run.interrupted"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    RUN_ORPHANED = "run.orphaned"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"
    TOOL_TIMEOUT = "tool.timeout"


class ToolTransition(StrEnum):
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"


class ToolFailureCode(StrEnum):
    MCP_UNAVAILABLE = "MCP_UNAVAILABLE"
    MCP_CONTRACT_ERROR = "MCP_CONTRACT_ERROR"
    MCP_DEGRADED = "MCP_DEGRADED"
    MCP_TOOL_EXECUTION_ERROR = "MCP_TOOL_EXECUTION_ERROR"
    MCP_TIMEOUT = "MCP_TIMEOUT"
    MCP_RESPONSE_VALIDATION_ERROR = "MCP_RESPONSE_VALIDATION_ERROR"
    EXECUTION_CANCELLED = "EXECUTION_CANCELLED"
    UNEXPECTED_ERROR = "UNEXPECTED_ERROR"


class OrphanReason(StrEnum):
    RUN_STALE = "run_stale"
    TOOL_EXECUTION_STALE = "tool_execution_stale"


class RunExecutionCorrelation(BaseModel):
    """Opaque identifiers only; user and MCP text never belongs in ledger correlation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    thread_id: SafeIdentifier
    run_id: SafeIdentifier
    request_id: SafeRequestId
    attempt: int = Field(ge=1)


class ResolveStationToolSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    tool: Literal[ToolCapability.RESOLVE_STATION] = ToolCapability.RESOLVE_STATION
    matched: bool
    candidate_count: int = Field(ge=0, le=20)
    clarification_required: bool


class GenerateAccessibilityBriefToolSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    tool: Literal[ToolCapability.GENERATE_ACCESSIBILITY_BRIEF] = (
        ToolCapability.GENERATE_ACCESSIBILITY_BRIEF
    )
    response_status: ResponseStatus
    clarification_required: bool
    evidence_count: int = Field(ge=0, le=100)
    limitation_count: int = Field(ge=0, le=100)


class AnswerFacilityQuestionToolSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    tool: Literal[ToolCapability.ANSWER_FACILITY_QUESTION] = ToolCapability.ANSWER_FACILITY_QUESTION
    response_status: ResponseStatus
    clarification_required: bool
    evidence_count: int = Field(ge=0, le=100)


type ToolExecutionSummary = Annotated[
    ResolveStationToolSummary
    | GenerateAccessibilityBriefToolSummary
    | AnswerFacilityQuestionToolSummary,
    Field(discriminator="tool"),
]


class RunEventRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    event_id: SafeIdentifier
    event_key: SafeIdentifier
    correlation: RunExecutionCorrelation
    sequence: int = Field(ge=1)
    phase: RunExecutionPhase
    event_type: RunEventType
    occurred_at: datetime
    tool_execution_id: SafeIdentifier | None = None
    logical_call_id: SafeIdentifier | None = None
    tool_attempt: int | None = Field(default=None, ge=1)
    tool: ToolCapability | None = None
    tool_transition: ToolTransition | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    error_code: ToolFailureCode | None = None
    tool_summary: ToolExecutionSummary | None = None

    @field_validator("occurred_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_event_shape(self) -> Self:
        expected_phase = {
            RunEventType.RUN_STARTED: RunExecutionPhase.RUNNING,
            RunEventType.RUN_RESUME_STARTED: RunExecutionPhase.RUNNING,
            RunEventType.RUN_INTERRUPTED: RunExecutionPhase.INTERRUPTED,
            RunEventType.RUN_COMPLETED: RunExecutionPhase.COMPLETED,
            RunEventType.RUN_FAILED: RunExecutionPhase.FAILED,
            RunEventType.RUN_ORPHANED: RunExecutionPhase.RUNNING,
            RunEventType.TOOL_STARTED: RunExecutionPhase.RUNNING,
            RunEventType.TOOL_COMPLETED: RunExecutionPhase.RUNNING,
            RunEventType.TOOL_FAILED: RunExecutionPhase.RUNNING,
            RunEventType.TOOL_TIMEOUT: RunExecutionPhase.RUNNING,
        }[self.event_type]
        if self.phase != expected_phase:
            raise ValueError("run event type and execution phase must match")
        expected_transition = {
            RunEventType.TOOL_STARTED: ToolTransition.STARTED,
            RunEventType.TOOL_COMPLETED: ToolTransition.COMPLETED,
            RunEventType.TOOL_FAILED: ToolTransition.FAILED,
            RunEventType.TOOL_TIMEOUT: ToolTransition.TIMEOUT,
        }.get(self.event_type)
        tool_values = (
            self.tool_execution_id,
            self.logical_call_id,
            self.tool_attempt,
            self.tool,
            self.tool_transition,
            self.duration_ms,
            self.error_code,
            self.tool_summary,
        )
        if expected_transition is None:
            if any(value is not None for value in tool_values):
                raise ValueError("run events cannot contain Tool fields")
            return self
        if (
            self.tool_execution_id is None
            or self.logical_call_id is None
            or self.tool_attempt is None
            or self.tool is None
        ):
            raise ValueError(
                "Tool events require execution, logical call and capability identifiers"
            )
        if self.tool_transition != expected_transition:
            raise ValueError("Tool event type and transition must match")
        if self.event_type == RunEventType.TOOL_STARTED:
            if any(
                value is not None
                for value in (self.duration_ms, self.error_code, self.tool_summary)
            ):
                raise ValueError("Tool start events cannot contain terminal fields")
            return self
        if self.duration_ms is None:
            raise ValueError("terminal Tool events require duration_ms")
        if self.event_type == RunEventType.TOOL_COMPLETED:
            if self.error_code is not None or self.tool_summary is None:
                raise ValueError("completed Tool events require only an allowlisted summary")
            if self.tool_summary.tool != self.tool:
                raise ValueError("Tool summary capability must match the execution")
        elif self.error_code is None or self.tool_summary is not None:
            raise ValueError("failed Tool events require only an allowlisted error code")
        if (
            self.event_type == RunEventType.TOOL_TIMEOUT
            and self.error_code != ToolFailureCode.MCP_TIMEOUT
        ):
            raise ValueError("timeout Tool events require MCP_TIMEOUT")
        if (
            self.event_type == RunEventType.TOOL_FAILED
            and self.error_code == ToolFailureCode.MCP_TIMEOUT
        ):
            raise ValueError("MCP_TIMEOUT must use the timeout transition")
        return self


class ToolExecutionHandle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    execution_id: SafeIdentifier
    logical_call_id: SafeIdentifier
    attempt: int = Field(ge=1)
    correlation: RunExecutionCorrelation
    tool: ToolCapability
    started_event_sequence: int = Field(ge=1)
    started_at: datetime

    @field_validator("started_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("started_at must be timezone-aware")
        return value

    @classmethod
    def from_started_event(cls, event: RunEventRecord) -> Self:
        if (
            event.event_type != RunEventType.TOOL_STARTED
            or event.tool_execution_id is None
            or event.logical_call_id is None
            or event.tool_attempt is None
            or event.tool is None
        ):
            raise ValueError("a Tool started event is required")
        return cls(
            execution_id=event.tool_execution_id,
            logical_call_id=event.logical_call_id,
            attempt=event.tool_attempt,
            correlation=event.correlation,
            tool=event.tool,
            started_event_sequence=event.sequence,
            started_at=event.occurred_at,
        )


class ToolExecutionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    handle: ToolExecutionHandle
    transition: ToolTransition
    terminal_event_sequence: int = Field(ge=1)
    completed_at: datetime
    duration_ms: int = Field(ge=0)
    error_code: ToolFailureCode | None = None
    summary: ToolExecutionSummary | None = None

    @field_validator("completed_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("completed_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_terminal_shape(self) -> Self:
        if self.transition == ToolTransition.STARTED:
            raise ValueError("ToolExecutionRecord requires a terminal transition")
        if self.terminal_event_sequence <= self.handle.started_event_sequence:
            raise ValueError("terminal event sequence must follow the start event")
        if self.transition == ToolTransition.COMPLETED:
            if self.error_code is not None or self.summary is None:
                raise ValueError("completed executions require only an allowlisted summary")
            if self.summary.tool != self.handle.tool:
                raise ValueError("Tool summary capability must match the execution")
        elif self.error_code is None or self.summary is not None:
            raise ValueError("failed executions require only an allowlisted error code")
        if (
            self.transition == ToolTransition.TIMEOUT
            and self.error_code != ToolFailureCode.MCP_TIMEOUT
        ):
            raise ValueError("timeout executions require MCP_TIMEOUT")
        if (
            self.transition == ToolTransition.FAILED
            and self.error_code == ToolFailureCode.MCP_TIMEOUT
        ):
            raise ValueError("MCP_TIMEOUT must use the timeout transition")
        return self


class OrphanedRunView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    correlation: RunExecutionCorrelation
    phase: Literal[RunExecutionPhase.RUNNING] = RunExecutionPhase.RUNNING
    reasons: tuple[OrphanReason, ...] = Field(min_length=1, max_length=2)
    last_sequence: int = Field(ge=1)
    last_event_type: RunEventType
    last_activity_at: datetime
    open_tool_execution_ids: tuple[SafeIdentifier, ...] = Field(max_length=32)

    @field_validator("last_activity_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("last_activity_at must be timezone-aware")
        return value
