from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from tests.factories import QUESTION
from tests.fakes import FakeMcpGateway

from barrier_free_mobility_agent.agent.graph import MobilityGraphRunner
from barrier_free_mobility_agent.application.conversation_repository import ConversationThread
from barrier_free_mobility_agent.application.durable_conversations import (
    DurableConversationCoordinator,
    DurableConversationOptions,
)
from barrier_free_mobility_agent.application.execution_worker import (
    DurableExecutionWorker,
    ExecutionWorkerOptions,
)
from barrier_free_mobility_agent.config import Settings
from barrier_free_mobility_agent.domain.errors import (
    ExecutionInProgressError,
    ExecutionRecoveryRequiredError,
    IdempotencyKeyReuseError,
    InvalidIdempotencyKeyError,
)
from barrier_free_mobility_agent.domain.execution_queue import ExecutionJobState
from barrier_free_mobility_agent.domain.routing import ToolCapability
from barrier_free_mobility_agent.persistence.memory import MemoryConversationRepository

OWNER_ID = "local-test-owner"
THREAD_ID = "a" * 32
HMAC_KEY = b"durable-conversation-test-key-32-bytes-minimum"
IDEMPOTENCY_KEY = "durable-test-key-0001"


async def _runtime(
    settings: Settings,
    *,
    wait: timedelta = timedelta(seconds=3),
) -> tuple[
    MemoryConversationRepository,
    DurableConversationCoordinator,
    DurableExecutionWorker,
]:
    repository = MemoryConversationRepository()
    await repository.create_thread(
        ConversationThread(
            thread_id=THREAD_ID,
            owner_id=OWNER_ID,
            flow_version=settings.agent_flow_version,
            state_schema_version=settings.agent_state_schema_version,
        ),
        capacity=10,
        total_capacity=10,
    )
    runner = MobilityGraphRunner(
        FakeMcpGateway(),
        settings,
        run_ledger=repository,
    )
    coordinator = DurableConversationCoordinator(
        repository,
        idempotency_hmac_key=HMAC_KEY,
        max_runs_per_thread=10,
        request_max_chars=settings.api_request_max_chars,
        flow_version=settings.agent_flow_version,
        state_schema_version=settings.agent_state_schema_version,
        options=DurableConversationOptions(
            bounded_wait=wait,
            poll_interval=timedelta(milliseconds=5),
        ),
    )
    worker = DurableExecutionWorker(
        runner,
        repository,
        worker_id="durable-test-worker",
        options=ExecutionWorkerOptions(
            lease_duration=timedelta(seconds=2),
            heartbeat_interval=timedelta(milliseconds=200),
            poll_interval=timedelta(milliseconds=5),
            max_error_backoff=timedelta(milliseconds=50),
            shutdown_grace=timedelta(seconds=1),
        ),
    )
    return repository, coordinator, worker


@pytest.mark.asyncio
async def test_durable_start_and_same_key_replay_converge_to_one_public_run(
    settings: Settings,
) -> None:
    repository, coordinator, worker = await _runtime(settings)
    await worker.start()
    try:
        first = await coordinator.start_run(
            THREAD_ID,
            QUESTION,
            idempotency_key=IDEMPOTENCY_KEY,
            request_id="request-1",
        )
        replayed = await coordinator.start_run(
            THREAD_ID,
            QUESTION,
            idempotency_key=IDEMPOTENCY_KEY,
            request_id="request-2",
        )
    finally:
        await worker.stop()

    thread = await repository.get_thread(THREAD_ID, owner_id=OWNER_ID)
    assert replayed == first
    assert thread.run_count == 1


@pytest.mark.asyncio
async def test_same_key_with_different_normalized_request_is_rejected(
    settings: Settings,
) -> None:
    _, coordinator, worker = await _runtime(settings)
    await worker.start()
    try:
        await coordinator.start_run(
            THREAD_ID,
            QUESTION,
            idempotency_key=IDEMPOTENCY_KEY,
        )
        with pytest.raises(IdempotencyKeyReuseError):
            await coordinator.start_run(
                THREAD_ID,
                QUESTION + " 다른 요청",
                idempotency_key=IDEMPOTENCY_KEY,
            )
    finally:
        await worker.stop()


@pytest.mark.asyncio
async def test_invalid_idempotency_key_is_a_safe_domain_error(settings: Settings) -> None:
    _, coordinator, _ = await _runtime(settings)

    with pytest.raises(InvalidIdempotencyKeyError):
        await coordinator.start_run(
            THREAD_ID,
            QUESTION,
            idempotency_key="too-short",
        )


@pytest.mark.asyncio
async def test_bounded_wait_never_executes_graph_and_replay_later_joins_same_job(
    settings: Settings,
) -> None:
    repository, immediate, worker = await _runtime(settings, wait=timedelta(0))
    await repository.register_execution_worker(
        "durable-test-worker",
        heartbeat_ttl=timedelta(seconds=15),
    )
    with pytest.raises(ExecutionInProgressError):
        await immediate.start_run(
            THREAD_ID,
            QUESTION,
            idempotency_key=IDEMPOTENCY_KEY,
        )
    thread_before = await repository.get_thread(THREAD_ID, owner_id=OWNER_ID)

    waiting = DurableConversationCoordinator(
        repository,
        idempotency_hmac_key=HMAC_KEY,
        max_runs_per_thread=10,
        request_max_chars=settings.api_request_max_chars,
        flow_version=settings.agent_flow_version,
        state_schema_version=settings.agent_state_schema_version,
        options=DurableConversationOptions(
            bounded_wait=timedelta(seconds=3),
            poll_interval=timedelta(milliseconds=5),
        ),
    )
    await worker.start()
    try:
        run = await waiting.start_run(
            THREAD_ID,
            QUESTION,
            idempotency_key=IDEMPOTENCY_KEY,
        )
    finally:
        await worker.stop()

    thread_after = await repository.get_thread(THREAD_ID, owner_id=OWNER_ID)
    assert run.run_id == thread_before.active_run_id
    assert thread_after.run_count == 1


@pytest.mark.asyncio
async def test_ambiguous_open_tool_failure_stops_polling_with_safe_recovery_error(
    settings: Settings,
) -> None:
    repository, coordinator, worker = await _runtime(settings)
    crashed_registration = await repository.register_execution_worker(
        "crashed-worker",
        heartbeat_ttl=timedelta(seconds=30),
    )
    submission = asyncio.create_task(
        coordinator.start_run(
            THREAD_ID,
            QUESTION,
            idempotency_key=IDEMPOTENCY_KEY,
        )
    )
    lease = None
    for _ in range(100):
        lease = await repository.claim_execution_job(
            registration=crashed_registration,
            lease_duration=timedelta(seconds=30),
        )
        if lease is not None:
            break
        await asyncio.sleep(0)
    assert lease is not None
    await repository.append_tool_started(
        lease.correlation,
        owner_id=OWNER_ID,
        execution_id="ambiguous-tool-1",
        logical_call_id="ambiguous-call-1",
        attempt=1,
        tool=ToolCapability.RESOLVE_STATION,
        execution_lease=lease,
    )
    await repository.release_execution_job(lease)

    await worker.start()
    try:
        with pytest.raises(ExecutionRecoveryRequiredError):
            await asyncio.wait_for(submission, timeout=1)
    finally:
        await worker.stop()

    stored = await repository.get_execution_job(lease.job_id, owner_id=OWNER_ID)
    assert stored.state == ExecutionJobState.FAILED
    assert stored.failure_code == "WORKER_RECOVERY_AMBIGUOUS"
