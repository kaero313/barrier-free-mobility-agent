from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import uvicorn
from langgraph.checkpoint.memory import InMemorySaver
from tests.fakes import FakeMcpGateway

import barrier_free_mobility_agent.cli as cli
from barrier_free_mobility_agent.config import PersistenceBackend, Settings
from barrier_free_mobility_agent.domain.execution_queue import ExecutionQueueHealth
from barrier_free_mobility_agent.persistence.memory import MemoryConversationRepository
from barrier_free_mobility_agent.persistence.replica_release_evidence import (
    ReplicaReleaseDatabaseEvidence,
)
from barrier_free_mobility_agent.persistence.worker_registration_state import (
    WorkerRegistrationState,
)


def test_main_starts_uvicorn_with_validated_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(_env_file=None, api_host="127.0.0.1", api_port=9080)
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setattr(sys, "argv", ["mobility-agent"])
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(
        uvicorn,
        "run",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    cli.main()

    assert calls == [
        (
            ("barrier_free_mobility_agent.main:app",),
            {"host": "127.0.0.1", "port": 9080, "log_config": None},
        )
    ]


@pytest.mark.asyncio
async def test_migration_rejects_the_memory_backend() -> None:
    with pytest.raises(SystemExit, match="PERSISTENCE_BACKEND=postgres"):
        await cli.migrate_persistence(Settings(_env_file=None))


@pytest.mark.asyncio
async def test_worker_role_rejects_the_memory_backend() -> None:
    with pytest.raises(SystemExit, match="PERSISTENCE_BACKEND=postgres"):
        await cli.run_execution_worker(Settings(_env_file=None))


@pytest.mark.asyncio
async def test_worker_health_rejects_the_memory_backend() -> None:
    with pytest.raises(SystemExit, match="PERSISTENCE_BACKEND=postgres"):
        await cli.check_execution_worker_health(Settings(_env_file=None))


@pytest.mark.asyncio
async def test_execution_health_rejects_the_memory_backend() -> None:
    with pytest.raises(SystemExit, match="PERSISTENCE_BACKEND=postgres"):
        await cli.report_execution_health(
            Settings(_env_file=None),
            stuck_after=timedelta(seconds=60),
        )


@pytest.mark.asyncio
async def test_execution_health_prints_one_redacted_json_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    checked_at = datetime(2026, 7, 16, 12, tzinfo=UTC)
    snapshot = ExecutionQueueHealth(
        checked_at=checked_at,
        accepting_new_jobs=True,
        ready_worker_count=2,
        queued_job_count=1,
        leased_job_count=1,
        expired_lease_count=0,
        recovery_attempt_count=1,
        stuck_job_count=0,
        oldest_queued_at=checked_at - timedelta(seconds=4),
        oldest_queued_age_seconds=4,
        stuck_after_seconds=12.5,
    )
    observed: list[tuple[str, timedelta, float]] = []

    async def fake_collect_execution_queue_health(
        *,
        database_url: str,
        stuck_after: timedelta,
        timeout_seconds: float,
    ) -> ExecutionQueueHealth:
        observed.append((database_url, stuck_after, timeout_seconds))
        return snapshot

    monkeypatch.setattr(
        cli,
        "collect_execution_queue_health",
        fake_collect_execution_queue_health,
    )
    settings = Settings(
        _env_file=None,
        persistence_backend=PersistenceBackend.POSTGRES,
        database_url="postgresql://mobility:do-not-print@database:5432/mobility",
        database_auto_setup=True,
        database_ready_timeout_seconds=1.25,
    )

    await cli.report_execution_health(
        settings,
        stuck_after=timedelta(seconds=12.5),
    )

    output = capsys.readouterr().out
    assert output.count("\n") == 1
    assert "do-not-print" not in output
    assert json.loads(output) == {
        "schema_version": "1",
        **snapshot.model_dump(mode="json"),
    }
    assert observed == [
        (
            "postgresql://mobility:do-not-print@database:5432/mobility",
            timedelta(seconds=12.5),
            1.25,
        )
    ]


def test_main_dispatches_execution_health_with_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        _env_file=None,
        persistence_backend=PersistenceBackend.POSTGRES,
        database_url="postgresql://mobility:secret@database:5432/mobility",
    )
    observed: list[tuple[Settings, timedelta]] = []

    async def fake_report_execution_health(
        actual_settings: Settings,
        *,
        stuck_after: timedelta,
    ) -> None:
        observed.append((actual_settings, stuck_after))

    monkeypatch.setattr(
        sys,
        "argv",
        ["mobility-agent", "execution-health", "--stuck-after-seconds", "7.5"],
    )
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "report_execution_health", fake_report_execution_health)

    cli.main()

    assert observed == [(settings, timedelta(seconds=7.5))]


