from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from langgraph.checkpoint.serde.encrypted import EncryptedSerializer
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from psycopg import AsyncConnection
from psycopg.adapt import AdaptersMap, PyFormat
from psycopg_pool import AsyncConnectionPool

import barrier_free_mobility_agent.persistence.runtime as persistence_runtime
from barrier_free_mobility_agent.application.checkpoint_fencing import (
    LeaseFencedCheckpointSaver,
)
from barrier_free_mobility_agent.config import PersistenceBackend, Settings
from barrier_free_mobility_agent.persistence.postgres import REPOSITORY_SCHEMA_VERSION
from barrier_free_mobility_agent.persistence.runtime import (
    _configure_postgres_connection,
    _execution_payload_cipher,
    _verify_checkpoint_key,
    _verify_execution_payload_keys,
    _verify_postgres_schema,
    open_persistence,
)


class _ConnectionWithAdapters:
    def __init__(self) -> None:
        self.adapters = AdaptersMap()


@pytest.mark.asyncio
async def test_memory_persistence_uses_strict_non_pickle_serializer() -> None:
    settings = Settings(_env_file=None)

    async with open_persistence(settings) as resources:
        assert resources.conversations.persistence == "ephemeral"
        assert isinstance(resources.checkpointer.serde, JsonPlusSerializer)
        assert resources.checkpointer.serde.pickle_fallback is False
        assert await resources.conversations.is_ready() is True


@pytest.mark.asyncio
async def test_checkpoint_encryption_wraps_the_strict_serializer() -> None:
    settings = Settings(
        _env_file=None,
        checkpoint_encryption_key="x" * 32,
    )

    async with open_persistence(settings) as resources:
        serializer = resources.checkpointer.serde
        assert isinstance(serializer, EncryptedSerializer)
        type_name, payload = serializer.dumps_typed({"mobility": "private"})
        assert type_name.endswith("+aes")
        assert b"private" not in payload
        assert serializer.loads_typed((type_name, payload)) == {"mobility": "private"}


@pytest.mark.asyncio
async def test_postgres_pool_forces_empty_strings_to_the_text_binary_oid() -> None:
    connection = _ConnectionWithAdapters()

    await _configure_postgres_connection(cast(AsyncConnection[Any], cast(object, connection)))

    dumper = connection.adapters.get_dumper(str, PyFormat.BINARY)
    assert dumper.oid == 25


@pytest.mark.asyncio
@pytest.mark.parametrize("auto_setup", [True, False])
async def test_postgres_persistence_owns_pool_and_verifies_startup(
    monkeypatch: pytest.MonkeyPatch,
    *,
    auto_setup: bool,
) -> None:
    pool = MagicMock()
    pool.open = AsyncMock()
    pool.close = AsyncMock()
    pool_factory = MagicMock(return_value=pool)
    checkpointer = MagicMock()
    checkpointer.setup = AsyncMock()
    saver_factory = MagicMock(return_value=checkpointer)
    conversations = MagicMock()
    conversations.persistence = "durable"
    conversations.setup = AsyncMock()
    repository_factory = MagicMock(return_value=conversations)
    verify_schema = AsyncMock()
    verify_key = AsyncMock()
    verify_payload_keys = AsyncMock()
    lock_urls: list[str] = []

    @asynccontextmanager
    async def migration_lock(database_url: str) -> AsyncIterator[None]:
        lock_urls.append(database_url)
        yield

    monkeypatch.setattr(persistence_runtime, "AsyncConnectionPool", pool_factory)
    monkeypatch.setattr(persistence_runtime, "AsyncPostgresSaver", saver_factory)
    monkeypatch.setattr(
        persistence_runtime,
        "PostgresConversationRepository",
        repository_factory,
    )
    monkeypatch.setattr(persistence_runtime, "_postgres_migration_lock", migration_lock)
    monkeypatch.setattr(persistence_runtime, "_verify_postgres_schema", verify_schema)
    monkeypatch.setattr(persistence_runtime, "_verify_checkpoint_key", verify_key)
    monkeypatch.setattr(
        persistence_runtime,
        "_verify_execution_payload_keys",
        verify_payload_keys,
    )
    settings = Settings(
        _env_file=None,
        persistence_backend=PersistenceBackend.POSTGRES,
        database_url="postgresql://agent:secret@postgres/agent",
        database_auto_setup=auto_setup,
    )

    async with open_persistence(settings) as resources:
        assert isinstance(resources.checkpointer, LeaseFencedCheckpointSaver)
        assert resources.conversations is conversations

    pool.open.assert_awaited_once_with(wait=True, timeout=10.0)
    pool.close.assert_awaited_once_with(timeout=10.0)
    verify_schema.assert_awaited_once_with(pool)
    verify_key.assert_awaited_once_with(pool, settings, initialize=auto_setup)
    verify_payload_keys.assert_awaited_once_with(pool, None)
    if auto_setup:
        assert lock_urls == ["postgresql://agent:secret@postgres/agent"]
        checkpointer.setup.assert_awaited_once_with()
        conversations.setup.assert_awaited_once_with()
    else:
        assert lock_urls == []
        checkpointer.setup.assert_not_awaited()
        conversations.setup.assert_not_awaited()


