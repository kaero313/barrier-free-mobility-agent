from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest
from pydantic import ValidationError

from barrier_free_mobility_agent.agent.models import GraphTurnStatus, ResumeMode
from barrier_free_mobility_agent.application.conversation_repository import ConversationThread
from barrier_free_mobility_agent.application.conversations import ConversationRun
from barrier_free_mobility_agent.domain.clarification import (
    ClarificationFieldAnswer,
    FieldsClarificationAnswer,
    TextClarificationAnswerValue,
)
from barrier_free_mobility_agent.domain.errors import (
    AgentPolicyViolationError,
    ExecutionJobNotFoundError,
    ExecutionLeaseLostError,
    ExecutionWorkerRegistrationLostError,
    ExecutionWorkerUnavailableError,
    IdempotencyKeyReuseError,
    StateInputBudgetExceededError,
    ThreadBusyError,
)
from barrier_free_mobility_agent.domain.execution_queue import (
    EXECUTION_PAYLOAD_MAX_BYTES,
    ExecutionJobState,
    ExecutionLease,
    ExecutionQueueHealth,
    ExecutionWorkerRegistration,
    ExecutionWorkerRegistrationState,
    ResumeCommandExecutionPayload,
    ResumeNewTurnExecutionPayload,
    StartExecutionPayload,
    validate_execution_payload_size,
)
from barrier_free_mobility_agent.domain.models import AnswerOutcome
from barrier_free_mobility_agent.domain.routing import ToolCapability
from barrier_free_mobility_agent.domain.run_ledger import (
    ResolveStationToolSummary,
    RunEventType,
    RunExecutionCorrelation,
    ToolTransition,
)
from barrier_free_mobility_agent.persistence.memory import MemoryConversationRepository

OWNER_ID = "owner-1"
THREAD_ID = "a" * 32


def _digest(value: str) -> str:
    return sha256(value.encode()).hexdigest()


async def _repository(*, owner_id: str = OWNER_ID) -> MemoryConversationRepository:
    repository = MemoryConversationRepository()
    await _add_thread(repository, thread_id=THREAD_ID, owner_id=owner_id)
    return repository


async def _claim_execution_job(
    repository: MemoryConversationRepository,
    *,
    worker_id: str,
    lease_duration: timedelta,
    now: datetime | None = None,
) -> ExecutionLease | None:
    registration = await repository.register_execution_worker(
        worker_id,
        heartbeat_ttl=timedelta(hours=1),
        now=now,
    )
    try:
        return await repository.claim_execution_job(
            registration=registration,
            lease_duration=lease_duration,
            now=now,
        )
    finally:
        await repository.remove_execution_worker(registration)


async def _add_thread(
    repository: MemoryConversationRepository,
    *,
    thread_id: str,
    owner_id: str,
) -> None:
    await repository.create_thread(
        ConversationThread(
            thread_id=thread_id,
            owner_id=owner_id,
            flow_version="queue-test-flow",
            state_schema_version="queue-test-state",
        ),
        capacity=10,
        total_capacity=10,
    )


def _correlation(
    *,
    thread_id: str = THREAD_ID,
    run_id: str = "b" * 32,
    request_id: str = "request-1",
) -> RunExecutionCorrelation:
    return RunExecutionCorrelation(
        thread_id=thread_id,
        run_id=run_id,
        request_id=request_id,
        attempt=1,
    )


@pytest.mark.asyncio
async def test_execution_payload_budget_error_never_retains_or_stores_raw_input() -> None:
    largest_text_payload = StartExecutionPayload(question="가" * 20_000)
    largest_new_turn_payload = ResumeNewTurnExecutionPayload(answer="가" * 20_000)
    assert (
        len(largest_text_payload.model_dump_json().encode("utf-8")) <= EXECUTION_PAYLOAD_MAX_BYTES
    )
    assert (
        len(largest_new_turn_payload.model_dump_json().encode("utf-8"))
        <= EXECUTION_PAYLOAD_MAX_BYTES
    )
    validate_execution_payload_size(largest_text_payload)
    validate_execution_payload_size(largest_new_turn_payload)

    sentinel = "RAW_EXECUTION_PAYLOAD_SENTINEL"
    field_value = sentinel + ("가" * (2_000 - len(sentinel)))
    large_fields = FieldsClarificationAnswer(
        values=tuple(
            ClarificationFieldAnswer(
                input_id=f"field-{index}",
                value=TextClarificationAnswerValue(value=field_value),
            )
            for index in range(16)
        )
    )
    oversized = ResumeCommandExecutionPayload(answer=large_fields)
    with pytest.raises(StateInputBudgetExceededError) as direct_error:
        validate_execution_payload_size(oversized)
    assert direct_error.value.context["budget"] == "execution_payload_bytes"
    assert direct_error.value.context["limit"] == EXECUTION_PAYLOAD_MAX_BYTES
    assert direct_error.value.context["actual"] > EXECUTION_PAYLOAD_MAX_BYTES
    for rendered in (
        str(direct_error.value),
        repr(direct_error.value),
        repr(direct_error.value.context),
    ):
        assert sentinel not in rendered

    repository = await _repository()
    correlation = _correlation()
    await repository.reserve_initial_run(correlation, owner_id=OWNER_ID, capacity=10)
    await repository.finalize_run(
        ConversationRun(
            run_id=correlation.run_id,
            thread_id=THREAD_ID,
            status=GraphTurnStatus.INTERRUPTED,
            resume_mode=ResumeMode.COMMAND,
            outcome=AnswerOutcome.question_too_complex(),
        ),
        correlation=correlation,
        owner_id=OWNER_ID,
    )
    with pytest.raises(StateInputBudgetExceededError) as repository_error:
        await repository.reserve_resume_execution(
            correlation.run_id,
            request_id="oversized-resume",
            owner_id=OWNER_ID,
            idempotency_digest=_digest("oversized-key"),
            request_fingerprint=_digest("oversized-request"),
            payload=oversized,
        )
    assert sentinel not in str(repository_error.value)
    assert sentinel not in repr(repository_error.value)
    assert sentinel not in repr(repository_error.value.context)
    events = await repository.get_run_events(correlation.run_id, owner_id=OWNER_ID)
    assert [event.event_type for event in events] == [
        RunEventType.RUN_STARTED,
        RunEventType.RUN_INTERRUPTED,
    ]
    assert (
        await _claim_execution_job(
            repository,
            worker_id="worker-1",
            lease_duration=timedelta(seconds=10),
        )
        is None
    )


