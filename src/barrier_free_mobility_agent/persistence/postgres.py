from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import uuid4

from psycopg import AsyncConnection, AsyncCursor, Error
from psycopg.errors import UniqueViolation
from psycopg.rows import DictRow, dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool, PoolClosed, PoolTimeout
from pydantic import ValidationError

from barrier_free_mobility_agent.application.conversation_repository import (
    ConversationThread,
)
from barrier_free_mobility_agent.domain.errors import (
    AgentCapacityExceededError,
    AgentPolicyViolationError,
    AgentStateIncompatibleError,
    ExecutionJobNotFoundError,
    ExecutionLeaseLostError,
    ExecutionWorkerRegistrationLostError,
    ExecutionWorkerUnavailableError,
    IdempotencyKeyReuseError,
    RunNotFoundError,
    StateInputBudgetExceededError,
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
    RUN_USAGE_SCHEMA_VERSION,
    RunUsage,
    RunUsageRevisionConflictError,
    reconcile_run_usage,
)
from barrier_free_mobility_agent.persistence.execution_payload_cipher import (
    EncryptedExecutionPayload,
    ExecutionPayloadCipher,
    ExecutionPayloadCipherError,
    execution_payload_associated_data,
)

if TYPE_CHECKING:
    from barrier_free_mobility_agent.application.conversations import ConversationRun

_MIGRATION_LOCK_ID = 6_344_739_711_031_137_987
_GLOBAL_CAPACITY_LOCK_ID = 7_009_651_431_252_004_131
_WORKER_REGISTRY_PRUNE_LOCK_ID = 6_971_843_205_771_104_219
_OWNER_CAPACITY_LOCK_SEED = 2_731_901
REPOSITORY_SCHEMA_VERSION = 9
RUN_PAYLOAD_SCHEMA_VERSION = 1
_LEGACY_INITIAL_REQUEST_ID = "legacy-unavailable"
_IDEMPOTENCY_LOCK_SEED = 5_079_719
_WORKER_REGISTRATION_LOCK_SEED = 8_041_283
_MAX_INCOMPATIBLE_JOBS_PER_CLAIM = 32
_INCOMPATIBLE_EXECUTION_FAILURE_CODE = "EXECUTION_PAYLOAD_INCOMPATIBLE"

_MIGRATION_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS agent_repository_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

_MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (
        1,
        (
            """
            CREATE TABLE agent_threads (
                thread_id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                active_run_id TEXT NULL,
                run_count INTEGER NOT NULL DEFAULT 0,
                flow_version TEXT NOT NULL,
                state_schema_version TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT agent_threads_thread_id_length
                    CHECK (char_length(thread_id) BETWEEN 1 AND 200),
                CONSTRAINT agent_threads_owner_id_length
                    CHECK (char_length(owner_id) BETWEEN 1 AND 200),
                CONSTRAINT agent_threads_active_run_id_length
                    CHECK (
                        active_run_id IS NULL
                        OR char_length(active_run_id) BETWEEN 1 AND 200
                    ),
                CONSTRAINT agent_threads_run_count_nonnegative CHECK (run_count >= 0),
                CONSTRAINT agent_threads_flow_version_length
                    CHECK (char_length(flow_version) BETWEEN 1 AND 100),
                CONSTRAINT agent_threads_state_schema_version_length
                    CHECK (char_length(state_schema_version) BETWEEN 1 AND 100)
            )
            """,
            """
            CREATE TABLE agent_runs (
                run_id TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL,
                status TEXT NOT NULL,
                resume_mode TEXT NOT NULL,
                outcome JSONB NOT NULL,
                context JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT agent_runs_run_id_length
                    CHECK (char_length(run_id) BETWEEN 1 AND 200),
                CONSTRAINT agent_runs_status
                    CHECK (status IN ('completed', 'interrupted', 'failed')),
                CONSTRAINT agent_runs_resume_mode
                    CHECK (resume_mode IN ('none', 'command', 'new_turn')),
                CONSTRAINT agent_runs_status_resume_mode
                    CHECK (
                        (status = 'interrupted' AND resume_mode IN ('command', 'new_turn'))
                        OR (status IN ('completed', 'failed') AND resume_mode = 'none')
                    ),
                CONSTRAINT agent_runs_outcome_object
                    CHECK (jsonb_typeof(outcome) = 'object'),
                CONSTRAINT agent_runs_context_object
                    CHECK (jsonb_typeof(context) = 'object'),
                CONSTRAINT agent_runs_thread_fk
                    FOREIGN KEY (thread_id)
                    REFERENCES agent_threads(thread_id)
                    ON DELETE CASCADE,
                CONSTRAINT agent_runs_thread_run_unique UNIQUE (thread_id, run_id)
            )
            """,
            """
            ALTER TABLE agent_threads
            ADD CONSTRAINT agent_threads_active_run_fk
            FOREIGN KEY (thread_id, active_run_id)
            REFERENCES agent_runs(thread_id, run_id)
            ON DELETE SET NULL (active_run_id)
            DEFERRABLE INITIALLY DEFERRED
            """,
            """
            CREATE INDEX agent_threads_owner_created_idx
            ON agent_threads(owner_id, created_at, thread_id)
            """,
            """
            CREATE INDEX agent_threads_active_idx
            ON agent_threads(owner_id, active_run_id)
            WHERE active_run_id IS NOT NULL
            """,
            """
            CREATE INDEX agent_runs_thread_created_idx
            ON agent_runs(thread_id, created_at, run_id)
            """,
        ),
    ),
    (
        2,
        (
            """
            CREATE TABLE agent_runtime_secrets (
                name TEXT PRIMARY KEY,
                fingerprint TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT agent_runtime_secrets_name_length
                    CHECK (char_length(name) BETWEEN 1 AND 100),
                CONSTRAINT agent_runtime_secrets_fingerprint_format
                    CHECK (fingerprint ~ '^[a-f0-9]{64}$')
            )
            """,
        ),
    ),
    (
        3,
        (
            """
            ALTER TABLE agent_runs
            ADD COLUMN payload_schema_version INTEGER NOT NULL DEFAULT 1,
            ADD CONSTRAINT agent_runs_payload_schema_version_positive
                CHECK (payload_schema_version >= 1)
            """,
        ),
    ),
    (
        4,
        (
            """
            ALTER TABLE agent_runs
            ADD COLUMN usage JSONB NULL
            """,
            """
            UPDATE agent_runs
            SET usage = '{
                "schema_version": "1",
                "revision": 0,
                "model_calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "cached_input_tokens": 0,
                "reasoning_output_tokens": 0,
                "context_reduction_count": 0,
                "reduction_reasons": [],
                "source": "none"
            }'::jsonb
            WHERE usage IS NULL
            """,
            """
            ALTER TABLE agent_runs
            ADD CONSTRAINT agent_runs_usage_object
                CHECK (jsonb_typeof(usage) = 'object')
            """,
            """
            ALTER TABLE agent_runs
            ALTER COLUMN usage SET NOT NULL
            """,
            """
            ALTER TABLE agent_runs
            ALTER COLUMN usage DROP DEFAULT
            """,
        ),
    ),
    (
        5,
        (
            """
            ALTER TABLE agent_runs
            ADD COLUMN execution_state TEXT NULL,
            ADD COLUMN initial_request_id TEXT NULL,
            ADD COLUMN execution_attempt INTEGER NOT NULL DEFAULT 0,
            ADD COLUMN next_event_sequence BIGINT NOT NULL DEFAULT 1
            """,
            """
            UPDATE agent_runs
            SET execution_state = status,
                initial_request_id = 'legacy-unavailable'
            WHERE execution_state IS NULL
            """,
            """
            ALTER TABLE agent_runs
            ALTER COLUMN execution_state SET NOT NULL,
            ALTER COLUMN initial_request_id SET NOT NULL,
            ALTER COLUMN status DROP NOT NULL,
            ALTER COLUMN resume_mode DROP NOT NULL,
            ALTER COLUMN outcome DROP NOT NULL,
            ALTER COLUMN context DROP NOT NULL,
            ALTER COLUMN usage DROP NOT NULL,
            ADD CONSTRAINT agent_runs_execution_state
                CHECK (execution_state IN ('running', 'interrupted', 'completed', 'failed')),
            ADD CONSTRAINT agent_runs_initial_request_id_format
                CHECK (initial_request_id ~ '^[A-Za-z0-9._-]{1,128}$'),
            ADD CONSTRAINT agent_runs_execution_attempt_nonnegative
                CHECK (execution_attempt >= 0),
            ADD CONSTRAINT agent_runs_next_event_sequence_positive
                CHECK (next_event_sequence >= 1),
            ADD CONSTRAINT agent_runs_projection_presence
                CHECK (
                    (
                        status IS NULL
                        AND resume_mode IS NULL
                        AND outcome IS NULL
                        AND context IS NULL
                        AND usage IS NULL
                    )
                    OR (
                        status IS NOT NULL
                        AND resume_mode IS NOT NULL
                        AND outcome IS NOT NULL
                        AND context IS NOT NULL
                        AND usage IS NOT NULL
                    )
                ),
            ADD CONSTRAINT agent_runs_execution_projection_consistency
                CHECK (
                    (
                        execution_state = 'running'
                        AND (status IS NULL OR status = 'interrupted')
                    )
                    OR (
                        execution_state IN ('interrupted', 'completed', 'failed')
                        AND status = execution_state
                    )
                )
            """,
            """
            CREATE TABLE run_events (
                event_id TEXT PRIMARY KEY,
                event_key TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                sequence BIGINT NOT NULL,
                request_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload_schema_version INTEGER NOT NULL DEFAULT 1,
                payload JSONB NOT NULL,
                occurred_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT run_events_event_id_length
                    CHECK (char_length(event_id) BETWEEN 1 AND 200),
                CONSTRAINT run_events_event_key_length
                    CHECK (char_length(event_key) BETWEEN 1 AND 200),
                CONSTRAINT run_events_request_id_format
                    CHECK (request_id ~ '^[A-Za-z0-9._-]{1,128}$'),
                CONSTRAINT run_events_sequence_positive CHECK (sequence >= 1),
                CONSTRAINT run_events_type
                    CHECK (event_type IN (
                        'run.started',
                        'run.resume_started',
                        'run.interrupted',
                        'run.completed',
                        'run.failed',
                        'run.orphaned',
                        'tool.started',
                        'tool.completed',
                        'tool.failed',
                        'tool.timeout'
                    )),
                CONSTRAINT run_events_payload_schema_version_positive
                    CHECK (payload_schema_version >= 1),
                CONSTRAINT run_events_payload_object
                    CHECK (jsonb_typeof(payload) = 'object'),
                CONSTRAINT run_events_payload_size
                    CHECK (octet_length(payload::text) <= 4096),
                CONSTRAINT run_events_run_fk
                    FOREIGN KEY (thread_id, run_id)
                    REFERENCES agent_runs(thread_id, run_id)
                    ON DELETE CASCADE,
                CONSTRAINT run_events_run_sequence_unique UNIQUE (run_id, sequence),
                CONSTRAINT run_events_run_event_key_unique UNIQUE (run_id, event_key)
            )
            """,
            """
            CREATE INDEX run_events_run_sequence_idx
            ON run_events(run_id, sequence)
            """,
            """
            CREATE UNIQUE INDEX run_events_tool_attempt_started_unique
            ON run_events(
                run_id,
                (payload ->> 'logical_call_id'),
                (payload ->> 'tool_attempt')
            )
            WHERE event_type = 'tool.started'
            """,
            """
            CREATE TABLE tool_executions (
                execution_id TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                request_id TEXT NOT NULL,
                logical_call_id TEXT NOT NULL,
                attempt INTEGER NOT NULL,
                tool_name TEXT NOT NULL,
                terminal_status TEXT NOT NULL,
                started_event_sequence BIGINT NOT NULL,
                terminal_event_sequence BIGINT NOT NULL,
                payload_schema_version INTEGER NOT NULL DEFAULT 1,
                payload JSONB NOT NULL,
                completed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT tool_executions_execution_id_length
                    CHECK (char_length(execution_id) BETWEEN 1 AND 200),
                CONSTRAINT tool_executions_request_id_format
                    CHECK (request_id ~ '^[A-Za-z0-9._-]{1,128}$'),
                CONSTRAINT tool_executions_logical_call_id_length
                    CHECK (char_length(logical_call_id) BETWEEN 1 AND 200),
                CONSTRAINT tool_executions_attempt_positive CHECK (attempt >= 1),
                CONSTRAINT tool_executions_tool_name
                    CHECK (tool_name IN (
                        'resolve_station',
                        'generate_accessibility_brief',
                        'answer_facility_question'
                    )),
                CONSTRAINT tool_executions_terminal_status
                    CHECK (terminal_status IN ('completed', 'failed', 'timeout')),
                CONSTRAINT tool_executions_sequence_order
                    CHECK (terminal_event_sequence > started_event_sequence),
                CONSTRAINT tool_executions_payload_schema_version_positive
                    CHECK (payload_schema_version >= 1),
                CONSTRAINT tool_executions_payload_object
                    CHECK (jsonb_typeof(payload) = 'object'),
                CONSTRAINT tool_executions_payload_size
                    CHECK (octet_length(payload::text) <= 4096),
                CONSTRAINT tool_executions_run_fk
                    FOREIGN KEY (thread_id, run_id)
                    REFERENCES agent_runs(thread_id, run_id)
                    ON DELETE CASCADE,
                CONSTRAINT tool_executions_started_event_fk
                    FOREIGN KEY (run_id, started_event_sequence)
                    REFERENCES run_events(run_id, sequence),
                CONSTRAINT tool_executions_terminal_event_fk
                    FOREIGN KEY (run_id, terminal_event_sequence)
                    REFERENCES run_events(run_id, sequence),
                CONSTRAINT tool_executions_logical_attempt_unique
                    UNIQUE (run_id, logical_call_id, attempt)
            )
            """,
            """
            CREATE INDEX tool_executions_run_completed_idx
            ON tool_executions(run_id, completed_at, execution_id)
            """,
            """
            CREATE FUNCTION reject_agent_ledger_update()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                RAISE EXCEPTION 'agent ledger rows are append-only';
            END;
            $$
            """,
            """
            CREATE TRIGGER run_events_reject_update
            BEFORE UPDATE ON run_events
            FOR EACH ROW EXECUTE FUNCTION reject_agent_ledger_update()
            """,
            """
            CREATE TRIGGER tool_executions_reject_update
            BEFORE UPDATE ON tool_executions
            FOR EACH ROW EXECUTE FUNCTION reject_agent_ledger_update()
            """,
        ),
    ),
    (
        6,
        (
            """
            ALTER TABLE agent_threads
            ADD CONSTRAINT agent_threads_thread_owner_unique
            UNIQUE (thread_id, owner_id)
            """,
            """
            CREATE TABLE run_execution_jobs (
                job_id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                request_id TEXT NOT NULL,
                execution_attempt INTEGER NOT NULL,
                operation TEXT NOT NULL,
                state TEXT NOT NULL,
                payload_schema_version INTEGER NOT NULL,
                idempotency_digest TEXT NOT NULL,
                request_fingerprint TEXT NOT NULL,
                payload JSONB NULL,
                checkpoint_cursor TEXT NULL,
                available_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                lease_token TEXT NULL,
                lease_owner TEXT NULL,
                lease_generation INTEGER NOT NULL DEFAULT 0,
                lease_expires_at TIMESTAMPTZ NULL,
                heartbeat_at TIMESTAMPTZ NULL,
                claim_count INTEGER NOT NULL DEFAULT 0,
                failure_code TEXT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMPTZ NULL,
                CONSTRAINT run_execution_jobs_job_id_length
                    CHECK (char_length(job_id) BETWEEN 1 AND 200),
                CONSTRAINT run_execution_jobs_owner_id_length
                    CHECK (char_length(owner_id) BETWEEN 1 AND 200),
                CONSTRAINT run_execution_jobs_request_id_format
                    CHECK (request_id ~ '^[A-Za-z0-9._-]{1,128}$'),
                CONSTRAINT run_execution_jobs_execution_attempt_positive
                    CHECK (execution_attempt >= 1),
                CONSTRAINT run_execution_jobs_operation
                    CHECK (operation IN ('start', 'resume_command', 'resume_new_turn')),
                CONSTRAINT run_execution_jobs_state
                    CHECK (state IN ('queued', 'leased', 'completed', 'failed')),
                CONSTRAINT run_execution_jobs_payload_schema_version
                    CHECK (payload_schema_version = 1),
                CONSTRAINT run_execution_jobs_idempotency_digest_format
                    CHECK (idempotency_digest ~ '^[a-f0-9]{64}$'),
                CONSTRAINT run_execution_jobs_request_fingerprint_format
                    CHECK (request_fingerprint ~ '^[a-f0-9]{64}$'),
                CONSTRAINT run_execution_jobs_payload_object
                    CHECK (payload IS NULL OR jsonb_typeof(payload) = 'object'),
                CONSTRAINT run_execution_jobs_payload_size
                    CHECK (payload IS NULL OR octet_length(payload::text) <= 131072),
                CONSTRAINT run_execution_jobs_payload_operation
                    CHECK (payload IS NULL OR payload ->> 'operation' = operation),
                CONSTRAINT run_execution_jobs_checkpoint_cursor_length
                    CHECK (
                        checkpoint_cursor IS NULL
                        OR char_length(checkpoint_cursor) BETWEEN 1 AND 200
                    ),
                CONSTRAINT run_execution_jobs_lease_token_length
                    CHECK (
                        lease_token IS NULL
                        OR char_length(lease_token) BETWEEN 1 AND 200
                    ),
                CONSTRAINT run_execution_jobs_lease_owner_length
                    CHECK (
                        lease_owner IS NULL
                        OR char_length(lease_owner) BETWEEN 1 AND 200
                    ),
                CONSTRAINT run_execution_jobs_lease_generation_nonnegative
                    CHECK (lease_generation >= 0),
                CONSTRAINT run_execution_jobs_claim_count_nonnegative
                    CHECK (claim_count >= 0),
                CONSTRAINT run_execution_jobs_claim_generation_match
                    CHECK (claim_count = lease_generation),
                CONSTRAINT run_execution_jobs_lease_window_positive
                    CHECK (
                        lease_expires_at IS NULL
                        OR heartbeat_at IS NULL
                        OR lease_expires_at > heartbeat_at
                    ),
                CONSTRAINT run_execution_jobs_failure_code_format
                    CHECK (
                        failure_code IS NULL
                        OR failure_code ~ '^[A-Z][A-Z0-9_]{0,99}$'
                    ),
                CONSTRAINT run_execution_jobs_timestamps_order
                    CHECK (
                        updated_at >= created_at
                        AND (completed_at IS NULL OR completed_at >= created_at)
                    ),
                CONSTRAINT run_execution_jobs_state_shape
                    CHECK (
                        (
                            state = 'queued'
                            AND payload IS NOT NULL
                            AND lease_token IS NULL
                            AND lease_owner IS NULL
                            AND lease_expires_at IS NULL
                            AND heartbeat_at IS NULL
                            AND completed_at IS NULL
                            AND failure_code IS NULL
                        )
                        OR (
                            state = 'leased'
                            AND payload IS NOT NULL
                            AND lease_token IS NOT NULL
                            AND lease_owner IS NOT NULL
                            AND lease_generation >= 1
                            AND lease_expires_at IS NOT NULL
                            AND heartbeat_at IS NOT NULL
                            AND completed_at IS NULL
                            AND failure_code IS NULL
                        )
                        OR (
                            state = 'completed'
                            AND payload IS NULL
                            AND lease_token IS NULL
                            AND lease_owner IS NULL
                            AND lease_expires_at IS NULL
                            AND heartbeat_at IS NULL
                            AND completed_at IS NOT NULL
                            AND failure_code IS NULL
                        )
                        OR (
                            state = 'failed'
                            AND payload IS NULL
                            AND lease_token IS NULL
                            AND lease_owner IS NULL
                            AND lease_expires_at IS NULL
                            AND heartbeat_at IS NULL
                            AND completed_at IS NOT NULL
                            AND failure_code IS NOT NULL
                        )
                    ),
                CONSTRAINT run_execution_jobs_run_fk
                    FOREIGN KEY (thread_id, run_id)
                    REFERENCES agent_runs(thread_id, run_id)
                    ON DELETE CASCADE,
                CONSTRAINT run_execution_jobs_thread_owner_fk
                    FOREIGN KEY (thread_id, owner_id)
                    REFERENCES agent_threads(thread_id, owner_id)
                    ON DELETE CASCADE,
                CONSTRAINT run_execution_jobs_owner_idempotency_unique
                    UNIQUE (owner_id, idempotency_digest),
                CONSTRAINT run_execution_jobs_run_attempt_unique
                    UNIQUE (run_id, execution_attempt)
            )
            """,
            """
            CREATE INDEX run_execution_jobs_queued_claim_idx
            ON run_execution_jobs(available_at, created_at, job_id)
            WHERE state = 'queued'
            """,
            """
            CREATE INDEX run_execution_jobs_expired_lease_idx
            ON run_execution_jobs(lease_expires_at, created_at, job_id)
            WHERE state = 'leased'
            """,
        ),
    ),
    (
        7,
        (
            """
            ALTER TABLE run_execution_jobs
                ADD COLUMN payload_ciphertext BYTEA NULL,
                ADD COLUMN payload_nonce BYTEA NULL,
                ADD COLUMN payload_key_fingerprint TEXT NULL,
                ADD COLUMN payload_encryption_version INTEGER NULL
            """,
            """
            UPDATE run_execution_jobs
            SET state = 'failed',
                payload = NULL,
                lease_token = NULL,
                lease_owner = NULL,
                lease_expires_at = NULL,
                heartbeat_at = NULL,
                failure_code = 'PAYLOAD_ENCRYPTION_MIGRATION_REQUIRED',
                completed_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE state IN ('queued', 'leased')
            """,
            """
            ALTER TABLE run_execution_jobs
            DROP CONSTRAINT run_execution_jobs_state_shape
            """,
            """
            ALTER TABLE run_execution_jobs
            ADD CONSTRAINT run_execution_jobs_plaintext_payload_forbidden
                CHECK (payload IS NULL),
            ADD CONSTRAINT run_execution_jobs_payload_ciphertext_size
                CHECK (
                    payload_ciphertext IS NULL
                    OR octet_length(payload_ciphertext) BETWEEN 17 AND 131072
                ),
            ADD CONSTRAINT run_execution_jobs_payload_nonce_size
                CHECK (payload_nonce IS NULL OR octet_length(payload_nonce) = 12),
            ADD CONSTRAINT run_execution_jobs_payload_key_fingerprint_format
                CHECK (
                    payload_key_fingerprint IS NULL
                    OR payload_key_fingerprint ~ '^[a-f0-9]{64}$'
                ),
            ADD CONSTRAINT run_execution_jobs_payload_encryption_version
                CHECK (
                    payload_encryption_version IS NULL
                    OR payload_encryption_version = 1
                ),
            ADD CONSTRAINT run_execution_jobs_encrypted_payload_shape
                CHECK (
                    (
                        state IN ('queued', 'leased')
                        AND payload_ciphertext IS NOT NULL
                        AND payload_nonce IS NOT NULL
                        AND payload_key_fingerprint IS NOT NULL
                        AND payload_encryption_version = 1
                    )
                    OR (
                        state IN ('completed', 'failed')
                        AND payload_ciphertext IS NULL
                        AND payload_nonce IS NULL
                        AND payload_key_fingerprint IS NULL
                        AND payload_encryption_version IS NULL
                    )
                ),
            ADD CONSTRAINT run_execution_jobs_state_shape
                CHECK (
                    (
                        state = 'queued'
                        AND lease_token IS NULL
                        AND lease_owner IS NULL
                        AND lease_expires_at IS NULL
                        AND heartbeat_at IS NULL
                        AND completed_at IS NULL
                        AND failure_code IS NULL
                    )
                    OR (
                        state = 'leased'
                        AND lease_token IS NOT NULL
                        AND lease_owner IS NOT NULL
                        AND lease_generation >= 1
                        AND lease_expires_at IS NOT NULL
                        AND heartbeat_at IS NOT NULL
                        AND completed_at IS NULL
                        AND failure_code IS NULL
                    )
                    OR (
                        state = 'completed'
                        AND lease_token IS NULL
                        AND lease_owner IS NULL
                        AND lease_expires_at IS NULL
                        AND heartbeat_at IS NULL
                        AND completed_at IS NOT NULL
                        AND failure_code IS NULL
                    )
                    OR (
                        state = 'failed'
                        AND lease_token IS NULL
                        AND lease_owner IS NULL
                        AND lease_expires_at IS NULL
                        AND heartbeat_at IS NULL
                        AND completed_at IS NOT NULL
                        AND failure_code IS NOT NULL
                    )
                )
            """,
        ),
    ),
    (
        8,
        (
            """
            ALTER TABLE agent_threads
                ADD COLUMN deletion_token TEXT NULL,
                ADD COLUMN deletion_requested_at TIMESTAMPTZ NULL,
                ADD CONSTRAINT agent_threads_deletion_token_length
                    CHECK (
                        deletion_token IS NULL
                        OR char_length(deletion_token) BETWEEN 1 AND 200
                    ),
                ADD CONSTRAINT agent_threads_deletion_shape
                    CHECK (
                        (deletion_token IS NULL AND deletion_requested_at IS NULL)
                        OR (deletion_token IS NOT NULL AND deletion_requested_at IS NOT NULL)
                    )
            """,
            """
            CREATE INDEX agent_threads_deletion_pending_idx
            ON agent_threads(deletion_requested_at, thread_id)
            WHERE deletion_token IS NOT NULL
            """,
        ),
    ),
    (
        9,
        (
            """
            CREATE TABLE execution_worker_registry (
                worker_id TEXT PRIMARY KEY,
                token TEXT NOT NULL UNIQUE,
                generation BIGINT NOT NULL,
                state TEXT NOT NULL,
                registered_at TIMESTAMPTZ NOT NULL,
                heartbeat_at TIMESTAMPTZ NOT NULL,
                expires_at TIMESTAMPTZ NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL,
                CONSTRAINT execution_worker_registry_worker_id_format
                    CHECK (worker_id ~ '^[A-Za-z0-9._:-]{1,200}$'),
                CONSTRAINT execution_worker_registry_token_format
                    CHECK (token ~ '^[A-Za-z0-9._:-]{1,200}$'),
                CONSTRAINT execution_worker_registry_generation_positive
                    CHECK (generation >= 1),
                CONSTRAINT execution_worker_registry_state
                    CHECK (state IN ('ready', 'draining')),
                CONSTRAINT execution_worker_registry_timestamp_order
                    CHECK (
                        heartbeat_at >= registered_at
                        AND updated_at >= registered_at
                    ),
                CONSTRAINT execution_worker_registry_state_shape
                    CHECK (expires_at > heartbeat_at)
            )
            """,
            """
            CREATE INDEX execution_worker_registry_ready_idx
            ON execution_worker_registry(expires_at, worker_id)
            WHERE state = 'ready'
            """,
        ),
    ),
)

