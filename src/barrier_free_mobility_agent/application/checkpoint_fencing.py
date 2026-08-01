from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)
from pydantic import ValidationError

from barrier_free_mobility_agent.application.conversation_repository import (
    ConversationRepository,
)
from barrier_free_mobility_agent.domain.errors import AgentPolicyViolationError
from barrier_free_mobility_agent.domain.execution_queue import ExecutionLease

_LEASE_CONFIG_KEY = "__bfm_execution_lease"


class LeaseFencedCheckpointSaver(BaseCheckpointSaver[str]):
    """Serialize checkpoint writes with the authoritative execution-lease row."""

    def __init__(
        self,
        delegate: BaseCheckpointSaver[str],
        repository: ConversationRepository,
    ) -> None:
        super().__init__(serde=delegate.serde)
        self._delegate = delegate
        self._repository = repository
        self._lock = asyncio.Lock()

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        async with self._lock:
            return await self._delegate.aget_tuple(config)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        async with self._lock:
            async for checkpoint in self._delegate.alist(
                config,
                filter=filter,
                before=before,
                limit=limit,
            ):
                yield checkpoint

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        async with self._lock:
            lease = _execution_lease(config)
            if lease is None:
                return await self._delegate.aput(config, checkpoint, metadata, new_versions)
            async with self._repository.guard_execution_lease(lease):
                stored = await self._delegate.aput(config, checkpoint, metadata, new_versions)
        return attach_execution_lease(stored, lease)

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        async with self._lock:
            lease = _execution_lease(config)
            if lease is None:
                await self._delegate.aput_writes(config, writes, task_id, task_path)
                return
            async with self._repository.guard_execution_lease(lease):
                await self._delegate.aput_writes(config, writes, task_id, task_path)

    async def adelete_thread(self, thread_id: str) -> None:
        async with self._lock:
            await self._delegate.adelete_thread(thread_id)

    def get_next_version(self, current: str | None, channel: None) -> str:
        return self._delegate.get_next_version(current, channel)


def attach_execution_lease(
    config: RunnableConfig,
    lease: ExecutionLease | None,
) -> RunnableConfig:
    if lease is None:
        return config
    configurable = dict(config.get("configurable", {}))
    configurable[_LEASE_CONFIG_KEY] = lease.model_dump(mode="json")
    return {
        **config,
        "configurable": configurable,
    }


def _execution_lease(config: RunnableConfig) -> ExecutionLease | None:
    configurable = config.get("configurable")
    if not isinstance(configurable, dict):
        return None
    raw_lease = configurable.get(_LEASE_CONFIG_KEY)
    if raw_lease is None:
        return None
    try:
        return ExecutionLease.model_validate(raw_lease)
    except ValidationError as exc:
        raise AgentPolicyViolationError(
            context={"reason": "checkpoint_execution_lease_invalid"}
        ) from exc
