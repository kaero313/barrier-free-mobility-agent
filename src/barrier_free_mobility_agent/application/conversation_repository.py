from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from barrier_free_mobility_agent.application.execution_queue import ExecutionQueue
from barrier_free_mobility_agent.application.run_ledger import (
    RunLedgerReader,
    RunLedgerWriter,
)
from barrier_free_mobility_agent.domain.execution_queue import ThreadDeletionReservation

if TYPE_CHECKING:
    from barrier_free_mobility_agent.application.conversations import ConversationRun
    from barrier_free_mobility_agent.domain.execution_queue import ExecutionLease

type PersistenceKind = Literal["ephemeral", "durable"]


class ConversationThread(BaseModel):
    """Owner-scoped metadata required to locate and resume conversation runs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    thread_id: str = Field(min_length=1, max_length=200)
    owner_id: str = Field(min_length=1, max_length=200)
    active_run_id: str | None = Field(default=None, min_length=1, max_length=200)
    run_count: int = Field(default=0, ge=0)
    flow_version: str = Field(min_length=1, max_length=100)
    state_schema_version: str = Field(min_length=1, max_length=100)


class ConversationRepository(RunLedgerWriter, RunLedgerReader, ExecutionQueue, Protocol):
    """Persistence contract; owner mismatches intentionally look like missing objects."""

    @property
    def persistence(self) -> PersistenceKind: ...

    async def create_thread(
        self,
        thread: ConversationThread,
        *,
        capacity: int,
        total_capacity: int | None = None,
    ) -> ConversationThread: ...

    async def get_thread(
        self,
        thread_id: str,
        *,
        owner_id: str,
    ) -> ConversationThread: ...

    async def add_run(
        self,
        run: ConversationRun,
        *,
        owner_id: str,
        capacity: int,
    ) -> ConversationThread: ...

    async def update_run(
        self,
        run: ConversationRun,
        *,
        owner_id: str,
    ) -> ConversationRun: ...

    async def get_run(
        self,
        run_id: str,
        *,
        owner_id: str,
    ) -> ConversationRun: ...

    async def delete_thread(
        self,
        thread_id: str,
        *,
        owner_id: str,
    ) -> None: ...

    async def prepare_thread_deletion(
        self,
        thread_id: str,
        *,
        owner_id: str,
    ) -> ThreadDeletionReservation: ...

    async def commit_thread_deletion(
        self,
        reservation: ThreadDeletionReservation,
    ) -> None: ...

    async def abort_thread_deletion(
        self,
        reservation: ThreadDeletionReservation,
    ) -> None: ...

    async def is_ready(self) -> bool: ...

    def guard_execution_lease(
        self,
        lease: ExecutionLease,
    ) -> AbstractAsyncContextManager[None]: ...
