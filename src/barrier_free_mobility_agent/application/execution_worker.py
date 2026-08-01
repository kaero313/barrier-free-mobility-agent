from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol

from barrier_free_mobility_agent.agent.models import GraphTurn
from barrier_free_mobility_agent.application.conversation_repository import (
    ConversationRepository,
)
from barrier_free_mobility_agent.application.conversations import (
    ConversationRun,
    failed_initial_run,
    failed_resume_run,
)
from barrier_free_mobility_agent.application.execution_checkpoint import (
    ExecutionCheckpointInspection,
    ExecutionCheckpointState,
)
from barrier_free_mobility_agent.domain.clarification import FieldsClarificationAnswer
from barrier_free_mobility_agent.domain.errors import (
    AgentPolicyViolationError,
    ExecutionLeaseLostError,
    ExecutionWorkerRegistrationLostError,
    RunNotFoundError,
)
from barrier_free_mobility_agent.domain.execution_queue import (
    ExecutionJobState,
    ExecutionLease,
    ExecutionOperation,
    ExecutionWorkerRegistration,
    ResumeCommandExecutionPayload,
    ResumeNewTurnExecutionPayload,
    StartExecutionPayload,
)
from barrier_free_mobility_agent.domain.identity import OwnerId
from barrier_free_mobility_agent.domain.run_ledger import (
    RunEventRecord,
    RunEventType,
    RunExecutionCorrelation,
)

logger = logging.getLogger(__name__)

_RECOVERY_AMBIGUOUS = "WORKER_RECOVERY_AMBIGUOUS"
_EXECUTION_FAILED = "WORKER_EXECUTION_FAILED"
_TERMINAL_RUN_EVENTS = {
    RunEventType.RUN_INTERRUPTED,
    RunEventType.RUN_COMPLETED,
    RunEventType.RUN_FAILED,
}
_TERMINAL_TOOL_EVENTS = {
    RunEventType.TOOL_COMPLETED,
    RunEventType.TOOL_FAILED,
    RunEventType.TOOL_TIMEOUT,
}


class ExecutionWorkerGraphRunner(Protocol):
    async def start(
        self,
        question: str,
        *,
        thread_id: str | None = None,
        owner_id: OwnerId,
        correlation: RunExecutionCorrelation,
        execution_lease: ExecutionLease,
    ) -> GraphTurn: ...

    async def resume(
        self,
        answer: str | FieldsClarificationAnswer,
        *,
        thread_id: str,
        owner_id: OwnerId,
        correlation: RunExecutionCorrelation,
        execution_lease: ExecutionLease,
    ) -> GraphTurn: ...

    async def continue_business_clarification(
        self,
        answer: str,
        *,
        thread_id: str,
        owner_id: OwnerId,
        correlation: RunExecutionCorrelation,
        execution_lease: ExecutionLease,
    ) -> GraphTurn: ...

    async def inspect_execution_checkpoint(
        self,
        thread_id: str,
        *,
        owner_id: OwnerId,
        checkpoint_cursor: str | None = None,
    ) -> ExecutionCheckpointInspection: ...

    async def recover_execution_checkpoint(
        self,
        *,
        thread_id: str,
        owner_id: OwnerId,
        correlation: RunExecutionCorrelation,
        execution_lease: ExecutionLease,
        checkpoint_cursor: str,
    ) -> GraphTurn: ...


class ExecutionWorkerFaultPoint(StrEnum):
    """Deterministic worker boundaries exposed only through dependency injection."""

    AFTER_CLAIM = "after_claim"
    BEFORE_GRAPH = "before_graph"
    BEFORE_FINALIZE = "before_finalize"
    AFTER_FINALIZE = "after_finalize"