_THREAD_COLUMNS = """
thread_id, owner_id, active_run_id, run_count, flow_version, state_schema_version
"""
_THREAD_SELECT_SQL = (
    f"SELECT {_THREAD_COLUMNS} FROM agent_threads WHERE thread_id = %s AND owner_id = %s"
)
_THREAD_SELECT_FOR_UPDATE_SQL = _THREAD_SELECT_SQL + " FOR UPDATE"

_EXECUTION_JOB_COLUMNS = """
job_id, owner_id, thread_id, run_id, request_id, execution_attempt, operation,
state, payload_schema_version, idempotency_digest, request_fingerprint, payload, checkpoint_cursor,
payload_ciphertext, payload_nonce, payload_key_fingerprint, payload_encryption_version,
available_at, lease_token, lease_owner, lease_generation, lease_expires_at,
heartbeat_at, claim_count, failure_code, created_at, updated_at, completed_at
"""

_EXECUTION_WORKER_COLUMNS = """
worker_id, token, generation, state, registered_at, heartbeat_at, expires_at
"""


class PostgresConversationRepository:
    """Durable metadata adapter backed by a caller-owned async connection pool.

    Metadata transitions and the execution queue lease substrate are transactional. The current
    graph path is not lease-bound yet, so deployments must keep one Agent replica until the worker
    runtime asserts the lease across checkpoint and Tool boundaries. Explicit queue clocks are a
    test-only facility; normal instances always compare leases against PostgreSQL server time.
    """

    def __init__(
        self,
        pool: AsyncConnectionPool[Any],
        *,
        ready_timeout_seconds: float = 2.0,
        allow_test_clock: bool = False,
        execution_payload_cipher: ExecutionPayloadCipher | None = None,
    ) -> None:
        if ready_timeout_seconds <= 0:
            raise ValueError("ready_timeout_seconds must be positive")
        self._pool = pool
        self._ready_timeout_seconds = ready_timeout_seconds
        self._allow_test_clock = allow_test_clock
        self._execution_payload_cipher = execution_payload_cipher

    @property
    def persistence(self) -> Literal["durable"]:
        return "durable"

    def _queue_clock_override(self, now: datetime | None) -> datetime | None:
        if now is not None and not self._allow_test_clock:
            raise ValueError("explicit execution queue clocks are test-only")
        return None if now is None else _explicit_execution_time(now)

    def _require_execution_payload_cipher(self) -> ExecutionPayloadCipher:
        if self._execution_payload_cipher is None:
            raise RuntimeError("PostgreSQL execution payload encryption is not configured.")
        return self._execution_payload_cipher

    async def setup(self) -> None:
        """Apply repository migrations under one transaction-scoped advisory lock."""

        async with (
            self._pool.connection() as connection,
            connection.transaction(),
        ):
            await connection.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (_MIGRATION_LOCK_ID,),
            )
            await connection.execute(_MIGRATION_TABLE_SQL)
            async with connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute("SELECT version FROM agent_repository_migrations")
                applied = {row["version"] for row in await cursor.fetchall()}
            for version, statements in _MIGRATIONS:
                if version in applied:
                    continue
                for statement in statements:
                    await connection.execute(statement)
                await connection.execute(
                    "INSERT INTO agent_repository_migrations (version) VALUES (%s)",
                    (version,),
                )

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
        async with (
            self._pool.connection() as connection,
            connection.transaction(),
        ):
            if total_capacity is not None:
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(%s)",
                    (_GLOBAL_CAPACITY_LOCK_ID,),
                )
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, %s))",
                (thread.owner_id, _OWNER_CAPACITY_LOCK_SEED),
            )
            async with connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    """
                    SELECT
                        count(*) AS total,
                        count(*) FILTER (WHERE owner_id = %s) AS owner_count
                    FROM agent_threads
                    """,
                    (thread.owner_id,),
                )
                row = await cursor.fetchone()
            if row is None:
                raise RuntimeError("thread capacity query returned no row")
            if total_capacity is not None and row["total"] >= total_capacity:
                raise AgentCapacityExceededError(
                    context={"resource": "threads_total", "limit": total_capacity}
                )
            if row["owner_count"] >= capacity:
                raise AgentCapacityExceededError(
                    context={"resource": "threads_per_owner", "limit": capacity}
                )
            try:
                await connection.execute(
                    """
                    INSERT INTO agent_threads (
                        thread_id,
                        owner_id,
                        active_run_id,
                        run_count,
                        flow_version,
                        state_schema_version
                    ) VALUES (%s, %s, NULL, 0, %s, %s)
                    """,
                    (
                        thread.thread_id,
                        thread.owner_id,
                        thread.flow_version,
                        thread.state_schema_version,
                    ),
                )
            except UniqueViolation as exc:
                raise AgentPolicyViolationError(context={"reason": "thread_id_conflict"}) from exc
        return thread.model_copy(deep=True)

    async def get_thread(
        self,
        thread_id: str,
        *,
        owner_id: str,
    ) -> ConversationThread:
        async with self._pool.connection() as connection:
            thread = await _select_thread(connection, thread_id, owner_id)
        if thread is None:
            raise ThreadNotFoundError(context={"thread_id": thread_id})
        return thread

    async def add_run(
        self,
        run: ConversationRun,
        *,
        owner_id: str,
        capacity: int,
    ) -> ConversationThread:
        _validate_capacity(capacity)
        async with (
            self._pool.connection() as connection,
            connection.transaction(),
        ):
            thread = await _select_thread(
                connection,
                run.thread_id,
                owner_id,
                for_update=True,
            )
            if thread is None:
                raise ThreadNotFoundError(context={"thread_id": run.thread_id})
            if thread.active_run_id is not None:
                raise ThreadBusyError(context={"thread_id": run.thread_id})
            if thread.run_count >= capacity:
                raise AgentCapacityExceededError(
                    context={"resource": "runs_per_thread", "limit": capacity}
                )
            try:
                await connection.execute(
                    """
                    INSERT INTO agent_runs (
                        run_id,
                        thread_id,
                        status,
                        resume_mode,
                        execution_state,
                        initial_request_id,
                        execution_attempt,
                        next_event_sequence,
                        payload_schema_version,
                        outcome,
                        context,
                        usage
                    ) VALUES (%s, %s, %s, %s, %s, %s, 0, 1, %s, %s, %s, %s)
                    """,
                    (
                        run.run_id,
                        run.thread_id,
                        run.status.value,
                        run.resume_mode.value,
                        run.status.value,
                        _LEGACY_INITIAL_REQUEST_ID,
                        RUN_PAYLOAD_SCHEMA_VERSION,
                        Jsonb(run.outcome.model_dump(mode="json")),
                        Jsonb(run.context.model_dump(mode="json")),
                        Jsonb(run.usage.model_dump(mode="json")),
                    ),
                )
            except UniqueViolation as exc:
                raise AgentPolicyViolationError(context={"reason": "run_id_conflict"}) from exc
            active_run_id = run.run_id if _is_resumable(run) else None
            await connection.execute(
                """
                UPDATE agent_threads
                SET active_run_id = %s,
                    run_count = run_count + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE thread_id = %s
                """,
                (active_run_id, run.thread_id),
            )
            return thread.model_copy(
                update={
                    "active_run_id": active_run_id,
                    "run_count": thread.run_count + 1,
                }
            )

    async def update_run(
        self,
        run: ConversationRun,
        *,
        owner_id: str,
    ) -> ConversationRun:
        async with (
            self._pool.connection() as connection,
            connection.transaction(),
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            await cursor.execute(
                """
                SELECT
                    r.thread_id,
                    r.usage,
                    r.initial_request_id,
                    r.execution_attempt,
                    EXISTS (
                        SELECT 1
                        FROM run_events AS e
                        WHERE e.run_id = r.run_id
                    ) AS has_ledger_events,
                    t.active_run_id
                FROM agent_runs AS r
                JOIN agent_threads AS t ON t.thread_id = r.thread_id
                WHERE r.run_id = %s AND t.owner_id = %s
                FOR UPDATE OF r, t
                """,
                (run.run_id, owner_id),
            )
            row = await cursor.fetchone()
            if row is None:
                raise RunNotFoundError(context={"run_id": run.run_id})
            if (
                row["initial_request_id"] != _LEGACY_INITIAL_REQUEST_ID
                or row["execution_attempt"] != 0
                or row["has_ledger_events"]
            ):
                raise AgentPolicyViolationError(context={"reason": "run_ledger_finalize_required"})
            if run.thread_id != row["thread_id"]:
                raise AgentPolicyViolationError(context={"reason": "run_thread_change_forbidden"})
            stored_usage = _usage_from_payload(row["usage"])
            try:
                selected_usage = reconcile_run_usage(stored_usage, run.usage)
            except RunUsageRevisionConflictError as exc:
                raise AgentPolicyViolationError(
                    context={"reason": "run_usage_revision_conflict"}
                ) from exc
            active_run_id = row["active_run_id"]
            if _is_resumable(run):
                if active_run_id not in {None, run.run_id}:
                    raise ThreadBusyError(context={"thread_id": run.thread_id})
                active_run_id = run.run_id
            elif active_run_id == run.run_id:
                active_run_id = None
            await connection.execute(
                """
                UPDATE agent_runs
                SET status = %s,
                    resume_mode = %s,
                    execution_state = %s,
                    outcome = %s,
                    context = %s,
                    usage = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE run_id = %s
                """,
                (
                    run.status.value,
                    run.resume_mode.value,
                    run.status.value,
                    Jsonb(run.outcome.model_dump(mode="json")),
                    Jsonb(run.context.model_dump(mode="json")),
                    Jsonb(selected_usage.model_dump(mode="json")),
                    run.run_id,
                ),
            )
            await connection.execute(
                """
                UPDATE agent_threads
                SET active_run_id = %s, updated_at = CURRENT_TIMESTAMP
                WHERE thread_id = %s
                """,
                (active_run_id, run.thread_id),
            )
        return run.model_copy(update={"usage": selected_usage}, deep=True)

    async def reserve_initial_run(
        self,
        correlation: RunExecutionCorrelation,
        *,
        owner_id: str,
        capacity: int,
    ) -> RunEventRecord:
        _validate_capacity(capacity)
        async with (
            self._pool.connection() as connection,
            connection.transaction(),
        ):
            thread = await _select_thread(
                connection,
                correlation.thread_id,
                owner_id,
                for_update=True,
            )
            if thread is None:
                raise ThreadNotFoundError(context={"thread_id": correlation.thread_id})
            await _ensure_thread_accepts_execution(connection, correlation.thread_id)
            if correlation.attempt != 1:
                raise AgentPolicyViolationError(
                    context={"reason": "initial_run_attempt_must_be_one"}
                )
            if thread.active_run_id is not None:
                raise ThreadBusyError(context={"thread_id": correlation.thread_id})
            if thread.run_count >= capacity:
                raise AgentCapacityExceededError(
                    context={"resource": "runs_per_thread", "limit": capacity}
                )
            event = _new_run_event(
                correlation,
                sequence=1,
                phase=RunExecutionPhase.RUNNING,
                event_type=RunEventType.RUN_STARTED,
            )
            try:
                await connection.execute(
                    """
                    INSERT INTO agent_runs (
                        run_id,
                        thread_id,
                        status,
                        resume_mode,
                        execution_state,
                        initial_request_id,
                        execution_attempt,
                        next_event_sequence,
                        payload_schema_version,
                        outcome,
                        context,
                        usage
                    ) VALUES (%s, %s, NULL, NULL, 'running', %s, 1, 2, %s, NULL, NULL, NULL)
                    """,
                    (
                        correlation.run_id,
                        correlation.thread_id,
                        correlation.request_id,
                        RUN_PAYLOAD_SCHEMA_VERSION,
                    ),
                )
                await _insert_run_event(connection, event)
            except UniqueViolation as exc:
                raise AgentPolicyViolationError(context={"reason": "run_id_conflict"}) from exc
            await connection.execute(
                """
                UPDATE agent_threads
                SET active_run_id = %s,
                    run_count = run_count + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE thread_id = %s
                """,
                (correlation.run_id, correlation.thread_id),
            )
        return event

    async def reserve_resume_attempt(
        self,
        run_id: str,
        *,
        request_id: str,
        owner_id: str,
    ) -> RunEventRecord:
        async with (
            self._pool.connection() as connection,
            connection.transaction(),
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            await cursor.execute(
                """
                SELECT
                    r.thread_id,
                    r.execution_state,
                    r.execution_attempt,
                    r.next_event_sequence,
                    t.active_run_id,
                    t.deletion_token
                FROM agent_runs AS r
                JOIN agent_threads AS t ON t.thread_id = r.thread_id
                WHERE r.run_id = %s AND t.owner_id = %s
                FOR UPDATE OF r, t
                """,
                (run_id, owner_id),
            )
            row = await cursor.fetchone()
            if row is None:
                raise RunNotFoundError(context={"run_id": run_id})
            if row["deletion_token"] is not None:
                raise ThreadBusyError(
                    context={
                        "thread_id": row["thread_id"],
                        "reason": "thread_deletion_prepared",
                    }
                )
            if row["execution_state"] != RunExecutionPhase.INTERRUPTED.value:
                raise AgentPolicyViolationError(context={"reason": "run_not_interrupted"})
            if row["active_run_id"] != run_id:
                raise AgentPolicyViolationError(context={"reason": "run_not_active"})
            attempt = row["execution_attempt"] + 1
            correlation = RunExecutionCorrelation(
                thread_id=row["thread_id"],
                run_id=run_id,
                request_id=request_id,
                attempt=attempt,
            )
            sequence = row["next_event_sequence"]
            event = _new_run_event(
                correlation,
                sequence=sequence,
                phase=RunExecutionPhase.RUNNING,
                event_type=RunEventType.RUN_RESUME_STARTED,
            )
            await connection.execute(
                """
                UPDATE agent_runs
                SET execution_state = 'running',
                    execution_attempt = %s,
                    next_event_sequence = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE run_id = %s
                """,
                (attempt, sequence + 1, run_id),
            )
            await _insert_run_event(connection, event)
        return event

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
        _validate_sha256_digest(idempotency_digest, field="idempotency_digest")
        _validate_sha256_digest(request_fingerprint, field="request_fingerprint")
        async with (
            self._pool.connection() as connection,
            connection.transaction(),
        ):
            await _lock_idempotency_key(
                connection,
                owner_id=owner_id,
                idempotency_digest=idempotency_digest,
            )
            thread = await _select_thread(
                connection,
                correlation.thread_id,
                owner_id,
                for_update=True,
            )
            if thread is None:
                raise ThreadNotFoundError(context={"thread_id": correlation.thread_id})
            await _ensure_thread_accepts_execution(connection, correlation.thread_id)
            existing = await _select_execution_job_by_idempotency(
                connection,
                owner_id=owner_id,
                idempotency_digest=idempotency_digest,
                payload_cipher=self._execution_payload_cipher,
            )
            if existing is not None:
                _validate_idempotency_replay(
                    existing,
                    request_fingerprint=request_fingerprint,
                    operation=payload.operation,
                    thread_id=correlation.thread_id,
                )
                return ExecutionReservation(job=existing, replayed=True)
            if require_available_worker:
                await _lock_available_execution_worker(connection)

            if correlation.attempt != 1:
                raise AgentPolicyViolationError(
                    context={"reason": "initial_run_attempt_must_be_one"}
                )
            if thread.active_run_id is not None:
                raise ThreadBusyError(context={"thread_id": correlation.thread_id})
            if thread.run_count >= capacity:
                raise AgentCapacityExceededError(
                    context={"resource": "runs_per_thread", "limit": capacity}
                )

            event = _new_run_event(
                correlation,
                sequence=1,
                phase=RunExecutionPhase.RUNNING,
                event_type=RunEventType.RUN_STARTED,
            )
            try:
                await connection.execute(
                    """
                    INSERT INTO agent_runs (
                        run_id,
                        thread_id,
                        status,
                        resume_mode,
                        execution_state,
                        initial_request_id,
                        execution_attempt,
                        next_event_sequence,
                        payload_schema_version,
                        outcome,
                        context,
                        usage
                    ) VALUES (%s, %s, NULL, NULL, 'running', %s, 1, 2, %s, NULL, NULL, NULL)
                    """,
                    (
                        correlation.run_id,
                        correlation.thread_id,
                        correlation.request_id,
                        RUN_PAYLOAD_SCHEMA_VERSION,
                    ),
                )
                await _insert_run_event(connection, event)
                job = await _insert_execution_job(
                    connection,
                    payload_cipher=self._require_execution_payload_cipher(),
                    owner_id=owner_id,
                    correlation=correlation,
                    operation=payload.operation,
                    idempotency_digest=idempotency_digest,
                    request_fingerprint=request_fingerprint,
                    payload=payload,
                )
            except UniqueViolation as exc:
                raise AgentPolicyViolationError(context={"reason": "run_id_conflict"}) from exc
            await connection.execute(
                """
                UPDATE agent_threads
                SET active_run_id = %s,
                    run_count = run_count + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE thread_id = %s
                """,
                (correlation.run_id, correlation.thread_id),
            )
        return ExecutionReservation(job=job, replayed=False)

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
        _validate_sha256_digest(idempotency_digest, field="idempotency_digest")
        _validate_sha256_digest(request_fingerprint, field="request_fingerprint")
        async with (
            self._pool.connection() as connection,
            connection.transaction(),
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            await _lock_idempotency_key(
                connection,
                owner_id=owner_id,
                idempotency_digest=idempotency_digest,
            )
            await cursor.execute(
                """
                SELECT r.thread_id, t.deletion_token
                FROM agent_runs AS r
                JOIN agent_threads AS t ON t.thread_id = r.thread_id
                WHERE r.run_id = %s AND t.owner_id = %s
                FOR UPDATE OF r, t
                """,
                (run_id, owner_id),
            )
            deletion_scope = await cursor.fetchone()
            if deletion_scope is None:
                raise RunNotFoundError(context={"run_id": run_id})
            if deletion_scope["deletion_token"] is not None:
                raise ThreadBusyError(
                    context={
                        "thread_id": deletion_scope["thread_id"],
                        "reason": "thread_deletion_prepared",
                    }
                )
            existing = await _select_execution_job_by_idempotency(
                connection,
                owner_id=owner_id,
                idempotency_digest=idempotency_digest,
                payload_cipher=self._execution_payload_cipher,
            )
            if existing is not None:
                _validate_idempotency_replay(
                    existing,
                    request_fingerprint=request_fingerprint,
                    operation=payload.operation,
                    run_id=run_id,
                )
                return ExecutionReservation(job=existing, replayed=True)
            if require_available_worker:
                await _lock_available_execution_worker(connection)

            await cursor.execute(
                """
                SELECT
                    r.thread_id,
                    r.execution_state,
                    r.execution_attempt,
                    r.next_event_sequence,
                    r.resume_mode,
                    t.active_run_id
                FROM agent_runs AS r
                JOIN agent_threads AS t ON t.thread_id = r.thread_id
                WHERE r.run_id = %s AND t.owner_id = %s
                FOR UPDATE OF r, t
                """,
                (run_id, owner_id),
            )
            row = await cursor.fetchone()
            if row is None:
                raise RunNotFoundError(context={"run_id": run_id})
            if (
                row["execution_state"] != RunExecutionPhase.INTERRUPTED.value
                or row["active_run_id"] != run_id
            ):
                raise AgentPolicyViolationError(context={"reason": "run_not_interrupted"})
            expected_resume_mode = {
                ExecutionOperation.RESUME_COMMAND: "command",
                ExecutionOperation.RESUME_NEW_TURN: "new_turn",
            }[payload.operation]
            if row["resume_mode"] != expected_resume_mode:
                raise AgentPolicyViolationError(
                    context={"reason": "execution_operation_resume_mode_mismatch"}
                )

            attempt = row["execution_attempt"] + 1
            correlation = RunExecutionCorrelation(
                thread_id=row["thread_id"],
                run_id=run_id,
                request_id=request_id,
                attempt=attempt,
            )
            sequence = row["next_event_sequence"]
            event = _new_run_event(
                correlation,
                sequence=sequence,
                phase=RunExecutionPhase.RUNNING,
                event_type=RunEventType.RUN_RESUME_STARTED,
            )
            await connection.execute(
                """
                UPDATE agent_runs
                SET execution_state = 'running',
                    execution_attempt = %s,
                    next_event_sequence = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE run_id = %s
                """,
                (attempt, sequence + 1, run_id),
            )
            await _insert_run_event(connection, event)
            try:
                job = await _insert_execution_job(
                    connection,
                    payload_cipher=self._require_execution_payload_cipher(),
                    owner_id=owner_id,
                    correlation=correlation,
                    operation=payload.operation,
                    idempotency_digest=idempotency_digest,
                    request_fingerprint=request_fingerprint,
                    payload=payload,
                )
            except UniqueViolation as exc:
                raise AgentPolicyViolationError(
                    context={"reason": "execution_job_conflict"}
                ) from exc
        return ExecutionReservation(job=job, replayed=False)

    async def find_execution_reservation(
        self,
        *,
        owner_id: str,
        idempotency_digests: tuple[str, ...],
    ) -> ExecutionReservation | None:
        digests = _validate_reservation_lookup_digests(idempotency_digests)
        async with (
            self._pool.connection() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            await cursor.execute(
                f"""
                SELECT {_EXECUTION_JOB_COLUMNS}
                FROM run_execution_jobs
                WHERE owner_id = %s
                  AND idempotency_digest = ANY(%s::text[])
                LIMIT 2
                """,
                (owner_id, list(digests)),
            )
            rows = await cursor.fetchall()
        if len(rows) > 1:
            raise AgentPolicyViolationError(
                context={"reason": "multiple_execution_reservations_matched"}
            )
        if not rows:
            return None
        job = _execution_job_from_row(
            rows[0],
            payload_cipher=self._execution_payload_cipher,
        )
        return ExecutionReservation(job=job, replayed=True)

    async def register_execution_worker(
        self,
        worker_id: str,
        *,
        heartbeat_ttl: timedelta,
        now: datetime | None = None,
    ) -> ExecutionWorkerRegistration:
        clock_override = self._queue_clock_override(now)
        _validate_execution_identifier(worker_id, field="worker_id")
        _validate_positive_duration(heartbeat_ttl, field="heartbeat_ttl")
        token = uuid4().hex
        async with (
            self._pool.connection() as connection,
            connection.transaction(),
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            # Registration is rare. Serializing the prune+upsert transaction avoids
            # cross-worker deadlocks when two expired identities restart together.
            await cursor.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (_WORKER_REGISTRY_PRUNE_LOCK_ID,),
            )
            await cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, %s))",
                (worker_id, _WORKER_REGISTRATION_LOCK_SEED),
            )
            await cursor.execute(
                f"""
                WITH execution_clock AS MATERIALIZED (
                    SELECT COALESCE(%s::timestamptz, clock_timestamp()) AS effective_now
                ),
                pruned_workers AS MATERIALIZED (
                    DELETE FROM execution_worker_registry AS stale_worker
                    USING execution_clock
                    WHERE stale_worker.worker_id <> %s
                      AND stale_worker.expires_at <= execution_clock.effective_now
                    RETURNING stale_worker.worker_id
                )
                INSERT INTO execution_worker_registry (
                    worker_id,
                    token,
                    generation,
                    state,
                    registered_at,
                    heartbeat_at,
                    expires_at,
                    updated_at
                )
                SELECT
                    %s,
                    %s,
                    1,
                    'ready',
                    execution_clock.effective_now,
                    execution_clock.effective_now,
                    execution_clock.effective_now + %s::interval,
                    execution_clock.effective_now
                FROM execution_clock
                ON CONFLICT (worker_id) DO UPDATE
                SET token = EXCLUDED.token,
                    generation = execution_worker_registry.generation + 1,
                    state = 'ready',
                    registered_at = EXCLUDED.registered_at,
                    heartbeat_at = EXCLUDED.heartbeat_at,
                    expires_at = EXCLUDED.expires_at,
                    updated_at = EXCLUDED.updated_at
                RETURNING {_EXECUTION_WORKER_COLUMNS}
                """,
                (clock_override, worker_id, worker_id, token, heartbeat_ttl),
            )
            row = await cursor.fetchone()
        if row is None:  # pragma: no cover - INSERT/UPDATE always returns one row
            raise RuntimeError("execution worker registration did not return a row")
        return _execution_worker_registration_from_row(row)

    async def heartbeat_execution_worker(
        self,
        registration: ExecutionWorkerRegistration,
        *,
        heartbeat_ttl: timedelta,
        now: datetime | None = None,
    ) -> ExecutionWorkerRegistration:
        clock_override = self._queue_clock_override(now)
        _validate_positive_duration(heartbeat_ttl, field="heartbeat_ttl")
        async with (
            self._pool.connection() as connection,
            connection.transaction(),
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            await cursor.execute(
                """
                WITH locked_worker AS MATERIALIZED (
                    SELECT worker_id
                    FROM execution_worker_registry
                    WHERE worker_id = %s
                      AND token = %s
                      AND generation = %s
                      AND state = 'ready'
                    FOR UPDATE
                ),
                execution_clock AS MATERIALIZED (
                    SELECT COALESCE(%s::timestamptz, clock_timestamp()) AS effective_now
                    FROM locked_worker
                )
                UPDATE execution_worker_registry AS worker
                SET heartbeat_at = execution_clock.effective_now,
                    expires_at = execution_clock.effective_now + %s::interval,
                    updated_at = execution_clock.effective_now
                FROM locked_worker, execution_clock
                WHERE worker.worker_id = locked_worker.worker_id
                RETURNING
                    worker.worker_id,
                    worker.token,
                    worker.generation,
                    worker.state,
                    worker.registered_at,
                    worker.heartbeat_at,
                    worker.expires_at
                """,
                (
                    registration.worker_id,
                    registration.token,
                    registration.generation,
                    clock_override,
                    heartbeat_ttl,
                ),
            )
            row = await cursor.fetchone()
        if row is None:
            raise ExecutionWorkerRegistrationLostError()
        return _execution_worker_registration_from_row(row)

    async def drain_execution_worker(
        self,
        registration: ExecutionWorkerRegistration,
        *,
        now: datetime | None = None,
    ) -> ExecutionWorkerRegistration:
        clock_override = self._queue_clock_override(now)
        async with (
            self._pool.connection() as connection,
            connection.transaction(),
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            await cursor.execute(
                """
                WITH locked_worker AS MATERIALIZED (
                    SELECT worker_id
                    FROM execution_worker_registry
                    WHERE worker_id = %s
                      AND token = %s
                      AND generation = %s
                    FOR UPDATE
                ),
                execution_clock AS MATERIALIZED (
                    SELECT COALESCE(%s::timestamptz, clock_timestamp()) AS effective_now
                    FROM locked_worker
                )
                UPDATE execution_worker_registry AS worker
                SET state = 'draining',
                    updated_at = execution_clock.effective_now
                FROM locked_worker, execution_clock
                WHERE worker.worker_id = locked_worker.worker_id
                RETURNING
                    worker.worker_id,
                    worker.token,
                    worker.generation,
                    worker.state,
                    worker.registered_at,
                    worker.heartbeat_at,
                    worker.expires_at
                """,
                (
                    registration.worker_id,
                    registration.token,
                    registration.generation,
                    clock_override,
                ),
            )
            row = await cursor.fetchone()
        if row is None:
            raise ExecutionWorkerRegistrationLostError()
        return _execution_worker_registration_from_row(row)

    async def remove_execution_worker(
        self,
        registration: ExecutionWorkerRegistration,
    ) -> None:
        async with (
            self._pool.connection() as connection,
            connection.transaction(),
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            await cursor.execute(
                """
                DELETE FROM execution_worker_registry
                WHERE worker_id = %s
                  AND token = %s
                  AND generation = %s
                RETURNING worker_id
                """,
                (
                    registration.worker_id,
                    registration.token,
                    registration.generation,
                ),
            )
            row = await cursor.fetchone()
        if row is None:
            raise ExecutionWorkerRegistrationLostError()

    async def is_execution_worker_ready(self, worker_id: str) -> bool:
        _validate_execution_identifier(worker_id, field="worker_id")
        try:
            async with asyncio.timeout(self._ready_timeout_seconds):
                async with (
                    self._pool.connection(timeout=self._ready_timeout_seconds) as connection,
                    connection.cursor(row_factory=dict_row) as cursor,
                ):
                    await cursor.execute(
                        """
                        WITH execution_clock AS MATERIALIZED (
                            SELECT clock_timestamp() AS effective_now
                        )
                        SELECT EXISTS (
                            SELECT 1
                            FROM execution_worker_registry AS worker
                            CROSS JOIN execution_clock
                            WHERE worker.worker_id = %s
                              AND worker.state = 'ready'
                              AND worker.expires_at > execution_clock.effective_now
                        ) AS ready
                        """,
                        (worker_id,),
                    )
                    row = await cursor.fetchone()
        except (TimeoutError, Error, PoolClosed, PoolTimeout):
            return False
        return row is not None and row["ready"] is True

    async def get_execution_queue_health(
        self,
        *,
        stuck_after: timedelta,
        now: datetime | None = None,
    ) -> ExecutionQueueHealth:
        clock_override = self._queue_clock_override(now)
        _validate_positive_duration(stuck_after, field="stuck_after")
        async with (
            self._pool.connection() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            await cursor.execute(
                """
                WITH execution_clock AS MATERIALIZED (
                    SELECT COALESCE(%s::timestamptz, clock_timestamp()) AS effective_now
                ),
                worker_health AS MATERIALIZED (
                    SELECT count(*) FILTER (
                        WHERE worker.state = 'ready'
                          AND worker.expires_at > execution_clock.effective_now
                    ) AS ready_worker_count
                    FROM execution_worker_registry AS worker
                    CROSS JOIN execution_clock
                ),
                job_health AS MATERIALIZED (
                    SELECT
                        count(*) FILTER (WHERE job.state = 'queued') AS queued_job_count,
                        count(*) FILTER (WHERE job.state = 'leased') AS leased_job_count,
                        count(*) FILTER (
                            WHERE job.state = 'leased'
                              AND job.lease_expires_at <= execution_clock.effective_now
                        ) AS expired_lease_count,
                        COALESCE(
                            sum(GREATEST(job.lease_generation - 1, 0)) FILTER (
                                WHERE job.state IN ('queued', 'leased')
                            ),
                            0
                        ) AS recovery_attempt_count,
                        count(*) FILTER (
                            WHERE (
                                job.state = 'queued'
                                AND job.available_at
                                    <= execution_clock.effective_now - %s::interval
                            ) OR (
                                job.state = 'leased'
                                AND job.lease_expires_at
                                    <= execution_clock.effective_now - %s::interval
                            )
                        ) AS stuck_job_count,
                        min(job.created_at) FILTER (
                            WHERE job.state = 'queued'
                        ) AS oldest_queued_at
                    FROM run_execution_jobs AS job
                    CROSS JOIN execution_clock
                    WHERE job.state IN ('queued', 'leased')
                )
                SELECT
                    execution_clock.effective_now AS checked_at,
                    worker_health.ready_worker_count > 0 AS accepting_new_jobs,
                    worker_health.ready_worker_count,
                    job_health.queued_job_count,
                    job_health.leased_job_count,
                    job_health.expired_lease_count,
                    job_health.recovery_attempt_count,
                    job_health.stuck_job_count,
                    job_health.oldest_queued_at,
                    CASE
                        WHEN job_health.oldest_queued_at IS NULL THEN NULL
                        ELSE GREATEST(
                            0,
                            EXTRACT(
                                EPOCH FROM (
                                    execution_clock.effective_now
                                    - job_health.oldest_queued_at
                                )
                            )
                        )::double precision
                    END AS oldest_queued_age_seconds
                FROM execution_clock, worker_health, job_health
                """,
                (clock_override, stuck_after, stuck_after),
            )
            row = await cursor.fetchone()
        if row is None:  # pragma: no cover - aggregate query always returns one row
            raise RuntimeError("execution queue health query did not return a row")
        return ExecutionQueueHealth(
            checked_at=row["checked_at"],
            accepting_new_jobs=row["accepting_new_jobs"],
            ready_worker_count=row["ready_worker_count"],
            queued_job_count=row["queued_job_count"],
            leased_job_count=row["leased_job_count"],
            expired_lease_count=row["expired_lease_count"],
            recovery_attempt_count=row["recovery_attempt_count"],
            stuck_job_count=row["stuck_job_count"],
            oldest_queued_at=row["oldest_queued_at"],
            oldest_queued_age_seconds=row["oldest_queued_age_seconds"],
            stuck_after_seconds=stuck_after.total_seconds(),
        )

    async def claim_execution_job(
        self,
        *,
        registration: ExecutionWorkerRegistration,
        lease_duration: timedelta,
        now: datetime | None = None,
    ) -> ExecutionLease | None:
        clock_override = self._queue_clock_override(now)
        _validate_execution_identifier(registration.worker_id, field="worker_id")
        _validate_lease_duration(lease_duration)
        for _ in range(_MAX_INCOMPATIBLE_JOBS_PER_CLAIM):
            token = uuid4().hex
            async with (
                self._pool.connection() as connection,
                connection.transaction(),
                connection.cursor(row_factory=dict_row) as cursor,
            ):
                await cursor.execute(
                    """
                    SELECT COALESCE(%s::timestamptz, clock_timestamp()) AS effective_now
                    """,
                    (clock_override,),
                )
                clock_row = await cursor.fetchone()
                if clock_row is None:  # pragma: no cover - scalar SELECT always returns one row
                    raise RuntimeError("execution claim clock query did not return a row")
                effective_now = clock_row["effective_now"]
                await cursor.execute(
                    """
                    SELECT worker.worker_id
                    FROM execution_worker_registry AS worker
                    WHERE worker.worker_id = %s
                      AND worker.token = %s
                      AND worker.generation = %s
                      AND worker.state = 'ready'
                      AND worker.expires_at > %s
                    FOR SHARE OF worker
                    """,
                    (
                        registration.worker_id,
                        registration.token,
                        registration.generation,
                        effective_now,
                    ),
                )
                if await cursor.fetchone() is None:
                    raise ExecutionWorkerRegistrationLostError(
                        context={
                            "worker_id": registration.worker_id,
                            "reason": "worker_registration_not_ready",
                        }
                    )
                await cursor.execute(
                    """
                    WITH candidate AS MATERIALIZED (
                        SELECT job.job_id
                        FROM run_execution_jobs AS job
                        WHERE job.payload_schema_version = 1
                          AND CASE job.state
                                WHEN 'queued' THEN job.available_at
                                WHEN 'leased' THEN job.lease_expires_at
                              END <= COALESCE(%s::timestamptz, clock_timestamp())
                        ORDER BY
                            CASE
                                WHEN job.state = 'queued' THEN job.available_at
                                ELSE job.lease_expires_at
                            END,
                            job.created_at,
                            job.job_id
                        LIMIT 1
                        FOR UPDATE OF job SKIP LOCKED
                    ),
                    execution_clock AS MATERIALIZED (
                        SELECT COALESCE(%s::timestamptz, clock_timestamp()) AS effective_now
                        FROM candidate
                    )
                    UPDATE run_execution_jobs AS job
                    SET state = 'leased',
                        lease_token = %s,
                        lease_owner = %s,
                        lease_generation = job.lease_generation + 1,
                        lease_expires_at = execution_clock.effective_now + %s::interval,
                        heartbeat_at = execution_clock.effective_now,
                        claim_count = job.claim_count + 1,
                        updated_at = execution_clock.effective_now
                    FROM candidate, execution_clock
                    WHERE job.job_id = candidate.job_id
                      AND CASE job.state
                            WHEN 'queued' THEN job.available_at
                            WHEN 'leased' THEN job.lease_expires_at
                          END <= execution_clock.effective_now
                    RETURNING job.*
                    """,
                    (
                        effective_now,
                        effective_now,
                        token,
                        registration.worker_id,
                        lease_duration,
                    ),
                )
                row = await cursor.fetchone()
                if row is None:
                    return None
                try:
                    job = _execution_job_from_row(
                        row,
                        payload_cipher=self._execution_payload_cipher,
                    )
                except AgentStateIncompatibleError:
                    await _quarantine_incompatible_execution_job(
                        cursor,
                        job_id=str(row["job_id"]),
                        token=token,
                        worker_id=registration.worker_id,
                    )
                    continue
            return _execution_lease_from_job(job)
        return None

    async def heartbeat_execution_job(
        self,
        lease: ExecutionLease,
        *,
        lease_duration: timedelta,
        checkpoint_cursor: str | None = None,
        now: datetime | None = None,
    ) -> ExecutionLease:
        clock_override = self._queue_clock_override(now)
        _validate_lease_duration(lease_duration)
        _validate_optional_execution_identifier(
            checkpoint_cursor,
            field="checkpoint_cursor",
        )
        async with (
            self._pool.connection() as connection,
            connection.transaction(),
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            await cursor.execute(
                """
                WITH locked_job AS MATERIALIZED (
                    SELECT job_id
                    FROM run_execution_jobs
                    WHERE job_id = %s
                      AND owner_id = %s
                      AND thread_id = %s
                      AND run_id = %s
                      AND request_id = %s
                      AND execution_attempt = %s
                      AND operation = %s
                      AND payload_schema_version = %s
                      AND state = 'leased'
                      AND lease_token = %s
                      AND lease_owner = %s
                      AND lease_generation = %s
                    FOR UPDATE
                ),
                execution_clock AS MATERIALIZED (
                    SELECT COALESCE(%s::timestamptz, clock_timestamp()) AS effective_now
                    FROM locked_job
                )
                UPDATE run_execution_jobs AS job
                SET heartbeat_at = execution_clock.effective_now,
                    lease_expires_at = execution_clock.effective_now + %s::interval,
                    checkpoint_cursor = COALESCE(%s, checkpoint_cursor),
                    updated_at = execution_clock.effective_now
                FROM locked_job, execution_clock
                WHERE job.job_id = locked_job.job_id
                  AND job.lease_expires_at > execution_clock.effective_now
                RETURNING job.*
                """,
                (
                    *_execution_lease_fence_parameters(lease),
                    clock_override,
                    lease_duration,
                    checkpoint_cursor,
                ),
            )
            row = await cursor.fetchone()
            if row is None:
                raise ExecutionLeaseLostError()
            job = _execution_job_from_row(row, payload_cipher=self._execution_payload_cipher)
        _validate_job_matches_lease(job, lease)
        return _execution_lease_from_job(job)

    async def complete_execution_job(
        self,
        lease: ExecutionLease,
        *,
        checkpoint_cursor: str | None = None,
        now: datetime | None = None,
    ) -> RunExecutionJob:
        _validate_optional_execution_identifier(
            checkpoint_cursor,
            field="checkpoint_cursor",
        )
        return await self._terminal_execution_job(
            lease,
            state=ExecutionJobState.COMPLETED,
            failure_code=None,
            checkpoint_cursor=checkpoint_cursor,
            now=now,
        )

    async def fail_execution_job(
        self,
        lease: ExecutionLease,
        *,
        failure_code: str,
        checkpoint_cursor: str | None = None,
        now: datetime | None = None,
    ) -> RunExecutionJob:
        _validate_optional_execution_identifier(
            checkpoint_cursor,
            field="checkpoint_cursor",
        )
        if re.fullmatch(r"[A-Z][A-Z0-9_]{0,99}", failure_code) is None:
            raise ValueError("failure_code must be a stable uppercase code")
        return await self._terminal_execution_job(
            lease,
            state=ExecutionJobState.FAILED,
            failure_code=failure_code,
            checkpoint_cursor=checkpoint_cursor,
            now=now,
        )

    async def release_execution_job(
        self,
        lease: ExecutionLease,
        *,
        available_at: datetime | None = None,
        checkpoint_cursor: str | None = None,
        now: datetime | None = None,
    ) -> RunExecutionJob:
        clock_override = self._queue_clock_override(now)
        _validate_optional_execution_identifier(
            checkpoint_cursor,
            field="checkpoint_cursor",
        )
        requested_available_at = (
            None if available_at is None else _explicit_execution_time(available_at)
        )
        async with (
            self._pool.connection() as connection,
            connection.transaction(),
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            await cursor.execute(
                """
                WITH locked_job AS MATERIALIZED (
                    SELECT job_id
                    FROM run_execution_jobs
                    WHERE job_id = %s
                      AND owner_id = %s
                      AND thread_id = %s
                      AND run_id = %s
                      AND request_id = %s
                      AND execution_attempt = %s
                      AND operation = %s
                      AND payload_schema_version = %s
                      AND state = 'leased'
                      AND lease_token = %s
                      AND lease_owner = %s
                      AND lease_generation = %s
                    FOR UPDATE
                ),
                execution_clock AS MATERIALIZED (
                    SELECT COALESCE(%s::timestamptz, clock_timestamp()) AS effective_now
                    FROM locked_job
                )
                UPDATE run_execution_jobs AS job
                SET state = 'queued',
                    available_at = GREATEST(
                        execution_clock.effective_now,
                        COALESCE(%s::timestamptz, execution_clock.effective_now)
                    ),
                    lease_token = NULL,
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    heartbeat_at = NULL,
                    checkpoint_cursor = COALESCE(%s, checkpoint_cursor),
                    updated_at = execution_clock.effective_now
                FROM locked_job, execution_clock
                WHERE job.job_id = locked_job.job_id
                  AND job.lease_expires_at > execution_clock.effective_now
                RETURNING job.*
                """,
                (
                    *_execution_lease_fence_parameters(lease),
                    clock_override,
                    requested_available_at,
                    checkpoint_cursor,
                ),
            )
            row = await cursor.fetchone()
            if row is None:
                raise ExecutionLeaseLostError()
            job = _execution_job_from_row(row, payload_cipher=self._execution_payload_cipher)
        _validate_job_matches_lease(job, lease, require_active=False)
        return job

    async def get_execution_job(
        self,
        job_id: str,
        *,
        owner_id: str,
    ) -> RunExecutionJob:
        async with (
            self._pool.connection() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            await cursor.execute(
                f"""
                SELECT {_EXECUTION_JOB_COLUMNS}
                FROM run_execution_jobs
                WHERE job_id = %s AND owner_id = %s
                """,
                (job_id, owner_id),
            )
            row = await cursor.fetchone()
        if row is None:
            raise ExecutionJobNotFoundError(context={"job_id": job_id})
        return _execution_job_from_row(row, payload_cipher=self._execution_payload_cipher)

    async def assert_execution_lease(
        self,
        lease: ExecutionLease,
        *,
        now: datetime | None = None,
    ) -> RunExecutionJob:
        clock_override = self._queue_clock_override(now)
        async with (
            self._pool.connection() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            await cursor.execute(
                f"""
                SELECT {_EXECUTION_JOB_COLUMNS}
                FROM run_execution_jobs
                WHERE job_id = %s
                  AND owner_id = %s
                  AND state = 'leased'
                  AND lease_token = %s
                  AND lease_owner = %s
                  AND lease_generation = %s
                  AND lease_expires_at > COALESCE(%s, clock_timestamp())
                """,
                (
                    lease.job_id,
                    lease.owner_id,
                    lease.token,
                    lease.worker_id,
                    lease.generation,
                    clock_override,
                ),
            )
            row = await cursor.fetchone()
        if row is None:
            raise ExecutionLeaseLostError()
        job = _execution_job_from_row(row, payload_cipher=self._execution_payload_cipher)
        _validate_job_matches_lease(job, lease)
        return job

    @asynccontextmanager
    async def guard_execution_lease(
        self,
        lease: ExecutionLease,
    ) -> AsyncIterator[None]:
        async with (
            self._pool.connection() as connection,
            connection.transaction(),
        ):
            await self._lock_active_execution_lease(connection, lease)
            yield

    async def _lock_active_execution_lease(
        self,
        connection: AsyncConnection[Any],
        lease: ExecutionLease,
    ) -> RunExecutionJob:
        """Fence a ledger mutation with the lease row in the same transaction."""

        async with connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                f"""
                SELECT {_EXECUTION_JOB_COLUMNS}
                FROM run_execution_jobs
                WHERE job_id = %s
                  AND owner_id = %s
                  AND thread_id = %s
                  AND run_id = %s
                  AND request_id = %s
                  AND execution_attempt = %s
                  AND operation = %s
                  AND payload_schema_version = %s
                  AND state = 'leased'
                  AND lease_token = %s
                  AND lease_owner = %s
                  AND lease_generation = %s
                FOR UPDATE
                """,
                _execution_lease_fence_parameters(lease),
            )
            row = await cursor.fetchone()
            if row is None:
                raise ExecutionLeaseLostError()
            await cursor.execute("SELECT clock_timestamp() AS checked_at")
            clock = await cursor.fetchone()
        if clock is None or row["lease_expires_at"] <= clock["checked_at"]:
            raise ExecutionLeaseLostError()
        job = _execution_job_from_row(row, payload_cipher=self._execution_payload_cipher)
        _validate_job_matches_lease(job, lease)
        return job

    @staticmethod
    async def _recheck_active_execution_lease(
        connection: AsyncConnection[Any],
        lease: ExecutionLease,
    ) -> None:
        async with connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                """
                SELECT 1
                FROM run_execution_jobs
                WHERE job_id = %s
                  AND state = 'leased'
                  AND lease_token = %s
                  AND lease_owner = %s
                  AND lease_generation = %s
                  AND lease_expires_at > clock_timestamp()
                """,
                (lease.job_id, lease.token, lease.worker_id, lease.generation),
            )
            if await cursor.fetchone() is None:
                raise ExecutionLeaseLostError()

    async def _terminal_execution_job(
        self,
        lease: ExecutionLease,
        *,
        state: Literal[ExecutionJobState.COMPLETED, ExecutionJobState.FAILED],
        failure_code: str | None,
        checkpoint_cursor: str | None,
        now: datetime | None,
    ) -> RunExecutionJob:
        clock_override = self._queue_clock_override(now)
        async with (
            self._pool.connection() as connection,
            connection.transaction(),
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            await cursor.execute(
                """
                WITH locked_job AS MATERIALIZED (
                    SELECT job_id
                    FROM run_execution_jobs
                    WHERE job_id = %s
                      AND owner_id = %s
                      AND thread_id = %s
                      AND run_id = %s
                      AND request_id = %s
                      AND execution_attempt = %s
                      AND operation = %s
                      AND payload_schema_version = %s
                      AND state = 'leased'
                      AND lease_token = %s
                      AND lease_owner = %s
                      AND lease_generation = %s
                    FOR UPDATE
                ),
                execution_clock AS MATERIALIZED (
                    SELECT COALESCE(%s::timestamptz, clock_timestamp()) AS effective_now
                    FROM locked_job
                )
                UPDATE run_execution_jobs AS job
                SET state = %s,
                    payload = NULL,
                    payload_ciphertext = NULL,
                    payload_nonce = NULL,
                    payload_key_fingerprint = NULL,
                    payload_encryption_version = NULL,
                    lease_token = NULL,
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    heartbeat_at = NULL,
                    failure_code = %s,
                    checkpoint_cursor = COALESCE(%s, checkpoint_cursor),
                    completed_at = execution_clock.effective_now,
                    updated_at = execution_clock.effective_now
                FROM locked_job, execution_clock
                WHERE job.job_id = locked_job.job_id
                  AND job.lease_expires_at > execution_clock.effective_now
                RETURNING job.*
                """,
                (
                    *_execution_lease_fence_parameters(lease),
                    clock_override,
                    state.value,
                    failure_code,
                    checkpoint_cursor,
                ),
            )
            row = await cursor.fetchone()
            if row is None:
                raise ExecutionLeaseLostError()
            job = _execution_job_from_row(row, payload_cipher=self._execution_payload_cipher)
        _validate_job_matches_lease(job, lease, require_active=False)
        return job

    async def finalize_run(
        self,
        run: ConversationRun,
        *,
        correlation: RunExecutionCorrelation,
        owner_id: str,
    ) -> ConversationRun:
        if run.run_id != correlation.run_id or run.thread_id != correlation.thread_id:
            raise AgentPolicyViolationError(context={"reason": "run_thread_change_forbidden"})
        async with (
            self._pool.connection() as connection,
            connection.transaction(),
        ):
            return await _finalize_ledger_run(
                connection,
                run,
                correlation,
                owner_id=owner_id,
            )

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
        if run.run_id != correlation.run_id or run.thread_id != correlation.thread_id:
            raise AgentPolicyViolationError(context={"reason": "run_thread_change_forbidden"})
        _validate_execution_terminal(job_state, failure_code=failure_code)
        _validate_optional_execution_identifier(checkpoint_cursor, field="checkpoint_cursor")
        async with (
            self._pool.connection() as connection,
            connection.transaction(),
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            await self._lock_active_execution_lease(connection, execution_lease)
            _validate_ledger_lease_scope(
                execution_lease,
                correlation=correlation,
                owner_id=owner_id,
            )
            stored = await _finalize_ledger_run(
                connection,
                run,
                correlation,
                owner_id=owner_id,
            )
            await cursor.execute(
                """
                WITH execution_clock AS MATERIALIZED (
                    SELECT clock_timestamp() AS effective_now
                )
                UPDATE run_execution_jobs AS job
                SET state = %s,
                    payload = NULL,
                    payload_ciphertext = NULL,
                    payload_nonce = NULL,
                    payload_key_fingerprint = NULL,
                    payload_encryption_version = NULL,
                    lease_token = NULL,
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    heartbeat_at = NULL,
                    checkpoint_cursor = COALESCE(%s, checkpoint_cursor),
                    failure_code = %s,
                    completed_at = execution_clock.effective_now,
                    updated_at = execution_clock.effective_now
                FROM execution_clock
                WHERE job_id = %s
                  AND state = 'leased'
                  AND lease_token = %s
                  AND lease_owner = %s
                  AND lease_generation = %s
                  AND lease_expires_at > execution_clock.effective_now
                RETURNING job.job_id
                """,
                (
                    job_state.value,
                    checkpoint_cursor,
                    failure_code,
                    execution_lease.job_id,
                    execution_lease.token,
                    execution_lease.worker_id,
                    execution_lease.generation,
                ),
            )
            if await cursor.fetchone() is None:
                raise ExecutionLeaseLostError()
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
        async with (
            self._pool.connection() as connection,
            connection.transaction(),
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            if execution_lease is not None:
                await self._lock_active_execution_lease(connection, execution_lease)
                _validate_ledger_lease_scope(
                    execution_lease,
                    correlation=correlation,
                    owner_id=owner_id,
                )
            row = await _lock_running_ledger_run(
                connection,
                correlation,
                owner_id=owner_id,
            )
            await cursor.execute(
                """
                SELECT
                    event_id,
                    event_key,
                    thread_id,
                    run_id,
                    sequence,
                    request_id,
                    event_type,
                    payload_schema_version,
                    payload,
                    occurred_at
                FROM run_events
                WHERE run_id = %s
                  AND event_type = 'tool.started'
                  AND payload ->> 'tool_execution_id' = %s
                """,
                (correlation.run_id, execution_id),
            )
            existing_row = await cursor.fetchone()
            if existing_row is not None:
                existing = ToolExecutionHandle.from_started_event(_run_event_from_row(existing_row))
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
                if execution_lease is not None:
                    await self._recheck_active_execution_lease(connection, execution_lease)
                return existing
            await cursor.execute(
                """
                SELECT payload ->> 'tool_execution_id' AS execution_id
                FROM run_events
                WHERE run_id = %s
                  AND event_type = 'tool.started'
                  AND payload ->> 'logical_call_id' = %s
                  AND payload ->> 'tool_attempt' = %s
                """,
                (correlation.run_id, logical_call_id, str(attempt)),
            )
            conflicting_attempt = await cursor.fetchone()
            if conflicting_attempt is not None:
                raise AgentPolicyViolationError(context={"reason": "tool_attempt_conflict"})
            sequence = row["next_event_sequence"]
            started_at = datetime.now(UTC)
            event = _new_run_event(
                correlation,
                sequence=sequence,
                phase=RunExecutionPhase.RUNNING,
                event_type=RunEventType.TOOL_STARTED,
                occurred_at=started_at,
                tool_execution_id=execution_id,
                logical_call_id=logical_call_id,
                tool_attempt=attempt,
                tool=tool,
                tool_transition=ToolTransition.STARTED,
            )
            await connection.execute(
                """
                UPDATE agent_runs
                SET next_event_sequence = %s
                WHERE run_id = %s
                """,
                (sequence + 1, correlation.run_id),
            )
            await _insert_run_event(connection, event)
            if execution_lease is not None:
                await self._recheck_active_execution_lease(connection, execution_lease)
        return ToolExecutionHandle.from_started_event(event)

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
        async with (
            self._pool.connection() as connection,
            connection.transaction(),
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            if execution_lease is not None:
                await self._lock_active_execution_lease(connection, execution_lease)
                _validate_ledger_lease_scope(
                    execution_lease,
                    correlation=handle.correlation,
                    owner_id=owner_id,
                )
            row = await _lock_running_ledger_run(
                connection,
                handle.correlation,
                owner_id=owner_id,
            )
            await cursor.execute(
                """
                SELECT
                    execution_id,
                    thread_id,
                    run_id,
                    request_id,
                    logical_call_id,
                    attempt,
                    tool_name,
                    terminal_status,
                    started_event_sequence,
                    terminal_event_sequence,
                    payload_schema_version,
                    payload,
                    completed_at
                FROM tool_executions
                WHERE execution_id = %s
                """,
                (handle.execution_id,),
            )
            existing_row = await cursor.fetchone()
            if existing_row is not None:
                existing = _tool_execution_from_row(existing_row)
                if (
                    existing.handle == handle
                    and existing.transition == transition
                    and existing.duration_ms == duration_ms
                    and existing.error_code == error_code
                    and existing.summary == summary
                ):
                    if execution_lease is not None:
                        await self._recheck_active_execution_lease(
                            connection,
                            execution_lease,
                        )
                    return existing
                raise AgentPolicyViolationError(context={"reason": "tool_terminal_conflict"})
            await cursor.execute(
                """
                SELECT
                    event_id,
                    event_key,
                    thread_id,
                    run_id,
                    sequence,
                    request_id,
                    event_type,
                    payload_schema_version,
                    payload,
                    occurred_at
                FROM run_events
                WHERE run_id = %s AND sequence = %s AND event_type = 'tool.started'
                """,
                (handle.correlation.run_id, handle.started_event_sequence),
            )
            started_row = await cursor.fetchone()
            if (
                started_row is None
                or ToolExecutionHandle.from_started_event(_run_event_from_row(started_row))
                != handle
            ):
                raise AgentPolicyViolationError(context={"reason": "tool_start_not_found"})
            sequence = row["next_event_sequence"]
            completed_at = datetime.now(UTC)
            event_type = _tool_terminal_event_type(transition)
            event = _new_run_event(
                handle.correlation,
                sequence=sequence,
                phase=RunExecutionPhase.RUNNING,
                event_type=event_type,
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
                terminal_event_sequence=sequence,
                completed_at=completed_at,
                duration_ms=duration_ms,
                error_code=error_code,
                summary=summary,
            )
            await connection.execute(
                """
                UPDATE agent_runs
                SET next_event_sequence = %s
                WHERE run_id = %s
                """,
                (sequence + 1, handle.correlation.run_id),
            )
            await _insert_run_event(connection, event)
            try:
                await connection.execute(
                    """
                    INSERT INTO tool_executions (
                        execution_id,
                        thread_id,
                        run_id,
                        request_id,
                        logical_call_id,
                        attempt,
                        tool_name,
                        terminal_status,
                        started_event_sequence,
                        terminal_event_sequence,
                        payload_schema_version,
                        payload,
                        completed_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 1, %s, %s)
                    """,
                    (
                        handle.execution_id,
                        handle.correlation.thread_id,
                        handle.correlation.run_id,
                        handle.correlation.request_id,
                        handle.logical_call_id,
                        handle.attempt,
                        handle.tool.value,
                        transition.value,
                        handle.started_event_sequence,
                        sequence,
                        Jsonb(record.model_dump(mode="json")),
                        completed_at,
                    ),
                )
            except UniqueViolation as exc:
                raise AgentPolicyViolationError(
                    context={"reason": "tool_terminal_conflict"}
                ) from exc
            if execution_lease is not None:
                await self._recheck_active_execution_lease(connection, execution_lease)
        return record

    async def get_run_events(
        self,
        run_id: str,
        *,
        owner_id: str,
    ) -> tuple[RunEventRecord, ...]:
        async with (
            self._pool.connection() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            await cursor.execute(
                """
                SELECT 1
                FROM agent_runs AS r
                JOIN agent_threads AS t ON t.thread_id = r.thread_id
                WHERE r.run_id = %s AND t.owner_id = %s
                """,
                (run_id, owner_id),
            )
            if await cursor.fetchone() is None:
                raise RunNotFoundError(context={"run_id": run_id})
            await cursor.execute(
                """
                SELECT
                    e.event_id,
                    e.event_key,
                    e.thread_id,
                    e.run_id,
                    e.sequence,
                    e.request_id,
                    e.event_type,
                    e.payload_schema_version,
                    e.payload,
                    e.occurred_at
                FROM run_events AS e
                WHERE e.run_id = %s
                ORDER BY e.sequence
                """,
                (run_id,),
            )
            rows = await cursor.fetchall()
        return tuple(_run_event_from_row(row) for row in rows)

    async def get_tool_executions(
        self,
        run_id: str,
        *,
        owner_id: str,
    ) -> tuple[ToolExecutionRecord, ...]:
        async with (
            self._pool.connection() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            await cursor.execute(
                """
                SELECT 1
                FROM agent_runs AS r
                JOIN agent_threads AS t ON t.thread_id = r.thread_id
                WHERE r.run_id = %s AND t.owner_id = %s
                """,
                (run_id, owner_id),
            )
            if await cursor.fetchone() is None:
                raise RunNotFoundError(context={"run_id": run_id})
            await cursor.execute(
                """
                SELECT
                    x.execution_id,
                    x.thread_id,
                    x.run_id,
                    x.request_id,
                    x.logical_call_id,
                    x.attempt,
                    x.tool_name,
                    x.terminal_status,
                    x.started_event_sequence,
                    x.terminal_event_sequence,
                    x.payload_schema_version,
                    x.payload,
                    x.completed_at
                FROM tool_executions AS x
                WHERE x.run_id = %s
                ORDER BY x.terminal_event_sequence
                """,
                (run_id,),
            )
            rows = await cursor.fetchall()
        return tuple(_tool_execution_from_row(row) for row in rows)

    async def find_orphaned_runs(
        self,
        *,
        owner_id: str,
        stale_before: datetime,
    ) -> tuple[OrphanedRunView, ...]:
        if stale_before.tzinfo is None or stale_before.utcoffset() is None:
            raise ValueError("stale_before must be timezone-aware")
        async with (
            self._pool.connection() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            await cursor.execute(
                """
                SELECT
                    last_event.event_id,
                    last_event.event_key,
                    last_event.thread_id,
                    last_event.run_id,
                    last_event.sequence,
                    last_event.request_id,
                    last_event.event_type,
                    last_event.payload_schema_version,
                    last_event.payload,
                    last_event.occurred_at
                FROM agent_runs AS r
                JOIN agent_threads AS t ON t.thread_id = r.thread_id
                JOIN LATERAL (
                    SELECT e.*
                    FROM run_events AS e
                    WHERE e.run_id = r.run_id
                    ORDER BY e.sequence DESC
                    LIMIT 1
                ) AS last_event ON TRUE
                WHERE t.owner_id = %s
                  AND r.execution_state = 'running'
                  AND last_event.occurred_at <= %s
                ORDER BY last_event.occurred_at, r.run_id
                LIMIT 100
                """,
                (owner_id, stale_before),
            )
            rows = await cursor.fetchall()
            orphans: list[OrphanedRunView] = []
            for row in rows:
                last = _run_event_from_row(row)
                await cursor.execute(
                    """
                    SELECT started.payload ->> 'tool_execution_id' AS execution_id
                    FROM run_events AS started
                    LEFT JOIN tool_executions AS terminal
                      ON terminal.run_id = started.run_id
                     AND terminal.started_event_sequence = started.sequence
                    WHERE started.run_id = %s
                      AND started.event_type = 'tool.started'
                      AND terminal.execution_id IS NULL
                    ORDER BY started.sequence
                    LIMIT 32
                    """,
                    (last.correlation.run_id,),
                )
                open_ids = tuple(open_row["execution_id"] for open_row in await cursor.fetchall())
                reasons = [OrphanReason.RUN_STALE]
                if open_ids:
                    reasons.append(OrphanReason.TOOL_EXECUTION_STALE)
                orphans.append(
                    OrphanedRunView(
                        correlation=last.correlation,
                        reasons=tuple(reasons),
                        last_sequence=last.sequence,
                        last_event_type=last.event_type,
                        last_activity_at=last.occurred_at,
                        open_tool_execution_ids=open_ids,
                    )
                )
        return tuple(orphans)

    async def get_run(
        self,
        run_id: str,
        *,
        owner_id: str,
    ) -> ConversationRun:
        async with (
            self._pool.connection() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            await cursor.execute(
                """
                SELECT
                    r.run_id,
                    r.thread_id,
                    r.status,
                    r.resume_mode,
                    r.payload_schema_version,
                    r.outcome,
                    r.context,
                    r.usage
                FROM agent_runs AS r
                JOIN agent_threads AS t ON t.thread_id = r.thread_id
                WHERE r.run_id = %s AND t.owner_id = %s AND r.status IS NOT NULL
                """,
                (run_id, owner_id),
            )
            row = await cursor.fetchone()
        if row is None:
            raise RunNotFoundError(context={"run_id": run_id})
        return _run_from_row(row)

    async def delete_thread(
        self,
        thread_id: str,
        *,
        owner_id: str,
    ) -> None:
        async with (
            self._pool.connection() as connection,
            connection.transaction(),
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            await cursor.execute(
                """
                SELECT deletion_token
                FROM agent_threads
                WHERE thread_id = %s AND owner_id = %s
                FOR UPDATE
                """,
                (thread_id, owner_id),
            )
            thread = await cursor.fetchone()
            if thread is None:
                raise ThreadNotFoundError(context={"thread_id": thread_id})
            if thread["deletion_token"] is not None:
                raise ThreadBusyError(
                    context={"thread_id": thread_id, "reason": "thread_deletion_prepared"}
                )
            await _ensure_no_active_execution_jobs(cursor, thread_id)
            await cursor.execute(
                """
                DELETE FROM agent_threads
                WHERE thread_id = %s AND owner_id = %s
                RETURNING thread_id
                """,
                (thread_id, owner_id),
            )
            deleted = await cursor.fetchone()
            if deleted is None:
                raise ThreadNotFoundError(context={"thread_id": thread_id})

    async def prepare_thread_deletion(
        self,
        thread_id: str,
        *,
        owner_id: str,
    ) -> ThreadDeletionReservation:
        token = uuid4().hex
        async with (
            self._pool.connection() as connection,
            connection.transaction(),
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            await cursor.execute(
                """
                SELECT deletion_token, deletion_requested_at
                FROM agent_threads
                WHERE thread_id = %s AND owner_id = %s
                FOR UPDATE
                """,
                (thread_id, owner_id),
            )
            row = await cursor.fetchone()
            if row is None:
                raise ThreadNotFoundError(context={"thread_id": thread_id})
            if row["deletion_token"] is not None:
                return ThreadDeletionReservation(
                    thread_id=thread_id,
                    owner_id=owner_id,
                    token=str(row["deletion_token"]),
                    requested_at=row["deletion_requested_at"],
                    replayed=True,
                )
            await _ensure_no_active_execution_jobs(cursor, thread_id)
            await cursor.execute(
                """
                UPDATE agent_threads
                SET deletion_token = %s,
                    deletion_requested_at = clock_timestamp(),
                    updated_at = clock_timestamp()
                WHERE thread_id = %s AND owner_id = %s
                RETURNING deletion_requested_at
                """,
                (token, thread_id, owner_id),
            )
            prepared = await cursor.fetchone()
            if prepared is None:
                raise ThreadNotFoundError(context={"thread_id": thread_id})
        return ThreadDeletionReservation(
            thread_id=thread_id,
            owner_id=owner_id,
            token=token,
            requested_at=prepared["deletion_requested_at"],
        )

    async def commit_thread_deletion(
        self,
        reservation: ThreadDeletionReservation,
    ) -> None:
        async with (
            self._pool.connection() as connection,
            connection.transaction(),
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            await cursor.execute(
                """
                SELECT 1
                FROM agent_threads
                WHERE thread_id = %s
                  AND owner_id = %s
                  AND deletion_token = %s
                FOR UPDATE
                """,
                (reservation.thread_id, reservation.owner_id, reservation.token),
            )
            if await cursor.fetchone() is None:
                raise AgentPolicyViolationError(
                    context={"reason": "thread_deletion_fence_mismatch"}
                )
            await _ensure_no_active_execution_jobs(cursor, reservation.thread_id)
            await cursor.execute(
                """
                DELETE FROM agent_threads
                WHERE thread_id = %s
                  AND owner_id = %s
                  AND deletion_token = %s
                RETURNING thread_id
                """,
                (reservation.thread_id, reservation.owner_id, reservation.token),
            )
            if await cursor.fetchone() is None:
                raise AgentPolicyViolationError(
                    context={"reason": "thread_deletion_fence_mismatch"}
                )

    async def abort_thread_deletion(
        self,
        reservation: ThreadDeletionReservation,
    ) -> None:
        async with (
            self._pool.connection() as connection,
            connection.transaction(),
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            await cursor.execute(
                """
                UPDATE agent_threads
                SET deletion_token = NULL,
                    deletion_requested_at = NULL,
                    updated_at = clock_timestamp()
                WHERE thread_id = %s
                  AND owner_id = %s
                  AND deletion_token = %s
                RETURNING thread_id
                """,
                (reservation.thread_id, reservation.owner_id, reservation.token),
            )
            if await cursor.fetchone() is None:
                raise AgentPolicyViolationError(
                    context={"reason": "thread_deletion_fence_mismatch"}
                )

    async def is_ready(self) -> bool:
        try:
            async with asyncio.timeout(self._ready_timeout_seconds):
                async with (
                    self._pool.connection(timeout=self._ready_timeout_seconds) as connection,
                    connection.cursor(row_factory=dict_row) as cursor,
                ):
                    required_tables = (
                        "agent_repository_migrations",
                        "agent_threads",
                        "agent_runs",
                        "run_events",
                        "tool_executions",
                        "run_execution_jobs",
                        "execution_worker_registry",
                        "agent_runtime_secrets",
                        "checkpoint_migrations",
                        "checkpoints",
                        "checkpoint_blobs",
                        "checkpoint_writes",
                    )
                    await cursor.execute(
                        "SELECT name, to_regclass(name) AS relation "
                        "FROM unnest(%s::text[]) AS name",
                        (list(required_tables),),
                    )
                    relations = await cursor.fetchall()
                    if any(row["relation"] is None for row in relations):
                        return False
                    await cursor.execute(
                        """
                        SELECT bool_and(
                            has_table_privilege(current_user, name, 'SELECT')
                            AND has_table_privilege(current_user, name, 'INSERT')
                            AND (
                                NOT (name = ANY(%s::text[]))
                                OR (
                                    has_table_privilege(current_user, name, 'UPDATE')
                                    AND has_table_privilege(current_user, name, 'DELETE')
                                )
                            )
                        ) AS ready
                        FROM unnest(%s::text[]) AS name
                        """,
                        (
                            [
                                "agent_threads",
                                "agent_runs",
                                "run_execution_jobs",
                                "execution_worker_registry",
                                "checkpoints",
                                "checkpoint_blobs",
                                "checkpoint_writes",
                            ],
                            [
                                "agent_threads",
                                "agent_runs",
                                "run_events",
                                "tool_executions",
                                "run_execution_jobs",
                                "execution_worker_registry",
                                "checkpoints",
                                "checkpoint_blobs",
                                "checkpoint_writes",
                            ],
                        ),
                    )
                    permission = await cursor.fetchone()
        except (TimeoutError, Error, PoolClosed, PoolTimeout):
            return False
        return permission is not None and permission["ready"] is True


async def _lock_idempotency_key(
    connection: AsyncConnection[Any],
    *,
    owner_id: str,
    idempotency_digest: str,
) -> None:
    await connection.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, %s))",
        (f"{owner_id}\x1f{idempotency_digest}", _IDEMPOTENCY_LOCK_SEED),
    )


async def _select_execution_job_by_idempotency(
    connection: AsyncConnection[Any],
    *,
    owner_id: str,
    idempotency_digest: str,
    payload_cipher: ExecutionPayloadCipher | None,
) -> RunExecutionJob | None:
    async with connection.cursor(row_factory=dict_row) as cursor:
        await cursor.execute(
            f"""
            SELECT {_EXECUTION_JOB_COLUMNS}
            FROM run_execution_jobs
            WHERE owner_id = %s AND idempotency_digest = %s
            """,
            (owner_id, idempotency_digest),
        )
        row = await cursor.fetchone()
    return None if row is None else _execution_job_from_row(row, payload_cipher=payload_cipher)


def _validate_idempotency_replay(
    job: RunExecutionJob,
    *,
    request_fingerprint: str,
    operation: ExecutionOperation,
    thread_id: str | None = None,
    run_id: str | None = None,
) -> None:
    matches_scope = (thread_id is None or job.correlation.thread_id == thread_id) and (
        run_id is None or job.correlation.run_id == run_id
    )
    if (
        job.request_fingerprint != request_fingerprint
        or job.operation != operation
        or not matches_scope
    ):
        raise IdempotencyKeyReuseError()


async def _insert_execution_job(
    connection: AsyncConnection[Any],
    *,
    payload_cipher: ExecutionPayloadCipher,
    owner_id: str,
    correlation: RunExecutionCorrelation,
    operation: ExecutionOperation,
    idempotency_digest: str,
    request_fingerprint: str,
    payload: ExecutionPayload,
) -> RunExecutionJob:
    job_id = uuid4().hex
    encrypted_payload = payload_cipher.encrypt(
        payload,
        associated_data=execution_payload_associated_data(
            job_id=job_id,
            owner_id=owner_id,
            correlation=correlation,
            operation=operation,
            payload_schema_version=1,
        ),
    )
    async with connection.cursor(row_factory=dict_row) as cursor:
        await cursor.execute(
            f"""
            INSERT INTO run_execution_jobs (
                job_id,
                owner_id,
                thread_id,
                run_id,
                request_id,
                execution_attempt,
                operation,
                state,
                payload_schema_version,
                idempotency_digest,
                request_fingerprint,
                payload,
                payload_ciphertext,
                payload_nonce,
                payload_key_fingerprint,
                payload_encryption_version
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, 'queued', 1, %s, %s, NULL,
                %s, %s, %s, %s
            )
            RETURNING {_EXECUTION_JOB_COLUMNS}
            """,
            (
                job_id,
                owner_id,
                correlation.thread_id,
                correlation.run_id,
                correlation.request_id,
                correlation.attempt,
                operation.value,
                idempotency_digest,
                request_fingerprint,
                encrypted_payload.ciphertext,
                encrypted_payload.nonce,
                encrypted_payload.key_fingerprint,
                encrypted_payload.encryption_version,
            ),
        )
        row = await cursor.fetchone()
    if row is None:  # INSERT .. RETURNING always yields one row.
        raise RuntimeError("Execution job insert did not return a row.")
    return _execution_job_from_row(row, payload_cipher=payload_cipher)


def _execution_job_from_row(
    row: Mapping[str, object],
    *,
    payload_cipher: ExecutionPayloadCipher | None,
) -> RunExecutionJob:
    job_id = str(row["job_id"])
    try:
        correlation = RunExecutionCorrelation(
            thread_id=str(row["thread_id"]),
            run_id=str(row["run_id"]),
            request_id=str(row["request_id"]),
            attempt=cast(int, row["execution_attempt"]),
        )
        operation = ExecutionOperation(str(row["operation"]))
        payload_schema_version = cast(Literal[1], row["payload_schema_version"])
        ciphertext = row["payload_ciphertext"]
        if ciphertext is None:
            payload = None
        else:
            if payload_cipher is None:
                raise ExecutionPayloadCipherError("execution payload key is unavailable")
            envelope = EncryptedExecutionPayload(
                ciphertext=bytes(cast(bytes, ciphertext)),
                nonce=bytes(cast(bytes, row["payload_nonce"])),
                key_fingerprint=str(row["payload_key_fingerprint"]),
                encryption_version=cast(int, row["payload_encryption_version"]),
            )
            payload = payload_cipher.decrypt(
                envelope,
                associated_data=execution_payload_associated_data(
                    job_id=job_id,
                    owner_id=str(row["owner_id"]),
                    correlation=correlation,
                    operation=operation,
                    payload_schema_version=payload_schema_version,
                ),
            )
        return RunExecutionJob(
            job_id=job_id,
            owner_id=str(row["owner_id"]),
            correlation=correlation,
            operation=operation,
            payload=payload,
            payload_schema_version=payload_schema_version,
            idempotency_digest=str(row["idempotency_digest"]),
            request_fingerprint=str(row["request_fingerprint"]),
            state=ExecutionJobState(str(row["state"])),
            available_at=row["available_at"],
            checkpoint_cursor=(
                None if row["checkpoint_cursor"] is None else str(row["checkpoint_cursor"])
            ),
            claim_count=cast(int, row["claim_count"]),
            lease_generation=cast(int, row["lease_generation"]),
            lease_token=None if row["lease_token"] is None else str(row["lease_token"]),
            lease_owner=None if row["lease_owner"] is None else str(row["lease_owner"]),
            lease_expires_at=row["lease_expires_at"],
            heartbeat_at=row["heartbeat_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"],
            failure_code=(None if row["failure_code"] is None else str(row["failure_code"])),
        )
    except (
        ExecutionPayloadCipherError,
        StateInputBudgetExceededError,
        TypeError,
        ValidationError,
        ValueError,
    ):
        raise AgentStateIncompatibleError(
            context={
                "reason": "execution_job_payload_incompatible",
                "job_id": job_id,
            }
        ) from None


def _execution_lease_from_job(job: RunExecutionJob) -> ExecutionLease:
    if (
        job.state != ExecutionJobState.LEASED
        or job.payload is None
        or job.lease_token is None
        or job.lease_owner is None
        or job.lease_expires_at is None
    ):
        raise ExecutionLeaseLostError()
    return ExecutionLease(
        job_id=job.job_id,
        owner_id=job.owner_id,
        correlation=job.correlation,
        operation=job.operation,
        payload=job.payload,
        payload_schema_version=job.payload_schema_version,
        worker_id=job.lease_owner,
        token=job.lease_token,
        generation=job.lease_generation,
        expires_at=job.lease_expires_at,
        heartbeat_at=job.heartbeat_at,
        checkpoint_cursor=job.checkpoint_cursor,
    )


def _execution_worker_registration_from_row(row: DictRow) -> ExecutionWorkerRegistration:
    return ExecutionWorkerRegistration(
        worker_id=row["worker_id"],
        token=row["token"],
        generation=row["generation"],
        state=ExecutionWorkerRegistrationState(row["state"]),
        registered_at=row["registered_at"],
        heartbeat_at=row["heartbeat_at"],
        expires_at=row["expires_at"],
    )


def _validate_job_matches_lease(
    job: RunExecutionJob,
    lease: ExecutionLease,
    *,
    require_active: bool = True,
) -> None:
    matches = (
        job.job_id == lease.job_id
        and job.owner_id == lease.owner_id
        and job.correlation == lease.correlation
        and job.operation == lease.operation
        and job.payload_schema_version == lease.payload_schema_version
        and job.lease_generation == lease.generation
    )
    if require_active:
        matches = matches and (
            job.state == ExecutionJobState.LEASED
            and job.payload == lease.payload
            and job.lease_token == lease.token
            and job.lease_owner == lease.worker_id
        )
    if not matches:
        raise ExecutionLeaseLostError()


def _execution_lease_fence_parameters(lease: ExecutionLease) -> tuple[object, ...]:
    """Bind every immutable lease field before allowing a state mutation."""

    return (
        lease.job_id,
        lease.owner_id,
        lease.correlation.thread_id,
        lease.correlation.run_id,
        lease.correlation.request_id,
        lease.correlation.attempt,
        lease.operation.value,
        lease.payload_schema_version,
        lease.token,
        lease.worker_id,
        lease.generation,
    )


def _validate_ledger_lease_scope(
    lease: ExecutionLease,
    *,
    correlation: RunExecutionCorrelation,
    owner_id: str,
) -> None:
    if lease.owner_id != owner_id or lease.correlation != correlation:
        raise ExecutionLeaseLostError()


def _explicit_execution_time(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("execution timestamps must be timezone-aware")
    return value


def _validate_lease_duration(lease_duration: timedelta) -> None:
    if lease_duration <= timedelta(0):
        raise ValueError("lease_duration must be positive")


def _validate_positive_duration(value: timedelta, *, field: str) -> None:
    if value <= timedelta(0):
        raise ValueError(f"{field} must be positive")


def _validate_execution_identifier(value: str, *, field: str) -> None:
    if re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", value) is None:
        raise ValueError(f"{field} must be a safe execution identifier")


def _validate_optional_execution_identifier(value: str | None, *, field: str) -> None:
    if value is not None:
        _validate_execution_identifier(value, field=field)


def _validate_sha256_digest(value: str, *, field: str) -> None:
    if re.fullmatch(r"[a-f0-9]{64}", value) is None:
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")


def _validate_reservation_lookup_digests(values: tuple[str, ...]) -> tuple[str, ...]:
    if not 1 <= len(values) <= 2:
        raise ValueError("idempotency_digests must contain one or two values")
    if len(set(values)) != len(values):
        raise ValueError("idempotency_digests must be distinct")
    for value in values:
        _validate_sha256_digest(value, field="idempotency_digest")
    return values


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


async def _finalize_ledger_run(
    connection: AsyncConnection[Any],
    run: ConversationRun,
    correlation: RunExecutionCorrelation,
    *,
    owner_id: str,
) -> ConversationRun:
    row = await _lock_running_ledger_run(
        connection,
        correlation,
        owner_id=owner_id,
    )
    async with connection.cursor(row_factory=dict_row) as cursor:
        await cursor.execute(
            """
            SELECT count(*) AS open_count
            FROM run_events AS started
            LEFT JOIN tool_executions AS terminal
              ON terminal.run_id = started.run_id
             AND terminal.started_event_sequence = started.sequence
            WHERE started.run_id = %s
              AND started.event_type = 'tool.started'
              AND terminal.execution_id IS NULL
            """,
            (run.run_id,),
        )
        open_tools = await cursor.fetchone()
    if open_tools is None or open_tools["open_count"] != 0:
        raise AgentPolicyViolationError(context={"reason": "open_tool_execution_exists"})
    selected_usage = run.usage
    if row["usage"] is not None:
        stored_usage = _usage_from_payload(row["usage"])
        try:
            selected_usage = reconcile_run_usage(stored_usage, run.usage)
        except RunUsageRevisionConflictError as exc:
            raise AgentPolicyViolationError(
                context={"reason": "run_usage_revision_conflict"}
            ) from exc
    phase, event_type = _terminal_run_state(run)
    sequence = row["next_event_sequence"]
    event = _new_run_event(
        correlation,
        sequence=sequence,
        phase=phase,
        event_type=event_type,
    )
    await connection.execute(
        """
        UPDATE agent_runs
        SET status = %s,
            resume_mode = %s,
            execution_state = %s,
            outcome = %s,
            context = %s,
            usage = %s,
            next_event_sequence = %s,
            updated_at = CURRENT_TIMESTAMP
        WHERE run_id = %s
        """,
        (
            run.status.value,
            run.resume_mode.value,
            phase.value,
            Jsonb(run.outcome.model_dump(mode="json")),
            Jsonb(run.context.model_dump(mode="json")),
            Jsonb(selected_usage.model_dump(mode="json")),
            sequence + 1,
            run.run_id,
        ),
    )
    await connection.execute(
        """
        UPDATE agent_threads
        SET active_run_id = %s, updated_at = CURRENT_TIMESTAMP
        WHERE thread_id = %s
        """,
        (
            run.run_id if phase == RunExecutionPhase.INTERRUPTED else None,
            run.thread_id,
        ),
    )
    await _insert_run_event(connection, event)
    return run.model_copy(update={"usage": selected_usage}, deep=True)


async def _lock_running_ledger_run(
    connection: AsyncConnection[Any],
    correlation: RunExecutionCorrelation,
    *,
    owner_id: str,
) -> DictRow:
    async with connection.cursor(row_factory=dict_row) as cursor:
        await cursor.execute(
            """
            SELECT
                r.thread_id,
                r.execution_state,
                r.execution_attempt,
                r.next_event_sequence,
                r.usage,
                t.active_run_id
            FROM agent_runs AS r
            JOIN agent_threads AS t ON t.thread_id = r.thread_id
            WHERE r.run_id = %s AND t.owner_id = %s
            FOR UPDATE OF r, t
            """,
            (correlation.run_id, owner_id),
        )
        row = await cursor.fetchone()
        if row is None:
            raise RunNotFoundError(context={"run_id": correlation.run_id})
        if (
            row["thread_id"] != correlation.thread_id
            or row["execution_state"] != RunExecutionPhase.RUNNING.value
            or row["execution_attempt"] != correlation.attempt
            or row["active_run_id"] != correlation.run_id
        ):
            raise AgentPolicyViolationError(context={"reason": "run_correlation_stale"})
        await cursor.execute(
            """
            SELECT
                event_id,
                event_key,
                thread_id,
                run_id,
                sequence,
                request_id,
                event_type,
                payload_schema_version,
                payload,
                occurred_at
            FROM run_events
            WHERE run_id = %s
              AND event_type IN ('run.started', 'run.resume_started')
            ORDER BY sequence DESC
            LIMIT 1
            """,
            (correlation.run_id,),
        )
        start_row = await cursor.fetchone()
    if start_row is None or _run_event_from_row(start_row).correlation != correlation:
        raise AgentPolicyViolationError(context={"reason": "run_correlation_stale"})
    return row


def _new_run_event(
    correlation: RunExecutionCorrelation,
    *,
    sequence: int,
    phase: RunExecutionPhase,
    event_type: RunEventType,
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
    event_key = (
        f"tool:{tool_execution_id}:{tool_transition.value}"
        if tool_execution_id is not None and tool_transition is not None
        else f"run:{correlation.attempt}:{event_type.value}"
    )
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


async def _insert_run_event(
    connection: AsyncConnection[Any],
    event: RunEventRecord,
) -> None:
    await connection.execute(
        """
        INSERT INTO run_events (
            event_id,
            event_key,
            thread_id,
            run_id,
            sequence,
            request_id,
            event_type,
            payload_schema_version,
            payload,
            occurred_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, 1, %s, %s)
        """,
        (
            event.event_id,
            event.event_key,
            event.correlation.thread_id,
            event.correlation.run_id,
            event.sequence,
            event.correlation.request_id,
            event.event_type.value,
            Jsonb(event.model_dump(mode="json")),
            event.occurred_at,
        ),
    )


def _run_event_from_row(row: DictRow) -> RunEventRecord:
    if row.get("payload_schema_version") != 1:
        raise AgentStateIncompatibleError(context={"expected_run_event_schema_version": "1"})
    try:
        event = RunEventRecord.model_validate(row.get("payload"))
    except ValidationError as exc:
        raise AgentStateIncompatibleError(
            context={"expected_run_event_schema_version": "1"}
        ) from exc
    expected = {
        "event_id": event.event_id,
        "event_key": event.event_key,
        "thread_id": event.correlation.thread_id,
        "run_id": event.correlation.run_id,
        "sequence": event.sequence,
        "request_id": event.correlation.request_id,
        "event_type": event.event_type.value,
    }
    if any(row.get(key) != value for key, value in expected.items()):
        raise AgentStateIncompatibleError(context={"expected_run_event_schema_version": "1"})
    return event


def _tool_execution_from_row(row: DictRow) -> ToolExecutionRecord:
    if row.get("payload_schema_version") != 1:
        raise AgentStateIncompatibleError(context={"expected_tool_execution_schema_version": "1"})
    try:
        record = ToolExecutionRecord.model_validate(row.get("payload"))
    except ValidationError as exc:
        raise AgentStateIncompatibleError(
            context={"expected_tool_execution_schema_version": "1"}
        ) from exc
    expected = {
        "execution_id": record.handle.execution_id,
        "thread_id": record.handle.correlation.thread_id,
        "run_id": record.handle.correlation.run_id,
        "request_id": record.handle.correlation.request_id,
        "logical_call_id": record.handle.logical_call_id,
        "attempt": record.handle.attempt,
        "tool_name": record.handle.tool.value,
        "terminal_status": record.transition.value,
        "started_event_sequence": record.handle.started_event_sequence,
        "terminal_event_sequence": record.terminal_event_sequence,
    }
    if any(row.get(key) != value for key, value in expected.items()):
        raise AgentStateIncompatibleError(context={"expected_tool_execution_schema_version": "1"})
    return record


def _terminal_run_state(
    run: ConversationRun,
) -> tuple[RunExecutionPhase, RunEventType]:
    mapping = {
        "interrupted": (RunExecutionPhase.INTERRUPTED, RunEventType.RUN_INTERRUPTED),
        "completed": (RunExecutionPhase.COMPLETED, RunEventType.RUN_COMPLETED),
        "failed": (RunExecutionPhase.FAILED, RunEventType.RUN_FAILED),
    }
    selected = mapping.get(str(run.status))
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


async def _select_thread(
    connection: AsyncConnection[Any],
    thread_id: str,
    owner_id: str,
    *,
    for_update: bool = False,
) -> ConversationThread | None:
    query = _THREAD_SELECT_FOR_UPDATE_SQL if for_update else _THREAD_SELECT_SQL
    async with connection.cursor(row_factory=dict_row) as cursor:
        await cursor.execute(
            query,
            (thread_id, owner_id),
        )
        row = await cursor.fetchone()
    return ConversationThread.model_validate(row) if row is not None else None


async def _ensure_thread_accepts_execution(
    connection: AsyncConnection[Any],
    thread_id: str,
) -> None:
    async with connection.cursor(row_factory=dict_row) as cursor:
        await cursor.execute(
            "SELECT deletion_token FROM agent_threads WHERE thread_id = %s",
            (thread_id,),
        )
        row = await cursor.fetchone()
    if row is None:
        raise ThreadNotFoundError(context={"thread_id": thread_id})
    if row["deletion_token"] is not None:
        raise ThreadBusyError(
            context={"thread_id": thread_id, "reason": "thread_deletion_prepared"}
        )


async def _lock_available_execution_worker(connection: AsyncConnection[Any]) -> None:
    """Linearize a new reservation with the current ready-worker registry."""

    async with connection.cursor(row_factory=dict_row) as cursor:
        await cursor.execute(
            """
            WITH execution_clock AS MATERIALIZED (
                SELECT clock_timestamp() AS effective_now
            )
            SELECT worker.worker_id
            FROM execution_worker_registry AS worker
            CROSS JOIN execution_clock
            WHERE worker.state = 'ready'
              AND worker.expires_at > execution_clock.effective_now
            ORDER BY worker.worker_id
            LIMIT 1
            FOR SHARE OF worker
            """
        )
        worker = await cursor.fetchone()
    if worker is None:
        raise ExecutionWorkerUnavailableError()


async def _ensure_no_active_execution_jobs(
    cursor: AsyncCursor[DictRow],
    thread_id: str,
) -> None:
    await cursor.execute(
        """
        SELECT 1
        FROM run_execution_jobs
        WHERE thread_id = %s
          AND state IN ('queued', 'leased')
        LIMIT 1
        """,
        (thread_id,),
    )
    if await cursor.fetchone() is not None:
        raise ThreadBusyError(context={"thread_id": thread_id, "reason": "execution_job_active"})


async def _quarantine_incompatible_execution_job(
    cursor: AsyncCursor[DictRow],
    *,
    job_id: str,
    token: str,
    worker_id: str,
) -> None:
    await cursor.execute(
        """
        WITH execution_clock AS MATERIALIZED (
            SELECT clock_timestamp() AS effective_now
        )
        UPDATE run_execution_jobs AS job
        SET state = 'failed',
            payload = NULL,
            payload_ciphertext = NULL,
            payload_nonce = NULL,
            payload_key_fingerprint = NULL,
            payload_encryption_version = NULL,
            lease_token = NULL,
            lease_owner = NULL,
            lease_expires_at = NULL,
            heartbeat_at = NULL,
            failure_code = %s,
            completed_at = execution_clock.effective_now,
            updated_at = execution_clock.effective_now
        FROM execution_clock
        WHERE job.job_id = %s
          AND job.state = 'leased'
          AND job.lease_token = %s
          AND job.lease_owner = %s
        RETURNING job.job_id
        """,
        (_INCOMPATIBLE_EXECUTION_FAILURE_CODE, job_id, token, worker_id),
    )
    if await cursor.fetchone() is None:
        raise ExecutionLeaseLostError()


def _run_from_row(row: DictRow) -> ConversationRun:
    # Delayed import avoids application.conversations -> repository import cycles.
    from barrier_free_mobility_agent.application.conversations import ConversationRun

    payload_schema_version = row.get("payload_schema_version")
    if payload_schema_version != RUN_PAYLOAD_SCHEMA_VERSION:
        raise AgentStateIncompatibleError(
            context={
                "expected_run_payload_schema_version": RUN_PAYLOAD_SCHEMA_VERSION,
            }
        )
    payload = dict(row)
    payload.pop("payload_schema_version", None)
    payload["usage"] = _usage_from_payload(payload.get("usage"))
    return ConversationRun.model_validate(payload)


def _usage_from_payload(payload: object) -> RunUsage:
    if (
        not isinstance(payload, Mapping)
        or payload.get("schema_version") != RUN_USAGE_SCHEMA_VERSION
    ):
        raise AgentStateIncompatibleError(
            context={"expected_run_usage_schema_version": RUN_USAGE_SCHEMA_VERSION}
        )
    try:
        return RunUsage.model_validate(dict(payload))
    except ValidationError as exc:
        raise AgentStateIncompatibleError(
            context={"expected_run_usage_schema_version": RUN_USAGE_SCHEMA_VERSION}
        ) from exc


def _validate_capacity(capacity: int) -> None:
    if capacity < 1:
        raise ValueError("capacity must be at least 1")


def _validate_optional_capacity(capacity: int | None) -> None:
    if capacity is not None:
        _validate_capacity(capacity)


def _is_resumable(run: ConversationRun) -> bool:
    return run.status == "interrupted" and run.resume_mode != "none"
