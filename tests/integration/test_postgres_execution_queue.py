from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from barrier_free_mobility_agent.agent.models import GraphTurnStatus, ResumeMode
from barrier_free_mobility_agent.api.error_projection import project_agent_error
from barrier_free_mobility_agent.application.conversation_repository import ConversationThread
from barrier_free_mobility_agent.application.conversations import ConversationRun
from barrier_free_mobility_agent.domain.errors import (
    AgentPolicyViolationError,
    AgentStateIncompatibleError,
    ExecutionJobNotFoundError,
    ExecutionLeaseLostError,
    ExecutionWorkerRegistrationLostError,
    ExecutionWorkerUnavailableError,
    IdempotencyKeyReuseError,
    ThreadBusyError,
)
from barrier_free_mobility_agent.domain.execution_queue import (
    ExecutionJobState,
    ExecutionLease,
    ExecutionReservation,
    ExecutionWorkerRegistration,
    ExecutionWorkerRegistrationState,
    ResumeNewTurnExecutionPayload,
    StartExecutionPayload,
    ThreadDeletionReservation,
)
from barrier_free_mobility_agent.domain.models import (
    AnswerEvidence,
    AnswerOutcome,
    AnswerSource,
    QuestionIntent,
    ResponseStatus,
)
from barrier_free_mobility_agent.domain.routing import ToolCapability
from barrier_free_mobility_agent.domain.run_ledger import (
    ResolveStationToolSummary,
    RunEventType,
    RunExecutionCorrelation,
    ToolTransition,
)
from barrier_free_mobility_agent.persistence import postgres as postgres_persistence
from barrier_free_mobility_agent.persistence.execution_payload_cipher import (
    ExecutionPayloadCipher,
)
from barrier_free_mobility_agent.persistence.postgres import (
    PostgresConversationRepository,
)
from barrier_free_mobility_agent.persistence.runtime import _configure_postgres_connection

_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not _DATABASE_URL,
    reason="TEST_DATABASE_URL is required for PostgreSQL integration tests.",
)


