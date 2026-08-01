from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    empty_checkpoint,
)

from barrier_free_mobility_agent.application.checkpoint_fencing import (
    LeaseFencedCheckpointSaver,
    attach_execution_lease,
)
from barrier_free_mobility_agent.application.conversation_repository import (
    ConversationRepository,
)
from barrier_free_mobility_agent.domain.errors import ExecutionLeaseLostError
from barrier_free_mobility_agent.domain.execution_queue import (
    ExecutionLease,
    ExecutionOperation,
    StartExecutionPayload,
)
from barrier_free_mobility_agent.domain.run_ledger import RunExecutionCorrelation


class _RecordingSaver(BaseCheckpointSaver[str]):
    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self._events = events

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        del metadata, new_versions
        self._events.append("delegate.put")
        return {
            "configurable": {
                "thread_id": config["configurable"]["thread_id"],
                "checkpoint_ns": config["configurable"].get("checkpoint_ns", ""),
                "checkpoint_id": checkpoint["id"],
            }
        }

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        del config, writes, task_id, task_path
        self._events.append("delegate.writes")

    async def adelete_thread(self, thread_id: str) -> None:
        del thread_id

    def get_next_version(self, current: str | None, channel: None) -> str:
        del channel
        return "1" if current is None else str(int(current) + 1)


class _BlockingReadSaver(_RecordingSaver):
    def __init__(
        self,
        events: list[str],
        *,
        read_started: asyncio.Event,
        release_read: asyncio.Event,
    ) -> None:
        super().__init__(events)
        self._read_started = read_started
        self._release_read = release_read

    async def aget_tuple(self, config: RunnableConfig) -> None:
        del config
        self._events.append("delegate.read.enter")
        self._read_started.set()
        await self._release_read.wait()
        self._events.append("delegate.read.exit")


class _GuardOnlyRepository:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.active = True

    @asynccontextmanager
    async def guard_execution_lease(
        self,
        lease: ExecutionLease,
    ) -> AsyncIterator[None]:
        del lease
        self.events.append("guard.enter")
        if not self.active:
            raise ExecutionLeaseLostError()
        try:
            yield
        finally:
            self.events.append("guard.exit")


def _lease() -> ExecutionLease:
    now = datetime.now(UTC)
    correlation = RunExecutionCorrelation(
        thread_id="thread-checkpoint-fence",
        run_id="run-checkpoint-fence",
        request_id="request-checkpoint-fence",
        attempt=1,
    )
    return ExecutionLease(
        job_id="job-checkpoint-fence",
        owner_id="owner-checkpoint-fence",
        correlation=correlation,
        operation=ExecutionOperation.START,
        payload=StartExecutionPayload(question="private"),
        worker_id="worker-checkpoint-fence",
        token="token-checkpoint-fence",
        generation=1,
        heartbeat_at=now,
        expires_at=now + timedelta(seconds=30),
    )


@pytest.mark.asyncio
async def test_checkpoint_write_holds_the_lease_guard_and_carries_private_config() -> None:
    events: list[str] = []
    repository = _GuardOnlyRepository(events)
    saver = LeaseFencedCheckpointSaver(
        _RecordingSaver(events),
        cast(ConversationRepository, repository),
    )
    lease = _lease()
    config = attach_execution_lease(
        {
            "configurable": {
                "thread_id": "owner:thread",
                "checkpoint_ns": "",
            }
        },
        lease,
    )
    checkpoint = empty_checkpoint()
    metadata: CheckpointMetadata = {"source": "input", "step": -1, "parents": {}}

    next_config = await saver.aput(config, checkpoint, metadata, {})

    assert events == ["guard.enter", "delegate.put", "guard.exit"]
    assert next_config["configurable"]["__bfm_execution_lease"] == lease.model_dump(mode="json")

    repository.active = False
    with pytest.raises(ExecutionLeaseLostError):
        await saver.aput(next_config, empty_checkpoint(), metadata, {})
    assert events == [
        "guard.enter",
        "delegate.put",
        "guard.exit",
        "guard.enter",
    ]


@pytest.mark.asyncio
async def test_checkpoint_write_without_a_worker_lease_is_a_plain_delegate_call() -> None:
    events: list[str] = []
    saver = LeaseFencedCheckpointSaver(
        _RecordingSaver(events),
        cast(ConversationRepository, _GuardOnlyRepository(events)),
    )
    config: RunnableConfig = {"configurable": {"thread_id": "owner:thread", "checkpoint_ns": ""}}

    await saver.aput(
        config,
        empty_checkpoint(),
        {"source": "input", "step": -1, "parents": {}},
        {},
    )

    assert events == ["delegate.put"]


@pytest.mark.asyncio
async def test_read_and_guarded_write_share_one_outer_lock_order() -> None:
    events: list[str] = []
    read_started = asyncio.Event()
    release_read = asyncio.Event()
    repository = _GuardOnlyRepository(events)
    saver = LeaseFencedCheckpointSaver(
        _BlockingReadSaver(
            events,
            read_started=read_started,
            release_read=release_read,
        ),
        cast(ConversationRepository, repository),
    )
    plain_config: RunnableConfig = {
        "configurable": {"thread_id": "owner:thread", "checkpoint_ns": ""}
    }
    guarded_config = attach_execution_lease(plain_config, _lease())
    metadata: CheckpointMetadata = {"source": "input", "step": -1, "parents": {}}

    read_task = asyncio.create_task(saver.aget_tuple(plain_config))
    await read_started.wait()

    write_started = asyncio.Event()

    async def write_checkpoint() -> None:
        write_started.set()
        await saver.aput(guarded_config, empty_checkpoint(), metadata, {})

    write_task = asyncio.create_task(write_checkpoint())
    await write_started.wait()
    await asyncio.sleep(0)

    assert events == ["delegate.read.enter"]

    release_read.set()
    await asyncio.gather(read_task, write_task)

    assert events == [
        "delegate.read.enter",
        "delegate.read.exit",
        "guard.enter",
        "delegate.put",
        "guard.exit",
    ]