@pytest.mark.asyncio
async def test_replica_release_evidence_rejects_memory_and_missing_database_url() -> None:
    window_started_at = datetime(2026, 7, 16, 12, tzinfo=UTC)
    memory_settings = Settings(_env_file=None)
    with pytest.raises(SystemExit, match="PERSISTENCE_BACKEND=postgres"):
        await cli.report_replica_release_evidence(
            memory_settings,
            window_started_at=window_started_at,
        )

    missing_url = memory_settings.model_copy(
        update={
            "persistence_backend": PersistenceBackend.POSTGRES,
            "database_url": None,
        }
    )
    with pytest.raises(SystemExit, match="DATABASE_URL"):
        await cli.report_replica_release_evidence(
            missing_url,
            window_started_at=window_started_at,
        )


@pytest.mark.asyncio
async def test_replica_release_evidence_prints_compact_model_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    window_started_at = datetime(2026, 7, 16, 11, tzinfo=UTC)
    evidence = ReplicaReleaseDatabaseEvidence(
        checked_at=datetime(2026, 7, 16, 12, tzinfo=UTC),
        window_started_at=window_started_at,
        ready_worker_count=2,
        registered_worker_count=2,
        expired_registration_count=0,
        draining_worker_count=0,
        registration_generation_total=3,
        created_job_count=2,
        completed_job_count=2,
        failed_job_count=0,
        active_job_count=0,
        recovered_job_count=1,
        recovery_attempt_count=1,
        max_claim_count=2,
        ambiguous_recovery_count=0,
        terminal_payload_retention_count=0,
        terminal_event_violation_count=0,
        active_lease_without_ready_worker_count=0,
    )
    observed: list[tuple[str, datetime, float]] = []

    async def fake_collect_replica_release_database_evidence(
        *,
        database_url: str,
        window_started_at: datetime,
        timeout_seconds: float,
    ) -> ReplicaReleaseDatabaseEvidence:
        observed.append((database_url, window_started_at, timeout_seconds))
        return evidence

    monkeypatch.setattr(
        cli,
        "collect_replica_release_database_evidence",
        fake_collect_replica_release_database_evidence,
    )
    settings = Settings(
        _env_file=None,
        persistence_backend=PersistenceBackend.POSTGRES,
        database_url="postgresql://mobility:do-not-print@database:5432/mobility",
        database_ready_timeout_seconds=1.25,
    )

    await cli.report_replica_release_evidence(
        settings,
        window_started_at=window_started_at,
    )

    output = capsys.readouterr().out
    assert output.count("\n") == 1
    assert "do-not-print" not in output
    assert " " not in output
    assert json.loads(output) == evidence.model_dump(mode="json")
    assert observed == [
        (
            "postgresql://mobility:do-not-print@database:5432/mobility",
            window_started_at,
            1.25,
        )
    ]


def test_main_dispatches_replica_release_evidence_with_aware_iso_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        _env_file=None,
        persistence_backend=PersistenceBackend.POSTGRES,
        database_url="postgresql://mobility:secret@database:5432/mobility",
    )
    observed: list[tuple[Settings, datetime]] = []

    async def fake_report_replica_release_evidence(
        actual_settings: Settings,
        *,
        window_started_at: datetime,
    ) -> None:
        observed.append((actual_settings, window_started_at))

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "mobility-agent",
            "replica-release-evidence",
            "--window-started-at",
            "2026-07-16T12:34:56+09:00",
        ],
    )
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(
        cli,
        "report_replica_release_evidence",
        fake_report_replica_release_evidence,
    )

    cli.main()

    assert observed == [(settings, datetime.fromisoformat("2026-07-16T12:34:56+09:00"))]


@pytest.mark.parametrize(
    "arguments",
    [
        ["mobility-agent", "replica-release-evidence"],
        [
            "mobility-agent",
            "replica-release-evidence",
            "--window-started-at",
            "2026-07-16T12:34:56",
        ],
    ],
)
def test_main_rejects_missing_or_naive_replica_release_window(
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
) -> None:
    monkeypatch.setattr(sys, "argv", arguments)

    with pytest.raises(SystemExit):
        cli.main()


@pytest.mark.asyncio
@pytest.mark.parametrize("ready", [True, False])
async def test_worker_health_uses_a_read_only_exact_worker_probe(
    monkeypatch: pytest.MonkeyPatch,
    ready: bool,
) -> None:
    observed_database_urls: list[str] = []
    observed_worker_ids: list[str] = []
    observed_registration_tokens: list[str] = []
    observed_registration_generations: list[int] = []
    observed_timeouts: list[float] = []

    async def fake_probe(
        *,
        database_url: str,
        worker_id: str,
        registration_token: str,
        registration_generation: int,
        timeout_seconds: float,
    ) -> bool:
        observed_database_urls.append(database_url)
        observed_worker_ids.append(worker_id)
        observed_registration_tokens.append(registration_token)
        observed_registration_generations.append(registration_generation)
        observed_timeouts.append(timeout_seconds)
        return ready

    monkeypatch.setattr(cli, "probe_worker_registration_ready", fake_probe)
    monkeypatch.setattr(
        cli,
        "read_worker_registration_state",
        lambda: WorkerRegistrationState(
            worker_id="probe-worker",
            token="probe-token",
            generation=7,
        ),
    )
    settings = Settings(
        _env_file=None,
        persistence_backend=PersistenceBackend.POSTGRES,
        database_url="postgresql://mobility:secret@database:5432/mobility",
        database_auto_setup=True,
        execution_worker_id="probe-worker",
    )

    if ready:
        await cli.check_execution_worker_health(settings)
    else:
        with pytest.raises(SystemExit, match="not ready"):
            await cli.check_execution_worker_health(settings)

    assert observed_worker_ids == ["probe-worker"]
    assert observed_registration_tokens == ["probe-token"]
    assert observed_registration_generations == [7]
    assert observed_database_urls == ["postgresql://mobility:secret@database:5432/mobility"]
    assert observed_timeouts == [settings.database_ready_timeout_seconds]


