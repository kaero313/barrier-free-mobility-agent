from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Literal, Protocol

from barrier_free_mobility_agent.domain.execution_queue import (
    ExecutionJobState,
    ExecutionLease,
)
from barrier_free_mobility_agent.domain.routing import ToolCapability
from barrier_free_mobility_agent.domain.run_ledger import (
    OrphanedRunView,
    RunEventRecord,
    RunExecutionCorrelation,
    ToolExecutionHandle,
    ToolExecutionRecord,
    ToolExecutionSummary,
    ToolFailureCode,
    ToolTransition,
)

if TYPE_CHECKING:
    from barrier_free_mobility_agent.application.conversations import ConversationRun


class RunLedgerWriter(Protocol):
    """Atomic run-ledger commands; implementations allocate event sequences."""

    async def reserve_initial_run(
        self,
        correlation: RunExecutionCorrelation,
        *,
        owner_id: str,
        capacity: int,
    ) -> RunEventRecord: ...

    async def reserve_resume_attempt(
        self,
        run_id: str,
        *,
        request_id: str,
        owner_id: str,
    ) -> RunEventRecord: ...

    async def finalize_run(
        self,
        run: ConversationRun,
        *,
        correlation: RunExecutionCorrelation,
        owner_id: str,
    ) -> ConversationRun: ...

    async def finalize_execution(
        self,
        run: ConversationRun,
        *,
        correlation: RunExecutionCorrelation,
        owner_id: str,
        execution_lease: ExecutionLease,
        job_state: Literal[ExecutionJobState.COMPLETED, ExecutionJobState.FAILED],
        checkpoint_cursor: str | None = None,
        failure_code: str | None = None,
    ) -> ConversationRun: ...

    async def append_tool_started(
        self,
        correlation: RunExecutionCorrelation,
        *,
        owner_id: str,
        execution_id: str,
        logical_call_id: str,
        attempt: int,
        tool: ToolCapability,
        execution_lease: ExecutionLease | None = None,
    ) -> ToolExecutionHandle: ...

    async def append_tool_terminal(
        self,
        handle: ToolExecutionHandle,
        *,
        owner_id: str,
        transition: ToolTransition,
        duration_ms: int,
        error_code: ToolFailureCode | None = None,
        summary: ToolExecutionSummary | None = None,
        execution_lease: ExecutionLease | None = None,
    ) -> ToolExecutionRecord: ...


class RunLedgerReader(Protocol):
    """Owner-scoped internal projections; no public API contract is implied."""

    async def get_run_events(
        self,
        run_id: str,
        *,
        owner_id: str,
    ) -> tuple[RunEventRecord, ...]: ...

    async def get_tool_executions(
        self,
        run_id: str,
        *,
        owner_id: str,
    ) -> tuple[ToolExecutionRecord, ...]: ...

    async def find_orphaned_runs(
        self,
        *,
        owner_id: str,
        stale_before: datetime,
    ) -> tuple[OrphanedRunView, ...]: ...
