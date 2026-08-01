from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import timedelta
from hashlib import sha256
from typing import Any
from uuid import uuid4

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    empty_checkpoint,
)
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from barrier_free_mobility_agent.application.checkpoint_fencing import (
    LeaseFencedCheckpointSaver,
    attach_execution_lease,
)
from barrier_free_mobility_agent.application.conversation_repository import ConversationThread
from barrier_free_mobility_agent.domain.errors import ExecutionLeaseLostError
from barrier_free_mobility_agent.domain.execution_queue import (
    ExecutionJobState,
    StartExecutionPayload,
)
from barrier_free_mobility_agent.domain.run_ledger import RunExecutionCorrelation
from barrier_free_mobility_agent.persistence.execution_payload_cipher import (
    ExecutionPayloadCipher,
)
from barrier_free_mobility_agent.persistence.postgres import PostgresConversationRepository
from barrier_free_mobility_agent.persistence.runtime import _configure_postgres_connection

_DATABASE_URL = os.getenv("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not _DATABASE_URL,
    reason="TEST_DATABASE_URL is required for PostgreSQL integration tests.",
)


class _BlockingCheckpointSaver(BaseCheckpointSaver[str]):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.put_calls = 0

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        del metadata, new_versions
        self.put_calls += 1
        self.entered.set()
        await self.release.wait()
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

    async def adelete_thread(self, thread_id: str) -> None:
        del thread_id

    def get_next_version(self, current: str | None, channel: None) -> str:
        del channel
        return "1" if current is None else str(int(current) + 1)


@pytest.mark.asyncio
async def test_checkpoint_write_serializes_handoff_and_rejects_the_stale_generation() -> None:
    assert _DATABASE_URL is not None
    suffix = uuid4().hex
    owner_id = f"checkpoint-owner-{suffix}"
    thread_id = f"checkpoint-thread-{suffix}"
    correlation = RunExecutionCorrelation(
        thread_id=thread_id,
        run_id=f"checkpoint-run-{suffix}",
        request_id=f"checkpoint-request-{suffix}",
        attempt=1,
    )

    async with _repository(_DATABASE_URL) as repository:
        await repository.create_thread(
            ConversationThread(
                thread_id=thread_id,
                owner_id=owner_id,
                flow_version="checkpoint-fence-flow",
                state_schema_version="checkpoint-fence-state",
            ),
            capacity=10,
        )
        first_registration = await repository.register_execution_worker(
            f"checkpoint-worker-a-{suffix}",
            heartbeat_ttl=timedelta(seconds=30),
        )
        second_registration = await repository.register_execution_worker(
            f"checkpoint-worker-b-{suffix}",
            heartbeat_ttl=timedelta(seconds=30),
        )
        await repository.reserve_initial_execution(
            correlation,
            owner_id=owner_id,
            capacity=10,
            idempotency_digest=_digest(f"checkpoint-key-{suffix}"),
            request_fingerprint=_digest(f"checkpoint-request-{suffix}"),
            payload=StartExecutionPayload(question="private"),
        )
        first_lease = await repository.claim_execution_job(
            registration=first_registration,
            lease_duration=timedelta(seconds=1),
        )
        assert first_lease is not None

        delegate = _BlockingCheckpointSaver()
        saver = LeaseFencedCheckpointSaver(delegate, repository)
        config = attach_execution_lease(
            {
                "configurable": {
                    "thread_id": f"{owner_id}:{thread_id}",
                    "checkpoint_ns": "",
                }
            },
            first_lease,
        )
        metadata: CheckpointMetadata = {"source": "input", "step": -1, "parents": {}}
        write = asyncio.create_task(
            saver.aput(config, empty_checkpoint(), metadata, {}),
            name="blocked-checkpoint-write",
        )
        await asyncio.wait_for(delegate.entered.wait(), timeout=5)
        await asyncio.sleep(1.1)

        claim_while_checkpoint_locked = await repository.claim_execution_job(
            registration=second_registration,
            lease_duration=timedelta(seconds=10),
        )
        assert claim_while_checkpoint_locked is None

        delegate.release.set()
        await asyncio.wait_for(write, timeout=5)
        reclaimed = await repository.claim_execution_job(
            registration=second_registration,
            lease_duration=timedelta(seconds=10),
        )
        assert reclaimed is not None
        assert reclaimed.generation == 2

        with pytest.raises(ExecutionLeaseLostError):
            await saver.aput(config, empty_checkpoint(), metadata, {})
        assert delegate.put_calls == 1

        completed = await repository.fail_execution_job(
            reclaimed,
            failure_code="CHECKPOINT_FENCE_TEST_CLEANUP",
        )
        assert completed.state == ExecutionJobState.FAILED
        await repository.remove_execution_worker(first_registration)
        await repository.remove_execution_worker(second_registration)
        await repository.delete_thread(thread_id, owner_id=owner_id)


def _digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


@asynccontextmanager
async def _repository(
    database_url: str,
) -> AsyncIterator[PostgresConversationRepository]:
    pool = AsyncConnectionPool(
        conninfo=database_url,
        kwargs={
            "autocommit": True,
            "prepare_threshold": 0,
            "row_factory": dict_row,
        },
        min_size=1,
        max_size=4,
        configure=_configure_postgres_connection,
        open=False,
        name="mobility-agent-checkpoint-fence-test",
    )
    await pool.open(wait=True, timeout=10)
    try:
        await AsyncPostgresSaver(pool).setup()
        repository = PostgresConversationRepository(
            pool,
            execution_payload_cipher=ExecutionPayloadCipher(b"f" * 32),
        )
        await repository.setup()
        yield repository
    finally:
        await pool.close()