class ExecutionWorkerFaultInjector(Protocol):
    async def hit(
        self,
        point: ExecutionWorkerFaultPoint,
        lease: ExecutionLease,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class ExecutionWorkerOptions:
    lease_duration: timedelta = timedelta(seconds=30)
    heartbeat_interval: timedelta = timedelta(seconds=5)
    readiness_timeout: timedelta = timedelta(seconds=15)
    poll_interval: timedelta = timedelta(milliseconds=250)
    max_error_backoff: timedelta = timedelta(seconds=5)
    shutdown_grace: timedelta = timedelta(seconds=30)

    def __post_init__(self) -> None:
        durations = {
            "lease_duration": self.lease_duration,
            "heartbeat_interval": self.heartbeat_interval,
            "readiness_timeout": self.readiness_timeout,
            "poll_interval": self.poll_interval,
            "max_error_backoff": self.max_error_backoff,
            "shutdown_grace": self.shutdown_grace,
        }
        if any(value <= timedelta(0) for value in durations.values()):
            raise ValueError("execution worker durations must be positive")
        if self.heartbeat_interval * 2 >= self.lease_duration:
            raise ValueError("heartbeat interval must be less than half the lease duration")
        if self.heartbeat_interval * 2 >= self.readiness_timeout:
            raise ValueError("heartbeat interval must be less than half the readiness timeout")


class ExecutionWorkerState(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    READY = "ready"
    DEGRADED = "degraded"
    DRAINING = "draining"


class ExecutionWorkerTick(StrEnum):
    IDLE = "idle"
    COMPLETED = "completed"
    CONVERGED = "converged"
    FAILED = "failed"
    LEASE_LOST = "lease_lost"


@dataclass(frozen=True, slots=True)
class ExecutionWorkerReadiness:
    state: ExecutionWorkerState
    worker_id: str
    active_job_id: str | None
    last_heartbeat_at: datetime | None
    last_error_code: str | None

    @property
    def ready(self) -> bool:
        return self.state == ExecutionWorkerState.READY


class DurableExecutionWorker:
    """Single-consumer executor with bounded polling, heartbeat and safe recovery."""

    def __init__(
        self,
        runner: ExecutionWorkerGraphRunner,
        repository: ConversationRepository,
        *,
        worker_id: str,
        options: ExecutionWorkerOptions | None = None,
        fault_injector: ExecutionWorkerFaultInjector | None = None,
    ) -> None:
        if not worker_id or len(worker_id) > 200:
            raise ValueError("worker_id must contain between 1 and 200 characters")
        self._runner = runner
        self._repository = repository
        self._worker_id = worker_id
        self._options = options or ExecutionWorkerOptions()
        self._fault_injector = fault_injector
        self._state = ExecutionWorkerState.STOPPED
        self._active_job_id: str | None = None
        self._active_lease: ExecutionLease | None = None
        self._last_heartbeat_at: datetime | None = None
        self._last_error_code: str | None = None
        self._stop_requested = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._presence_task: asyncio.Task[None] | None = None
        self._registration: ExecutionWorkerRegistration | None = None

    @property
    def readiness(self) -> ExecutionWorkerReadiness:
        return ExecutionWorkerReadiness(
            state=self._state,
            worker_id=self._worker_id,
            active_job_id=self._active_job_id,
            last_heartbeat_at=self._last_heartbeat_at,
            last_error_code=self._last_error_code,
        )

    @property
    def registration(self) -> ExecutionWorkerRegistration | None:
        registration = self._registration
        return None if registration is None else registration.model_copy(deep=True)

    async def wait_for_internal_exit(self) -> None:
        """Return when either long-running worker task exits without an external stop."""

        tasks = tuple(task for task in (self._task, self._presence_task) if task is not None)
        if not tasks:
            raise RuntimeError("execution worker is not running")
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            await task

    async def is_ready(self) -> bool:
        return (
            self.readiness.ready
            and await self._repository.is_ready()
            and await self._repository.is_execution_worker_ready(self._worker_id)
        )

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            raise RuntimeError("execution worker is already running")
        self._state = ExecutionWorkerState.STARTING
        self._last_error_code = None
        if not await self._repository.is_ready():
            self._state = ExecutionWorkerState.DEGRADED
            self._last_error_code = "REPOSITORY_NOT_READY"
            raise RuntimeError("execution worker repository is not ready")
        self._stop_requested.clear()
        self._registration = await self._repository.register_execution_worker(
            self._worker_id,
            heartbeat_ttl=self._options.readiness_timeout,
        )
        self._last_heartbeat_at = self._registration.heartbeat_at
        self._state = ExecutionWorkerState.READY
        self._task = asyncio.create_task(self._run(), name=f"execution-worker:{self._worker_id}")
        self._presence_task = asyncio.create_task(
            self._presence_heartbeat_loop(),
            name=f"execution-worker-presence:{self._worker_id}",
        )

    async def stop(self, *, grace: timedelta | None = None) -> None:
        task = self._task
        if task is None and self._registration is None:
            self._state = ExecutionWorkerState.STOPPED
            return
        self._state = ExecutionWorkerState.DRAINING
        self._stop_requested.set()
        await self._stop_presence_heartbeat()
        registration = self._registration
        if registration is not None:
            try:
                self._registration = await self._repository.drain_execution_worker(registration)
            except Exception:
                self._last_error_code = "WORKER_REGISTRATION_DRAIN_FAILED"
                logger.exception("Failed to mark durable execution worker as draining")
        timeout = (grace or self._options.shutdown_grace).total_seconds()
        try:
            if task is not None:
                await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except TimeoutError:
            if task is not None:
                task.cancel()
                task.add_done_callback(_consume_task_completion)
            await self._revoke_active_execution_lease()
        finally:
            registration = self._registration
            if registration is not None:
                try:
                    await self._repository.remove_execution_worker(registration)
                except Exception:
                    self._last_error_code = "WORKER_REGISTRATION_REMOVE_FAILED"
                    logger.exception("Failed to remove durable execution worker registration")
            self._registration = None
            self._task = None
            self._active_job_id = None
            self._active_lease = None
            self._state = ExecutionWorkerState.STOPPED

    async def _presence_heartbeat_loop(self) -> None:
        while not self._stop_requested.is_set():
            await self._wait_or_stop(self._options.heartbeat_interval)
            if self._stop_requested.is_set():
                return
            registration = self._registration
            if registration is None:
                self._state = ExecutionWorkerState.DEGRADED
                self._last_error_code = "WORKER_REGISTRATION_MISSING"
                self._stop_requested.set()
                return
            try:
                refreshed = await self._repository.heartbeat_execution_worker(
                    registration,
                    heartbeat_ttl=self._options.readiness_timeout,
                )
            except ExecutionWorkerRegistrationLostError:
                self._state = ExecutionWorkerState.DEGRADED
                self._last_error_code = "WORKER_REGISTRATION_LOST"
                self._registration = None
                self._stop_requested.set()
                logger.exception("Durable execution worker registration was fenced")
                return
            except Exception:
                self._state = ExecutionWorkerState.DEGRADED
                self._last_error_code = "WORKER_PRESENCE_HEARTBEAT_FAILED"
                logger.exception("Durable execution worker presence heartbeat failed")
                continue
            self._registration = refreshed
            self._last_heartbeat_at = refreshed.heartbeat_at
            if self._last_error_code == "WORKER_PRESENCE_HEARTBEAT_FAILED":
                self._last_error_code = None
                self._state = ExecutionWorkerState.READY

    async def _stop_presence_heartbeat(self) -> None:
        task = self._presence_task
        self._presence_task = None
        if task is None:
            return
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def run_once(self) -> ExecutionWorkerTick:
        registration = self._registration
        if registration is None:
            raise ExecutionWorkerRegistrationLostError(
                context={
                    "worker_id": self._worker_id,
                    "reason": "worker_registration_missing",
                }
            )
        lease = await self._repository.claim_execution_job(
            registration=registration,
            lease_duration=self._options.lease_duration,
        )
        if lease is None:
            return ExecutionWorkerTick.IDLE
        self._active_job_id = lease.job_id
        self._active_lease = lease
        try:
            try:
                await self._hit_fault_point(ExecutionWorkerFaultPoint.AFTER_CLAIM, lease)
            except ExecutionLeaseLostError:
                return ExecutionWorkerTick.LEASE_LOST
            except asyncio.CancelledError:
                raise
            except Exception as error:
                return await self._handle_execution_failure(
                    lease,
                    current=None,
                    error=error,
                )
            return await self._run_claimed_job(lease)
        finally:
            self._active_job_id = None
            self._active_lease = None

    async def _run(self) -> None:
        consecutive_errors = 0
        try:
            while not self._stop_requested.is_set():
                try:
                    tick = await self.run_once()
                    consecutive_errors = 0
                    self._last_error_code = None
                    if self._state != ExecutionWorkerState.DRAINING:
                        self._state = ExecutionWorkerState.READY
                except asyncio.CancelledError:
                    raise
                except ExecutionWorkerRegistrationLostError:
                    self._state = ExecutionWorkerState.DEGRADED
                    self._last_error_code = "WORKER_REGISTRATION_LOST"
                    self._registration = None
                    self._stop_requested.set()
                    logger.exception(
                        "Durable execution worker registration was fenced during claim"
                    )
                    return
                except Exception:
                    consecutive_errors += 1
                    self._state = ExecutionWorkerState.DEGRADED
                    self._last_error_code = "WORKER_LOOP_ERROR"
                    logger.exception("Durable execution worker loop failed")
                    await self._wait_or_stop(self._error_backoff(consecutive_errors))
                    continue
                if tick == ExecutionWorkerTick.IDLE:
                    await self._wait_or_stop(self._options.poll_interval)
        finally:
            if self._state != ExecutionWorkerState.DRAINING:
                self._state = ExecutionWorkerState.STOPPED

    async def _run_claimed_job(self, lease: ExecutionLease) -> ExecutionWorkerTick:
        finished = asyncio.Event()
        current_lease = [lease]
        execution = asyncio.create_task(
            self._execute_job(lease),
            name=f"execution-job:{lease.job_id}",
        )
        heartbeat = asyncio.create_task(
            self._heartbeat_loop(current_lease, finished),
            name=f"execution-heartbeat:{lease.job_id}",
        )
        try:
            done, _ = await asyncio.wait(
                {execution, heartbeat},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if execution in done:
                return await execution
            if heartbeat in done:
                heartbeat_error = heartbeat.exception()
                if heartbeat_error is not None:
                    execution.cancel()
                    if isinstance(heartbeat_error, ExecutionLeaseLostError):
                        return ExecutionWorkerTick.LEASE_LOST
                    raise heartbeat_error
            raise RuntimeError("execution worker tasks completed without a result")
        finally:
            finished.set()
            if not heartbeat.done():
                heartbeat.cancel()
                heartbeat.add_done_callback(_consume_task_completion)
            else:
                _consume_task_completion(heartbeat)
            if not execution.done():
                execution.cancel()
                execution.add_done_callback(_consume_task_completion)
            else:
                _consume_task_completion(execution)

    async def _heartbeat_loop(
        self,
        current_lease: list[ExecutionLease],
        finished: asyncio.Event,
    ) -> None:
        while not finished.is_set():
            try:
                await asyncio.wait_for(
                    finished.wait(),
                    timeout=self._options.heartbeat_interval.total_seconds(),
                )
                return
            except TimeoutError:
                pass
            lease = current_lease[0]
            checkpoint = await self._runner.inspect_execution_checkpoint(
                lease.correlation.thread_id,
                owner_id=OwnerId(lease.owner_id),
            )
            cursor = (
                checkpoint.cursor
                if _checkpoint_matches_lease(
                    checkpoint,
                    lease,
                    allow_prior_generation=True,
                )
                else lease.checkpoint_cursor
            )
            refreshed = await self._repository.heartbeat_execution_job(
                lease,
                lease_duration=self._options.lease_duration,
                checkpoint_cursor=cursor,
            )
            current_lease[0] = refreshed
            self._active_lease = refreshed
            self._last_heartbeat_at = datetime.now(UTC)

    async def _revoke_active_execution_lease(self) -> None:
        lease = self._active_lease
        if lease is None:
            return
        try:
            await self._repository.release_execution_job(
                lease,
                checkpoint_cursor=lease.checkpoint_cursor,
            )
        except ExecutionLeaseLostError:
            # A concurrent terminal transition or reclaim already fenced this task.
            return
        except Exception:
            self._last_error_code = "WORKER_LEASE_REVOKE_FAILED"
            logger.exception(
                "Failed to revoke durable execution lease during forced shutdown: job_id=%s",
                lease.job_id,
            )
            raise

    async def _execute_job(self, lease: ExecutionLease) -> ExecutionWorkerTick:
        current: ConversationRun | None = None
        try:
            if lease.generation > 1:
                terminal = await self._recover_or_converge(lease)
                if terminal is not None:
                    return terminal
            current = await self._current_resume_snapshot(lease)
            await self._hit_fault_point(ExecutionWorkerFaultPoint.BEFORE_GRAPH, lease)
            turn = await self._invoke_graph(lease)
            checkpoint = await self._runner.inspect_execution_checkpoint(
                lease.correlation.thread_id,
                owner_id=OwnerId(lease.owner_id),
            )
            if not _checkpoint_matches_lease(checkpoint, lease):
                return await self._fail_closed(lease, events=None)
            assert checkpoint.cursor is not None
            run = _conversation_run(turn, lease.correlation)
            await self._hit_fault_point(ExecutionWorkerFaultPoint.BEFORE_FINALIZE, lease)
            await self._repository.finalize_execution(
                run,
                correlation=lease.correlation,
                owner_id=lease.owner_id,
                execution_lease=lease,
                job_state=ExecutionJobState.COMPLETED,
                checkpoint_cursor=checkpoint.cursor,
            )
            await self._hit_fault_point(ExecutionWorkerFaultPoint.AFTER_FINALIZE, lease)
            return ExecutionWorkerTick.COMPLETED
        except ExecutionLeaseLostError:
            return ExecutionWorkerTick.LEASE_LOST
        except asyncio.CancelledError:
            raise
        except Exception as error:
            return await self._handle_execution_failure(
                lease,
                current=current,
                error=error,
            )

    async def _handle_execution_failure(
        self,
        lease: ExecutionLease,
        *,
        current: ConversationRun | None,
        error: Exception,
    ) -> ExecutionWorkerTick:
        logger.error(
            "Durable execution job failed: job_id=%s run_id=%s attempt=%s",
            lease.job_id,
            lease.correlation.run_id,
            lease.correlation.attempt,
            exc_info=(type(error), error, error.__traceback__),
        )
        try:
            failed = (
                failed_initial_run(lease.correlation)
                if lease.operation == ExecutionOperation.START
                else failed_resume_run(_require_resume_snapshot(current))
            )
            checkpoint = await self._runner.inspect_execution_checkpoint(
                lease.correlation.thread_id,
                owner_id=OwnerId(lease.owner_id),
            )
            await self._repository.finalize_execution(
                failed,
                correlation=lease.correlation,
                owner_id=lease.owner_id,
                execution_lease=lease,
                job_state=ExecutionJobState.FAILED,
                checkpoint_cursor=(
                    checkpoint.cursor
                    if _checkpoint_matches_lease(checkpoint, lease)
                    else lease.checkpoint_cursor
                ),
                failure_code=_EXECUTION_FAILED,
            )
        except ExecutionLeaseLostError:
            return ExecutionWorkerTick.LEASE_LOST
        except Exception:
            logger.exception(
                "Execution failure could not finalize run; failing job only: job_id=%s",
                lease.job_id,
            )
            try:
                await self._repository.fail_execution_job(
                    lease,
                    failure_code=_EXECUTION_FAILED,
                    checkpoint_cursor=lease.checkpoint_cursor,
                )
            except ExecutionLeaseLostError:
                return ExecutionWorkerTick.LEASE_LOST
        return ExecutionWorkerTick.FAILED

    async def _recover_or_converge(
        self,
        lease: ExecutionLease,
    ) -> ExecutionWorkerTick | None:
        events = await self._repository.get_run_events(
            lease.correlation.run_id,
            owner_id=lease.owner_id,
        )
        if _has_terminal_event(events, lease.correlation):
            try:
                await self._repository.get_run(
                    lease.correlation.run_id,
                    owner_id=lease.owner_id,
                )
            except RunNotFoundError:
                return await self._fail_closed(lease, events=events)
            await self._repository.complete_execution_job(
                lease,
                checkpoint_cursor=lease.checkpoint_cursor,
            )
            return ExecutionWorkerTick.CONVERGED
        if _open_tool_execution_ids(events):
            return await self._fail_closed(lease, events=events)
        if lease.checkpoint_cursor is None:
            latest = await self._runner.inspect_execution_checkpoint(
                lease.correlation.thread_id,
                owner_id=OwnerId(lease.owner_id),
            )
            # A replacement may replay from input only when the crashed claimant left
            # neither a checkpoint nor an open Tool attempt. This is evidence-based
            # recovery, not lease-expiry-driven replay.
            if (
                lease.operation == ExecutionOperation.START
                and latest.state == ExecutionCheckpointState.MISSING
            ):
                return None
            return await self._fail_closed(lease, events=events)
        checkpoint = await self._runner.inspect_execution_checkpoint(
            lease.correlation.thread_id,
            owner_id=OwnerId(lease.owner_id),
            checkpoint_cursor=lease.checkpoint_cursor,
        )
        if (
            checkpoint.state == ExecutionCheckpointState.MISSING
            or checkpoint.cursor != lease.checkpoint_cursor
            or not _checkpoint_matches_lease(
                checkpoint,
                lease,
                require_prior_generation=True,
            )
        ):
            return await self._fail_closed(lease, events=events)
        await self._hit_fault_point(ExecutionWorkerFaultPoint.BEFORE_GRAPH, lease)
        turn = await self._runner.recover_execution_checkpoint(
            thread_id=lease.correlation.thread_id,
            owner_id=OwnerId(lease.owner_id),
            correlation=lease.correlation,
            execution_lease=lease,
            checkpoint_cursor=lease.checkpoint_cursor,
        )
        latest = await self._runner.inspect_execution_checkpoint(
            lease.correlation.thread_id,
            owner_id=OwnerId(lease.owner_id),
        )
        if not _checkpoint_matches_lease(latest, lease):
            return await self._fail_closed(lease, events=events)
        assert latest.cursor is not None
        await self._hit_fault_point(ExecutionWorkerFaultPoint.BEFORE_FINALIZE, lease)
        await self._repository.finalize_execution(
            _conversation_run(turn, lease.correlation),
            correlation=lease.correlation,
            owner_id=lease.owner_id,
            execution_lease=lease,
            job_state=ExecutionJobState.COMPLETED,
            checkpoint_cursor=latest.cursor,
        )
        await self._hit_fault_point(ExecutionWorkerFaultPoint.AFTER_FINALIZE, lease)
        return ExecutionWorkerTick.CONVERGED

    async def _fail_closed(
        self,
        lease: ExecutionLease,
        *,
        events: Sequence[RunEventRecord] | None,
    ) -> ExecutionWorkerTick:
        selected_events = (
            tuple(events)
            if events is not None
            else await self._repository.get_run_events(
                lease.correlation.run_id,
                owner_id=lease.owner_id,
            )
        )
        if _open_tool_execution_ids(selected_events):
            await self._repository.fail_execution_job(
                lease,
                failure_code=_RECOVERY_AMBIGUOUS,
                checkpoint_cursor=lease.checkpoint_cursor,
            )
            return ExecutionWorkerTick.FAILED
        try:
            current = await self._current_resume_snapshot(lease)
            failed = (
                failed_initial_run(lease.correlation)
                if lease.operation == ExecutionOperation.START
                else failed_resume_run(_require_resume_snapshot(current))
            )
            await self._repository.finalize_execution(
                failed,
                correlation=lease.correlation,
                owner_id=lease.owner_id,
                execution_lease=lease,
                job_state=ExecutionJobState.FAILED,
                checkpoint_cursor=lease.checkpoint_cursor,
                failure_code=_RECOVERY_AMBIGUOUS,
            )
        except (AgentPolicyViolationError, RunNotFoundError):
            await self._repository.fail_execution_job(
                lease,
                failure_code=_RECOVERY_AMBIGUOUS,
                checkpoint_cursor=lease.checkpoint_cursor,
            )
        return ExecutionWorkerTick.FAILED

    async def _current_resume_snapshot(self, lease: ExecutionLease) -> ConversationRun | None:
        if lease.operation == ExecutionOperation.START:
            return None
        return await self._repository.get_run(
            lease.correlation.run_id,
            owner_id=lease.owner_id,
        )

    async def _invoke_graph(self, lease: ExecutionLease) -> GraphTurn:
        payload = lease.payload
        owner_id = OwnerId(lease.owner_id)
        if isinstance(payload, StartExecutionPayload):
            return await self._runner.start(
                payload.question,
                thread_id=lease.correlation.thread_id,
                owner_id=owner_id,
                correlation=lease.correlation,
                execution_lease=lease,
            )
        if isinstance(payload, ResumeCommandExecutionPayload):
            return await self._runner.resume(
                payload.answer,
                thread_id=lease.correlation.thread_id,
                owner_id=owner_id,
                correlation=lease.correlation,
                execution_lease=lease,
            )
        if isinstance(payload, ResumeNewTurnExecutionPayload):
            return await self._runner.continue_business_clarification(
                payload.answer,
                thread_id=lease.correlation.thread_id,
                owner_id=owner_id,
                correlation=lease.correlation,
                execution_lease=lease,
            )
        raise AgentPolicyViolationError(context={"reason": "execution_payload_unsupported"})

    async def _hit_fault_point(
        self,
        point: ExecutionWorkerFaultPoint,
        lease: ExecutionLease,
    ) -> None:
        injector = self._fault_injector
        if injector is not None:
            await injector.hit(point, lease)

    async def _wait_or_stop(self, duration: timedelta) -> None:
        with suppress(TimeoutError):
            await asyncio.wait_for(
                self._stop_requested.wait(),
                timeout=duration.total_seconds(),
            )

    def _error_backoff(self, failures: int) -> timedelta:
        seconds = min(
            self._options.poll_interval.total_seconds() * (2 ** min(failures - 1, 8)),
            self._options.max_error_backoff.total_seconds(),
        )
        return timedelta(seconds=seconds)


def _conversation_run(turn: GraphTurn, correlation: RunExecutionCorrelation) -> ConversationRun:
    return ConversationRun(
        run_id=correlation.run_id,
        thread_id=correlation.thread_id,
        status=turn.status,
        resume_mode=turn.resume_mode,
        outcome=turn.outcome,
        context=turn.context,
        usage=turn.usage,
    )


def _require_resume_snapshot(run: ConversationRun | None) -> ConversationRun:
    if run is None:
        raise AgentPolicyViolationError(context={"reason": "resume_snapshot_missing"})
    return run


def _has_terminal_event(
    events: Sequence[RunEventRecord],
    correlation: RunExecutionCorrelation,
) -> bool:
    return any(
        event.correlation == correlation and event.event_type in _TERMINAL_RUN_EVENTS
        for event in events
    )


def _checkpoint_matches_lease(
    checkpoint: ExecutionCheckpointInspection,
    lease: ExecutionLease,
    *,
    allow_prior_generation: bool = False,
    require_prior_generation: bool = False,
) -> bool:
    generation = checkpoint.lease_generation
    matches = (
        checkpoint.cursor is not None
        and checkpoint.correlation == lease.correlation
        and checkpoint.execution_job_id == lease.job_id
        and generation is not None
    )
    if not matches or generation is None:
        return False
    if require_prior_generation:
        return generation < lease.generation
    if allow_prior_generation:
        return generation <= lease.generation
    return generation == lease.generation


def _open_tool_execution_ids(events: Sequence[RunEventRecord]) -> frozenset[str]:
    started = {
        event.tool_execution_id
        for event in events
        if event.event_type == RunEventType.TOOL_STARTED and event.tool_execution_id is not None
    }
    terminal = {
        event.tool_execution_id
        for event in events
        if event.event_type in _TERMINAL_TOOL_EVENTS and event.tool_execution_id is not None
    }
    return frozenset(started - terminal)


def _consume_task_completion[ResultT](task: asyncio.Task[ResultT]) -> None:
    if task.cancelled():
        return
    with suppress(asyncio.CancelledError):
        task.exception()