@pytest.mark.asyncio
async def test_concurrent_initial_reservation_replays_one_job_and_run() -> None:
    repository = await _repository()
    digest = _digest("idempotency-key")
    fingerprint = _digest("POST:/runs:question")

    reservations = await asyncio.gather(
        *(
            repository.reserve_initial_execution(
                _correlation(
                    run_id=f"{index:032x}",
                    request_id=f"request-{index}",
                ),
                owner_id=OWNER_ID,
                capacity=10,
                idempotency_digest=digest,
                request_fingerprint=fingerprint,
                payload=StartExecutionPayload(question="서울역에서 시청역까지 갈 수 있어?"),
            )
            for index in range(1, 21)
        )
    )

    assert sum(not reservation.replayed for reservation in reservations) == 1
    assert len({reservation.job.job_id for reservation in reservations}) == 1
    assert len({reservation.correlation for reservation in reservations}) == 1
    selected = reservations[0]
    thread = await repository.get_thread(THREAD_ID, owner_id=OWNER_ID)
    events = await repository.get_run_events(
        selected.correlation.run_id,
        owner_id=OWNER_ID,
    )
    assert thread.run_count == 1
    assert thread.active_run_id == selected.correlation.run_id
    assert [event.event_type for event in events] == [RunEventType.RUN_STARTED]


@pytest.mark.asyncio
async def test_idempotency_reuse_rejects_a_different_request_fingerprint() -> None:
    repository = await _repository()
    digest = _digest("shared-key")
    first = await repository.reserve_initial_execution(
        _correlation(),
        owner_id=OWNER_ID,
        capacity=10,
        idempotency_digest=digest,
        request_fingerprint=_digest("first-request"),
        payload=StartExecutionPayload(question="첫 질문"),
    )

    with pytest.raises(IdempotencyKeyReuseError):
        await repository.reserve_initial_execution(
            _correlation(run_id="c" * 32, request_id="request-2"),
            owner_id=OWNER_ID,
            capacity=10,
            idempotency_digest=digest,
            request_fingerprint=_digest("different-request"),
            payload=StartExecutionPayload(question="다른 질문"),
        )

    stored = await repository.get_execution_job(first.job.job_id, owner_id=OWNER_ID)
    assert stored == first.job


@pytest.mark.asyncio
async def test_idempotency_replay_requires_the_same_target_but_is_owner_scoped() -> None:
    repository = await _repository()
    second_thread_id = "c" * 32
    other_owner_thread_id = "d" * 32
    other_owner_id = "owner-2"
    await _add_thread(repository, thread_id=second_thread_id, owner_id=OWNER_ID)
    await _add_thread(
        repository,
        thread_id=other_owner_thread_id,
        owner_id=other_owner_id,
    )
    digest = _digest("shared-target-key")
    fingerprint = _digest("same-request-body")
    payload = StartExecutionPayload(question="동일한 질문")
    await repository.reserve_initial_execution(
        _correlation(),
        owner_id=OWNER_ID,
        capacity=10,
        idempotency_digest=digest,
        request_fingerprint=fingerprint,
        payload=payload,
    )

    with pytest.raises(IdempotencyKeyReuseError):
        await repository.reserve_initial_execution(
            _correlation(
                thread_id=second_thread_id,
                run_id="e" * 32,
                request_id="request-2",
            ),
            owner_id=OWNER_ID,
            capacity=10,
            idempotency_digest=digest,
            request_fingerprint=fingerprint,
            payload=payload,
        )

    other_owner = await repository.reserve_initial_execution(
        _correlation(
            thread_id=other_owner_thread_id,
            run_id="f" * 32,
            request_id="request-3",
        ),
        owner_id=other_owner_id,
        capacity=10,
        idempotency_digest=digest,
        request_fingerprint=fingerprint,
        payload=payload,
    )
    assert other_owner.replayed is False


