from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from hashlib import sha256
from secrets import compare_digest
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.base import SerializerProtocol
from langgraph.checkpoint.serde.encrypted import EncryptedSerializer
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from psycopg import AsyncConnection, Error
from psycopg.rows import dict_row
from psycopg.types.string import StrBinaryDumper
from psycopg_pool import AsyncConnectionPool

from barrier_free_mobility_agent.application.checkpoint_fencing import (
    LeaseFencedCheckpointSaver,
)
from barrier_free_mobility_agent.application.conversation_repository import (
    ConversationRepository,
)
from barrier_free_mobility_agent.config import PersistenceBackend, Settings
from barrier_free_mobility_agent.persistence.execution_payload_cipher import (
    ExecutionPayloadCipher,
)
from barrier_free_mobility_agent.persistence.memory import MemoryConversationRepository
from barrier_free_mobility_agent.persistence.postgres import (
    REPOSITORY_SCHEMA_VERSION,
    PostgresConversationRepository,
)


@dataclass(frozen=True, slots=True)
class PersistenceResources:
    checkpointer: BaseCheckpointSaver[str]
    conversations: ConversationRepository


@asynccontextmanager
async def open_persistence(settings: Settings) -> AsyncIterator[PersistenceResources]:
    """Own the checkpointer and repository for exactly one application lifespan."""

    serializer = _checkpoint_serializer(settings)
    if settings.persistence_backend == PersistenceBackend.MEMORY:
        memory_conversations = MemoryConversationRepository()
        yield PersistenceResources(
            checkpointer=LeaseFencedCheckpointSaver(
                InMemorySaver(serde=serializer),
                memory_conversations,
            ),
            conversations=memory_conversations,
        )
        return

    configured_url = settings.database_url
    if configured_url is None:  # Settings validation makes this unreachable.
        raise RuntimeError("PostgreSQL persistence is missing DATABASE_URL.")

    pool: AsyncConnectionPool[Any] = AsyncConnectionPool(
        conninfo=configured_url.get_secret_value(),
        kwargs={
            "autocommit": True,
            "prepare_threshold": 0,
            "row_factory": dict_row,
        },
        min_size=settings.database_pool_min_size,
        max_size=settings.database_pool_max_size,
        timeout=settings.database_connect_timeout_seconds,
        configure=_configure_postgres_connection,
        open=False,
        name="mobility-agent",
    )
    try:
        await pool.open(
            wait=True,
            timeout=settings.database_connect_timeout_seconds,
        )
        checkpointer = AsyncPostgresSaver(pool, serde=serializer)
        payload_cipher = _execution_payload_cipher(settings)
        conversations = PostgresConversationRepository(
            pool,
            ready_timeout_seconds=settings.database_ready_timeout_seconds,
            execution_payload_cipher=payload_cipher,
        )
        if settings.database_auto_setup:
            async with _postgres_migration_lock(configured_url.get_secret_value()):
                await checkpointer.setup()
                await conversations.setup()
        await _verify_postgres_schema(pool)
        await _verify_checkpoint_key(
            pool,
            settings,
            initialize=settings.database_auto_setup,
        )
        await _verify_execution_payload_keys(
            pool,
            payload_cipher,
        )
        yield PersistenceResources(
            checkpointer=LeaseFencedCheckpointSaver(checkpointer, conversations),
            conversations=conversations,
        )
    finally:
        await pool.close(timeout=settings.database_connect_timeout_seconds)


def _checkpoint_serializer(settings: Settings) -> SerializerProtocol:
    serializer: SerializerProtocol = JsonPlusSerializer(
        pickle_fallback=False,
        allowed_json_modules=None,
        allowed_msgpack_modules=None,
    )
    encryption_key = settings.checkpoint_encryption_key
    if encryption_key is None:
        return serializer
    return EncryptedSerializer.from_pycryptodome_aes(
        serializer,
        key=encryption_key.get_secret_value().encode("utf-8"),
    )


def _execution_payload_cipher(settings: Settings) -> ExecutionPayloadCipher | None:
    active = settings.execution_payload_encryption_key
    if active is None:
        return None
    return ExecutionPayloadCipher(
        active.get_secret_value().encode("utf-8"),
        previous_keys=tuple(
            key.get_secret_value().encode("utf-8")
            for key in settings.execution_payload_previous_encryption_keys
        ),
    )


async def _configure_postgres_connection(connection: AsyncConnection[Any]) -> None:
    # AsyncPostgresSaver 3.1.0 uses binary cursors. psycopg 3.3.x otherwise adapts
    # the empty root checkpoint namespace as bytea, which PostgreSQL cannot compare
    # with the checkpointer's text column (langgraph issue #7916).
    connection.adapters.register_dumper(str, StrBinaryDumper)


