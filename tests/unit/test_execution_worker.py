from __future__ import annotations

import asyncio
from datetime import timedelta
from hashlib import sha256
from time import monotonic

import pytest

from barrier_free_mobility_agent.agent.models import GraphTurn, GraphTurnStatus, ResumeMode
from barrier_free_mobility_agent.application.conversation_repository import ConversationThread
from barrier_free_mobility_agent.application.conversations import ConversationRun
from barrier_free_mobility_agent.application.execution_checkpoint import (
    ExecutionCheckpointInspection,
    ExecutionCheckpointState,
)
from barrier_free_mobility_agent.application.execution_worker import (
    DurableExecutionWorker,
    ExecutionWorkerFaultPoint,
    ExecutionWorkerOptions,
    ExecutionWorkerState,
    ExecutionWorkerTick,
)
from barrier_free_mobility_agent.domain.execution_queue import (
    ExecutionJobState,
    ExecutionLease,
    ExecutionWorkerRegistration,
    ResumeCommandExecutionPayload,
    StartExecutionPayload,
)
from barrier_free_mobility_agent.domain.identity import OwnerId
from barrier_free_mobility_agent.domain.models import AnswerOutcome
from barrier_free_mobility_agent.domain.routing import ToolCapability
from barrier_free_mobility_agent.domain.run_ledger import RunExecutionCorrelation
from barrier_free_mobility_agent.persistence.memory import MemoryConversationRepository

OWNER_ID = "worker-owner"
THREAD_ID = "a" * 32
RUN_ID = "b" * 32