@pytest.mark.asyncio
async def test_resume_idempotency_replay_requires_the_same_run() -> None:
    repository = await _repository()
    second_thread_id = "c" * 32
    await _add_thread(repository, thread_id=second_thread_id, owner_id=OWNER_ID)
    first_correlation = _correlation()
    second_correlation = _correlation(
        thread_id=second_thread_id,
        run_id="d" * 32,
        request_id="request-2",
    )
    for correlation in (first_correlation, second_correlation):
        await repository.reserve_initial_run(correlation, owner_id=OWNER_ID, capacity=10)
        await repository.finalize_run(
            ConversationRun(
                run_id=correlation.run_id,
                thread_id=correlation.thread_id,
                status=GraphTurnStatus.INTERRUPTED,
                resume_mode=ResumeMode.COMMAND,
                outcome=AnswerOutcome.question_too_complex(),
            ),
            correlation=correlation,
            owner_id=OWNER_ID,
        )
    digest = _digest("shared-resume-key")
    fingerprint = _digest("same-resume-body")
    payload = ResumeCommandExecutionPayload(answer="동일한 답변")
    await repository.reserve_resume_execution(
        first_correlation.run_id,
        request_id="resume-request-1",
        owner_id=OWNER_ID,
        idempotency_digest=digest,
        request_fingerprint=fingerprint,
        payload=payload,
    )

    with pytest.raises(IdempotencyKeyReuseError):
        await repository.reserve_resume_execution(
            second_correlation.run_id,
            request_id="resume-request-2",
            owner_id=OWNER_ID,
            idempotency_digest=digest,
            request_fingerprint=fingerprint,
            payload=payload,
        )


@pytest.mark.asyncio
async def test_resume_reservation_replays_after_terminal_job_without_new_event() -> None:
    repository = await _repository()
    initial = await repository.reserve_initial_execution(
        _correlation(),
        owner_id=OWNER_ID,
        capacity=10,
        idempotency_digest=_digest("initial-key"),
        request_fingerprint=_digest("initial-request"),
        payload=StartExecutionPayload(question="출발지와 도착지를 확인해 줘"),
    )
    claimed_at = datetime.now(UTC) + timedelta(seconds=1)
    initial_lease = await _claim_execution_job(
        repository,
        worker_id="worker-1",
        lease_duration=timedelta(seconds=30),
        now=claimed_at,
    )
    assert initial_lease is not None
    interrupted = ConversationRun(
        run_id=initial.correlation.run_id,
        thread_id=THREAD_ID,
        status=GraphTurnStatus.INTERRUPTED,
        resume_mode=ResumeMode.COMMAND,
        outcome=AnswerOutcome.question_too_complex(),
    )
    await repository.finalize_run(
        interrupted,
        correlation=initial.correlation,
        owner_id=OWNER_ID,
    )
    await repository.complete_execution_job(
        initial_lease,
        checkpoint_cursor="checkpoint:initial",
        now=claimed_at + timedelta(seconds=1),
    )

    resume_digest = _digest("resume-key")
    resume_fingerprint = _digest("resume-request")
    reserved = await repository.reserve_resume_execution(
        initial.correlation.run_id,
        request_id="resume-request-1",
        owner_id=OWNER_ID,
        idempotency_digest=resume_digest,
        request_fingerprint=resume_fingerprint,
        payload=ResumeCommandExecutionPayload(answer="서울역에서 시청역"),
    )
    resume_lease = await _claim_execution_job(
        repository,
        worker_id="worker-1",
        lease_duration=timedelta(seconds=30),
        now=claimed_at + timedelta(seconds=2),
    )
    assert resume_lease is not None
    await repository.finalize_run(
        ConversationRun(
            run_id=reserved.correlation.run_id,
            thread_id=THREAD_ID,
            status=GraphTurnStatus.COMPLETED,
            resume_mode=ResumeMode.NONE,
            outcome=AnswerOutcome.question_too_complex(),
        ),
        correlation=reserved.correlation,
        owner_id=OWNER_ID,
    )
    await repository.complete_execution_job(
        resume_lease,
        now=claimed_at + timedelta(seconds=3),
    )
    replayed = await repository.reserve_resume_execution(
        initial.correlation.run_id,
        request_id="resume-request-2",
        owner_id=OWNER_ID,
        idempotency_digest=resume_digest,
        request_fingerprint=resume_fingerprint,
        payload=ResumeCommandExecutionPayload(answer="서울역에서 시청역"),
    )

    assert replayed.replayed is True
    assert replayed.correlation == reserved.correlation
    assert replayed.job.state == ExecutionJobState.COMPLETED
    assert replayed.job.payload is None
    events = await repository.get_run_events(initial.correlation.run_id, owner_id=OWNER_ID)
    assert [event.event_type for event in events].count(RunEventType.RUN_RESUME_STARTED) == 1


@pytest.mark.asyncio
async def test_resume_operation_must_match_the_interrupted_resume_mode() -> None:
    repository = await _repository()
    correlation = _correlation()
    await repository.reserve_initial_run(correlation, owner_id=OWNER_ID, capacity=10)
    await repository.finalize_run(
        ConversationRun(
            run_id=correlation.run_id,
            thread_id=THREAD_ID,
            status=GraphTurnStatus.INTERRUPTED,
            resume_mode=ResumeMode.COMMAND,
            outcome=AnswerOutcome.question_too_complex(),
        ),
        correlation=correlation,
        owner_id=OWNER_ID,
    )

    with pytest.raises(AgentPolicyViolationError) as raised:
        await repository.reserve_resume_execution(
            correlation.run_id,
            request_id="resume-request",
            owner_id=OWNER_ID,
            idempotency_digest=_digest("resume-key"),
            request_fingerprint=_digest("resume-request"),
            payload=ResumeNewTurnExecutionPayload(answer="새 질문"),
        )
    assert raised.value.context == {"reason": "execution_operation_resume_mode_mismatch"}