@asynccontextmanager
async def _postgres_migration_lock(database_url: str) -> AsyncIterator[None]:
    connection = await AsyncConnection.connect(
        database_url,
        autocommit=True,
        prepare_threshold=0,
        row_factory=dict_row,
    )
    await _configure_postgres_connection(connection)
    lock_id = 4_820_191_825_733_004_177
    try:
        await connection.execute("SELECT pg_advisory_lock(%s)", (lock_id,))
        yield
    finally:
        with suppress(Error):
            await connection.execute("SELECT pg_advisory_unlock(%s)", (lock_id,))
        await connection.close()


async def _verify_postgres_schema(pool: AsyncConnectionPool[Any]) -> None:
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
    async with pool.connection() as connection, connection.cursor() as cursor:
        await cursor.execute(
            "SELECT name, to_regclass(name) AS relation FROM unnest(%s::text[]) AS name",
            (list(required_tables),),
        )
        missing = [row["name"] for row in await cursor.fetchall() if row["relation"] is None]
        if missing:
            raise RuntimeError("PostgreSQL persistence schema is not initialized.")
        await cursor.execute(
            """
            SELECT
                (SELECT max(version) FROM agent_repository_migrations) AS repository,
                (SELECT max(v) FROM checkpoint_migrations) AS checkpoint
            """
        )
        versions = await cursor.fetchone()
        await cursor.execute(
            """
            SELECT count(*) AS invalid_count
            FROM pg_index AS index_state
            JOIN pg_class AS indexed_table
              ON indexed_table.oid = index_state.indrelid
            JOIN pg_namespace AS table_namespace
              ON table_namespace.oid = indexed_table.relnamespace
            WHERE table_namespace.nspname = current_schema()
              AND indexed_table.relname = ANY(%s::text[])
              AND NOT index_state.indisvalid
            """,
            (
                [
                    "agent_threads",
                    "agent_runs",
                    "run_events",
                    "tool_executions",
                    "run_execution_jobs",
                    "execution_worker_registry",
                    "checkpoints",
                    "checkpoint_writes",
                ],
            ),
        )
        invalid_indexes = await cursor.fetchone()
    if versions is None or versions["repository"] != REPOSITORY_SCHEMA_VERSION:
        raise RuntimeError("PostgreSQL conversation schema migration is not current.")
    if versions["checkpoint"] != len(AsyncPostgresSaver.MIGRATIONS) - 1:
        raise RuntimeError("PostgreSQL checkpoint schema migration is not current.")
    if invalid_indexes is None or invalid_indexes["invalid_count"] != 0:
        raise RuntimeError("PostgreSQL persistence contains an invalid index.")


async def _verify_checkpoint_key(
    pool: AsyncConnectionPool[Any],
    settings: Settings,
    *,
    initialize: bool,
) -> None:
    configured_key = settings.checkpoint_encryption_key
    fingerprint = (
        sha256(
            b"barrier-free-mobility:checkpoint-key:v1\0"
            + configured_key.get_secret_value().encode("utf-8")
        ).hexdigest()
        if configured_key is not None
        else None
    )
    async with (
        pool.connection() as connection,
        connection.transaction(),
        connection.cursor() as cursor,
    ):
        if initialize:
            await cursor.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (7_218_209_591_447_113_003,),
            )
        await cursor.execute(
            "SELECT fingerprint FROM agent_runtime_secrets WHERE name = %s",
            ("checkpoint-aes-v1",),
        )
        row = await cursor.fetchone()
        if row is None and fingerprint is not None and initialize:
            await cursor.execute(
                """
                INSERT INTO agent_runtime_secrets (name, fingerprint)
                VALUES (%s, %s)
                """,
                ("checkpoint-aes-v1", fingerprint),
            )
            return
    if row is None:
        if fingerprint is None:
            return
        raise RuntimeError("PostgreSQL checkpoint encryption key is not initialized.")
    if fingerprint is None or not compare_digest(row["fingerprint"], fingerprint):
        raise RuntimeError("PostgreSQL checkpoint encryption key does not match this database.")


async def _verify_execution_payload_keys(
    pool: AsyncConnectionPool[Any],
    cipher: ExecutionPayloadCipher | None,
) -> None:
    """Fail startup when an active job cannot be decrypted by the configured keyring."""

    async with pool.connection() as connection, connection.cursor() as cursor:
        await cursor.execute(
            """
            SELECT DISTINCT payload_key_fingerprint
            FROM run_execution_jobs
            WHERE state IN ('queued', 'leased')
            """
        )
        fingerprints = {
            row["payload_key_fingerprint"]
            for row in await cursor.fetchall()
            if row["payload_key_fingerprint"] is not None
        }
    if not fingerprints:
        return
    if cipher is None or not fingerprints.issubset(cipher.known_fingerprints):
        raise RuntimeError(
            "PostgreSQL execution payload encryption keyring cannot decrypt active jobs."
        )