@pytest.mark.asyncio
async def test_postgres_schema_verification_checks_versions_and_indexes() -> None:
    cursor = _runtime_cursor(
        rows=[
            {"name": name, "relation": name}
            for name in (
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
        ],
        values=[
            {"repository": REPOSITORY_SCHEMA_VERSION, "checkpoint": 9},
            {"invalid_count": 0},
        ],
    )
    pool = _runtime_pool(cursor)

    await _verify_postgres_schema(cast(AsyncConnectionPool[Any], pool))

    assert cursor.execute.await_count == 3


@pytest.mark.asyncio
async def test_postgres_schema_verification_rejects_missing_tables() -> None:
    cursor = _runtime_cursor(
        rows=[{"name": "agent_runs", "relation": None}],
        values=[
            {"repository": REPOSITORY_SCHEMA_VERSION, "checkpoint": 9},
            {"invalid_count": 0},
        ],
    )
    pool = _runtime_pool(cursor)

    with pytest.raises(RuntimeError, match="schema is not initialized"):
        await _verify_postgres_schema(cast(AsyncConnectionPool[Any], pool))


@pytest.mark.asyncio
async def test_checkpoint_key_verification_initializes_fingerprint() -> None:
    cursor = _runtime_cursor(rows=[], values=[None])
    pool = _runtime_pool(cursor, transaction=True)
    settings = Settings(_env_file=None, checkpoint_encryption_key="x" * 32)

    await _verify_checkpoint_key(
        cast(AsyncConnectionPool[Any], pool),
        settings,
        initialize=True,
    )

    statements = [call.args[0] for call in cursor.execute.await_args_list]
    assert any("pg_advisory_xact_lock" in statement for statement in statements)
    assert any("INSERT INTO agent_runtime_secrets" in statement for statement in statements)


@pytest.mark.asyncio
async def test_checkpoint_key_verification_rejects_a_mismatch() -> None:
    cursor = _runtime_cursor(rows=[], values=[{"fingerprint": "0" * 64}])
    pool = _runtime_pool(cursor, transaction=True)
    settings = Settings(_env_file=None, checkpoint_encryption_key="x" * 32)

    with pytest.raises(RuntimeError, match="does not match"):
        await _verify_checkpoint_key(
            cast(AsyncConnectionPool[Any], pool),
            settings,
            initialize=False,
        )


@pytest.mark.asyncio
async def test_execution_payload_keyring_verification_supports_safe_rotation() -> None:
    settings = Settings(
        _env_file=None,
        execution_payload_encryption_key="a" * 32,
        idempotency_hmac_key="h" * 32,
        execution_payload_previous_encryption_keys=["b" * 32],
    )
    cipher = _execution_payload_cipher(settings)
    assert cipher is not None
    previous_fingerprint = next(
        fingerprint
        for fingerprint in cipher.known_fingerprints
        if fingerprint != cipher.active_fingerprint
    )
    cursor = _runtime_cursor(
        rows=[{"payload_key_fingerprint": previous_fingerprint}],
        values=[],
    )

    await _verify_execution_payload_keys(
        cast(AsyncConnectionPool[Any], _runtime_pool(cursor)),
        cipher,
    )

    unknown_cursor = _runtime_cursor(
        rows=[{"payload_key_fingerprint": "0" * 64}],
        values=[],
    )
    with pytest.raises(RuntimeError, match="cannot decrypt active jobs"):
        await _verify_execution_payload_keys(
            cast(AsyncConnectionPool[Any], _runtime_pool(unknown_cursor)),
            cipher,
        )


def _runtime_cursor(*, rows: list[dict[str, object]], values: list[object]) -> MagicMock:
    cursor = MagicMock()
    cursor.execute = AsyncMock()
    cursor.fetchall = AsyncMock(return_value=rows)
    cursor.fetchone = AsyncMock(side_effect=values)
    return cursor


def _runtime_pool(cursor: MagicMock, *, transaction: bool = False) -> MagicMock:
    cursor_context = MagicMock()
    cursor_context.__aenter__ = AsyncMock(return_value=cursor)
    cursor_context.__aexit__ = AsyncMock(return_value=False)
    connection = MagicMock()
    connection.cursor.return_value = cursor_context
    if transaction:
        transaction_context = MagicMock()
        transaction_context.__aenter__ = AsyncMock(return_value=None)
        transaction_context.__aexit__ = AsyncMock(return_value=False)
        connection.transaction.return_value = transaction_context
    connection_context = MagicMock()
    connection_context.__aenter__ = AsyncMock(return_value=connection)
    connection_context.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.connection.return_value = connection_context
    return pool
