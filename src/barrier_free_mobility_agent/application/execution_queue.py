from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol

from barrier_free_mobility_agent.domain.execution_queue import (
    ExecutionLease,
    ExecutionQueueHealth,
    ExecutionReservation,
    ExecutionWorkerRegistration,
    ResumeExecutionPayload,
    RunExecutionJob,
    StartExecutionPayload,
)
from barrier_free_mobility_agent.domain.run_ledger import RunExecutionCorrelation


class ExecutionQueue(Protocol):
    """Atomic command reservation and fenced worker coordination.

    ``now`` is a deterministic repository-contract test seam. A durable production
    adapter must reject a caller-supplied value unless it was explicitly constructed
    as a test harness; production lease authority comes from the database clock.
    """

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
    ) -> ExecutionReservation: ...

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
    ) -> ExecutionReservation: ...

    async def find_execution_reservation(
        self,
        *,
        owner_id: str,
        idempotency_digests: tuple[str, ...],
    ) -> ExecutionReservation | None: ...

    async def register_execution_worker(
        self,
        worker_id: str,
        *,
        heartbeat_ttl: timedelta,
        now: datetime | None = None,
    ) -> ExecutionWorkerRegistration: ...

    async def heartbeat_execution_worker(
        self,
        registration: ExecutionWorkerRegistration,
        *,
        heartbeat_ttl: timedelta,
        now: datetime | None = None,
    ) -> ExecutionWorkerRegistration: ...

    async def drain_execution_worker(
        self,
        registration: ExecutionWorkerRegistration,
        *,
        now: datetime | None = None,
    ) -> ExecutionWorkerRegistration: ...

    async def remove_execution_worker(
        self,
        registration: ExecutionWorkerRegistration,
    ) -> None: ...

    async def is_execution_worker_ready(self, worker_id: str) -> bool: ...

    async def get_execution_queue_health(
        self,
        *,
        stuck_after: timedelta,
        now: datetime | None = None,
    ) -> ExecutionQueueHealth: ...

    async def claim_execution_job(
        self,
        *,
        registration: ExecutionWorkerRegistration,
        lease_duration: timedelta,
        now: datetime | None = None,
    ) -> ExecutionLease | None: ...

    async def heartbeat_execution_job(
        self,
        lease: ExecutionLease,
        *,
        lease_duration: timedelta,
        checkpoint_cursor: str | None = None,
        now: datetime | None = None,
    ) -> ExecutionLease: ...

    async def complete_execution_job(
        self,
        lease: ExecutionLease,
        *,
        checkpoint_cursor: str | None = None,
        now: datetime | None = None,
    ) -> RunExecutionJob: ...

    async def fail_execution_job(
        self,
        lease: ExecutionLease,
        *,
        failure_code: str,
        checkpoint_cursor: str | None = None,
        now: datetime | None = None,
    ) -> RunExecutionJob: ...

    async def release_execution_job(
        self,
        lease: ExecutionLease,
        *,
        available_at: datetime | None = None,
        checkpoint_cursor: str | None = None,
        now: datetime | None = None,
    ) -> RunExecutionJob: ...

    async def get_execution_job(
        self,
        job_id: str,
        *,
        owner_id: str,
    ) -> RunExecutionJob: ...

    async def assert_execution_lease(
        self,
        lease: ExecutionLease,
        *,
        now: datetime | None = None,
    ) -> RunExecutionJob: ...
