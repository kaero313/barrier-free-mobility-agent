from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from barrier_free_mobility_agent.application.conversation_repository import (
    ConversationThread,
)
from barrier_free_mobility_agent.domain.errors import (
    AgentCapacityExceededError,
    AgentPolicyViolationError,
    ExecutionJobNotFoundError,
    ExecutionLeaseLostError,
    ExecutionWorkerRegistrationLostError,
    ExecutionWorkerUnavailableError,
    IdempotencyKeyReuseError,
    RunNotFoundError,
    ThreadBusyError,
    ThreadNotFoundError,
)
from barrier_free_mobility_agent.domain.execution_queue import (
    ExecutionJobState,
    ExecutionLease,
    ExecutionOperation,
    ExecutionPayload,
    ExecutionQueueHealth,
    ExecutionReservation,
    ExecutionWorkerRegistration,
    ExecutionWorkerRegistrationState,
    ResumeCommandExecutionPayload,
    ResumeExecutionPayload,
    RunExecutionJob,
    StartExecutionPayload,
    ThreadDeletionReservation,
    validate_execution_payload_size,
)
from barrier_free_mobility_agent.domain.routing import ToolCapability
from barrier_free_mobility_agent.domain.run_ledger import (
    OrphanedRunView,
    OrphanReason,
    RunEventRecord,
    RunEventType,
    RunExecutionCorrelation,
    RunExecutionPhase,
    ToolExecutionHandle,
    ToolExecutionRecord,
    ToolExecutionSummary,
    ToolFailureCode,
    ToolTransition,
)
from barrier_free_mobility_agent.domain.usage import (
    RunUsageRevisionConflictError,
    reconcile_run_usage,
)

if TYPE_CHECKING:
    from barrier_free_mobility_agent.application.conversations import ConversationRun