@pytest.mark.asyncio
async def test_worker_role_rejects_missing_execution_keys() -> None:
    settings = Settings(
        _env_file=None,
        persistence_backend=PersistenceBackend.POSTGRES,
        database_url="postgresql://mobility:secret@database:5432/mobility",
    )
    with pytest.raises(SystemExit, match="EXECUTION_PAYLOAD_ENCRYPTION_KEY"):
        await cli.run_execution_worker(settings)


@pytest.mark.asyncio
async def test_worker_role_owns_gateway_and_persistence_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    gateway = FakeMcpGateway()
    repository = MemoryConversationRepository()

    @asynccontextmanager
    async def fake_open_persistence(settings: Settings) -> AsyncIterator[object]:
        del settings
        yield SimpleNamespace(
            checkpointer=InMemorySaver(),
            conversations=repository,
        )

    monkeypatch.setattr(cli, "open_persistence", fake_open_persistence)
    registration_state_path = tmp_path / "worker-registration.json"
    monkeypatch.setenv("EXECUTION_WORKER_STATE_FILE", str(registration_state_path))
    stop = asyncio.Event()
    stop.set()
    settings = Settings(
        _env_file=None,
        persistence_backend=PersistenceBackend.POSTGRES,
        database_url="postgresql://mobility:secret@database:5432/mobility",
        execution_payload_encryption_key="p" * 32,
        idempotency_hmac_key="h" * 32,
    )

    await cli.run_execution_worker(settings, stop_event=stop, mcp_gateway=gateway)

    assert gateway.initialize_calls == 1
    assert gateway.closed is True
    assert registration_state_path.exists() is False


@pytest.mark.asyncio
async def test_worker_role_exits_when_its_presence_registration_is_replaced(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    gateway = FakeMcpGateway()
    repository = MemoryConversationRepository()

    @asynccontextmanager
    async def fake_open_persistence(settings: Settings) -> AsyncIterator[object]:
        del settings
        yield SimpleNamespace(
            checkpointer=InMemorySaver(),
            conversations=repository,
        )

    monkeypatch.setattr(cli, "open_persistence", fake_open_persistence)
    registration_state_path = tmp_path / "worker-registration.json"
    monkeypatch.setenv("EXECUTION_WORKER_STATE_FILE", str(registration_state_path))
    settings = Settings(
        _env_file=None,
        persistence_backend=PersistenceBackend.POSTGRES,
        database_url="postgresql://mobility:secret@database:5432/mobility",
        execution_payload_encryption_key="p" * 32,
        idempotency_hmac_key="h" * 32,
        execution_worker_id="fenced-worker",
        execution_worker_lease_seconds=0.2,
        execution_worker_heartbeat_seconds=0.02,
        execution_worker_readiness_timeout_seconds=0.1,
        execution_worker_poll_seconds=0.01,
        execution_worker_max_error_backoff_seconds=0.02,
        execution_worker_shutdown_grace_seconds=0.2,
    )
    running = asyncio.create_task(
        cli.run_execution_worker(
            settings,
            stop_event=asyncio.Event(),
            mcp_gateway=gateway,
        )
    )
    for _ in range(100):
        if registration_state_path.exists():
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("worker registration state was not published")

    await repository.register_execution_worker(
        "fenced-worker",
        heartbeat_ttl=timedelta(seconds=1),
    )

    with pytest.raises(RuntimeError, match="WORKER_REGISTRATION_LOST"):
        await asyncio.wait_for(running, timeout=2)
    assert registration_state_path.exists() is False
    assert gateway.closed is True


@pytest.mark.asyncio
async def test_migration_forces_setup_and_closes_resources(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    observed: list[Settings] = []

    @asynccontextmanager
    async def fake_open_persistence(settings: Settings) -> AsyncIterator[object]:
        observed.append(settings)
        yield object()

    settings = Settings(
        _env_file=None,
        persistence_backend=PersistenceBackend.POSTGRES,
        database_url="postgresql://mobility:secret@database:5432/mobility",
        database_auto_setup=False,
    )
    monkeypatch.setattr(cli, "open_persistence", fake_open_persistence)

    await cli.migrate_persistence(settings)

    assert len(observed) == 1
    assert observed[0].database_auto_setup is True
    assert capsys.readouterr().out == "PostgreSQL persistence migrations completed.\n"