@pytest.mark.asyncio
async def test_heartbeat_release_reclaim_and_completion_use_fenced_lease() -> None:
    repository = await _repository()
    reservation = await repository.reserve_initial_execution(
        _correlation(),
        owner_id=OWNER_ID,
        capacity=10,
        idempotency_digest=_digest("lease-key"),
        request_fingerprint=_digest("lease-request"),
        payload=StartExecutionPayload(question="이동 가능 여부를 확인해 줘"),
    )
    claimed_at = datetime.now(UTC) + timedelta(seconds=1)
    first = await _claim_execution_job(
        repository,
        worker_id="worker-1",
        lease_duration=timedelta(seconds=10),
        now=claimed_at,
    )
    assert first is not None
    heartbeat = await repository.heartbeat_execution_job(
        first,
        lease_duration=timedelta(seconds=20),
        checkpoint_cursor="checkpoint:1",
        now=claimed_at + timedelta(seconds=5),
    )
    assert heartbeat.expires_at == claimed_at + timedelta(seconds=25)
    assert heartbeat.checkpoint_cursor == "checkpoint:1"
    invalid_lease = heartbeat.model_dump(mode="python")
    invalid_lease["expires_at"] = heartbeat.heartbeat_at
    with pytest.raises(ValidationError, match="expires_at must follow heartbeat_at"):
        ExecutionLease.model_validate(invalid_lease)

    stale_token = heartbeat.model_copy(update={"token": "stale-token"})
    with pytest.raises(ExecutionLeaseLostError):
        await repository.assert_execution_lease(
            stale_token,
            now=claimed_at + timedelta(seconds=6),
        )

    released = await repository.release_execution_job(
        heartbeat,
        available_at=claimed_at + timedelta(seconds=10),
        checkpoint_cursor="checkpoint:2",
        now=claimed_at + timedelta(seconds=6),
    )
    assert released.state == ExecutionJobState.QUEUED
    assert released.payload is not None
    assert released.lease_generation == 1
    assert released.claim_count == 1
    with pytest.raises(ExecutionLeaseLostError):
        await repository.assert_execution_lease(
            heartbeat,
            now=claimed_at + timedelta(seconds=7),
        )

    assert (
        await _claim_execution_job(
            repository,
            worker_id="worker-2",
            lease_duration=timedelta(seconds=10),
            now=claimed_at + timedelta(seconds=9),
        )
        is None
    )
    second = await _claim_execution_job(
        repository,
        worker_id="worker-2",
        lease_duration=timedelta(seconds=10),
        now=claimed_at + timedelta(seconds=10),
    )
    assert second is not None
    assert second.generation == 2
    assert second.token != first.token
    assert second.checkpoint_cursor == "checkpoint:2"
    assert second.payload_schema_version == 1
    await repository.finalize_run(
        ConversationRun(
            run_id=reservation.correlation.run_id,
            thread_id=THREAD_ID,
            status=GraphTurnStatus.COMPLETED,
            resume_mode=ResumeMode.NONE,
            outcome=AnswerOutcome.question_too_complex(),
        ),
        correlation=reservation.correlation,
        owner_id=OWNER_ID,
    )
    completed = await repository.complete_execution_job(
        second,
        checkpoint_cursor="checkpoint:3",
        now=claimed_at + timedelta(seconds=11),
    )
    assert completed.state == ExecutionJobState.COMPLETED
    assert completed.payload is None
    assert completed.payload_schema_version == 1
    assert completed.checkpoint_cursor == "checkpoint:3"
    assert completed.lease_token is None
    assert completed.completed_at == claimed_at + timedelta(seconds=11)
    stored = await repository.get_execution_job(reservation.job.job_id, owner_id=OWNER_ID)
    assert stored == completed


@pytest.mark.asyncio
async def test_expired_lease_is_reclaimed_and_old_worker_cannot_commit() -> None:
    repository = await _repository()
    reservation = await repository.reserve_initial_execution(
        _correlation(),
        owner_id=OWNER_ID,
        capacity=10,
        idempotency_digest=_digest("expiry-key"),
        request_fingerprint=_digest("expiry-request"),
        payload=StartExecutionPayload(question="질문"),
    )
    claimed_at = datetime.now(UTC) + timedelta(seconds=1)
    first = await _claim_execution_job(
        repository,
        worker_id="worker-1",
        lease_duration=timedelta(seconds=5),
        now=claimed_at,
    )
    assert first is not None
    expires_at = claimed_at + timedelta(seconds=5)

    with pytest.raises(ExecutionLeaseLostError):
        await repository.heartbeat_execution_job(
            first,
            lease_duration=timedelta(seconds=5),
            now=expires_at,
        )
    second = await _claim_execution_job(
        repository,
        worker_id="worker-2",
        lease_duration=timedelta(seconds=5),
        now=expires_at,
    )
    assert second is not None
    assert second.generation == 2
    with pytest.raises(ExecutionLeaseLostError):
        await repository.complete_execution_job(
            first,
            now=expires_at + timedelta(seconds=1),
        )

    await repository.finalize_run(
        ConversationRun(
            run_id=reservation.correlation.run_id,
            thread_id=THREAD_ID,
            status=GraphTurnStatus.FAILED,
            resume_mode=ResumeMode.NONE,
            outcome=AnswerOutcome.question_too_complex(),
        ),
        correlation=reservation.correlation,
        owner_id=OWNER_ID,
    )
    failed = await repository.fail_execution_job(
        second,
        failure_code="WORKER_EXECUTION_FAILED",
        checkpoint_cursor="checkpoint:failed",
        now=expires_at + timedelta(seconds=1),
    )
    assert failed.state == ExecutionJobState.FAILED
    assert failed.payload is None
    assert failed.failure_code == "WORKER_EXECUTION_FAILED"
    assert failed.claim_count == 2
    assert await repository.get_execution_job(reservation.job.job_id, owner_id=OWNER_ID) == failed


