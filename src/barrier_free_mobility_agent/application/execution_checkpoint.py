from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from barrier_free_mobility_agent.domain.run_ledger import RunExecutionCorrelation


class ExecutionCheckpointState(StrEnum):
    """Safe execution-recovery projection of one LangGraph checkpoint."""

    MISSING = "missing"
    CONTINUABLE = "continuable"
    INTERRUPTED = "interrupted"
    TERMINAL = "terminal"


class ExecutionCheckpointInspection(BaseModel):
    """Opaque checkpoint metadata; graph state and user input never cross this boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    state: ExecutionCheckpointState
    cursor: (
        Annotated[
            str,
            Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._:-]+$"),
        ]
        | None
    ) = None
    correlation: RunExecutionCorrelation | None = None
    execution_job_id: (
        Annotated[
            str,
            Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._:-]+$"),
        ]
        | None
    ) = None
    lease_generation: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_execution_provenance(self) -> Self:
        provenance = (self.execution_job_id, self.lease_generation)
        if (provenance[0] is None) != (provenance[1] is None):
            raise ValueError("checkpoint execution provenance must be complete")
        if provenance[0] is not None and self.correlation is None:
            raise ValueError("checkpoint execution provenance requires correlation")
        return self