def _digest(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _correlation() -> RunExecutionCorrelation:
    return RunExecutionCorrelation(
        thread_id=THREAD_ID,
        run_id=RUN_ID,
        request_id="worker-request",
        attempt=1,
    )


def _turn() -> GraphTurn:
    return GraphTurn(
        status=GraphTurnStatus.INTERRUPTED,
        resume_mode=ResumeMode.COMMAND,
        outcome=AnswerOutcome.question_too_complex(),
        state={},
    )


class WorkerGraphFake:
    def __init__(self) -> None:
        self.inspection = ExecutionCheckpointInspection(state=ExecutionCheckpointState.MISSING)
        self.start_calls = 0
        self.resume_calls = 0
        self.recover_calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.block = False
        self.ignore_cancellation = False
        self.cancellation_seen = asyncio.Event()
        self.block_inspection = False
        self.inspection_entered = asyncio.Event()
        self.release_inspection = asyncio.Event()

    async def start(
        self,
        question: str,
        *,
        thread_id: str | None = None,
        owner_id: OwnerId,
        correlation: RunExecutionCorrelation,
        execution_lease: ExecutionLease,
    ) -> GraphTurn:
        del question, thread_id, owner_id
        self.start_calls += 1
        self.started.set()
        if self.block:
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancellation_seen.set()
                if not self.ignore_cancellation:
                    raise
                await self.release.wait()
        self.inspection = ExecutionCheckpointInspection(
            state=ExecutionCheckpointState.INTERRUPTED,
            cursor="checkpoint:1",
            correlation=correlation,
            execution_job_id=execution_lease.job_id,
            lease_generation=execution_lease.generation,
        )
        return _turn()

    async def resume(
        self,
        answer: object,
        *,
        thread_id: str,
        owner_id: OwnerId,
        correlation: RunExecutionCorrelation,
        execution_lease: ExecutionLease,
    ) -> GraphTurn:
        del answer, thread_id, owner_id, correlation, execution_lease
        self.resume_calls += 1
        raise AssertionError("resume was not expected")

    async def continue_business_clarification(
        self,
        answer: str,
        *,
        thread_id: str,
        owner_id: OwnerId,
        correlation: RunExecutionCorrelation,
        execution_lease: ExecutionLease,
    ) -> GraphTurn:
        del answer, thread_id, owner_id, correlation, execution_lease
        raise AssertionError("new-turn resume was not expected")

    async def inspect_execution_checkpoint(
        self,
        thread_id: str,
        *,
        owner_id: OwnerId,
        checkpoint_cursor: str | None = None,
    ) -> ExecutionCheckpointInspection:
        del thread_id, owner_id
        if self.block_inspection:
            self.inspection_entered.set()
            try:
                await self.release_inspection.wait()
            except asyncio.CancelledError:
                self.cancellation_seen.set()
                if not self.ignore_cancellation:
                    raise
                await self.release_inspection.wait()
        if checkpoint_cursor is not None and checkpoint_cursor != self.inspection.cursor:
            return ExecutionCheckpointInspection(state=ExecutionCheckpointState.MISSING)
        return self.inspection

    async def recover_execution_checkpoint(
        self,
        *,
        thread_id: str,
        owner_id: OwnerId,
        correlation: RunExecutionCorrelation,
        execution_lease: ExecutionLease,
        checkpoint_cursor: str,
    ) -> GraphTurn:
        del thread_id, owner_id, checkpoint_cursor
        self.recover_calls += 1
        self.inspection = self.inspection.model_copy(
            update={
                "correlation": correlation,
                "execution_job_id": execution_lease.job_id,
                "lease_generation": execution_lease.generation,
            }
        )
        return _turn()


class RecordingFaultInjector:
    def __init__(
        self,
        repository: MemoryConversationRepository,
        graph: WorkerGraphFake,
        *,
        fail_at: ExecutionWorkerFaultPoint | None = None,
    ) -> None:
        self._repository = repository
        self._graph = graph
        self._fail_at = fail_at
        self.observations: list[tuple[ExecutionWorkerFaultPoint, ExecutionJobState, int]] = []

    async def hit(
        self,
        point: ExecutionWorkerFaultPoint,
        lease: ExecutionLease,
    ) -> None:
        job = await self._repository.get_execution_job(
            lease.job_id,
            owner_id=lease.owner_id,
        )
        self.observations.append((point, job.state, self._graph.start_calls))
        if point == self._fail_at:
            raise RuntimeError(f"injected fault at {point}")


async def _reserved_repository() -> tuple[MemoryConversationRepository, str]:
    repository = MemoryConversationRepository()
    await repository.create_thread(
        ConversationThread(
            thread_id=THREAD_ID,
            owner_id=OWNER_ID,
            flow_version="worker-flow",
            state_schema_version="worker-state",
        ),
        capacity=10,
        total_capacity=10,
    )
    reservation = await repository.reserve_initial_execution(
        _correlation(),
        owner_id=OWNER_ID,
        capacity=10,
        idempotency_digest=_digest("worker-key"),
        request_fingerprint=_digest("worker-request"),
        payload=StartExecutionPayload(question="서울역에서 시청역까지 갈 수 있어?"),
    )
    return repository, reservation.job.job_id


async def _register_worker(
    repository: MemoryConversationRepository,
    worker_id: str,
) -> ExecutionWorkerRegistration:
    return await repository.register_execution_worker(
        worker_id,
        heartbeat_ttl=timedelta(seconds=15),
    )


async def _arm_worker(
    worker: DurableExecutionWorker,
    repository: MemoryConversationRepository,
) -> None:
    worker._registration = await _register_worker(repository, worker.readiness.worker_id)


def _options() -> ExecutionWorkerOptions:
    return ExecutionWorkerOptions(
        lease_duration=timedelta(seconds=2),
        heartbeat_interval=timedelta(milliseconds=200),
        poll_interval=timedelta(milliseconds=10),
        max_error_backoff=timedelta(milliseconds=50),
        shutdown_grace=timedelta(seconds=1),
    )


@pytest.mark.asyncio
async def test_worker_claims_executes_and_atomically_finalizes_job_and_snapshot() -> None:
    repository, job_id = await _reserved_repository()
    graph = WorkerGraphFake()
    worker = DurableExecutionWorker(
        graph,
        repository,
        worker_id="worker-1",
        options=_options(),
    )
    await _arm_worker(worker, repository)

    assert await worker.run_once() == ExecutionWorkerTick.COMPLETED

    job = await repository.get_execution_job(job_id, owner_id=OWNER_ID)
    run = await repository.get_run(RUN_ID, owner_id=OWNER_ID)
    assert graph.start_calls == 1
    assert job.state == ExecutionJobState.COMPLETED
    assert job.payload is None
    assert job.checkpoint_cursor == "checkpoint:1"
    assert run.status == GraphTurnStatus.INTERRUPTED


@pytest.mark.asyncio
async def test_fault_points_follow_the_claim_graph_and_finalize_boundaries() -> None:
    repository, _ = await _reserved_repository()
    graph = WorkerGraphFake()
    injector = RecordingFaultInjector(repository, graph)
    worker = DurableExecutionWorker(
        graph,
        repository,
        worker_id="worker-1",
        options=_options(),
        fault_injector=injector,
    )
    await _arm_worker(worker, repository)

    assert await worker.run_once() == ExecutionWorkerTick.COMPLETED
    assert injector.observations == [
        (ExecutionWorkerFaultPoint.AFTER_CLAIM, ExecutionJobState.LEASED, 0),
        (ExecutionWorkerFaultPoint.BEFORE_GRAPH, ExecutionJobState.LEASED, 0),
        (ExecutionWorkerFaultPoint.BEFORE_FINALIZE, ExecutionJobState.LEASED, 1),
        (ExecutionWorkerFaultPoint.AFTER_FINALIZE, ExecutionJobState.COMPLETED, 1),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault_point",
    [
        ExecutionWorkerFaultPoint.AFTER_CLAIM,
        ExecutionWorkerFaultPoint.BEFORE_GRAPH,
        ExecutionWorkerFaultPoint.BEFORE_FINALIZE,
    ],
)
async def test_pre_terminal_injected_fault_uses_the_existing_failure_transition(
    fault_point: ExecutionWorkerFaultPoint,
) -> None:
    repository, job_id = await _reserved_repository()
    graph = WorkerGraphFake()
    injector = RecordingFaultInjector(repository, graph, fail_at=fault_point)
    worker = DurableExecutionWorker(
        graph,
        repository,
        worker_id="worker-1",
        options=_options(),
        fault_injector=injector,
    )
    await _arm_worker(worker, repository)

    assert await worker.run_once() == ExecutionWorkerTick.FAILED

    job = await repository.get_execution_job(job_id, owner_id=OWNER_ID)
    assert job.state == ExecutionJobState.FAILED
    assert job.failure_code == "WORKER_EXECUTION_FAILED"
    assert graph.start_calls == (
        1 if fault_point == ExecutionWorkerFaultPoint.BEFORE_FINALIZE else 0
    )


@pytest.mark.asyncio
async def test_post_finalize_injected_fault_cannot_replace_the_committed_terminal_result() -> None:
    repository, job_id = await _reserved_repository()
    graph = WorkerGraphFake()
    injector = RecordingFaultInjector(
        repository,
        graph,
        fail_at=ExecutionWorkerFaultPoint.AFTER_FINALIZE,
    )
    worker = DurableExecutionWorker(
        graph,
        repository,
        worker_id="worker-1",
        options=_options(),
        fault_injector=injector,
    )
    await _arm_worker(worker, repository)

    assert await worker.run_once() == ExecutionWorkerTick.LEASE_LOST

    job = await repository.get_execution_job(job_id, owner_id=OWNER_ID)
    assert job.state == ExecutionJobState.COMPLETED
    assert job.failure_code is None


@pytest.mark.asyncio
async def test_reclaimed_job_retries_fresh_only_when_checkpoint_and_open_tool_are_absent() -> None:
    repository, job_id = await _reserved_repository()
    first_registration = await _register_worker(repository, "dead-worker")
    first = await repository.claim_execution_job(
        registration=first_registration,
        lease_duration=timedelta(seconds=30),
    )
    assert first is not None
    await repository.release_execution_job(first)
    graph = WorkerGraphFake()
    worker = DurableExecutionWorker(
        graph,
        repository,
        worker_id="replacement-worker",
        options=_options(),
    )
    await _arm_worker(worker, repository)

    assert await worker.run_once() == ExecutionWorkerTick.COMPLETED

    job = await repository.get_execution_job(job_id, owner_id=OWNER_ID)
    assert graph.start_calls == 1
    assert graph.recover_calls == 0
    assert job.state == ExecutionJobState.COMPLETED
    assert job.failure_code is None


@pytest.mark.asyncio
async def test_reclaimed_job_recovers_only_the_recorded_same_attempt_checkpoint() -> None:
    repository, job_id = await _reserved_repository()
    first_registration = await _register_worker(repository, "dead-worker")
    first = await repository.claim_execution_job(
        registration=first_registration,
        lease_duration=timedelta(seconds=30),
    )
    assert first is not None
    heartbeat = await repository.heartbeat_execution_job(
        first,
        lease_duration=timedelta(seconds=30),
        checkpoint_cursor="checkpoint:recoverable",
    )
    await repository.release_execution_job(heartbeat)
    graph = WorkerGraphFake()
    graph.inspection = ExecutionCheckpointInspection(
        state=ExecutionCheckpointState.CONTINUABLE,
        cursor="checkpoint:recoverable",
        correlation=_correlation(),
        execution_job_id=first.job_id,
        lease_generation=first.generation,
    )
    injector = RecordingFaultInjector(repository, graph)
    worker = DurableExecutionWorker(
        graph,
        repository,
        worker_id="replacement-worker",
        options=_options(),
        fault_injector=injector,
    )
    await _arm_worker(worker, repository)

    assert await worker.run_once() == ExecutionWorkerTick.CONVERGED

    job = await repository.get_execution_job(job_id, owner_id=OWNER_ID)
    assert graph.start_calls == 0
    assert graph.recover_calls == 1
    assert job.state == ExecutionJobState.COMPLETED
    assert job.payload is None
    assert injector.observations == [
        (ExecutionWorkerFaultPoint.AFTER_CLAIM, ExecutionJobState.LEASED, 0),
        (ExecutionWorkerFaultPoint.BEFORE_GRAPH, ExecutionJobState.LEASED, 0),
        (ExecutionWorkerFaultPoint.BEFORE_FINALIZE, ExecutionJobState.LEASED, 0),
        (ExecutionWorkerFaultPoint.AFTER_FINALIZE, ExecutionJobState.COMPLETED, 0),
    ]


@pytest.mark.asyncio
async def test_reclaimed_job_rejects_a_checkpoint_cursor_mismatch() -> None:
    repository, job_id = await _reserved_repository()
    first_registration = await _register_worker(repository, "dead-worker")
    first = await repository.claim_execution_job(
        registration=first_registration,
        lease_duration=timedelta(seconds=30),
    )
    assert first is not None
    heartbeat = await repository.heartbeat_execution_job(
        first,
        lease_duration=timedelta(seconds=30),
        checkpoint_cursor="checkpoint:recorded",
    )
    await repository.release_execution_job(heartbeat)
    graph = WorkerGraphFake()
    graph.inspection = ExecutionCheckpointInspection(
        state=ExecutionCheckpointState.CONTINUABLE,
        cursor="checkpoint:newer",
        correlation=_correlation(),
        execution_job_id=first.job_id,
        lease_generation=first.generation,
    )
    worker = DurableExecutionWorker(
        graph,
        repository,
        worker_id="replacement-worker",
        options=_options(),
    )
    await _arm_worker(worker, repository)

    assert await worker.run_once() == ExecutionWorkerTick.FAILED

    job = await repository.get_execution_job(job_id, owner_id=OWNER_ID)
    assert graph.start_calls == 0
    assert graph.recover_calls == 0
    assert job.state == ExecutionJobState.FAILED
    assert job.failure_code == "WORKER_RECOVERY_AMBIGUOUS"


@pytest.mark.asyncio
async def test_reclaimed_resume_without_checkpoint_never_replays_command_from_scratch() -> None:
    repository = MemoryConversationRepository()
    await repository.create_thread(
        ConversationThread(
            thread_id=THREAD_ID,
            owner_id=OWNER_ID,
            flow_version="worker-flow",
            state_schema_version="worker-state",
        ),
        capacity=10,
        total_capacity=10,
    )
    correlation = _correlation()
    await repository.reserve_initial_run(correlation, owner_id=OWNER_ID, capacity=10)
    await repository.finalize_run(
        ConversationRun(
            run_id=RUN_ID,
            thread_id=THREAD_ID,
            status=GraphTurnStatus.INTERRUPTED,
            resume_mode=ResumeMode.COMMAND,
            outcome=AnswerOutcome.question_too_complex(),
        ),
        correlation=correlation,
        owner_id=OWNER_ID,
    )
    reservation = await repository.reserve_resume_execution(
        RUN_ID,
        request_id="resume-request",
        owner_id=OWNER_ID,
        idempotency_digest=_digest("resume-key"),
        request_fingerprint=_digest("resume-request"),
        payload=ResumeCommandExecutionPayload(answer="답변"),
    )
    first_registration = await _register_worker(repository, "dead-worker")
    first = await repository.claim_execution_job(
        registration=first_registration,
        lease_duration=timedelta(seconds=30),
    )
    assert first is not None
    await repository.release_execution_job(first)
    graph = WorkerGraphFake()
    worker = DurableExecutionWorker(
        graph,
        repository,
        worker_id="replacement-worker",
        options=_options(),
    )
    await _arm_worker(worker, repository)

    assert await worker.run_once() == ExecutionWorkerTick.FAILED

    job = await repository.get_execution_job(reservation.job.job_id, owner_id=OWNER_ID)
    assert graph.resume_calls == 0
    assert job.failure_code == "WORKER_RECOVERY_AMBIGUOUS"


@pytest.mark.asyncio
async def test_open_tool_attempt_is_not_replayed_after_lease_handoff() -> None:
    repository, job_id = await _reserved_repository()
    first_registration = await _register_worker(repository, "old-worker")
    first = await repository.claim_execution_job(
        registration=first_registration,
        lease_duration=timedelta(seconds=30),
    )
    assert first is not None
    await repository.append_tool_started(
        first.correlation,
        owner_id=OWNER_ID,
        execution_id="tool-execution-1",
        logical_call_id="tool-call-1",
        attempt=1,
        tool=ToolCapability.RESOLVE_STATION,
        execution_lease=first,
    )
    await repository.release_execution_job(first)
    graph = WorkerGraphFake()
    worker = DurableExecutionWorker(
        graph,
        repository,
        worker_id="replacement-worker",
        options=_options(),
    )
    await _arm_worker(worker, repository)

    assert await worker.run_once() == ExecutionWorkerTick.FAILED

    job = await repository.get_execution_job(job_id, owner_id=OWNER_ID)
    assert graph.start_calls == 0
    assert graph.recover_calls == 0
    assert job.state == ExecutionJobState.FAILED
    assert job.failure_code == "WORKER_RECOVERY_AMBIGUOUS"


@pytest.mark.asyncio
async def test_graceful_stop_drains_active_job_and_stops_claiming() -> None:
    repository, job_id = await _reserved_repository()
    graph = WorkerGraphFake()
    graph.block = True
    worker = DurableExecutionWorker(
        graph,
        repository,
        worker_id="worker-1",
        options=_options(),
    )
    await worker.start()
    await asyncio.wait_for(graph.started.wait(), timeout=1)

    stopping = asyncio.create_task(worker.stop())
    await asyncio.sleep(0)
    assert worker.readiness.state == ExecutionWorkerState.DRAINING
    graph.release.set()
    await asyncio.wait_for(stopping, timeout=1)

    job = await repository.get_execution_job(job_id, owner_id=OWNER_ID)
    assert job.state == ExecutionJobState.COMPLETED
    assert worker.readiness.state.value == ExecutionWorkerState.STOPPED.value
    assert worker.readiness.active_job_id is None


@pytest.mark.asyncio
async def test_shutdown_grace_is_a_hard_bound_when_execution_ignores_cancellation() -> None:
    repository, job_id = await _reserved_repository()
    graph = WorkerGraphFake()
    graph.block = True
    graph.ignore_cancellation = True
    worker = DurableExecutionWorker(
        graph,
        repository,
        worker_id="worker-1",
        options=_options(),
    )
    await worker.start()
    await asyncio.wait_for(graph.started.wait(), timeout=1)

    started = monotonic()
    await worker.stop(grace=timedelta(milliseconds=20))
    elapsed = monotonic() - started

    assert elapsed < 0.5
    assert worker.readiness.state.value == ExecutionWorkerState.STOPPED.value
    await asyncio.wait_for(graph.cancellation_seen.wait(), timeout=1)
    revoked = await repository.get_execution_job(job_id, owner_id=OWNER_ID)
    assert revoked.state == ExecutionJobState.QUEUED
    assert revoked.lease_token is None
    graph.release.set()
    for _ in range(100):
        await asyncio.sleep(0.01)
        if graph.inspection.cursor == "checkpoint:1":
            break
    else:
        pytest.fail("cancelled graph task did not finish after its test barrier was released")
    await asyncio.sleep(0.05)
    after_release = await repository.get_execution_job(job_id, owner_id=OWNER_ID)
    assert after_release.state == ExecutionJobState.QUEUED


@pytest.mark.asyncio
async def test_shutdown_grace_bounds_a_hung_checkpoint_inspection() -> None:
    repository, _ = await _reserved_repository()
    graph = WorkerGraphFake()
    graph.block_inspection = True
    graph.ignore_cancellation = True
    worker = DurableExecutionWorker(
        graph,
        repository,
        worker_id="worker-1",
        options=_options(),
    )
    await worker.start()
    await asyncio.wait_for(graph.inspection_entered.wait(), timeout=1)

    started = monotonic()
    await worker.stop(grace=timedelta(milliseconds=20))
    elapsed = monotonic() - started

    assert elapsed < 0.5
    assert worker.readiness.state.value == ExecutionWorkerState.STOPPED.value
    graph.release_inspection.set()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_idle_worker_renews_presence_and_removes_registration_on_stop() -> None:
    repository = MemoryConversationRepository()
    worker = DurableExecutionWorker(
        WorkerGraphFake(),
        repository,
        worker_id="idle-worker",
        options=_options(),
    )

    await worker.start()
    initial_heartbeat = worker.readiness.last_heartbeat_at
    assert initial_heartbeat is not None
    assert await worker.is_ready() is True

    for _ in range(100):
        await asyncio.sleep(0.01)
        renewed = worker.readiness.last_heartbeat_at
        if renewed is not None and renewed > initial_heartbeat:
            break
    else:
        pytest.fail("idle worker presence heartbeat was not renewed")

    await worker.stop()

    assert worker.readiness.state == ExecutionWorkerState.STOPPED
    assert await repository.is_execution_worker_ready("idle-worker") is False


@pytest.mark.asyncio
async def test_replaced_presence_registration_stops_the_stale_worker() -> None:
    repository = MemoryConversationRepository()
    worker = DurableExecutionWorker(
        WorkerGraphFake(),
        repository,
        worker_id="fenced-worker",
        options=_options(),
    )
    await worker.start()
    replacement = await repository.register_execution_worker(
        "fenced-worker",
        heartbeat_ttl=timedelta(seconds=15),
    )

    for _ in range(100):
        await asyncio.sleep(0.01)
        if worker.readiness.state == ExecutionWorkerState.STOPPED:
            break
    else:
        pytest.fail("stale worker did not stop after its registration was fenced")

    assert worker.readiness.last_error_code == "WORKER_REGISTRATION_LOST"
    assert await repository.is_execution_worker_ready(replacement.worker_id) is True
    await worker.stop()