@pytest.mark.asyncio
async def test_execution_jobs_are_owner_scoped_and_deleted_with_the_thread() -> None:
    repository = await _repository()
    reservation = await repository.reserve_initial_execution(
        _correlation(),
        owner_id=OWNER_ID,
        capacity=10,
        idempotency_digest=_digest("owner-key"),
        request_fingerprint=_digest("owner-request"),
        payload=StartExecutionPayload(question="질문"),
    )

    with pytest.raises(ExecutionJobNotFoundError):
        await repository.get_execution_job(reservation.job.job_id, owner_id="another-owner")
    with pytest.raises(ThreadBusyError):
        await repository.delete_thread(THREAD_ID, owner_id=OWNER_ID)
    lease = await _claim_execution_job(
        repository,
        worker_id="delete-test-worker",
        lease_duration=timedelta(seconds=30),
    )
    assert lease is not None
    await repository.fail_execution_job(
        lease,
        failure_code="DELETE_TEST_TERMINAL",
    )
    await repository.delete_thread(THREAD_ID, owner_id=OWNER_ID)
    with pytest.raises(ExecutionJobNotFoundError):
        await repository.get_execution_job(reservation.job.job_id, owner_id=OWNER_ID)


@pytest.mark.asyncio
async def test_lease_bound_tool_ledger_and_run_job_finalization_are_atomic() -> None:
    repository = await _repository()
    reservation = await repository.reserve_initial_execution(
        _correlation(),
        owner_id=OWNER_ID,
        capacity=10,
        idempotency_digest=_digest("atomic-key"),
        request_fingerprint=_digest("atomic-request"),
        payload=StartExecutionPayload(question="question"),
    )
    lease = await _claim_execution_job(
        repository,
        worker_id="worker-atomic",
        lease_duration=timedelta(minutes=1),
    )
    assert lease is not None
    stale = lease.model_copy(update={"token": "stale-token"})
    with pytest.raises(ExecutionLeaseLostError):
        await repository.append_tool_started(
            reservation.correlation,
            owner_id=OWNER_ID,
            execution_id="tool-stale",
            logical_call_id="logical-stale",
            attempt=1,
            tool=ToolCapability.RESOLVE_STATION,
            execution_lease=stale,
        )

    handle = await repository.append_tool_started(
        reservation.correlation,
        owner_id=OWNER_ID,
        execution_id="tool-valid",
        logical_call_id="logical-valid",
        attempt=1,
        tool=ToolCapability.RESOLVE_STATION,
        execution_lease=lease,
    )
    with pytest.raises(ExecutionLeaseLostError):
        await repository.append_tool_started(
            reservation.correlation,
            owner_id=OWNER_ID,
            execution_id="tool-valid",
            logical_call_id="logical-valid",
            attempt=1,
            tool=ToolCapability.RESOLVE_STATION,
            execution_lease=stale,
        )
    record = await repository.append_tool_terminal(
        handle,
        owner_id=OWNER_ID,
        transition=ToolTransition.COMPLETED,
        duration_ms=1,
        summary=ResolveStationToolSummary(
            matched=True,
            candidate_count=1,
            clarification_required=False,
        ),
        execution_lease=lease,
    )
    with pytest.raises(ExecutionLeaseLostError):
        await repository.append_tool_terminal(
            handle,
            owner_id=OWNER_ID,
            transition=ToolTransition.COMPLETED,
            duration_ms=1,
            summary=record.summary,
            execution_lease=stale,
        )
    run = ConversationRun(
        run_id=reservation.correlation.run_id,
        thread_id=THREAD_ID,
        status=GraphTurnStatus.COMPLETED,
        resume_mode=ResumeMode.NONE,
        outcome=AnswerOutcome.question_too_complex(),
    )
    stored = await repository.finalize_execution(
        run,
        correlation=reservation.correlation,
        owner_id=OWNER_ID,
        execution_lease=lease,
        job_state=ExecutionJobState.COMPLETED,
        checkpoint_cursor="checkpoint:terminal",
    )

    assert stored == run
    job = await repository.get_execution_job(reservation.job.job_id, owner_id=OWNER_ID)
    assert job.state == ExecutionJobState.COMPLETED
    assert job.payload is None
    assert job.checkpoint_cursor == "checkpoint:terminal"
    events = await repository.get_run_events(run.run_id, owner_id=OWNER_ID)
    assert [event.event_type for event in events] == [
        RunEventType.RUN_STARTED,
        RunEventType.TOOL_STARTED,
        RunEventType.TOOL_COMPLETED,
        RunEventType.RUN_COMPLETED,
    ]


@pytest.mark.asyncio
async def test_execution_reservation_lookup_is_owner_scoped_and_replayed() -> None:
    repository = await _repository()
    digest = _digest("lookup-key")
    reservation = await repository.reserve_initial_execution(
        _correlation(),
        owner_id=OWNER_ID,
        capacity=10,
        idempotency_digest=digest,
        request_fingerprint=_digest("lookup-request"),
        payload=StartExecutionPayload(question="question"),
    )

    found = await repository.find_execution_reservation(
        owner_id=OWNER_ID,
        idempotency_digests=(digest,),
    )
    assert found is not None
    assert found.replayed is True
    assert found.job == reservation.job
    assert (
        await repository.find_execution_reservation(
            owner_id="other-owner",
            idempotency_digests=(digest,),
        )
        is None
    )