async def _claim_execution_job(
    repository: PostgresConversationRepository,
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


@pytest.mark.asyncio
async def test_postgres_execution_queue_schema_nine_has_worker_registry() -> None:
    assert _DATABASE_URL is not None
    async with _repository(_DATABASE_URL):
        version = await _fetch_one(
            _DATABASE_URL,
            "SELECT max(version) AS version FROM agent_repository_migrations",
        )
        columns = await _fetch_all(
            _DATABASE_URL,
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = 'run_execution_jobs'
            """,
        )
        constraints = await _fetch_all(
            _DATABASE_URL,
            """
            SELECT conname
            FROM pg_constraint
            WHERE conrelid = 'run_execution_jobs'::regclass
            """,
        )
        thread_constraints = await _fetch_all(
            _DATABASE_URL,
            """
            SELECT conname
            FROM pg_constraint
            WHERE conrelid = 'agent_threads'::regclass
            """,
        )
        indexes = await _fetch_all(
            _DATABASE_URL,
            """
            SELECT indexname
            FROM pg_indexes
            WHERE schemaname = current_schema()
              AND tablename = 'run_execution_jobs'
            """,
        )
        worker_columns = await _fetch_all(
            _DATABASE_URL,
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = 'execution_worker_registry'
            """,
        )
        worker_constraints = await _fetch_all(
            _DATABASE_URL,
            """
            SELECT conname
            FROM pg_constraint
            WHERE conrelid = 'execution_worker_registry'::regclass
            """,
        )
        worker_indexes = await _fetch_all(
            _DATABASE_URL,
            """
            SELECT indexname
            FROM pg_indexes
            WHERE schemaname = current_schema()
              AND tablename = 'execution_worker_registry'
            """,
        )

    assert version == {"version": 9}
    assert {
        "job_id",
        "owner_id",
        "thread_id",
        "run_id",
        "request_id",
        "execution_attempt",
        "operation",
        "state",
        "payload_schema_version",
        "idempotency_digest",
        "request_fingerprint",
        "payload",
        "payload_ciphertext",
        "payload_nonce",
        "payload_key_fingerprint",
        "payload_encryption_version",
        "checkpoint_cursor",
        "available_at",
        "lease_token",
        "lease_owner",
        "lease_generation",
        "lease_expires_at",
        "heartbeat_at",
        "claim_count",
        "failure_code",
        "created_at",
        "updated_at",
        "completed_at",
    } == {str(row["column_name"]) for row in columns}
    assert {
        "run_execution_jobs_owner_idempotency_unique",
        "run_execution_jobs_run_attempt_unique",
        "run_execution_jobs_state_shape",
        "run_execution_jobs_payload_schema_version",
        "run_execution_jobs_lease_window_positive",
        "run_execution_jobs_run_fk",
        "run_execution_jobs_thread_owner_fk",
        "run_execution_jobs_plaintext_payload_forbidden",
        "run_execution_jobs_encrypted_payload_shape",
    }.issubset({str(row["conname"]) for row in constraints})
    assert "agent_threads_thread_owner_unique" in {
        str(row["conname"]) for row in thread_constraints
    }
    assert {
        "run_execution_jobs_queued_claim_idx",
        "run_execution_jobs_expired_lease_idx",
    }.issubset({str(row["indexname"]) for row in indexes})
    assert {
        "worker_id",
        "token",
        "generation",
        "state",
        "registered_at",
        "heartbeat_at",
        "expires_at",
        "updated_at",
    } == {str(row["column_name"]) for row in worker_columns}
    assert {
        "execution_worker_registry_generation_positive",
        "execution_worker_registry_state",
        "execution_worker_registry_timestamp_order",
        "execution_worker_registry_state_shape",
    }.issubset({str(row["conname"]) for row in worker_constraints})
    assert "execution_worker_registry_ready_idx" in {
        str(row["indexname"]) for row in worker_indexes
    }


@pytest.mark.asyncio
async def test_postgres_execution_queue_rejects_test_clock_by_default() -> None:
    assert _DATABASE_URL is not None
    async with _repository(_DATABASE_URL, allow_test_clock=False) as repository:
        registration = await repository.register_execution_worker(
            "worker-clock-test",
            heartbeat_ttl=timedelta(seconds=30),
        )
        with pytest.raises(ValueError, match="test-only"):
            await repository.claim_execution_job(
                registration=registration,
                lease_duration=timedelta(seconds=10),
                now=datetime.now(UTC),
            )
        await repository.remove_execution_worker(registration)


@pytest.mark.asyncio
async def test_postgres_worker_registry_is_db_clocked_fenced_and_drainable() -> None:
    assert _DATABASE_URL is not None
    worker_id = f"worker-registry-{uuid4().hex}"
    registered_at = datetime.now(UTC)

    async with _repository(_DATABASE_URL) as repository:
        baseline = await repository.get_execution_queue_health(
            stuck_after=timedelta(seconds=10),
            now=registered_at,
        )
        first = await repository.register_execution_worker(
            worker_id,
            heartbeat_ttl=timedelta(seconds=30),
            now=registered_at,
        )
        assert first.generation == 1
        assert first.state == ExecutionWorkerRegistrationState.READY
        assert first.heartbeat_at == registered_at
        assert first.expires_at == registered_at + timedelta(seconds=30)
        assert await repository.is_execution_worker_ready(worker_id) is True

        renewed_at = registered_at + timedelta(seconds=2)
        renewed = await repository.heartbeat_execution_worker(
            first,
            heartbeat_ttl=timedelta(seconds=30),
            now=renewed_at,
        )
        assert renewed.heartbeat_at == renewed_at
        assert renewed.expires_at == renewed_at + timedelta(seconds=30)

        replacement = await repository.register_execution_worker(
            worker_id,
            heartbeat_ttl=timedelta(seconds=30),
            now=registered_at + timedelta(seconds=3),
        )
        assert replacement.generation == 2
        assert replacement.token != first.token
        with pytest.raises(ExecutionWorkerRegistrationLostError):
            await repository.heartbeat_execution_worker(
                renewed,
                heartbeat_ttl=timedelta(seconds=30),
                now=registered_at + timedelta(seconds=4),
            )
        with pytest.raises(ExecutionWorkerRegistrationLostError):
            await repository.drain_execution_worker(
                renewed,
                now=registered_at + timedelta(seconds=4),
            )
        with pytest.raises(ExecutionWorkerRegistrationLostError):
            await repository.remove_execution_worker(renewed)

        ready_health = await repository.get_execution_queue_health(
            stuck_after=timedelta(seconds=10),
            now=registered_at + timedelta(seconds=4),
        )
        assert ready_health.ready_worker_count == baseline.ready_worker_count + 1
        drained = await repository.drain_execution_worker(
            replacement,
            now=registered_at + timedelta(seconds=5),
        )
        assert drained.state == ExecutionWorkerRegistrationState.DRAINING
        assert drained.heartbeat_at == replacement.heartbeat_at
        assert drained.expires_at == replacement.expires_at
        assert await repository.is_execution_worker_ready(worker_id) is False
        drained_health = await repository.get_execution_queue_health(
            stuck_after=timedelta(seconds=10),
            now=registered_at + timedelta(seconds=5),
        )
        assert drained_health.ready_worker_count == baseline.ready_worker_count
        await repository.remove_execution_worker(drained)


@pytest.mark.asyncio
async def test_postgres_worker_registration_prunes_other_expired_identities() -> None:
    assert _DATABASE_URL is not None
    suffix = uuid4().hex
    registered_at = datetime.now(UTC)

    async with _repository(_DATABASE_URL) as repository:
        expired = await repository.register_execution_worker(
            f"expired-registry-{suffix}",
            heartbeat_ttl=timedelta(seconds=1),
            now=registered_at,
        )
        current = await repository.register_execution_worker(
            f"current-registry-{suffix}",
            heartbeat_ttl=timedelta(seconds=30),
            now=registered_at + timedelta(seconds=2),
        )

        with pytest.raises(ExecutionWorkerRegistrationLostError):
            await repository.remove_execution_worker(expired)
        await repository.remove_execution_worker(current)


@pytest.mark.asyncio
async def test_postgres_concurrent_worker_replacements_prune_without_deadlock() -> None:
    assert _DATABASE_URL is not None
    suffix = uuid4().hex
    first_worker_id = f"concurrent-prune-1-{suffix}"
    second_worker_id = f"concurrent-prune-2-{suffix}"
    registered_at = datetime.now(UTC)

    async with (
        _repository(_DATABASE_URL) as first_repository,
        _repository(_DATABASE_URL) as second_repository,
    ):
        await first_repository.register_execution_worker(
            first_worker_id,
            heartbeat_ttl=timedelta(seconds=1),
            now=registered_at,
        )
        await first_repository.register_execution_worker(
            second_worker_id,
            heartbeat_ttl=timedelta(seconds=1),
            now=registered_at,
        )

        first, second = await asyncio.wait_for(
            asyncio.gather(
                first_repository.register_execution_worker(
                    first_worker_id,
                    heartbeat_ttl=timedelta(seconds=30),
                    now=registered_at + timedelta(seconds=2),
                ),
                second_repository.register_execution_worker(
                    second_worker_id,
                    heartbeat_ttl=timedelta(seconds=30),
                    now=registered_at + timedelta(seconds=2),
                ),
            ),
            timeout=5,
        )

        assert first.worker_id == first_worker_id
        assert second.worker_id == second_worker_id
        assert await first_repository.is_execution_worker_ready(first_worker_id)
        assert await second_repository.is_execution_worker_ready(second_worker_id)
        await first_repository.remove_execution_worker(first)
        await second_repository.remove_execution_worker(second)


@pytest.mark.asyncio
async def test_postgres_claim_requires_current_ready_registration_and_competes_once() -> None:
    assert _DATABASE_URL is not None
    suffix = uuid4().hex
    owner_id = f"owner-registration-claim-{suffix}"
    thread_id = f"thread-registration-claim-{suffix}"
    correlation = RunExecutionCorrelation(
        thread_id=thread_id,
        run_id=f"run-registration-claim-{suffix}",
        request_id=f"request-registration-claim-{suffix}",
        attempt=1,
    )

    async with _repository(_DATABASE_URL) as repository:
        await repository.create_thread(_thread(thread_id, owner_id), capacity=10)
        reservation = await repository.reserve_initial_execution(
            correlation,
            owner_id=owner_id,
            capacity=10,
            idempotency_digest=_digest(f"registration-claim-key-{suffix}"),
            request_fingerprint=_digest(f"registration-claim-request-{suffix}"),
            payload=StartExecutionPayload(question="registered worker only"),
        )
        claim_at = reservation.job.available_at + timedelta(seconds=5)
        absent = ExecutionWorkerRegistration(
            worker_id=f"absent-worker-{suffix}",
            token=f"absent-token-{suffix}",
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
            f"expired-claim-worker-{suffix}",
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
            f"draining-claim-worker-{suffix}",
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
            f"replaced-claim-worker-{suffix}",
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
            f"ready-claim-worker-1-{suffix}",
            heartbeat_ttl=timedelta(seconds=30),
            now=claim_at,
        )
        second_registration = await repository.register_execution_worker(
            f"ready-claim-worker-2-{suffix}",
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
        stored = await repository.get_execution_job(
            reservation.job.job_id,
            owner_id=owner_id,
        )
        assert stored.state == ExecutionJobState.LEASED
        assert stored.claim_count == 1
        assert stored.lease_generation == 1

        await repository.fail_execution_job(
            leases[0],
            failure_code="REGISTRATION_CLAIM_TEST_CLEANUP",
            now=claim_at + timedelta(seconds=1),
        )
        with pytest.raises(ExecutionWorkerRegistrationLostError):
            await repository.remove_execution_worker(expired)
        await repository.remove_execution_worker(draining)
        await repository.remove_execution_worker(replacement)
        await repository.remove_execution_worker(first_registration)
        await repository.remove_execution_worker(second_registration)
        await repository.delete_thread(thread_id, owner_id=owner_id)


@pytest.mark.asyncio
async def test_postgres_new_reservation_requires_a_live_worker_but_replay_does_not() -> None:
    assert _DATABASE_URL is not None
    suffix = uuid4().hex
    owner_id = f"owner-admission-{suffix}"
    thread_id = f"thread-admission-{suffix}"
    worker_id = f"worker-admission-{suffix}"
    correlation = RunExecutionCorrelation(
        thread_id=thread_id,
        run_id=f"run-admission-{suffix}",
        request_id=f"request-admission-{suffix}",
        attempt=1,
    )
    digest = _digest(f"admission-key-{suffix}")
    fingerprint = _digest(f"admission-request-{suffix}")
    payload = StartExecutionPayload(question="worker admission gate")

    async with _repository(_DATABASE_URL) as repository:
        initial_health = await repository.get_execution_queue_health(
            stuck_after=timedelta(seconds=10)
        )
        assert initial_health.ready_worker_count == 0
        await repository.create_thread(_thread(thread_id, owner_id), capacity=10)
        registration = await repository.register_execution_worker(
            worker_id,
            heartbeat_ttl=timedelta(seconds=30),
        )
        reservation = await repository.reserve_initial_execution(
            correlation,
            owner_id=owner_id,
            capacity=10,
            idempotency_digest=digest,
            request_fingerprint=fingerprint,
            payload=payload,
            require_available_worker=True,
        )
        drained = await repository.drain_execution_worker(registration)

        replay = await repository.reserve_initial_execution(
            correlation,
            owner_id=owner_id,
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
                correlation.model_copy(
                    update={
                        "run_id": f"run-rejected-{suffix}",
                        "request_id": f"request-rejected-{suffix}",
                    }
                ),
                owner_id=owner_id,
                capacity=10,
                idempotency_digest=_digest(f"rejected-key-{suffix}"),
                request_fingerprint=_digest(f"rejected-request-{suffix}"),
                payload=payload,
                require_available_worker=True,
            )
        counts = await _fetch_one(
            _DATABASE_URL,
            """
            SELECT
                (SELECT count(*) FROM agent_runs WHERE thread_id = %s) AS runs,
                (SELECT count(*) FROM run_events WHERE thread_id = %s) AS events,
                (SELECT count(*) FROM run_execution_jobs WHERE thread_id = %s) AS jobs
            """,
            (thread_id, thread_id, thread_id),
        )
        assert counts == {"runs": 1, "events": 1, "jobs": 1}
        thread = await repository.get_thread(thread_id, owner_id=owner_id)
        assert thread.active_run_id == correlation.run_id
        assert thread.run_count == 1

        lease = await _claim_execution_job(
            repository,
            worker_id=f"cleanup-{worker_id}",
            lease_duration=timedelta(seconds=10),
        )
        assert lease is not None
        await repository.fail_execution_job(lease, failure_code="ADMISSION_TEST_CLEANUP")
        await repository.remove_execution_worker(drained)
        await repository.delete_thread(thread_id, owner_id=owner_id)


@pytest.mark.asyncio
async def test_postgres_queue_health_aggregates_age_expiry_recovery_and_stuck_jobs() -> None:
    assert _DATABASE_URL is not None
    suffix = uuid4().hex
    owner_id = f"owner-health-{suffix}"
    leased_thread_id = f"thread-health-leased-{suffix}"
    queued_thread_id = f"thread-health-queued-{suffix}"
    health_at = datetime.now(UTC) + timedelta(seconds=30)

    async with _repository(_DATABASE_URL) as repository:
        await repository.create_thread(_thread(leased_thread_id, owner_id), capacity=10)
        leased_reservation = await repository.reserve_initial_execution(
            RunExecutionCorrelation(
                thread_id=leased_thread_id,
                run_id=f"run-health-leased-{suffix}",
                request_id=f"request-health-leased-{suffix}",
                attempt=1,
            ),
            owner_id=owner_id,
            capacity=10,
            idempotency_digest=_digest(f"health-leased-key-{suffix}"),
            request_fingerprint=_digest(f"health-leased-request-{suffix}"),
            payload=StartExecutionPayload(question="leased health job"),
        )
        first = await _claim_execution_job(
            repository,
            worker_id=f"worker-health-first-{suffix}",
            lease_duration=timedelta(seconds=5),
            now=health_at - timedelta(seconds=20),
        )
        assert first is not None
        assert first.job_id == leased_reservation.job.job_id
        recovered = await _claim_execution_job(
            repository,
            worker_id=f"worker-health-recovery-{suffix}",
            lease_duration=timedelta(seconds=5),
            now=health_at - timedelta(seconds=10),
        )
        assert recovered is not None
        assert recovered.job_id == leased_reservation.job.job_id
        assert recovered.generation == 2

        await repository.create_thread(_thread(queued_thread_id, owner_id), capacity=10)
        queued_reservation = await repository.reserve_initial_execution(
            RunExecutionCorrelation(
                thread_id=queued_thread_id,
                run_id=f"run-health-queued-{suffix}",
                request_id=f"request-health-queued-{suffix}",
                attempt=1,
            ),
            owner_id=owner_id,
            capacity=10,
            idempotency_digest=_digest(f"health-queued-key-{suffix}"),
            request_fingerprint=_digest(f"health-queued-request-{suffix}"),
            payload=StartExecutionPayload(question="queued health job"),
        )

        health = await repository.get_execution_queue_health(
            stuck_after=timedelta(seconds=2),
            now=health_at,
        )
        assert health.checked_at == health_at
        assert health.queued_job_count == 1
        assert health.leased_job_count == 1
        assert health.expired_lease_count == 1
        assert health.recovery_attempt_count == 1
        assert health.stuck_job_count == 2
        assert health.oldest_queued_at is not None
        assert health.oldest_queued_age_seconds is not None
        assert health.oldest_queued_age_seconds >= 20
        assert queued_reservation.job.payload is not None

        cleanup_ids: set[str] = set()
        for offset in range(2):
            cleanup = await _claim_execution_job(
                repository,
                worker_id=f"worker-health-cleanup-{offset}-{suffix}",
                lease_duration=timedelta(seconds=10),
                now=health_at + timedelta(seconds=offset),
            )
            assert cleanup is not None
            cleanup_ids.add(cleanup.job_id)
            await repository.fail_execution_job(
                cleanup,
                failure_code="HEALTH_TEST_CLEANUP",
                now=health_at + timedelta(seconds=offset, milliseconds=500),
            )
        assert cleanup_ids == {
            leased_reservation.job.job_id,
            queued_reservation.job.job_id,
        }
        await repository.delete_thread(leased_thread_id, owner_id=owner_id)
        await repository.delete_thread(queued_thread_id, owner_id=owner_id)


@pytest.mark.asyncio
async def test_postgres_thread_deletion_fence_serializes_with_new_job_reservation() -> None:
    assert _DATABASE_URL is not None
    suffix = uuid4().hex
    owner_id = f"owner-delete-{suffix}"
    thread_id = f"thread-delete-{suffix}"
    correlation = RunExecutionCorrelation(
        thread_id=thread_id,
        run_id=f"run-delete-{suffix}",
        request_id=f"request-delete-{suffix}",
        attempt=1,
    )

    async with _repository(_DATABASE_URL) as repository:
        await repository.create_thread(_thread(thread_id, owner_id), capacity=10)
        deletion_result, execution_result = await asyncio.gather(
            repository.prepare_thread_deletion(thread_id, owner_id=owner_id),
            repository.reserve_initial_execution(
                correlation,
                owner_id=owner_id,
                capacity=10,
                idempotency_digest=_digest(f"delete-race-key-{suffix}"),
                request_fingerprint=_digest(f"delete-race-request-{suffix}"),
                payload=StartExecutionPayload(question="race"),
            ),
            return_exceptions=True,
        )

        successful_deletion = isinstance(deletion_result, ThreadDeletionReservation)
        successful_execution = isinstance(execution_result, ExecutionReservation)
        assert successful_deletion != successful_execution
        rejected = execution_result if successful_deletion else deletion_result
        assert isinstance(rejected, ThreadBusyError)

        if successful_deletion:
            assert isinstance(deletion_result, ThreadDeletionReservation)
            replayed = await repository.prepare_thread_deletion(
                thread_id,
                owner_id=owner_id,
            )
            assert replayed.token == deletion_result.token
            assert replayed.replayed is True
            await repository.abort_thread_deletion(deletion_result)
            execution = await repository.reserve_initial_execution(
                correlation,
                owner_id=owner_id,
                capacity=10,
                idempotency_digest=_digest(f"delete-race-key-{suffix}"),
                request_fingerprint=_digest(f"delete-race-request-{suffix}"),
                payload=StartExecutionPayload(question="race"),
            )
        else:
            assert isinstance(execution_result, ExecutionReservation)
            execution = execution_result

        with pytest.raises(ThreadBusyError):
            await repository.prepare_thread_deletion(thread_id, owner_id=owner_id)
        lease = await _claim_execution_job(
            repository,
            worker_id=f"delete-worker-{suffix}",
            lease_duration=timedelta(seconds=30),
        )
        assert lease is not None
        assert lease.job_id == execution.job.job_id
        await repository.fail_execution_job(lease, failure_code="DELETE_TEST_TERMINAL")
        prepared = await repository.prepare_thread_deletion(thread_id, owner_id=owner_id)
        with pytest.raises(ThreadBusyError):
            await repository.reserve_initial_execution(
                correlation.model_copy(update={"run_id": f"blocked-{suffix}"}),
                owner_id=owner_id,
                capacity=10,
                idempotency_digest=_digest(f"blocked-key-{suffix}"),
                request_fingerprint=_digest(f"blocked-request-{suffix}"),
                payload=StartExecutionPayload(question="blocked"),
            )
        await repository.commit_thread_deletion(prepared)


@pytest.mark.asyncio
async def test_postgres_concurrent_heartbeat_uses_post_lock_database_clock() -> None:
    assert _DATABASE_URL is not None
    suffix = uuid4().hex
    owner_id = f"owner-{suffix}"
    thread_id = f"thread-{suffix}"

    async with _repository(_DATABASE_URL) as repository:
        await repository.create_thread(_thread(thread_id, owner_id), capacity=10)
        reservation = await repository.reserve_initial_execution(
            RunExecutionCorrelation(
                thread_id=thread_id,
                run_id=f"run-{suffix}",
                request_id=f"request-{suffix}",
                attempt=1,
            ),
            owner_id=owner_id,
            capacity=10,
            idempotency_digest=_digest(f"idempotency-{suffix}"),
            request_fingerprint=_digest(f"request-{suffix}"),
            payload=StartExecutionPayload(question="heartbeat 경합을 확인해줘"),
        )
        lease = await _claim_execution_job(
            repository,
            worker_id=f"worker-{suffix}",
            lease_duration=timedelta(seconds=30),
        )
        assert lease is not None
        renewed = await asyncio.gather(
            repository.heartbeat_execution_job(
                lease,
                lease_duration=timedelta(seconds=30),
            ),
            repository.heartbeat_execution_job(
                lease,
                lease_duration=timedelta(seconds=30),
            ),
        )
        stored = await repository.get_execution_job(
            reservation.job.job_id,
            owner_id=owner_id,
        )
        latest = max(renewed, key=lambda item: item.heartbeat_at)
        assert stored.heartbeat_at == latest.heartbeat_at
        assert stored.updated_at == latest.heartbeat_at
        assert stored.lease_expires_at == latest.expires_at
        assert stored.lease_expires_at is not None
        assert stored.heartbeat_at is not None
        assert stored.lease_expires_at > stored.heartbeat_at
        await repository.complete_execution_job(latest)
        await repository.delete_thread(thread_id, owner_id=owner_id)


@pytest.mark.asyncio
async def test_postgres_heartbeat_rechecks_clock_after_waiting_for_row_lock() -> None:
    """A lease that expires while its row lock is queued must not be renewed."""

    assert _DATABASE_URL is not None
    suffix = uuid4().hex
    owner_id = f"owner-{suffix}"
    thread_id = f"thread-{suffix}"

    async with _repository(_DATABASE_URL) as repository:
        await repository.create_thread(_thread(thread_id, owner_id), capacity=10)
        await repository.reserve_initial_execution(
            RunExecutionCorrelation(
                thread_id=thread_id,
                run_id=f"run-{suffix}",
                request_id=f"request-{suffix}",
                attempt=1,
            ),
            owner_id=owner_id,
            capacity=10,
            idempotency_digest=_digest(f"idempotency-{suffix}"),
            request_fingerprint=_digest(f"request-{suffix}"),
            payload=StartExecutionPayload(question="잠금 대기 중 만료 검증"),
        )
        lease = await _claim_execution_job(
            repository,
            worker_id=f"worker-{suffix}",
            lease_duration=timedelta(seconds=30),
        )
        assert lease is not None

        blocker = await AsyncConnection.connect(_DATABASE_URL, row_factory=dict_row)
        heartbeat_task: asyncio.Task[ExecutionLease] | None = None
        try:
            async with blocker.transaction():
                await blocker.execute(
                    """
                    WITH execution_clock AS MATERIALIZED (
                        SELECT clock_timestamp() AS effective_now
                    )
                    UPDATE run_execution_jobs
                    SET heartbeat_at = execution_clock.effective_now,
                        lease_expires_at = (
                            execution_clock.effective_now + interval '300 milliseconds'
                        ),
                        updated_at = execution_clock.effective_now
                    FROM execution_clock
                    WHERE job_id = %s
                    """,
                    (lease.job_id,),
                )
                heartbeat_task = asyncio.create_task(
                    repository.heartbeat_execution_job(
                        lease,
                        lease_duration=timedelta(seconds=30),
                    )
                )
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(
                        asyncio.shield(heartbeat_task),
                        timeout=0.05,
                    )
                await blocker.execute("SELECT pg_sleep(0.6)")

            with pytest.raises(ExecutionLeaseLostError):
                await heartbeat_task
        finally:
            if heartbeat_task is not None and not heartbeat_task.done():
                heartbeat_task.cancel()
                await asyncio.gather(heartbeat_task, return_exceptions=True)
            await blocker.close()

        await _fail_claimable_execution_jobs(
            repository,
            worker_id=f"cleanup-heartbeat-{suffix}",
        )
        await repository.delete_thread(thread_id, owner_id=owner_id)


@pytest.mark.asyncio
async def test_postgres_initial_execution_rolls_back_if_job_insert_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _DATABASE_URL is not None
    suffix = uuid4().hex
    owner_id = f"owner-{suffix}"
    thread_id = f"thread-{suffix}"

    async def fail_job_insert(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("injected execution job insert failure")

    async with _repository(_DATABASE_URL) as repository:
        await repository.create_thread(_thread(thread_id, owner_id), capacity=10)
        monkeypatch.setattr(postgres_persistence, "_insert_execution_job", fail_job_insert)

        with pytest.raises(RuntimeError, match="injected execution job insert failure"):
            await repository.reserve_initial_execution(
                RunExecutionCorrelation(
                    thread_id=thread_id,
                    run_id=f"run-{suffix}",
                    request_id=f"request-{suffix}",
                    attempt=1,
                ),
                owner_id=owner_id,
                capacity=10,
                idempotency_digest=_digest(f"idempotency-{suffix}"),
                request_fingerprint=_digest(f"request-{suffix}"),
                payload=StartExecutionPayload(question="원자적 예약 실패를 확인해줘"),
            )

        thread = await repository.get_thread(thread_id, owner_id=owner_id)
        assert thread.active_run_id is None
        assert thread.run_count == 0
        counts = await _fetch_one(
            _DATABASE_URL,
            """
            SELECT
                (SELECT count(*) FROM agent_runs WHERE thread_id = %s) AS runs,
                (SELECT count(*) FROM run_events WHERE thread_id = %s) AS events,
                (SELECT count(*) FROM run_execution_jobs WHERE thread_id = %s) AS jobs
            """,
            (thread_id, thread_id, thread_id),
        )
        assert counts == {"runs": 0, "events": 0, "jobs": 0}
        await repository.delete_thread(thread_id, owner_id=owner_id)


@pytest.mark.asyncio
async def test_postgres_corrupt_payload_is_quarantined_without_head_of_line_blocking() -> None:
    assert _DATABASE_URL is not None
    suffix = uuid4().hex
    owner_id = f"owner-{suffix}"
    thread_id = f"thread-{suffix}"
    valid_thread_id = f"thread-valid-{suffix}"
    sentinel = f"private-execution-payload-{suffix}"

    async with _repository(_DATABASE_URL) as repository:
        await repository.create_thread(_thread(thread_id, owner_id), capacity=10)
        reservation = await repository.reserve_initial_execution(
            RunExecutionCorrelation(
                thread_id=thread_id,
                run_id=f"run-{suffix}",
                request_id=f"request-{suffix}",
                attempt=1,
            ),
            owner_id=owner_id,
            capacity=10,
            idempotency_digest=_digest(f"idempotency-{suffix}"),
            request_fingerprint=_digest(f"request-{suffix}"),
            payload=StartExecutionPayload(question=sentinel),
        )
        encrypted_row = await _fetch_one(
            _DATABASE_URL,
            """
            SELECT payload, payload_ciphertext, payload_key_fingerprint
            FROM run_execution_jobs
            WHERE job_id = %s
            """,
            (reservation.job.job_id,),
        )
        assert encrypted_row["payload"] is None
        ciphertext = encrypted_row["payload_ciphertext"]
        assert isinstance(ciphertext, (bytes, bytearray, memoryview))
        assert sentinel.encode("utf-8") not in bytes(ciphertext)
        assert len(str(encrypted_row["payload_key_fingerprint"])) == 64
        await _execute(
            _DATABASE_URL,
            """
            UPDATE run_execution_jobs
                SET payload_ciphertext = set_byte(
                    payload_ciphertext,
                    0,
                    (get_byte(payload_ciphertext, 0) + 1) %% 256
                )
            WHERE job_id = %s
            """,
            (reservation.job.job_id,),
        )

        with pytest.raises(AgentStateIncompatibleError) as invalid_info:
            await repository.get_execution_job(
                reservation.job.job_id,
                owner_id=owner_id,
            )
        _assert_incompatible_payload_is_safe(
            invalid_info.value,
            job_id=reservation.job.job_id,
            sentinel=sentinel,
        )

        await repository.create_thread(_thread(valid_thread_id, owner_id), capacity=10)
        valid = await repository.reserve_initial_execution(
            RunExecutionCorrelation(
                thread_id=valid_thread_id,
                run_id=f"run-valid-{suffix}",
                request_id=f"request-valid-{suffix}",
                attempt=1,
            ),
            owner_id=owner_id,
            capacity=10,
            idempotency_digest=_digest(f"idempotency-valid-{suffix}"),
            request_fingerprint=_digest(f"request-valid-{suffix}"),
            payload=StartExecutionPayload(question="valid payload"),
        )
        lease = await _claim_execution_job(
            repository,
            worker_id=f"worker-{suffix}",
            lease_duration=timedelta(seconds=10),
            now=max(reservation.job.available_at, valid.job.available_at)
            + timedelta(milliseconds=1),
        )
        assert lease is not None
        assert lease.job_id == valid.job.job_id
        state = await _fetch_one(
            _DATABASE_URL,
            """
            SELECT
                state,
                claim_count,
                lease_generation,
                lease_token,
                failure_code,
                payload_ciphertext
            FROM run_execution_jobs
            WHERE job_id = %s
            """,
            (reservation.job.job_id,),
        )
        assert state == {
            "state": "failed",
            "claim_count": 1,
            "lease_generation": 1,
            "lease_token": None,
            "failure_code": "EXECUTION_PAYLOAD_INCOMPATIBLE",
            "payload_ciphertext": None,
        }
        quarantined = await repository.get_execution_job(
            reservation.job.job_id,
            owner_id=owner_id,
        )
        assert quarantined.state == ExecutionJobState.FAILED
        assert quarantined.payload is None
        await repository.fail_execution_job(
            lease,
            failure_code="TEST_CLEANUP",
            now=lease.heartbeat_at + timedelta(seconds=1),
        )
        await repository.delete_thread(thread_id, owner_id=owner_id)
        await repository.delete_thread(valid_thread_id, owner_id=owner_id)


@pytest.mark.asyncio
async def test_postgres_execution_reservation_is_atomic_idempotent_and_scoped() -> None:
    assert _DATABASE_URL is not None
    suffix = uuid4().hex
    owner_id = f"owner-{suffix}"
    other_owner_id = f"owner-other-{suffix}"
    thread_id = f"thread-{suffix}"
    other_thread_id = f"thread-other-{suffix}"
    other_owner_thread_id = f"thread-other-owner-{suffix}"
    digest = _digest(f"idempotency-{suffix}")
    fingerprint = _digest(f"request-{suffix}")
    payload = StartExecutionPayload(question="시청역에서 이동 가능한 경로를 알려줘")
    correlations = tuple(
        RunExecutionCorrelation(
            thread_id=thread_id,
            run_id=f"run-{index}-{suffix}",
            request_id=f"request-{index}-{suffix}",
            attempt=1,
        )
        for index in range(2)
    )

    async with _repository(_DATABASE_URL) as repository:
        await repository.create_thread(_thread(thread_id, owner_id), capacity=10)
        await repository.create_thread(_thread(other_thread_id, owner_id), capacity=10)
        await repository.create_thread(
            _thread(other_owner_thread_id, other_owner_id),
            capacity=10,
        )

        reservations = await asyncio.gather(
            *(
                repository.reserve_initial_execution(
                    correlation,
                    owner_id=owner_id,
                    capacity=10,
                    idempotency_digest=digest,
                    request_fingerprint=fingerprint,
                    payload=payload,
                )
                for correlation in correlations
            )
        )

        assert {reservation.job.job_id for reservation in reservations} == {
            reservations[0].job.job_id
        }
        assert sorted(reservation.replayed for reservation in reservations) == [False, True]
        assert reservations[0].correlation == reservations[1].correlation
        events = await repository.get_run_events(
            reservations[0].correlation.run_id,
            owner_id=owner_id,
        )
        assert [event.event_type for event in events] == [RunEventType.RUN_STARTED]
        found = await repository.find_execution_reservation(
            owner_id=owner_id,
            idempotency_digests=(digest,),
        )
        assert found is not None
        assert found.replayed is True
        assert found.job == reservations[0].job
        assert (
            await repository.find_execution_reservation(
                owner_id=other_owner_id,
                idempotency_digests=(_digest("owner-hidden"),),
            )
            is None
        )

        with pytest.raises(IdempotencyKeyReuseError):
            await repository.reserve_initial_execution(
                correlations[0].model_copy(
                    update={"thread_id": other_thread_id, "run_id": f"cross-target-{suffix}"}
                ),
                owner_id=owner_id,
                capacity=10,
                idempotency_digest=digest,
                request_fingerprint=fingerprint,
                payload=payload,
            )
        with pytest.raises(IdempotencyKeyReuseError):
            await repository.reserve_initial_execution(
                correlations[0].model_copy(update={"run_id": f"changed-{suffix}"}),
                owner_id=owner_id,
                capacity=10,
                idempotency_digest=digest,
                request_fingerprint=_digest(f"changed-request-{suffix}"),
                payload=payload,
            )
        with pytest.raises(ExecutionJobNotFoundError):
            await repository.get_execution_job(
                reservations[0].job.job_id,
                owner_id=other_owner_id,
            )

        second_digest = _digest(f"second-idempotency-{suffix}")
        await repository.reserve_initial_execution(
            RunExecutionCorrelation(
                thread_id=other_thread_id,
                run_id=f"run-second-{suffix}",
                request_id=f"request-second-{suffix}",
                attempt=1,
            ),
            owner_id=owner_id,
            capacity=10,
            idempotency_digest=second_digest,
            request_fingerprint=_digest(f"second-request-{suffix}"),
            payload=payload,
        )
        with pytest.raises(AgentPolicyViolationError) as lookup_error:
            await repository.find_execution_reservation(
                owner_id=owner_id,
                idempotency_digests=(digest, second_digest),
            )
        assert lookup_error.value.context == {"reason": "multiple_execution_reservations_matched"}

        independent = await repository.reserve_initial_execution(
            RunExecutionCorrelation(
                thread_id=other_owner_thread_id,
                run_id=f"run-other-owner-{suffix}",
                request_id=f"request-other-owner-{suffix}",
                attempt=1,
            ),
            owner_id=other_owner_id,
            capacity=10,
            idempotency_digest=digest,
            request_fingerprint=fingerprint,
            payload=payload,
        )
        assert independent.replayed is False
        assert independent.job.job_id != reservations[0].job.job_id

        rows = await _fetch_all(
            _DATABASE_URL,
            """
            SELECT owner_id, count(*) AS count
            FROM run_execution_jobs
            WHERE idempotency_digest = %s
            GROUP BY owner_id
            ORDER BY owner_id
            """,
            (digest,),
        )
        assert {str(row["owner_id"]): row["count"] for row in rows} == {
            owner_id: 1,
            other_owner_id: 1,
        }

        await _fail_claimable_execution_jobs(
            repository,
            worker_id=f"cleanup-reservations-{suffix}",
        )
        await repository.delete_thread(thread_id, owner_id=owner_id)
        await repository.delete_thread(other_thread_id, owner_id=owner_id)
        await repository.delete_thread(other_owner_thread_id, owner_id=other_owner_id)


@pytest.mark.asyncio
async def test_postgres_execution_lease_is_fenced_reclaimable_and_scrubbed() -> None:
    assert _DATABASE_URL is not None
    suffix = uuid4().hex
    owner_id = f"owner-{suffix}"
    thread_id = f"thread-{suffix}"
    sentinel = f"private-question-{suffix}"

    async with _repository(_DATABASE_URL) as repository:
        await repository.create_thread(_thread(thread_id, owner_id), capacity=10)
        reservation = await repository.reserve_initial_execution(
            RunExecutionCorrelation(
                thread_id=thread_id,
                run_id=f"run-{suffix}",
                request_id=f"request-{suffix}",
                attempt=1,
            ),
            owner_id=owner_id,
            capacity=10,
            idempotency_digest=_digest(f"idempotency-{suffix}"),
            request_fingerprint=_digest(f"request-{suffix}"),
            payload=StartExecutionPayload(question=sentinel),
        )
        first_claim_at = reservation.job.available_at + timedelta(milliseconds=1)
        first_registration = await repository.register_execution_worker(
            f"worker-1-{suffix}",
            heartbeat_ttl=timedelta(hours=1),
            now=first_claim_at,
        )
        contender_registration = await repository.register_execution_worker(
            f"worker-contender-{suffix}",
            heartbeat_ttl=timedelta(hours=1),
            now=first_claim_at,
        )
        concurrent_claims = await asyncio.gather(
            repository.claim_execution_job(
                registration=first_registration,
                lease_duration=timedelta(seconds=10),
                now=first_claim_at,
            ),
            repository.claim_execution_job(
                registration=contender_registration,
                lease_duration=timedelta(seconds=10),
                now=first_claim_at,
            ),
        )
        leased_claims = [claim for claim in concurrent_claims if claim is not None]
        assert len(leased_claims) == 1
        assert sum(claim is None for claim in concurrent_claims) == 1
        await repository.remove_execution_worker(first_registration)
        await repository.remove_execution_worker(contender_registration)
        first = leased_claims[0]
        assert first.generation == 1
        assert first.heartbeat_at == first_claim_at

        renewed_at = first_claim_at + timedelta(seconds=2)
        renewed = await repository.heartbeat_execution_job(
            first,
            lease_duration=timedelta(seconds=10),
            checkpoint_cursor=f"checkpoint-1-{suffix}",
            now=renewed_at,
        )
        assert renewed.heartbeat_at == renewed_at
        assert renewed.expires_at == renewed_at + timedelta(seconds=10)

        reclaimed = await _claim_execution_job(
            repository,
            worker_id=f"worker-2-{suffix}",
            lease_duration=timedelta(seconds=10),
            now=renewed.expires_at,
        )
        assert reclaimed is not None
        assert reclaimed.generation == 2
        assert reclaimed.checkpoint_cursor == f"checkpoint-1-{suffix}"
        with pytest.raises(ExecutionLeaseLostError):
            await repository.assert_execution_lease(first, now=renewed.expires_at)
        stale_mutation_at = reclaimed.heartbeat_at + timedelta(milliseconds=1)
        with pytest.raises(ExecutionLeaseLostError):
            await repository.heartbeat_execution_job(
                first,
                lease_duration=timedelta(seconds=10),
                now=stale_mutation_at,
            )
        with pytest.raises(ExecutionLeaseLostError):
            await repository.complete_execution_job(first, now=stale_mutation_at)

        released_at = reclaimed.heartbeat_at + timedelta(seconds=1)
        released = await repository.release_execution_job(
            reclaimed,
            available_at=first_claim_at,
            checkpoint_cursor=f"checkpoint-2-{suffix}",
            now=released_at,
        )
        assert released.state == ExecutionJobState.QUEUED
        assert released.available_at == released_at
        assert released.payload is not None

        final_lease = await _claim_execution_job(
            repository,
            worker_id=f"worker-3-{suffix}",
            lease_duration=timedelta(seconds=10),
            now=released.available_at,
        )
        assert final_lease is not None
        assert final_lease.generation == 3
        completed = await repository.complete_execution_job(
            final_lease,
            checkpoint_cursor=f"checkpoint-3-{suffix}",
            now=final_lease.heartbeat_at + timedelta(seconds=1),
        )
        assert completed.state == ExecutionJobState.COMPLETED
        assert completed.payload is None
        assert completed.claim_count == completed.lease_generation == 3
        assert completed.checkpoint_cursor == f"checkpoint-3-{suffix}"
        assert completed.lease_token is None
        assert completed.lease_owner is None

        stored = await _fetch_one(
            _DATABASE_URL,
            """
            SELECT payload, lease_token, lease_owner, failure_code
            FROM run_execution_jobs
            WHERE job_id = %s
            """,
            (completed.job_id,),
        )
        assert stored == {
            "payload": None,
            "lease_token": None,
            "lease_owner": None,
            "failure_code": None,
        }
        assert sentinel not in str(stored)

        failed_thread_id = f"thread-failed-{suffix}"
        await repository.create_thread(_thread(failed_thread_id, owner_id), capacity=10)
        failed_reservation = await repository.reserve_initial_execution(
            RunExecutionCorrelation(
                thread_id=failed_thread_id,
                run_id=f"run-failed-{suffix}",
                request_id=f"request-failed-{suffix}",
                attempt=1,
            ),
            owner_id=owner_id,
            capacity=10,
            idempotency_digest=_digest(f"failed-idempotency-{suffix}"),
            request_fingerprint=_digest(f"failed-request-{suffix}"),
            payload=StartExecutionPayload(question=f"failed-{sentinel}"),
        )
        failed_lease = await _claim_execution_job(
            repository,
            worker_id=f"worker-failed-{suffix}",
            lease_duration=timedelta(seconds=10),
            now=failed_reservation.job.available_at + timedelta(milliseconds=1),
        )
        assert failed_lease is not None
        failed = await repository.fail_execution_job(
            failed_lease,
            failure_code="WORKER_EXECUTION_FAILED",
            now=failed_lease.heartbeat_at + timedelta(seconds=1),
        )
        assert failed.state == ExecutionJobState.FAILED
        assert failed.payload is None
        assert failed.failure_code == "WORKER_EXECUTION_FAILED"

        await repository.delete_thread(thread_id, owner_id=owner_id)
        await repository.delete_thread(failed_thread_id, owner_id=owner_id)


@pytest.mark.asyncio
async def test_postgres_lease_bound_ledger_and_job_finalize_in_one_transaction() -> None:
    assert _DATABASE_URL is not None
    suffix = uuid4().hex
    owner_id = f"owner-{suffix}"
    thread_id = f"thread-{suffix}"
    correlation = RunExecutionCorrelation(
        thread_id=thread_id,
        run_id=f"run-{suffix}",
        request_id=f"request-{suffix}",
        attempt=1,
    )

    async with _repository(_DATABASE_URL) as repository:
        await repository.create_thread(_thread(thread_id, owner_id), capacity=10)
        reservation = await repository.reserve_initial_execution(
            correlation,
            owner_id=owner_id,
            capacity=10,
            idempotency_digest=_digest(f"atomic-key-{suffix}"),
            request_fingerprint=_digest(f"atomic-request-{suffix}"),
            payload=StartExecutionPayload(question="private question"),
        )
        lease = await _claim_execution_job(
            repository,
            worker_id=f"worker-{suffix}",
            lease_duration=timedelta(seconds=30),
            now=reservation.job.available_at + timedelta(milliseconds=1),
        )
        assert lease is not None
        with pytest.raises(ExecutionLeaseLostError):
            await repository.append_tool_started(
                correlation,
                owner_id=owner_id,
                execution_id=f"tool-stale-{suffix}",
                logical_call_id=f"logical-stale-{suffix}",
                attempt=1,
                tool=ToolCapability.RESOLVE_STATION,
                execution_lease=lease.model_copy(update={"token": "stale-token"}),
            )

        handle = await repository.append_tool_started(
            correlation,
            owner_id=owner_id,
            execution_id=f"tool-{suffix}",
            logical_call_id=f"logical-{suffix}",
            attempt=1,
            tool=ToolCapability.RESOLVE_STATION,
            execution_lease=lease,
        )
        await repository.append_tool_terminal(
            handle,
            owner_id=owner_id,
            transition=ToolTransition.COMPLETED,
            duration_ms=1,
            summary=ResolveStationToolSummary(
                matched=True,
                candidate_count=1,
                clarification_required=False,
            ),
            execution_lease=lease,
        )
        run = ConversationRun(
            run_id=correlation.run_id,
            thread_id=thread_id,
            status=GraphTurnStatus.COMPLETED,
            resume_mode=ResumeMode.NONE,
            outcome=AnswerOutcome.question_too_complex(),
        )
        assert (
            await repository.finalize_execution(
                run,
                correlation=correlation,
                owner_id=owner_id,
                execution_lease=lease,
                job_state=ExecutionJobState.COMPLETED,
                checkpoint_cursor="checkpoint:terminal",
            )
            == run
        )

        job = await repository.get_execution_job(reservation.job.job_id, owner_id=owner_id)
        assert job.state == ExecutionJobState.COMPLETED
        assert job.payload is None
        row = await _fetch_one(
            _DATABASE_URL,
            """
            SELECT payload, payload_ciphertext, payload_nonce, payload_key_fingerprint
            FROM run_execution_jobs
            WHERE job_id = %s
            """,
            (job.job_id,),
        )
        assert row == {
            "payload": None,
            "payload_ciphertext": None,
            "payload_nonce": None,
            "payload_key_fingerprint": None,
        }
        events = await repository.get_run_events(correlation.run_id, owner_id=owner_id)
        assert [event.event_type for event in events] == [
            RunEventType.RUN_STARTED,
            RunEventType.TOOL_STARTED,
            RunEventType.TOOL_COMPLETED,
            RunEventType.RUN_COMPLETED,
        ]
        await repository.delete_thread(thread_id, owner_id=owner_id)


@pytest.mark.asyncio
async def test_postgres_idempotent_tool_replay_rechecks_lease_at_transaction_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _DATABASE_URL is not None
    suffix = uuid4().hex
    owner_id = f"owner-recheck-{suffix}"
    thread_id = f"thread-recheck-{suffix}"
    correlation = RunExecutionCorrelation(
        thread_id=thread_id,
        run_id=f"run-recheck-{suffix}",
        request_id=f"request-recheck-{suffix}",
        attempt=1,
    )

    async with _repository(_DATABASE_URL) as repository:
        await repository.create_thread(_thread(thread_id, owner_id), capacity=10)
        await repository.reserve_initial_execution(
            correlation,
            owner_id=owner_id,
            capacity=10,
            idempotency_digest=_digest(f"recheck-key-{suffix}"),
            request_fingerprint=_digest(f"recheck-request-{suffix}"),
            payload=StartExecutionPayload(question="private"),
        )
        lease = await _claim_execution_job(
            repository,
            worker_id=f"worker-recheck-{suffix}",
            lease_duration=timedelta(seconds=10),
        )
        assert lease is not None
        handle = await repository.append_tool_started(
            correlation,
            owner_id=owner_id,
            execution_id=f"tool-recheck-{suffix}",
            logical_call_id=f"logical-recheck-{suffix}",
            attempt=1,
            tool=ToolCapability.RESOLVE_STATION,
            execution_lease=lease,
        )
        short_lease = await repository.heartbeat_execution_job(
            lease,
            lease_duration=timedelta(milliseconds=500),
        )
        original_recheck = repository._recheck_active_execution_lease
        recheck_calls = 0

        async def delayed_recheck(
            connection: AsyncConnection[object],
            checked_lease: ExecutionLease,
        ) -> None:
            nonlocal recheck_calls
            recheck_calls += 1
            await asyncio.sleep(0.7)
            await original_recheck(connection, checked_lease)

        monkeypatch.setattr(repository, "_recheck_active_execution_lease", delayed_recheck)
        with pytest.raises(ExecutionLeaseLostError):
            await repository.append_tool_started(
                correlation,
                owner_id=owner_id,
                execution_id=handle.execution_id,
                logical_call_id=handle.logical_call_id,
                attempt=handle.attempt,
                tool=handle.tool,
                execution_lease=short_lease,
            )
        assert recheck_calls == 1

        monkeypatch.setattr(
            repository,
            "_recheck_active_execution_lease",
            original_recheck,
        )
        reclaimed = await _claim_execution_job(
            repository,
            worker_id=f"worker-reclaimed-{suffix}",
            lease_duration=timedelta(seconds=10),
        )
        assert reclaimed is not None
        record = await repository.append_tool_terminal(
            handle,
            owner_id=owner_id,
            transition=ToolTransition.COMPLETED,
            duration_ms=1,
            summary=ResolveStationToolSummary(
                matched=True,
                candidate_count=1,
                clarification_required=False,
            ),
            execution_lease=reclaimed,
        )
        short_terminal_lease = await repository.heartbeat_execution_job(
            reclaimed,
            lease_duration=timedelta(milliseconds=500),
        )
        recheck_calls = 0
        monkeypatch.setattr(repository, "_recheck_active_execution_lease", delayed_recheck)
        with pytest.raises(ExecutionLeaseLostError):
            await repository.append_tool_terminal(
                handle,
                owner_id=owner_id,
                transition=record.transition,
                duration_ms=record.duration_ms,
                summary=record.summary,
                execution_lease=short_terminal_lease,
            )
        assert recheck_calls == 1

        monkeypatch.setattr(
            repository,
            "_recheck_active_execution_lease",
            original_recheck,
        )
        cleanup_lease = await _claim_execution_job(
            repository,
            worker_id=f"worker-cleanup-{suffix}",
            lease_duration=timedelta(seconds=10),
        )
        assert cleanup_lease is not None
        await repository.fail_execution_job(
            cleanup_lease,
            failure_code="TEST_CLEANUP",
        )
        await repository.delete_thread(thread_id, owner_id=owner_id)


@pytest.mark.asyncio
async def test_postgres_resume_reservation_replays_before_running_state_check() -> None:
    assert _DATABASE_URL is not None
    suffix = uuid4().hex
    owner_id = f"owner-{suffix}"
    thread_id = f"thread-{suffix}"
    initial = RunExecutionCorrelation(
        thread_id=thread_id,
        run_id=f"run-{suffix}",
        request_id=f"request-initial-{suffix}",
        attempt=1,
    )

    async with _repository(_DATABASE_URL) as repository:
        await repository.create_thread(_thread(thread_id, owner_id), capacity=10)
        initial_reservation = await repository.reserve_initial_execution(
            initial,
            owner_id=owner_id,
            capacity=10,
            idempotency_digest=_digest(f"initial-key-{suffix}"),
            request_fingerprint=_digest(f"initial-request-{suffix}"),
            payload=StartExecutionPayload(question="출발지에서 목적지까지 갈 수 있어?"),
        )
        initial_lease = await _claim_execution_job(
            repository,
            worker_id=f"worker-initial-{suffix}",
            lease_duration=timedelta(seconds=10),
            now=initial_reservation.job.available_at + timedelta(milliseconds=1),
        )
        assert initial_lease is not None
        await repository.finalize_run(
            _interrupted_run(initial.run_id, thread_id),
            correlation=initial,
            owner_id=owner_id,
        )
        await repository.complete_execution_job(
            initial_lease,
            now=initial_lease.heartbeat_at + timedelta(seconds=1),
        )

        digest = _digest(f"resume-key-{suffix}")
        fingerprint = _digest(f"resume-request-{suffix}")
        payload = ResumeNewTurnExecutionPayload(answer="엘리베이터가 필요해")
        worker_id = f"worker-resume-admission-{suffix}"
        registration = await repository.register_execution_worker(
            worker_id,
            heartbeat_ttl=timedelta(seconds=30),
        )
        reserved = await repository.reserve_resume_execution(
            initial.run_id,
            request_id=f"request-resume-{suffix}",
            owner_id=owner_id,
            idempotency_digest=digest,
            request_fingerprint=fingerprint,
            payload=payload,
            require_available_worker=True,
        )
        drained = await repository.drain_execution_worker(registration)
        replayed = await repository.reserve_resume_execution(
            initial.run_id,
            request_id=f"request-replayed-{suffix}",
            owner_id=owner_id,
            idempotency_digest=digest,
            request_fingerprint=fingerprint,
            payload=payload,
            require_available_worker=True,
        )
        assert replayed.replayed is True
        assert replayed.job == reserved.job

        with pytest.raises(IdempotencyKeyReuseError):
            await repository.reserve_resume_execution(
                initial.run_id,
                request_id=f"request-conflict-{suffix}",
                owner_id=owner_id,
                idempotency_digest=digest,
                request_fingerprint=_digest(f"other-resume-request-{suffix}"),
                payload=payload,
                require_available_worker=True,
            )
        with pytest.raises(ExecutionWorkerUnavailableError):
            await repository.reserve_resume_execution(
                initial.run_id,
                request_id=f"request-unavailable-{suffix}",
                owner_id=owner_id,
                idempotency_digest=_digest(f"unavailable-resume-key-{suffix}"),
                request_fingerprint=fingerprint,
                payload=payload,
                require_available_worker=True,
            )
        replacement = await repository.register_execution_worker(
            worker_id,
            heartbeat_ttl=timedelta(seconds=30),
        )
        with pytest.raises(AgentPolicyViolationError) as error_info:
            await repository.reserve_resume_execution(
                initial.run_id,
                request_id=f"request-second-{suffix}",
                owner_id=owner_id,
                idempotency_digest=_digest(f"other-resume-key-{suffix}"),
                request_fingerprint=fingerprint,
                payload=payload,
                require_available_worker=True,
            )
        assert error_info.value.context == {"reason": "run_not_interrupted"}

        events = await repository.get_run_events(initial.run_id, owner_id=owner_id)
        assert [event.event_type for event in events] == [
            RunEventType.RUN_STARTED,
            RunEventType.RUN_INTERRUPTED,
            RunEventType.RUN_RESUME_STARTED,
        ]
        assert reserved.correlation.attempt == 2
        await _fail_claimable_execution_jobs(
            repository,
            worker_id=f"cleanup-resume-{suffix}",
        )
        await repository.remove_execution_worker(replacement)
        with pytest.raises(ExecutionWorkerRegistrationLostError):
            await repository.remove_execution_worker(drained)
        await repository.delete_thread(thread_id, owner_id=owner_id)


def _thread(thread_id: str, owner_id: str) -> ConversationThread:
    return ConversationThread(
        thread_id=thread_id,
        owner_id=owner_id,
        flow_version="mobility-v2",
        state_schema_version="3",
    )


def _interrupted_run(run_id: str, thread_id: str) -> ConversationRun:
    return ConversationRun(
        run_id=run_id,
        thread_id=thread_id,
        status=GraphTurnStatus.INTERRUPTED,
        resume_mode=ResumeMode.NEW_TURN,
        outcome=AnswerOutcome(
            source=AnswerSource.AGENT,
            status=ResponseStatus.NEEDS_CLARIFICATION,
            intent=QuestionIntent.UNKNOWN,
            user_message="추가 정보가 필요합니다.",
            clarification_needed=True,
            questions=["이동 조건을 알려주세요."],
            evidence=AnswerEvidence(),
        ),
    )


def _digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


async def _fail_claimable_execution_jobs(
    repository: PostgresConversationRepository,
    *,
    worker_id: str,
) -> None:
    while lease := await _claim_execution_job(
        repository,
        worker_id=worker_id,
        lease_duration=timedelta(seconds=30),
    ):
        await repository.fail_execution_job(
            lease,
            failure_code="TEST_CLEANUP",
        )


def _assert_incompatible_payload_is_safe(
    error: AgentStateIncompatibleError,
    *,
    job_id: str,
    sentinel: str,
) -> None:
    assert error.context == {
        "reason": "execution_job_payload_incompatible",
        "job_id": job_id,
    }
    projected = project_agent_error(error, request_id="execution-payload-test")
    observable = f"{error!s}\n{error!r}\n{projected.response.model_dump(mode='json')!r}"
    assert sentinel not in observable


@asynccontextmanager
async def _repository(
    database_url: str,
    *,
    allow_test_clock: bool = True,
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
        name="mobility-agent-execution-queue-test",
    )
    await pool.open(wait=True, timeout=10)
    try:
        checkpointer = AsyncPostgresSaver(pool)
        await checkpointer.setup()
        repository = PostgresConversationRepository(
            pool,
            allow_test_clock=allow_test_clock,
            execution_payload_cipher=ExecutionPayloadCipher(b"q" * 32),
        )
        await repository.setup()
        yield repository
    finally:
        await pool.close()


async def _fetch_one(
    database_url: str,
    query: str,
    params: tuple[object, ...] = (),
) -> dict[str, object]:
    rows = await _fetch_all(database_url, query, params)
    assert len(rows) == 1
    return rows[0]


async def _fetch_all(
    database_url: str,
    query: str,
    params: tuple[object, ...] = (),
) -> list[dict[str, object]]:
    connection = await AsyncConnection.connect(database_url, row_factory=dict_row)
    try:
        async with connection.cursor() as cursor:
            await cursor.execute(query, params)
            return [dict(row) for row in await cursor.fetchall()]
    finally:
        await connection.close()


async def _execute(
    database_url: str,
    query: str,
    params: tuple[object, ...] = (),
) -> None:
    connection = await AsyncConnection.connect(database_url)
    try:
        async with connection.transaction():
            await connection.execute(query, params)
    finally:
        await connection.close()
