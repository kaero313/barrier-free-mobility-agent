from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from barrier_free_mobility_agent.domain.clarification import FieldsClarificationAnswer
from barrier_free_mobility_agent.domain.errors import StateInputBudgetExceededError
from barrier_free_mobility_agent.domain.run_ledger import RunExecutionCorrelation

EXECUTION_PAYLOAD_MAX_BYTES = 65_536

type Sha256Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
type ExecutionIdentifier = Annotated[
    str,
    Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._:-]+$"),
]


class ExecutionOperation(StrEnum):
    START = "start"
    RESUME_COMMAND = "resume_command"
    RESUME_NEW_TURN = "resume_new_turn"


class ExecutionJobState(StrEnum):
    QUEUED = "queued"
    LEASED = "leased"
    COMPLETED = "completed"
    FAILED = "failed"


class ExecutionWorkerRegistrationState(StrEnum):
    READY = "ready"
    DRAINING = "draining"


class ExecutionWorkerRegistration(BaseModel):
    """Fenced, renewable presence record for one execution worker identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    worker_id: ExecutionIdentifier
    token: ExecutionIdentifier
    generation: int = Field(ge=1)
    state: ExecutionWorkerRegistrationState
    registered_at: datetime
    heartbeat_at: datetime
    expires_at: datetime

    @field_validator("registered_at", "heartbeat_at", "expires_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("worker registration timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_timestamps(self) -> Self:
        if self.heartbeat_at < self.registered_at:
            raise ValueError("heartbeat_at cannot precede registered_at")
        if self.expires_at <= self.heartbeat_at:
            raise ValueError("expires_at must follow heartbeat_at")
        return self


class ExecutionQueueHealth(BaseModel):
    """Internal queue and worker-presence snapshot from one authoritative clock."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    checked_at: datetime
    accepting_new_jobs: bool
    ready_worker_count: int = Field(ge=0)
    queued_job_count: int = Field(ge=0)
    leased_job_count: int = Field(ge=0)
    expired_lease_count: int = Field(ge=0)
    recovery_attempt_count: int = Field(ge=0)
    stuck_job_count: int = Field(ge=0)
    oldest_queued_at: datetime | None = None
    oldest_queued_age_seconds: float | None = Field(default=None, ge=0)
    stuck_after_seconds: float = Field(gt=0)

    @field_validator("checked_at", "oldest_queued_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("queue health timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_snapshot(self) -> Self:
        if self.accepting_new_jobs != (self.ready_worker_count > 0):
            raise ValueError("accepting_new_jobs must reflect ready_worker_count")
        if self.expired_lease_count > self.leased_job_count:
            raise ValueError("expired leases cannot exceed leased jobs")
        if self.stuck_job_count > self.queued_job_count + self.leased_job_count:
            raise ValueError("stuck jobs cannot exceed active jobs")
        oldest_fields = (self.oldest_queued_at, self.oldest_queued_age_seconds)
        if self.queued_job_count == 0 and any(value is not None for value in oldest_fields):
            raise ValueError("an empty queue cannot have oldest-job metadata")
        if self.queued_job_count > 0 and any(value is None for value in oldest_fields):
            raise ValueError("a non-empty queue requires oldest-job metadata")
        if self.oldest_queued_at is not None and self.oldest_queued_at > self.checked_at:
            raise ValueError("oldest_queued_at cannot follow checked_at")
        return self


class StartExecutionPayload(BaseModel):
    """Private input for one initial graph invocation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    operation: Literal[ExecutionOperation.START] = ExecutionOperation.START
    question: str = Field(min_length=1, max_length=20_000)

    @field_validator("question")
    @classmethod
    def require_nonblank_question(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("question must not be blank")
        return value


class ResumeCommandExecutionPayload(BaseModel):
    """Private input for a LangGraph Command(resume=...) invocation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    operation: Literal[ExecutionOperation.RESUME_COMMAND] = ExecutionOperation.RESUME_COMMAND
    answer: Annotated[str, Field(min_length=1, max_length=20_000)] | FieldsClarificationAnswer

    @field_validator("answer")
    @classmethod
    def require_nonblank_text_answer(
        cls,
        value: str | FieldsClarificationAnswer,
    ) -> str | FieldsClarificationAnswer:
        if isinstance(value, str) and not value.strip():
            raise ValueError("answer must not be blank")
        return value


class ResumeNewTurnExecutionPayload(BaseModel):
    """Private input for a bounded business-clarification follow-up turn."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    operation: Literal[ExecutionOperation.RESUME_NEW_TURN] = ExecutionOperation.RESUME_NEW_TURN
    answer: str = Field(min_length=1, max_length=20_000)

    @field_validator("answer")
    @classmethod
    def require_nonblank_answer(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("answer must not be blank")
        return value


type ResumeExecutionPayload = ResumeCommandExecutionPayload | ResumeNewTurnExecutionPayload
type ExecutionPayload = Annotated[
    StartExecutionPayload | ResumeCommandExecutionPayload | ResumeNewTurnExecutionPayload,
    Field(discriminator="operation"),
]


def validate_execution_payload_size(payload: ExecutionPayload) -> None:
    """Reject oversized private input without retaining it in a validation error."""

    serialized_bytes = len(payload.model_dump_json().encode("utf-8"))
    if serialized_bytes > EXECUTION_PAYLOAD_MAX_BYTES:
        raise StateInputBudgetExceededError(
            context={
                "budget": "execution_payload_bytes",
                "limit": EXECUTION_PAYLOAD_MAX_BYTES,
                "actual": serialized_bytes,
            }
        )


class RunExecutionJob(BaseModel):
    """Durable coordination state; terminal jobs never retain private input."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    job_id: ExecutionIdentifier
    owner_id: str = Field(min_length=1, max_length=200)
    correlation: RunExecutionCorrelation
    operation: ExecutionOperation
    payload_schema_version: Literal[1] = 1
    payload: ExecutionPayload | None = Field(repr=False)
    idempotency_digest: Sha256Digest = Field(
        description=(
            "Domain-separated HMAC of operation, target, and client idempotency key; "
            "owner scope is stored separately."
        )
    )
    request_fingerprint: Sha256Digest
    state: ExecutionJobState
    available_at: datetime
    checkpoint_cursor: ExecutionIdentifier | None = None
    claim_count: int = Field(default=0, ge=0)
    lease_generation: int = Field(default=0, ge=0)
    lease_token: ExecutionIdentifier | None = None
    lease_owner: ExecutionIdentifier | None = None
    lease_expires_at: datetime | None = None
    heartbeat_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    failure_code: (
        Annotated[
            str,
            Field(min_length=1, max_length=100, pattern=r"^[A-Z][A-Z0-9_]*$"),
        ]
        | None
    ) = None

    @field_validator(
        "available_at",
        "lease_expires_at",
        "heartbeat_at",
        "created_at",
        "updated_at",
        "completed_at",
    )
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("execution timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        if self.payload is not None and self.payload.operation != self.operation:
            raise ValueError("operation and payload must match")
        if self.claim_count != self.lease_generation:
            raise ValueError("claim count and lease generation must match")
        lease_fields = (
            self.lease_token,
            self.lease_owner,
            self.lease_expires_at,
            self.heartbeat_at,
        )
        if self.state == ExecutionJobState.QUEUED:
            if self.payload is None or any(value is not None for value in lease_fields):
                raise ValueError("queued jobs require payload and no active lease")
            if self.completed_at is not None or self.failure_code is not None:
                raise ValueError("queued jobs cannot contain terminal fields")
        elif self.state == ExecutionJobState.LEASED:
            if self.payload is None or any(value is None for value in lease_fields):
                raise ValueError("leased jobs require payload and complete lease metadata")
            if self.lease_generation < 1:
                raise ValueError("leased jobs require a positive lease generation")
            if self.completed_at is not None or self.failure_code is not None:
                raise ValueError("leased jobs cannot contain terminal fields")
            assert self.heartbeat_at is not None
            assert self.lease_expires_at is not None
            if self.heartbeat_at < self.created_at:
                raise ValueError("heartbeat_at cannot precede created_at")
            if self.lease_expires_at <= self.heartbeat_at:
                raise ValueError("lease_expires_at must follow heartbeat_at")
        elif self.state == ExecutionJobState.COMPLETED:
            if self.payload is not None or any(value is not None for value in lease_fields):
                raise ValueError("completed jobs must scrub payload and lease metadata")
            if self.completed_at is None or self.failure_code is not None:
                raise ValueError("completed jobs require only a completion timestamp")
        else:
            if self.payload is not None or any(value is not None for value in lease_fields):
                raise ValueError("failed jobs must scrub payload and lease metadata")
            if self.completed_at is None or self.failure_code is None:
                raise ValueError("failed jobs require terminal metadata")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        if self.completed_at is not None and self.completed_at < self.created_at:
            raise ValueError("completed_at cannot precede created_at")
        return self


class ExecutionReservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    job: RunExecutionJob
    replayed: bool

    @property
    def correlation(self) -> RunExecutionCorrelation:
        return self.job.correlation


class ExecutionLease(BaseModel):
    """Fenced worker claim for exactly one execution attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    job_id: ExecutionIdentifier
    owner_id: str = Field(min_length=1, max_length=200)
    correlation: RunExecutionCorrelation
    operation: ExecutionOperation
    payload_schema_version: Literal[1] = 1
    payload: ExecutionPayload = Field(repr=False)
    worker_id: ExecutionIdentifier
    token: ExecutionIdentifier
    generation: int = Field(ge=1)
    heartbeat_at: datetime
    expires_at: datetime
    checkpoint_cursor: ExecutionIdentifier | None = None

    @field_validator("heartbeat_at", "expires_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("expires_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_operation(self) -> Self:
        if self.payload.operation != self.operation:
            raise ValueError("operation and payload must match")
        if self.expires_at <= self.heartbeat_at:
            raise ValueError("expires_at must follow heartbeat_at")
        return self


class ThreadDeletionReservation(BaseModel):
    """Durable fence that blocks new execution reservations during checkpoint cleanup."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    thread_id: ExecutionIdentifier
    owner_id: str = Field(min_length=1, max_length=200)
    token: ExecutionIdentifier
    requested_at: datetime
    replayed: bool = False

    @field_validator("requested_at")
    @classmethod
    def require_requested_at_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("requested_at must be timezone-aware")
        return value