@pytest.mark.asyncio
async def test_thread_deletion_fence_blocks_new_jobs_and_can_be_aborted() -> None:
    repository = await _repository()

    prepared = await repository.prepare_thread_deletion(THREAD_ID, owner_id=OWNER_ID)
    replayed = await repository.prepare_thread_deletion(THREAD_ID, owner_id=OWNER_ID)
    assert replayed.token == prepared.token
    assert replayed.replayed is True
    with pytest.raises(ThreadBusyError):
        await repository.reserve_initial_execution(
            _correlation(),
            owner_id=OWNER_ID,
            capacity=10,
            idempotency_digest=_digest("deleting-key"),
            request_fingerprint=_digest("deleting-request"),
            payload=StartExecutionPayload(question="blocked"),
        )

    await repository.abort_thread_deletion(prepared)
    reservation = await repository.reserve_initial_execution(
        _correlation(),
        owner_id=OWNER_ID,
        capacity=10,
        idempotency_digest=_digest("after-abort-key"),
        request_fingerprint=_digest("after-abort-request"),
        payload=StartExecutionPayload(question="accepted"),
    )
    assert reservation.replayed is False


@pytest.mark.asyncio
async def test_worker_registration_is_renewable_draining_and_fenced() -> None:
    repository = await _repository()
    registered_at = datetime.now(UTC)
    first = await repository.register_execution_worker(
        "worker-registry-1",
        heartbeat_ttl=timedelta(seconds=15),
        now=registered_at,
    )

    assert first.state == ExecutionWorkerRegistrationState.READY
    assert first.generation == 1
    assert first.heartbeat_at == registered_at

    heartbeat = await repository.heartbeat_execution_worker(
        first,
        heartbeat_ttl=timedelta(seconds=15),
        now=registered_at + timedelta(seconds=5),
    )
    assert heartbeat.token == first.token
    assert heartbeat.expires_at == registered_at + timedelta(seconds=20)

    replacement = await repository.register_execution_worker(
        first.worker_id,
        heartbeat_ttl=timedelta(seconds=15),
        now=registered_at + timedelta(seconds=6),
    )
    assert replacement.generation == 2
    assert replacement.token != first.token

    with pytest.raises(ExecutionWorkerRegistrationLostError):
        await repository.heartbeat_execution_worker(
            heartbeat,
            heartbeat_ttl=timedelta(seconds=15),
            now=registered_at + timedelta(seconds=7),
        )
    with pytest.raises(ExecutionWorkerRegistrationLostError):
        await repository.drain_execution_worker(
            heartbeat,
            now=registered_at + timedelta(seconds=7),
        )
    with pytest.raises(ExecutionWorkerRegistrationLostError):
        await repository.remove_execution_worker(heartbeat)

    draining = await repository.drain_execution_worker(
        replacement,
        now=registered_at + timedelta(seconds=8),
    )
    assert draining.state == ExecutionWorkerRegistrationState.DRAINING
    assert await repository.is_execution_worker_ready(draining.worker_id) is False
    with pytest.raises(ExecutionWorkerRegistrationLostError):
        await repository.heartbeat_execution_worker(
            draining,
            heartbeat_ttl=timedelta(seconds=15),
            now=registered_at + timedelta(seconds=9),
        )

    await repository.remove_execution_worker(draining)
    with pytest.raises(ExecutionWorkerRegistrationLostError):
        await repository.remove_execution_worker(draining)


@pytest.mark.asyncio
async def test_worker_registration_prunes_other_expired_identities() -> None:
    repository = await _repository()
    registered_at = datetime.now(UTC)
    expired = await repository.register_execution_worker(
        "expired-worker-registration",
        heartbeat_ttl=timedelta(seconds=1),
        now=registered_at,
    )

    current = await repository.register_execution_worker(
        "current-worker-registration",
        heartbeat_ttl=timedelta(seconds=30),
        now=registered_at + timedelta(seconds=2),
    )

    with pytest.raises(ExecutionWorkerRegistrationLostError):
        await repository.remove_execution_worker(expired)
    await repository.remove_execution_worker(current)