class MemoryConversationRepository:
    """Owner-aware process-local repository with atomic metadata transitions."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._threads: dict[str, ConversationThread] = {}
        self._runs: dict[str, ConversationRun] = {}
        self._run_correlations: dict[str, RunExecutionCorrelation] = {}
        self._run_phases: dict[str, RunExecutionPhase] = {}
        self._run_events: dict[str, list[RunEventRecord]] = {}
        self._open_tool_executions: dict[str, ToolExecutionHandle] = {}
        self._tool_executions: dict[str, ToolExecutionRecord] = {}
        self._tool_attempt_keys: set[tuple[str, str, int]] = set()
        self._execution_jobs: dict[str, RunExecutionJob] = {}
        self._execution_idempotency: dict[tuple[str, str], str] = {}
        self._execution_workers: dict[str, ExecutionWorkerRegistration] = {}
        self._thread_deletions: dict[str, ThreadDeletionReservation] = {}

    @property
    def persistence(self) -> Literal["ephemeral"]:
        return "ephemeral"

    async def create_thread(
        self,
        thread: ConversationThread,
        *,
        capacity: int,
        total_capacity: int | None = None,
    ) -> ConversationThread:
        _validate_capacity(capacity)
        _validate_optional_capacity(total_capacity)
        if thread.active_run_id is not None or thread.run_count != 0:
            raise AgentPolicyViolationError(context={"reason": "new_thread_must_be_empty"})
        async with self._lock:
            if thread.thread_id in self._threads:
                raise AgentPolicyViolationError(context={"reason": "thread_id_conflict"})
            if total_capacity is not None and len(self._threads) >= total_capacity:
                raise AgentCapacityExceededError(
                    context={"resource": "threads_total", "limit": total_capacity}
                )
            owner_count = sum(
                existing.owner_id == thread.owner_id for existing in self._threads.values()
            )
            if owner_count >= capacity:
                raise AgentCapacityExceededError(
                    context={"resource": "threads_per_owner", "limit": capacity}
                )
            stored = thread.model_copy(deep=True)
            self._threads[stored.thread_id] = stored
            return stored.model_copy(deep=True)

    async def get_thread(
        self,
        thread_id: str,
        *,
        owner_id: str,
    ) -> ConversationThread:
        async with self._lock:
            thread = self._owned_thread(thread_id, owner_id)
            return thread.model_copy(deep=True)

    async def add_run(
        self,
        run: ConversationRun,
        *,
        owner_id: str,
        capacity: int,
    ) -> ConversationThread:
        _validate_capacity(capacity)
        async with self._lock:
            thread = self._owned_thread(run.thread_id, owner_id)
            if thread.active_run_id is not None:
                raise ThreadBusyError(context={"thread_id": run.thread_id})
            if thread.run_count >= capacity:
                raise AgentCapacityExceededError(
                    context={"resource": "runs_per_thread", "limit": capacity}
                )
            if run.run_id in self._runs or run.run_id in self._run_correlations:
                raise AgentPolicyViolationError(context={"reason": "run_id_conflict"})
            stored_run = run.model_copy(deep=True)
            self._runs[stored_run.run_id] = stored_run
            updated = thread.model_copy(
                update={
                    "active_run_id": (stored_run.run_id if _is_resumable(stored_run) else None),
                    "run_count": thread.run_count + 1,
                }
            )
            self._threads[thread.thread_id] = updated
            return updated.model_copy(deep=True)

    async def update_run(
        self,
        run: ConversationRun,
        *,
        owner_id: str,
    ) -> ConversationRun:
        async with self._lock:
            if run.run_id in self._run_correlations:
                raise AgentPolicyViolationError(context={"reason": "run_ledger_finalize_required"})
            existing = self._runs.get(run.run_id)
            if existing is None:
                raise RunNotFoundError(context={"run_id": run.run_id})
            try:
                thread = self._owned_thread(existing.thread_id, owner_id)
            except ThreadNotFoundError as exc:
                raise RunNotFoundError(context={"run_id": run.run_id}) from exc
            if run.thread_id != existing.thread_id:
                raise AgentPolicyViolationError(context={"reason": "run_thread_change_forbidden"})
            try:
                selected_usage = reconcile_run_usage(existing.usage, run.usage)
            except RunUsageRevisionConflictError as exc:
                raise AgentPolicyViolationError(
                    context={"reason": "run_usage_revision_conflict"}
                ) from exc
            active_run_id = thread.active_run_id
            if _is_resumable(run):
                if active_run_id not in {None, run.run_id}:
                    raise ThreadBusyError(context={"thread_id": thread.thread_id})
                active_run_id = run.run_id
            elif active_run_id == run.run_id:
                active_run_id = None
            stored = run.model_copy(update={"usage": selected_usage}, deep=True)
            self._runs[stored.run_id] = stored
            self._threads[thread.thread_id] = thread.model_copy(
                update={"active_run_id": active_run_id}
            )
            return stored.model_copy(deep=True)

    async def reserve_initial_execution(
        self,
        correlation: RunExecutionCorrelation,
        *,
        owner_id: str,
        capacity: int,
        idempotency_digest: str,
        request_fingerprint: str,
        payload: StartExecutionPayload,
        require_available_worker: bool = False,
    ) -> ExecutionReservation:
        _validate_capacity(capacity)
        validate_execution_payload_size(payload)
        _validate_digest(idempotency_digest, name="idempotency_digest")
        _validate_digest(request_fingerprint, name="request_fingerprint")
        now = datetime.now(UTC)
        async with self._lock:
            thread = self._owned_thread(correlation.thread_id, owner_id)
            self._require_thread_accepts_execution(thread.thread_id)
            replay = self._replayed_execution(
                owner_id=owner_id,
                idempotency_digest=idempotency_digest,
                request_fingerprint=request_fingerprint,
                expected_operation=payload.operation,
                target_kind="thread",
                target_id=correlation.thread_id,
            )
            if replay is not None:
                return replay
            if require_available_worker:
                self._require_available_worker(now)
            if correlation.attempt != 1:
                raise AgentPolicyViolationError(
                    context={"reason": "initial_run_attempt_must_be_one"}
                )
            if correlation.run_id in self._runs or correlation.run_id in self._run_correlations:
                raise AgentPolicyViolationError(context={"reason": "run_id_conflict"})
            if thread.active_run_id is not None:
                raise ThreadBusyError(context={"thread_id": correlation.thread_id})
            if thread.run_count >= capacity:
                raise AgentCapacityExceededError(
                    context={"resource": "runs_per_thread", "limit": capacity}
                )
            event = self._new_event(
                correlation,
                phase=RunExecutionPhase.RUNNING,
                event_type=RunEventType.RUN_STARTED,
                event_key=_run_event_key(RunEventType.RUN_STARTED, correlation),
                occurred_at=now,
            )
            job = _new_execution_job(
                owner_id=owner_id,
                correlation=correlation,
                payload=payload,
                idempotency_digest=idempotency_digest,
                request_fingerprint=request_fingerprint,
                now=now,
            )
            self._run_correlations[correlation.run_id] = correlation.model_copy(deep=True)
            self._run_phases[correlation.run_id] = RunExecutionPhase.RUNNING
            self._run_events[correlation.run_id] = [event]
            self._threads[thread.thread_id] = thread.model_copy(
                update={
                    "active_run_id": correlation.run_id,
                    "run_count": thread.run_count + 1,
                }
            )
            self._execution_jobs[job.job_id] = job
            self._execution_idempotency[(owner_id, idempotency_digest)] = job.job_id
            return ExecutionReservation(job=job.model_copy(deep=True), replayed=False)

    async def reserve_resume_execution(
        self,
        run_id: str,
        *,
        request_id: str,
        owner_id: str,
        idempotency_digest: str,
        request_fingerprint: str,
        payload: ResumeExecutionPayload,
        require_available_worker: bool = False,
    ) -> ExecutionReservation:
        validate_execution_payload_size(payload)
        _validate_digest(idempotency_digest, name="idempotency_digest")
        _validate_digest(request_fingerprint, name="request_fingerprint")
        now = datetime.now(UTC)
        async with self._lock:
            current_correlation = self._owned_ledger_correlation(run_id, owner_id)
            self._require_thread_accepts_execution(current_correlation.thread_id)
            replay = self._replayed_execution(
                owner_id=owner_id,
                idempotency_digest=idempotency_digest,
                request_fingerprint=request_fingerprint,
                expected_operation=payload.operation,
                target_kind="run",
                target_id=run_id,
            )
            if replay is not None:
                return replay
            if require_available_worker:
                self._require_available_worker(now)
            if self._run_phases.get(run_id) != RunExecutionPhase.INTERRUPTED:
                raise AgentPolicyViolationError(context={"reason": "run_not_interrupted"})
            thread = self._owned_thread(current_correlation.thread_id, owner_id)
            if thread.active_run_id != run_id:
                raise AgentPolicyViolationError(context={"reason": "run_not_active"})
            current_run = self._runs.get(run_id)
            if current_run is None or not _resume_operation_matches(current_run, payload):
                raise AgentPolicyViolationError(
                    context={"reason": "execution_operation_resume_mode_mismatch"}
                )
            correlation = RunExecutionCorrelation(
                thread_id=current_correlation.thread_id,
                run_id=run_id,
                request_id=request_id,
                attempt=current_correlation.attempt + 1,
            )
            event = self._new_event(
                correlation,
                phase=RunExecutionPhase.RUNNING,
                event_type=RunEventType.RUN_RESUME_STARTED,
                event_key=_run_event_key(RunEventType.RUN_RESUME_STARTED, correlation),
                occurred_at=now,
            )
            job = _new_execution_job(
                owner_id=owner_id,
                correlation=correlation,
                payload=payload,
                idempotency_digest=idempotency_digest,
                request_fingerprint=request_fingerprint,
                now=now,
            )
            self._run_correlations[run_id] = correlation
            self._run_phases[run_id] = RunExecutionPhase.RUNNING
            self._run_events[run_id].append(event)
            self._execution_jobs[job.job_id] = job
            self._execution_idempotency[(owner_id, idempotency_digest)] = job.job_id
            return ExecutionReservation(job=job.model_copy(deep=True), replayed=False)

    async def find_execution_reservation(
        self,
        *,
        owner_id: str,
        idempotency_digests: tuple[str, ...],
    ) -> ExecutionReservation | None:
        digests = _validate_reservation_lookup_digests(idempotency_digests)
        async with self._lock:
            matched_ids = {
                job_id
                for digest in digests
                if (job_id := self._execution_idempotency.get((owner_id, digest))) is not None
            }
            if len(matched_ids) > 1:
                raise AgentPolicyViolationError(
                    context={"reason": "multiple_execution_reservations_matched"}
                )
            if not matched_ids:
                return None
            job = self._execution_jobs[next(iter(matched_ids))]
            return ExecutionReservation(job=job.model_copy(deep=True), replayed=True)

    async def register_execution_worker(
        self,
        worker_id: str,
        *,
        heartbeat_ttl: timedelta,
        now: datetime | None = None,
    ) -> ExecutionWorkerRegistration:
        _validate_execution_identifier(worker_id, field="worker_id")
        _validate_positive_duration(heartbeat_ttl, field="heartbeat_ttl")
        registered_at = _coerce_utc_datetime(now)
        async with self._lock:
            expired_worker_ids = tuple(
                registered_worker_id
                for registered_worker_id, registered_worker in self._execution_workers.items()
                if registered_worker_id != worker_id
                and registered_worker.expires_at <= registered_at
            )
            for expired_worker_id in expired_worker_ids:
                del self._execution_workers[expired_worker_id]
            previous = self._execution_workers.get(worker_id)
            registration = ExecutionWorkerRegistration(
                worker_id=worker_id,
                token=uuid4().hex,
                generation=1 if previous is None else previous.generation + 1,
                state=ExecutionWorkerRegistrationState.READY,
                registered_at=registered_at,
                heartbeat_at=registered_at,
                expires_at=registered_at + heartbeat_ttl,
            )
            self._execution_workers[worker_id] = registration
            return registration.model_copy(deep=True)

    async def heartbeat_execution_worker(
        self,
        registration: ExecutionWorkerRegistration,
        *,
        heartbeat_ttl: timedelta,
        now: datetime | None = None,
    ) -> ExecutionWorkerRegistration:
        _validate_positive_duration(heartbeat_ttl, field="heartbeat_ttl")
        heartbeat_at = _coerce_utc_datetime(now)
        async with self._lock:
            current = self._active_worker_registration(registration)
            if current.state != ExecutionWorkerRegistrationState.READY:
                raise ExecutionWorkerRegistrationLostError(
                    context={
                        "worker_id": registration.worker_id,
                        "reason": "worker_registration_not_ready",
                    }
                )
            updated = _replace_worker_registration(
                current,
                heartbeat_at=heartbeat_at,
                expires_at=heartbeat_at + heartbeat_ttl,
            )
            self._execution_workers[updated.worker_id] = updated
            return updated.model_copy(deep=True)

    async def drain_execution_worker(
        self,
        registration: ExecutionWorkerRegistration,
        *,
        now: datetime | None = None,
    ) -> ExecutionWorkerRegistration:
        _coerce_utc_datetime(now)
        async with self._lock:
            current = self._active_worker_registration(registration)
            updated = _replace_worker_registration(
                current,
                state=ExecutionWorkerRegistrationState.DRAINING,
            )
            self._execution_workers[updated.worker_id] = updated
            return updated.model_copy(deep=True)

    async def remove_execution_worker(
        self,
        registration: ExecutionWorkerRegistration,
    ) -> None:
        async with self._lock:
            current = self._active_worker_registration(registration)
            self._execution_workers.pop(current.worker_id)

    async def is_execution_worker_ready(self, worker_id: str) -> bool:
        _validate_execution_identifier(worker_id, field="worker_id")
        checked_at = datetime.now(UTC)
        async with self._lock:
            registration = self._execution_workers.get(worker_id)
            return registration is not None and _worker_is_ready(
                registration,
                now=checked_at,
            )

    async def get_execution_queue_health(
        self,
        *,
        stuck_after: timedelta,
        now: datetime | None = None,
    ) -> ExecutionQueueHealth:
        _validate_positive_duration(stuck_after, field="stuck_after")
        checked_at = _coerce_utc_datetime(now)
        async with self._lock:
            active_jobs = tuple(
                job
                for job in self._execution_jobs.values()
                if job.state in {ExecutionJobState.QUEUED, ExecutionJobState.LEASED}
            )
            queued_jobs = tuple(job for job in active_jobs if job.state == ExecutionJobState.QUEUED)
            leased_jobs = tuple(job for job in active_jobs if job.state == ExecutionJobState.LEASED)
            ready_worker_count = sum(
                _worker_is_ready(registration, now=checked_at)
                for registration in self._execution_workers.values()
            )
            oldest_queued_at = min(job.created_at for job in queued_jobs) if queued_jobs else None
            return ExecutionQueueHealth(
                checked_at=checked_at,
                accepting_new_jobs=ready_worker_count > 0,
                ready_worker_count=ready_worker_count,
                queued_job_count=len(queued_jobs),
                leased_job_count=len(leased_jobs),
                expired_lease_count=sum(
                    job.lease_expires_at is not None and job.lease_expires_at <= checked_at
                    for job in leased_jobs
                ),
                recovery_attempt_count=sum(max(job.claim_count - 1, 0) for job in active_jobs),
                stuck_job_count=sum(
                    (
                        job.state == ExecutionJobState.QUEUED
                        and job.available_at <= checked_at - stuck_after
                    )
                    or (
                        job.state == ExecutionJobState.LEASED
                        and job.lease_expires_at is not None
                        and job.lease_expires_at <= checked_at - stuck_after
                    )
                    for job in active_jobs
                ),
                oldest_queued_at=oldest_queued_at,
                oldest_queued_age_seconds=(
                    max((checked_at - oldest_queued_at).total_seconds(), 0.0)
                    if oldest_queued_at is not None
                    else None
                ),
                stuck_after_seconds=stuck_after.total_seconds(),
            )

    async def claim_execution_job(
        self,
        *,
        registration: ExecutionWorkerRegistration,
        lease_duration: timedelta,
        now: datetime | None = None,
    ) -> ExecutionLease | None:
        _validate_lease_duration(lease_duration)
        claimed_at = _coerce_utc_datetime(now)
        async with self._lock:
            current_registration = self._active_worker_registration(registration)
            if not _worker_is_ready(current_registration, now=claimed_at):
                raise ExecutionWorkerRegistrationLostError(
                    context={
                        "worker_id": registration.worker_id,
                        "reason": "worker_registration_not_ready",
                    }
                )
            eligible = [
                job
                for job in self._execution_jobs.values()
                if (job.state == ExecutionJobState.QUEUED and job.available_at <= claimed_at)
                or (
                    job.state == ExecutionJobState.LEASED
                    and job.lease_expires_at is not None
                    and job.lease_expires_at <= claimed_at
                )
            ]
            if not eligible:
                return None
            current = min(
                eligible,
                key=lambda job: (job.available_at, job.created_at, job.job_id),
            )
            generation = current.lease_generation + 1
            updated = _replace_execution_job(
                current,
                state=ExecutionJobState.LEASED,
                claim_count=current.claim_count + 1,
                lease_generation=generation,
                lease_token=uuid4().hex,
                lease_owner=registration.worker_id,
                lease_expires_at=claimed_at + lease_duration,
                heartbeat_at=claimed_at,
                updated_at=claimed_at,
            )
            self._execution_jobs[updated.job_id] = updated
            return _execution_lease_from_job(updated)

    async def heartbeat_execution_job(
        self,
        lease: ExecutionLease,
        *,
        lease_duration: timedelta,
        checkpoint_cursor: str | None = None,
        now: datetime | None = None,
    ) -> ExecutionLease:
        _validate_lease_duration(lease_duration)
        heartbeat_at = _coerce_utc_datetime(now)
        async with self._lock:
            current = self._active_execution_lease(lease, now=heartbeat_at)
            updated = _replace_execution_job(
                current,
                checkpoint_cursor=(
                    checkpoint_cursor
                    if checkpoint_cursor is not None
                    else current.checkpoint_cursor
                ),
                lease_expires_at=heartbeat_at + lease_duration,
                heartbeat_at=heartbeat_at,
                updated_at=heartbeat_at,
            )
            self._execution_jobs[updated.job_id] = updated
            return _execution_lease_from_job(updated)

    async def complete_execution_job(
        self,
        lease: ExecutionLease,
        *,
        checkpoint_cursor: str | None = None,
        now: datetime | None = None,
    ) -> RunExecutionJob:
        completed_at = _coerce_utc_datetime(now)
        async with self._lock:
            current = self._active_execution_lease(lease, now=completed_at)
            updated = _replace_execution_job(
                current,
                state=ExecutionJobState.COMPLETED,
                payload=None,
                checkpoint_cursor=(
                    checkpoint_cursor
                    if checkpoint_cursor is not None
                    else current.checkpoint_cursor
                ),
                lease_token=None,
                lease_owner=None,
                lease_expires_at=None,
                heartbeat_at=None,
                completed_at=completed_at,
                failure_code=None,
                updated_at=completed_at,
            )
            self._execution_jobs[updated.job_id] = updated
            return updated.model_copy(deep=True)

    async def fail_execution_job(
        self,
        lease: ExecutionLease,
        *,
        failure_code: str,
        checkpoint_cursor: str | None = None,
        now: datetime | None = None,
    ) -> RunExecutionJob:
        completed_at = _coerce_utc_datetime(now)
        async with self._lock:
            current = self._active_execution_lease(lease, now=completed_at)
            updated = _replace_execution_job(
                current,
                state=ExecutionJobState.FAILED,
                payload=None,
                checkpoint_cursor=(
                    checkpoint_cursor
                    if checkpoint_cursor is not None
                    else current.checkpoint_cursor
                ),
                lease_token=None,
                lease_owner=None,
                lease_expires_at=None,
                heartbeat_at=None,
                completed_at=completed_at,
                failure_code=failure_code,
                updated_at=completed_at,
            )
            self._execution_jobs[updated.job_id] = updated
            return updated.model_copy(deep=True)

    async def release_execution_job(
        self,
        lease: ExecutionLease,
        *,
        available_at: datetime | None = None,
        checkpoint_cursor: str | None = None,
        now: datetime | None = None,
    ) -> RunExecutionJob:
        released_at = _coerce_utc_datetime(now)
        next_available_at = (
            released_at
            if available_at is None
            else max(released_at, _coerce_utc_datetime(available_at))
        )
        async with self._lock:
            current = self._active_execution_lease(lease, now=released_at)
            updated = _replace_execution_job(
                current,
                state=ExecutionJobState.QUEUED,
                checkpoint_cursor=(
                    checkpoint_cursor
                    if checkpoint_cursor is not None
                    else current.checkpoint_cursor
                ),
                available_at=next_available_at,
                lease_token=None,
                lease_owner=None,
                lease_expires_at=None,
                heartbeat_at=None,
                updated_at=released_at,
            )
            self._execution_jobs[updated.job_id] = updated
            return updated.model_copy(deep=True)

    async def get_execution_job(
        self,
        job_id: str,
        *,
        owner_id: str,
    ) -> RunExecutionJob:
        async with self._lock:
            job = self._execution_jobs.get(job_id)
            if job is None or job.owner_id != owner_id:
                raise ExecutionJobNotFoundError(context={"job_id": job_id})
            return job.model_copy(deep=True)

    async def assert_execution_lease(
        self,
        lease: ExecutionLease,
        *,
        now: datetime | None = None,
    ) -> RunExecutionJob:
        checked_at = _coerce_utc_datetime(now)
        async with self._lock:
            current = self._active_execution_lease(lease, now=checked_at)
            return current.model_copy(deep=True)

    @asynccontextmanager
    async def guard_execution_lease(
        self,
        lease: ExecutionLease,
    ) -> AsyncIterator[None]:
        async with self._lock:
            self._active_execution_lease(lease, now=datetime.now(UTC))
            yield

    async def reserve_initial_run(
        self,
        correlation: RunExecutionCorrelation,
        *,
        owner_id: str,
        capacity: int,
    ) -> RunEventRecord:
        _validate_capacity(capacity)
        async with self._lock:
            thread = self._owned_thread(correlation.thread_id, owner_id)
            self._require_thread_accepts_execution(thread.thread_id)
            if correlation.attempt != 1:
                raise AgentPolicyViolationError(
                    context={"reason": "initial_run_attempt_must_be_one"}
                )
            if correlation.run_id in self._runs or correlation.run_id in self._run_correlations:
                raise AgentPolicyViolationError(context={"reason": "run_id_conflict"})
            if thread.active_run_id is not None:
                raise ThreadBusyError(context={"thread_id": correlation.thread_id})
            if thread.run_count >= capacity:
                raise AgentCapacityExceededError(
                    context={"resource": "runs_per_thread", "limit": capacity}
                )
            event = self._new_event(
                correlation,
                phase=RunExecutionPhase.RUNNING,
                event_type=RunEventType.RUN_STARTED,
                event_key=_run_event_key(RunEventType.RUN_STARTED, correlation),
            )
            self._run_correlations[correlation.run_id] = correlation.model_copy(deep=True)
            self._run_phases[correlation.run_id] = RunExecutionPhase.RUNNING
            self._run_events[correlation.run_id] = [event]
            self._threads[thread.thread_id] = thread.model_copy(
                update={
                    "active_run_id": correlation.run_id,
                    "run_count": thread.run_count + 1,
                }
            )
            return event.model_copy(deep=True)

    async def reserve_resume_attempt(
        self,
        run_id: str,
        *,
        request_id: str,
        owner_id: str,
    ) -> RunEventRecord:
        async with self._lock:
            current = self._owned_ledger_correlation(run_id, owner_id)
            if self._run_phases.get(run_id) != RunExecutionPhase.INTERRUPTED:
                raise AgentPolicyViolationError(context={"reason": "run_not_interrupted"})
            thread = self._owned_thread(current.thread_id, owner_id)
            self._require_thread_accepts_execution(thread.thread_id)
            if thread.active_run_id != run_id:
                raise AgentPolicyViolationError(context={"reason": "run_not_active"})
            correlation = RunExecutionCorrelation(
                thread_id=current.thread_id,
                run_id=run_id,
                request_id=request_id,
                attempt=current.attempt + 1,
            )
            event_key = _run_event_key(RunEventType.RUN_RESUME_STARTED, correlation)
            event = self._new_event(
                correlation,
                phase=RunExecutionPhase.RUNNING,
                event_type=RunEventType.RUN_RESUME_STARTED,
                event_key=event_key,
            )
            self._run_correlations[run_id] = correlation
            self._run_phases[run_id] = RunExecutionPhase.RUNNING
            self._run_events[run_id].append(event)
            return event.model_copy(deep=True)

    async def finalize_run(
        self,
        run: ConversationRun,
        *,
        correlation: RunExecutionCorrelation,
        owner_id: str,
    ) -> ConversationRun:
        async with self._lock:
            return self._finalize_run_locked(run, correlation=correlation, owner_id=owner_id)

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
    ) -> ConversationRun:
        _validate_execution_terminal(job_state, failure_code=failure_code)
        _validate_optional_execution_identifier(checkpoint_cursor, field="checkpoint_cursor")
        async with self._lock:
            completed_at = datetime.now(UTC)
            current = self._active_execution_lease(execution_lease, now=completed_at)
            self._require_ledger_lease(
                execution_lease,
                correlation=correlation,
                owner_id=owner_id,
            )
            terminal_job = _replace_execution_job(
                current,
                state=job_state,
                payload=None,
                checkpoint_cursor=(
                    checkpoint_cursor
                    if checkpoint_cursor is not None
                    else current.checkpoint_cursor
                ),
                lease_token=None,
                lease_owner=None,
                lease_expires_at=None,
                heartbeat_at=None,
                completed_at=completed_at,
                failure_code=failure_code,
                updated_at=completed_at,
            )
            stored = self._finalize_run_locked(
                run,
                correlation=correlation,
                owner_id=owner_id,
            )
            self._execution_jobs[current.job_id] = terminal_job
            return stored

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
    ) -> ToolExecutionHandle:
        async with self._lock:
            if execution_lease is not None:
                self._active_execution_lease(execution_lease, now=datetime.now(UTC))
                self._require_ledger_lease(
                    execution_lease,
                    correlation=correlation,
                    owner_id=owner_id,
                )
            self._require_active_correlation(correlation, owner_id)
            existing = self._open_tool_executions.get(execution_id)
            if existing is not None:
                expected = (
                    existing.logical_call_id == logical_call_id
                    and existing.attempt == attempt
                    and existing.correlation == correlation
                    and existing.tool == tool
                )
                if not expected:
                    raise AgentPolicyViolationError(
                        context={"reason": "tool_execution_id_conflict"}
                    )
                return existing.model_copy(deep=True)
            if execution_id in self._tool_executions:
                raise AgentPolicyViolationError(
                    context={"reason": "tool_execution_already_terminal"}
                )
            attempt_key = (correlation.run_id, logical_call_id, attempt)
            if attempt_key in self._tool_attempt_keys:
                raise AgentPolicyViolationError(context={"reason": "tool_attempt_conflict"})
            open_for_run = sum(
                handle.correlation.run_id == correlation.run_id
                for handle in self._open_tool_executions.values()
            )
            if open_for_run >= 32:
                raise AgentPolicyViolationError(context={"reason": "open_tool_execution_limit"})
            started_at = datetime.now(UTC)
            event = self._new_event(
                correlation,
                phase=RunExecutionPhase.RUNNING,
                event_type=RunEventType.TOOL_STARTED,
                event_key=f"tool:{execution_id}:started",
                occurred_at=started_at,
                tool_execution_id=execution_id,
                logical_call_id=logical_call_id,
                tool_attempt=attempt,
                tool=tool,
                tool_transition=ToolTransition.STARTED,
            )
            handle = ToolExecutionHandle.from_started_event(event)
            self._run_events[correlation.run_id].append(event)
            self._open_tool_executions[execution_id] = handle
            self._tool_attempt_keys.add(attempt_key)
            return handle.model_copy(deep=True)

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
    ) -> ToolExecutionRecord:
        if transition == ToolTransition.STARTED:
            raise AgentPolicyViolationError(context={"reason": "tool_terminal_transition_required"})
        async with self._lock:
            if execution_lease is not None:
                self._active_execution_lease(execution_lease, now=datetime.now(UTC))
                self._require_ledger_lease(
                    execution_lease,
                    correlation=handle.correlation,
                    owner_id=owner_id,
                )
            self._require_active_correlation(handle.correlation, owner_id)
            existing_record = self._tool_executions.get(handle.execution_id)
            if existing_record is not None:
                if (
                    existing_record.handle == handle
                    and existing_record.transition == transition
                    and existing_record.duration_ms == duration_ms
                    and existing_record.error_code == error_code
                    and existing_record.summary == summary
                ):
                    return existing_record.model_copy(deep=True)
                raise AgentPolicyViolationError(context={"reason": "tool_terminal_conflict"})
            stored_handle = self._open_tool_executions.get(handle.execution_id)
            if stored_handle is None or stored_handle != handle:
                raise AgentPolicyViolationError(context={"reason": "tool_start_not_found"})
            completed_at = datetime.now(UTC)
            event_type = _tool_terminal_event_type(transition)
            event = self._new_event(
                handle.correlation,
                phase=RunExecutionPhase.RUNNING,
                event_type=event_type,
                event_key=f"tool:{handle.execution_id}:{transition.value}",
                occurred_at=completed_at,
                tool_execution_id=handle.execution_id,
                logical_call_id=handle.logical_call_id,
                tool_attempt=handle.attempt,
                tool=handle.tool,
                tool_transition=transition,
                duration_ms=duration_ms,
                error_code=error_code,
                tool_summary=summary,
            )
            record = ToolExecutionRecord(
                handle=handle,
                transition=transition,
                terminal_event_sequence=event.sequence,
                completed_at=completed_at,
                duration_ms=duration_ms,
                error_code=error_code,
                summary=summary,
            )
            self._run_events[handle.correlation.run_id].append(event)
            self._tool_executions[handle.execution_id] = record
            self._open_tool_executions.pop(handle.execution_id)
            return record.model_copy(deep=True)

    async def get_run_events(
        self,
        run_id: str,
        *,
        owner_id: str,
    ) -> tuple[RunEventRecord, ...]:
        async with self._lock:
            self._owned_ledger_run(run_id, owner_id)
            return tuple(event.model_copy(deep=True) for event in self._run_events.get(run_id, ()))

    async def get_tool_executions(
        self,
        run_id: str,
        *,
        owner_id: str,
    ) -> tuple[ToolExecutionRecord, ...]:
        async with self._lock:
            self._owned_ledger_run(run_id, owner_id)
            records = sorted(
                (
                    record
                    for record in self._tool_executions.values()
                    if record.handle.correlation.run_id == run_id
                ),
                key=lambda record: record.terminal_event_sequence,
            )
            return tuple(record.model_copy(deep=True) for record in records)

    async def find_orphaned_runs(
        self,
        *,
        owner_id: str,
        stale_before: datetime,
    ) -> tuple[OrphanedRunView, ...]:
        if stale_before.tzinfo is None or stale_before.utcoffset() is None:
            raise ValueError("stale_before must be timezone-aware")
        async with self._lock:
            orphans: list[OrphanedRunView] = []
            for run_id, phase in self._run_phases.items():
                if phase != RunExecutionPhase.RUNNING:
                    continue
                correlation = self._run_correlations[run_id]
                thread = self._threads.get(correlation.thread_id)
                if thread is None or thread.owner_id != owner_id:
                    continue
                events = self._run_events[run_id]
                last = events[-1]
                if last.occurred_at > stale_before:
                    continue
                open_ids = tuple(
                    sorted(
                        execution_id
                        for execution_id, handle in self._open_tool_executions.items()
                        if handle.correlation.run_id == run_id
                    )
                )
                reasons = [OrphanReason.RUN_STALE]
                if open_ids:
                    reasons.append(OrphanReason.TOOL_EXECUTION_STALE)
                orphans.append(
                    OrphanedRunView(
                        correlation=correlation,
                        reasons=tuple(reasons),
                        last_sequence=last.sequence,
                        last_event_type=last.event_type,
                        last_activity_at=last.occurred_at,
                        open_tool_execution_ids=open_ids,
                    )
                )
            orphans.sort(key=lambda item: (item.last_activity_at, item.correlation.run_id))
            return tuple(item.model_copy(deep=True) for item in orphans)

    async def get_run(
        self,
        run_id: str,
        *,
        owner_id: str,
    ) -> ConversationRun:
        async with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                raise RunNotFoundError(context={"run_id": run_id})
            try:
                self._owned_thread(run.thread_id, owner_id)
            except ThreadNotFoundError as exc:
                raise RunNotFoundError(context={"run_id": run_id}) from exc
            return run.model_copy(deep=True)

    async def delete_thread(
        self,
        thread_id: str,
        *,
        owner_id: str,
    ) -> None:
        async with self._lock:
            self._owned_thread(thread_id, owner_id)
            self._require_no_active_execution_jobs(thread_id)
            if thread_id in self._thread_deletions:
                raise ThreadBusyError(
                    context={"thread_id": thread_id, "reason": "thread_deletion_prepared"}
                )
            self._delete_thread_locked(thread_id)

    async def prepare_thread_deletion(
        self,
        thread_id: str,
        *,
        owner_id: str,
    ) -> ThreadDeletionReservation:
        async with self._lock:
            self._owned_thread(thread_id, owner_id)
            existing = self._thread_deletions.get(thread_id)
            if existing is not None:
                return existing.model_copy(update={"replayed": True})
            self._require_no_active_execution_jobs(thread_id)
            reservation = ThreadDeletionReservation(
                thread_id=thread_id,
                owner_id=owner_id,
                token=uuid4().hex,
                requested_at=datetime.now(UTC),
            )
            self._thread_deletions[thread_id] = reservation
            return reservation.model_copy(deep=True)

    async def commit_thread_deletion(
        self,
        reservation: ThreadDeletionReservation,
    ) -> None:
        async with self._lock:
            self._owned_thread(reservation.thread_id, reservation.owner_id)
            stored = self._thread_deletions.get(reservation.thread_id)
            if stored is None or stored.token != reservation.token:
                raise AgentPolicyViolationError(
                    context={"reason": "thread_deletion_fence_mismatch"}
                )
            self._require_no_active_execution_jobs(reservation.thread_id)
            self._delete_thread_locked(reservation.thread_id)

    async def abort_thread_deletion(
        self,
        reservation: ThreadDeletionReservation,
    ) -> None:
        async with self._lock:
            self._owned_thread(reservation.thread_id, reservation.owner_id)
            stored = self._thread_deletions.get(reservation.thread_id)
            if stored is None or stored.token != reservation.token:
                raise AgentPolicyViolationError(
                    context={"reason": "thread_deletion_fence_mismatch"}
                )
            self._thread_deletions.pop(reservation.thread_id, None)

    async def is_ready(self) -> bool:
        return True

    def _replayed_execution(
        self,
        *,
        owner_id: str,
        idempotency_digest: str,
        request_fingerprint: str,
        expected_operation: ExecutionOperation,
        target_kind: Literal["thread", "run"],
        target_id: str,
    ) -> ExecutionReservation | None:
        job_id = self._execution_idempotency.get((owner_id, idempotency_digest))
        if job_id is None:
            return None
        job = self._execution_jobs.get(job_id)
        if job is None:
            raise AgentPolicyViolationError(
                context={"reason": "execution_idempotency_index_invalid"}
            )
        stored_target_id = (
            job.correlation.thread_id if target_kind == "thread" else job.correlation.run_id
        )
        if (
            job.request_fingerprint != request_fingerprint
            or job.operation != expected_operation
            or stored_target_id != target_id
        ):
            raise IdempotencyKeyReuseError(
                context={
                    "job_id": job.job_id,
                    "reason": "request_fingerprint_mismatch",
                }
            )
        return ExecutionReservation(job=job.model_copy(deep=True), replayed=True)

    def _active_worker_registration(
        self,
        registration: ExecutionWorkerRegistration,
    ) -> ExecutionWorkerRegistration:
        current = self._execution_workers.get(registration.worker_id)
        if (
            current is None
            or current.token != registration.token
            or current.generation != registration.generation
        ):
            raise ExecutionWorkerRegistrationLostError(
                context={
                    "worker_id": registration.worker_id,
                    "reason": "worker_registration_fence_mismatch",
                }
            )
        return current

    def _require_available_worker(self, now: datetime) -> None:
        if not any(
            _worker_is_ready(registration, now=now)
            for registration in self._execution_workers.values()
        ):
            raise ExecutionWorkerUnavailableError()

    def _active_execution_lease(
        self,
        lease: ExecutionLease,
        *,
        now: datetime,
    ) -> RunExecutionJob:
        job = self._execution_jobs.get(lease.job_id)
        matches = (
            job is not None
            and job.owner_id == lease.owner_id
            and job.correlation == lease.correlation
            and job.operation == lease.operation
            and job.payload_schema_version == lease.payload_schema_version
            and job.payload == lease.payload
            and job.state == ExecutionJobState.LEASED
            and job.lease_owner == lease.worker_id
            and job.lease_token == lease.token
            and job.lease_generation == lease.generation
        )
        if not matches or job is None:
            raise ExecutionLeaseLostError(
                context={"job_id": lease.job_id, "reason": "lease_fence_mismatch"}
            )
        if job.lease_expires_at is None or job.lease_expires_at <= now:
            raise ExecutionLeaseLostError(
                context={"job_id": lease.job_id, "reason": "lease_expired"}
            )
        return job

    @staticmethod
    def _require_ledger_lease(
        lease: ExecutionLease,
        *,
        correlation: RunExecutionCorrelation,
        owner_id: str,
    ) -> None:
        if lease.owner_id != owner_id or lease.correlation != correlation:
            raise ExecutionLeaseLostError(
                context={"job_id": lease.job_id, "reason": "ledger_lease_scope_mismatch"}
            )

    def _finalize_run_locked(
        self,
        run: ConversationRun,
        *,
        correlation: RunExecutionCorrelation,
        owner_id: str,
    ) -> ConversationRun:
        self._require_active_correlation(correlation, owner_id)
        if self._run_phases.get(run.run_id) != RunExecutionPhase.RUNNING:
            raise AgentPolicyViolationError(context={"reason": "run_not_running"})
        if run.run_id != correlation.run_id or run.thread_id != correlation.thread_id:
            raise AgentPolicyViolationError(context={"reason": "run_thread_change_forbidden"})
        if any(
            handle.correlation.run_id == run.run_id
            for handle in self._open_tool_executions.values()
        ):
            raise AgentPolicyViolationError(context={"reason": "open_tool_execution_exists"})
        phase, event_type = _terminal_run_state(run)
        previous = self._runs.get(run.run_id)
        selected_usage = run.usage
        if previous is not None:
            try:
                selected_usage = reconcile_run_usage(previous.usage, run.usage)
            except RunUsageRevisionConflictError as exc:
                raise AgentPolicyViolationError(
                    context={"reason": "run_usage_revision_conflict"}
                ) from exc
        event = self._new_event(
            correlation,
            phase=phase,
            event_type=event_type,
            event_key=_run_event_key(event_type, correlation),
        )
        stored = run.model_copy(update={"usage": selected_usage}, deep=True)
        self._runs[run.run_id] = stored
        self._run_phases[run.run_id] = phase
        self._run_events[run.run_id].append(event)
        thread = self._owned_thread(run.thread_id, owner_id)
        self._threads[thread.thread_id] = thread.model_copy(
            update={"active_run_id": run.run_id if phase == RunExecutionPhase.INTERRUPTED else None}
        )
        return stored.model_copy(deep=True)

    def _require_thread_accepts_execution(self, thread_id: str) -> None:
        if thread_id in self._thread_deletions:
            raise ThreadBusyError(
                context={"thread_id": thread_id, "reason": "thread_deletion_prepared"}
            )

    def _require_no_active_execution_jobs(self, thread_id: str) -> None:
        if any(
            job.correlation.thread_id == thread_id
            and job.state in {ExecutionJobState.QUEUED, ExecutionJobState.LEASED}
            for job in self._execution_jobs.values()
        ):
            raise ThreadBusyError(
                context={"thread_id": thread_id, "reason": "execution_job_active"}
            )

    def _delete_thread_locked(self, thread_id: str) -> None:
        ledger_run_ids = {
            run_id
            for run_id, correlation in self._run_correlations.items()
            if correlation.thread_id == thread_id
        }
        self._threads.pop(thread_id)
        self._thread_deletions.pop(thread_id, None)
        self._runs = {
            run_id: run for run_id, run in self._runs.items() if run.thread_id != thread_id
        }
        for run_id in ledger_run_ids:
            self._run_correlations.pop(run_id, None)
            self._run_phases.pop(run_id, None)
            self._run_events.pop(run_id, None)
        self._open_tool_executions = {
            execution_id: handle
            for execution_id, handle in self._open_tool_executions.items()
            if handle.correlation.run_id not in ledger_run_ids
        }
        self._tool_executions = {
            execution_id: record
            for execution_id, record in self._tool_executions.items()
            if record.handle.correlation.run_id not in ledger_run_ids
        }
        self._tool_attempt_keys = {
            key for key in self._tool_attempt_keys if key[0] not in ledger_run_ids
        }
        execution_job_ids = {
            job_id
            for job_id, job in self._execution_jobs.items()
            if job.correlation.thread_id == thread_id
        }
        for job_id in execution_job_ids:
            self._execution_jobs.pop(job_id, None)
        self._execution_idempotency = {
            key: job_id
            for key, job_id in self._execution_idempotency.items()
            if job_id not in execution_job_ids
        }

    def _owned_thread(self, thread_id: str, owner_id: str) -> ConversationThread:
        thread = self._threads.get(thread_id)
        if thread is None or thread.owner_id != owner_id:
            raise ThreadNotFoundError(context={"thread_id": thread_id})
        return thread

    def _owned_ledger_correlation(
        self,
        run_id: str,
        owner_id: str,
    ) -> RunExecutionCorrelation:
        correlation = self._run_correlations.get(run_id)
        if correlation is None:
            raise RunNotFoundError(context={"run_id": run_id})
        try:
            self._owned_thread(correlation.thread_id, owner_id)
        except ThreadNotFoundError as exc:
            raise RunNotFoundError(context={"run_id": run_id}) from exc
        return correlation

    def _owned_ledger_run(self, run_id: str, owner_id: str) -> None:
        correlation = self._run_correlations.get(run_id)
        if correlation is not None:
            self._owned_ledger_correlation(run_id, owner_id)
            return
        run = self._runs.get(run_id)
        if run is None:
            raise RunNotFoundError(context={"run_id": run_id})
        try:
            self._owned_thread(run.thread_id, owner_id)
        except ThreadNotFoundError as exc:
            raise RunNotFoundError(context={"run_id": run_id}) from exc

    def _require_active_correlation(
        self,
        correlation: RunExecutionCorrelation,
        owner_id: str,
    ) -> None:
        current = self._owned_ledger_correlation(correlation.run_id, owner_id)
        if current != correlation or current.thread_id != correlation.thread_id:
            raise AgentPolicyViolationError(context={"reason": "run_correlation_stale"})
        if self._run_phases.get(correlation.run_id) != RunExecutionPhase.RUNNING:
            raise AgentPolicyViolationError(context={"reason": "run_not_running"})

    def _event_by_key(self, run_id: str, event_key: str) -> RunEventRecord | None:
        return next(
            (event for event in self._run_events.get(run_id, ()) if event.event_key == event_key),
            None,
        )

    def _new_event(
        self,
        correlation: RunExecutionCorrelation,
        *,
        phase: RunExecutionPhase,
        event_type: RunEventType,
        event_key: str,
        occurred_at: datetime | None = None,
        tool_execution_id: str | None = None,
        logical_call_id: str | None = None,
        tool_attempt: int | None = None,
        tool: ToolCapability | None = None,
        tool_transition: ToolTransition | None = None,
        duration_ms: int | None = None,
        error_code: ToolFailureCode | None = None,
        tool_summary: ToolExecutionSummary | None = None,
    ) -> RunEventRecord:
        if self._event_by_key(correlation.run_id, event_key) is not None:
            raise AgentPolicyViolationError(context={"reason": "run_event_key_conflict"})
        events = self._run_events.get(correlation.run_id, ())
        sequence = len(events) + 1
        if events and events[-1].sequence != sequence - 1:
            raise AgentPolicyViolationError(context={"reason": "run_event_sequence_invalid"})
        return RunEventRecord(
            event_id=uuid4().hex,
            event_key=event_key,
            correlation=correlation,
            sequence=sequence,
            phase=phase,
            event_type=event_type,
            occurred_at=occurred_at or datetime.now(UTC),
            tool_execution_id=tool_execution_id,
            logical_call_id=logical_call_id,
            tool_attempt=tool_attempt,
            tool=tool,
            tool_transition=tool_transition,
            duration_ms=duration_ms,
            error_code=error_code,
            tool_summary=tool_summary,
        )


def _validate_capacity(capacity: int) -> None:
    if capacity < 1:
        raise ValueError("capacity must be at least 1")


def _validate_optional_capacity(capacity: int | None) -> None:
    if capacity is not None:
        _validate_capacity(capacity)


def _validate_digest(value: str, *, name: str) -> None:
    if re.fullmatch(r"[a-f0-9]{64}", value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")


def _validate_reservation_lookup_digests(values: tuple[str, ...]) -> tuple[str, ...]:
    if not 1 <= len(values) <= 2:
        raise ValueError("idempotency_digests must contain one or two values")
    if len(set(values)) != len(values):
        raise ValueError("idempotency_digests must be distinct")
    for value in values:
        _validate_digest(value, name="idempotency_digest")
    return values


def _validate_lease_duration(value: timedelta) -> None:
    _validate_positive_duration(value, field="lease_duration")


def _validate_positive_duration(value: timedelta, *, field: str) -> None:
    if value <= timedelta(0):
        raise ValueError(f"{field} must be positive")


def _validate_execution_identifier(value: str, *, field: str) -> None:
    if re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", value) is None:
        raise ValueError(f"{field} must be a safe execution identifier")


def _validate_optional_execution_identifier(value: str | None, *, field: str) -> None:
    if value is not None:
        _validate_execution_identifier(value, field=field)


def _validate_execution_terminal(
    state: Literal[ExecutionJobState.COMPLETED, ExecutionJobState.FAILED],
    *,
    failure_code: str | None,
) -> None:
    if state not in {ExecutionJobState.COMPLETED, ExecutionJobState.FAILED}:
        raise ValueError("execution terminal state must be completed or failed")
    if state == ExecutionJobState.COMPLETED and failure_code is not None:
        raise ValueError("completed execution jobs cannot contain a failure code")
    if state == ExecutionJobState.FAILED and (
        failure_code is None or re.fullmatch(r"[A-Z][A-Z0-9_]{0,99}", failure_code) is None
    ):
        raise ValueError("failed execution jobs require a stable uppercase failure code")


def _coerce_utc_datetime(value: datetime | None) -> datetime:
    selected = value or datetime.now(UTC)
    if selected.tzinfo is None or selected.utcoffset() is None:
        raise ValueError("execution timestamps must be timezone-aware")
    return selected


def _worker_is_ready(
    registration: ExecutionWorkerRegistration,
    *,
    now: datetime,
) -> bool:
    return (
        registration.state == ExecutionWorkerRegistrationState.READY
        and registration.expires_at > now
    )


def _new_execution_job(
    *,
    owner_id: str,
    correlation: RunExecutionCorrelation,
    payload: ExecutionPayload,
    idempotency_digest: str,
    request_fingerprint: str,
    now: datetime,
) -> RunExecutionJob:
    return RunExecutionJob(
        job_id=uuid4().hex,
        owner_id=owner_id,
        correlation=correlation,
        operation=payload.operation,
        payload=payload,
        idempotency_digest=idempotency_digest,
        request_fingerprint=request_fingerprint,
        state=ExecutionJobState.QUEUED,
        available_at=now,
        created_at=now,
        updated_at=now,
    )


def _replace_execution_job(
    job: RunExecutionJob,
    **updates: object,
) -> RunExecutionJob:
    payload = job.model_dump(mode="python")
    payload.update(updates)
    return RunExecutionJob.model_validate(payload)


def _replace_worker_registration(
    registration: ExecutionWorkerRegistration,
    **updates: object,
) -> ExecutionWorkerRegistration:
    payload = registration.model_dump(mode="python")
    payload.update(updates)
    return ExecutionWorkerRegistration.model_validate(payload)


def _execution_lease_from_job(job: RunExecutionJob) -> ExecutionLease:
    if (
        job.state != ExecutionJobState.LEASED
        or job.payload is None
        or job.lease_owner is None
        or job.lease_token is None
        or job.lease_expires_at is None
        or job.heartbeat_at is None
    ):
        raise AgentPolicyViolationError(context={"reason": "execution_job_not_leased"})
    return ExecutionLease(
        job_id=job.job_id,
        owner_id=job.owner_id,
        correlation=job.correlation,
        operation=job.operation,
        payload_schema_version=job.payload_schema_version,
        payload=job.payload,
        worker_id=job.lease_owner,
        token=job.lease_token,
        generation=job.lease_generation,
        heartbeat_at=job.heartbeat_at,
        expires_at=job.lease_expires_at,
        checkpoint_cursor=job.checkpoint_cursor,
    )


def _resume_operation_matches(
    run: ConversationRun,
    payload: ResumeExecutionPayload,
) -> bool:
    if isinstance(payload, ResumeCommandExecutionPayload):
        return str(run.resume_mode) == "command"
    return str(run.resume_mode) == "new_turn"


def _is_resumable(run: ConversationRun) -> bool:
    return run.status == "interrupted" and run.resume_mode != "none"


def _terminal_run_state(
    run: ConversationRun,
) -> tuple[RunExecutionPhase, RunEventType]:
    status = str(run.status)
    resume_mode = str(run.resume_mode)
    if (status == "interrupted" and resume_mode == "none") or (
        status in {"completed", "failed"} and resume_mode != "none"
    ):
        raise AgentPolicyViolationError(context={"reason": "run_status_resume_mode_invalid"})
    mapping = {
        "interrupted": (
            RunExecutionPhase.INTERRUPTED,
            RunEventType.RUN_INTERRUPTED,
        ),
        "completed": (
            RunExecutionPhase.COMPLETED,
            RunEventType.RUN_COMPLETED,
        ),
        "failed": (RunExecutionPhase.FAILED, RunEventType.RUN_FAILED),
    }
    selected = mapping.get(status)
    if selected is None:
        raise AgentPolicyViolationError(context={"reason": "run_terminal_status_invalid"})
    return selected


def _tool_terminal_event_type(transition: ToolTransition) -> RunEventType:
    mapping = {
        ToolTransition.COMPLETED: RunEventType.TOOL_COMPLETED,
        ToolTransition.FAILED: RunEventType.TOOL_FAILED,
        ToolTransition.TIMEOUT: RunEventType.TOOL_TIMEOUT,
    }
    selected = mapping.get(transition)
    if selected is None:
        raise AgentPolicyViolationError(context={"reason": "tool_terminal_transition_required"})
    return selected


def _run_event_key(
    event_type: RunEventType,
    correlation: RunExecutionCorrelation,
) -> str:
    return f"run:{correlation.attempt}:{event_type.value}"