@pytest.mark.asyncio
async def test_claim_requires_current_ready_registration_and_competes_once() -> None:
    repository = await _repository()
    reservation = await repository.reserve_initial_execution(
        _correlation(),
        owner_id=OWNER_ID,
        capacity=10,
        idempotency_digest=_digest("registration-bound-claim-key"),
        request_fingerprint=_digest("registration-bound-claim-request"),
        payload=StartExecutionPayload(question="등록된 워커만 실행해 줘"),
    )
    claim_at = reservation.job.available_at + timedelta(seconds=5)
    absent = ExecutionWorkerRegistration(
        worker_id="absent-worker",
        token="absent-token",
        generation=1,
        state=ExecutionWorkerRegistrationState.READY,
        registered_at=claim_at - timedelta(seconds=1),
        heartbeat_at=claim_at - timedelta(seconds=1),
        expires_at=claim_at + timedelta(seconds=30),
    )
    with pytest.raises(ExecutionWorkerRegistrationLostError):
        await repository.claim_execution_job(
            registration=absent,
            lease_duration=timedelta(seconds=10),
            now=claim_at,
        )

    expired = await repository.register_execution_worker(
        "expired-claim-worker",
        heartbeat_ttl=timedelta(seconds=1),
        now=claim_at - timedelta(seconds=2),
    )
    with pytest.raises(ExecutionWorkerRegistrationLostError):
        await repository.claim_execution_job(
            registration=expired,
            lease_duration=timedelta(seconds=10),
            now=claim_at,
        )

    ready_to_drain = await repository.register_execution_worker(
        "draining-claim-worker",
        heartbeat_ttl=timedelta(seconds=30),
        now=claim_at - timedelta(seconds=1),
    )
    draining = await repository.drain_execution_worker(
        ready_to_drain,
        now=claim_at,
    )
    with pytest.raises(ExecutionWorkerRegistrationLostError):
        await repository.claim_execution_job(
            registration=draining,
            lease_duration=timedelta(seconds=10),
            now=claim_at,
        )

    stale = await repository.register_execution_worker(
        "replaced-claim-worker",
        heartbeat_ttl=timedelta(seconds=30),
        now=claim_at - timedelta(seconds=1),
    )
    replacement = await repository.register_execution_worker(
        stale.worker_id,
        heartbeat_ttl=timedelta(seconds=30),
        now=claim_at,
    )
    with pytest.raises(ExecutionWorkerRegistrationLostError):
        await repository.claim_execution_job(
            registration=stale,
            lease_duration=timedelta(seconds=10),
            now=claim_at,
        )

    first_registration = await repository.register_execution_worker(
        "ready-claim-worker-1",
        heartbeat_ttl=timedelta(seconds=30),
        now=claim_at,
    )
    second_registration = await repository.register_execution_worker(
        "ready-claim-worker-2",
        heartbeat_ttl=timedelta(seconds=30),
        now=claim_at,
    )
    claims = await asyncio.gather(
        repository.claim_execution_job(
            registration=first_registration,
            lease_duration=timedelta(seconds=10),
            now=claim_at,
        ),
        repository.claim_execution_job(
            registration=second_registration,
            lease_duration=timedelta(seconds=10),
            now=claim_at,
        ),
    )
    leases = [lease for lease in claims if lease is not None]
    assert len(leases) == 1
    assert leases[0].job_id == reservation.job.job_id
    assert leases[0].worker_id in {
        first_registration.worker_id,
        second_registration.worker_id,
    }
    assert await repository.get_execution_job(
        reservation.job.job_id,
        owner_id=OWNER_ID,
    ) == reservation.job.model_copy(
        update={
            "state": ExecutionJobState.LEASED,
            "claim_count": 1,
            "lease_generation": 1,
            "lease_token": leases[0].token,
            "lease_owner": leases[0].worker_id,
            "lease_expires_at": leases[0].expires_at,
            "heartbeat_at": leases[0].heartbeat_at,
            "updated_at": leases[0].heartbeat_at,
        }
    )
    await repository.remove_execution_worker(replacement)
    await repository.remove_execution_worker(draining)
    await repository.remove_execution_worker(first_registration)
    await repository.remove_execution_worker(second_registration)


@pytest.mark.asyncio
async def test_worker_admission_gate_rejects_new_jobs_but_allows_replay() -> None:
    repository = await _repository()
    correlation = _correlation()
    payload = StartExecutionPayload(question="서울역에서 시청역까지 이동 가능해?")
    digest = _digest("worker-gate-key")
    fingerprint = _digest("worker-gate-request")

    with pytest.raises(ExecutionWorkerUnavailableError):
        await repository.reserve_initial_execution(
            correlation,
            owner_id=OWNER_ID,
            capacity=10,
            idempotency_digest=digest,
            request_fingerprint=fingerprint,
            payload=payload,
            require_available_worker=True,
        )
    thread = await repository.get_thread(THREAD_ID, owner_id=OWNER_ID)
    assert thread.active_run_id is None
    assert thread.run_count == 0
    unregistered_at = datetime.now(UTC)
    with pytest.raises(ExecutionWorkerRegistrationLostError):
        await repository.claim_execution_job(
            registration=ExecutionWorkerRegistration(
                worker_id="unregistered-worker",
                token="unregistered-token",
                generation=1,
                state=ExecutionWorkerRegistrationState.READY,
                registered_at=unregistered_at,
                heartbeat_at=unregistered_at,
                expires_at=unregistered_at + timedelta(seconds=10),
            ),
            lease_duration=timedelta(seconds=10),
        )

    registration = await repository.register_execution_worker(
        "worker-gate-1",
        heartbeat_ttl=timedelta(seconds=30),
    )
    reservation = await repository.reserve_initial_execution(
        correlation,
        owner_id=OWNER_ID,
        capacity=10,
        idempotency_digest=digest,
        request_fingerprint=fingerprint,
        payload=payload,
        require_available_worker=True,
    )
    draining = await repository.drain_execution_worker(registration)

    replay = await repository.reserve_initial_execution(
        correlation,
        owner_id=OWNER_ID,
        capacity=10,
        idempotency_digest=digest,
        request_fingerprint=fingerprint,
        payload=payload,
        require_available_worker=True,
    )
    assert replay.replayed is True
    assert replay.job.job_id == reservation.job.job_id

    with pytest.raises(ExecutionWorkerUnavailableError):
        await repository.reserve_initial_execution(
            _correlation(run_id="c" * 32, request_id="request-2"),
            owner_id=OWNER_ID,
            capacity=10,
            idempotency_digest=_digest("worker-gate-second-key"),
            request_fingerprint=_digest("worker-gate-second-request"),
            payload=payload,
            require_available_worker=True,
        )
    await repository.remove_execution_worker(draining)


@pytest.mark.asyncio
async def test_worker_admission_gate_rejects_expired_worker_and_resume() -> None:
    repository = await _repository()
    expired_at = datetime.now(UTC) - timedelta(seconds=5)
    await repository.register_execution_worker(
        "expired-worker",
        heartbeat_ttl=timedelta(seconds=1),
        now=expired_at,
    )
    with pytest.raises(ExecutionWorkerUnavailableError):
        await repository.reserve_initial_execution(
            _correlation(),
            owner_id=OWNER_ID,
            capacity=10,
            idempotency_digest=_digest("expired-worker-key"),
            request_fingerprint=_digest("expired-worker-request"),
            payload=StartExecutionPayload(question="이동 가능해?"),
            require_available_worker=True,
        )

    correlation = _correlation(run_id="d" * 32, request_id="resume-source")
    await repository.reserve_initial_run(correlation, owner_id=OWNER_ID, capacity=10)
    await repository.finalize_run(
        ConversationRun(
            run_id=correlation.run_id,
            thread_id=THREAD_ID,
            status=GraphTurnStatus.INTERRUPTED,
            resume_mode=ResumeMode.COMMAND,
            outcome=AnswerOutcome.question_too_complex(),
        ),
        correlation=correlation,
        owner_id=OWNER_ID,
    )
    with pytest.raises(ExecutionWorkerUnavailableError):
        await repository.reserve_resume_execution(
            correlation.run_id,
            request_id="resume-gated",
            owner_id=OWNER_ID,
            idempotency_digest=_digest("resume-gated-key"),
            request_fingerprint=_digest("resume-gated-request"),
            payload=ResumeCommandExecutionPayload(answer="서울역에서 시청역"),
            require_available_worker=True,
        )
    assert [
        event.event_type
        for event in await repository.get_run_events(correlation.run_id, owner_id=OWNER_ID)
    ] == [RunEventType.RUN_STARTED, RunEventType.RUN_INTERRUPTED]


@pytest.mark.asyncio
async def test_execution_queue_health_aggregates_worker_and_recovery_state() -> None:
    repository = await _repository()
    registered_at = datetime.now(UTC)
    health_registration = await repository.register_execution_worker(
        "health-worker",
        heartbeat_ttl=timedelta(minutes=5),
        now=registered_at,
    )
    reservation = await repository.reserve_initial_execution(
        _correlation(),
        owner_id=OWNER_ID,
        capacity=10,
        idempotency_digest=_digest("health-key"),
        request_fingerprint=_digest("health-request"),
        payload=StartExecutionPayload(question="상태를 확인해 줘"),
    )
    claimed_at = reservation.job.created_at + timedelta(seconds=1)
    first = await repository.claim_execution_job(
        registration=health_registration,
        lease_duration=timedelta(seconds=2),
        now=claimed_at,
    )
    assert first is not None

    expired_snapshot = await repository.get_execution_queue_health(
        stuck_after=timedelta(seconds=1),
        now=claimed_at + timedelta(seconds=3),
    )
    assert expired_snapshot == ExecutionQueueHealth(
        checked_at=claimed_at + timedelta(seconds=3),
        accepting_new_jobs=True,
        ready_worker_count=1,
        queued_job_count=0,
        leased_job_count=1,
        expired_lease_count=1,
        recovery_attempt_count=0,
        stuck_job_count=1,
        oldest_queued_at=None,
        oldest_queued_age_seconds=None,
        stuck_after_seconds=1.0,
    )

    replacement_registration = await repository.register_execution_worker(
        "replacement-worker",
        heartbeat_ttl=timedelta(minutes=5),
        now=claimed_at + timedelta(seconds=3),
    )
    second = await repository.claim_execution_job(
        registration=replacement_registration,
        lease_duration=timedelta(seconds=10),
        now=claimed_at + timedelta(seconds=3),
    )
    assert second is not None
    recovered_snapshot = await repository.get_execution_queue_health(
        stuck_after=timedelta(seconds=1),
        now=claimed_at + timedelta(seconds=3),
    )
    assert recovered_snapshot.recovery_attempt_count == 1
    assert recovered_snapshot.expired_lease_count == 0
    assert recovered_snapshot.stuck_job_count == 0


def test_worker_registration_and_queue_health_reject_inconsistent_snapshots() -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValidationError, match="expires_at must follow heartbeat_at"):
        ExecutionWorkerRegistration(
            worker_id="worker-invalid",
            token="token-invalid",
            generation=1,
            state=ExecutionWorkerRegistrationState.READY,
            registered_at=now,
            heartbeat_at=now,
            expires_at=now,
        )
    with pytest.raises(ValidationError, match="accepting_new_jobs"):
        ExecutionQueueHealth(
            checked_at=now,
            accepting_new_jobs=False,
            ready_worker_count=1,
            queued_job_count=0,
            leased_job_count=0,
            expired_lease_count=0,
            recovery_attempt_count=0,
            stuck_job_count=0,
            oldest_queued_at=None,
            oldest_queued_age_seconds=None,
            stuck_after_seconds=30,
        )
