from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import signal
from datetime import datetime, timedelta

import uvicorn

from barrier_free_mobility_agent.agent.graph import MobilityGraphRunner
from barrier_free_mobility_agent.application.execution_worker import (
    DurableExecutionWorker,
    ExecutionWorkerOptions,
)
from barrier_free_mobility_agent.application.ports import MobilityMcpGateway
from barrier_free_mobility_agent.config import PersistenceBackend, Settings, get_settings
from barrier_free_mobility_agent.domain.errors import AgentError
from barrier_free_mobility_agent.integrations.mcp.gateway import LangChainMcpGateway
from barrier_free_mobility_agent.observability.logging import configure_logging
from barrier_free_mobility_agent.persistence.execution_health import (
    collect_execution_queue_health,
)
from barrier_free_mobility_agent.persistence.replica_release_evidence import (
    collect_replica_release_database_evidence,
)
from barrier_free_mobility_agent.persistence.runtime import open_persistence
from barrier_free_mobility_agent.persistence.worker_health import (
    probe_worker_registration_ready,
)
from barrier_free_mobility_agent.persistence.worker_registration_state import (
    WorkerRegistrationState,
    publish_worker_registration_state,
    read_worker_registration_state,
    remove_worker_registration_state,
    resolve_execution_worker_id,
)

logger = logging.getLogger(__name__)

_DEFAULT_EXECUTION_HEALTH_STUCK_AFTER_SECONDS = 60.0


def main() -> None:
    parser = argparse.ArgumentParser(prog="mobility-agent")
    parser.add_argument(
        "command",
        choices=(
            "serve",
            "migrate",
            "worker",
            "worker-health",
            "execution-health",
            "replica-release-evidence",
        ),
        default="serve",
        nargs="?",
    )
    parser.add_argument(
        "--stuck-after-seconds",
        default=_DEFAULT_EXECUTION_HEALTH_STUCK_AFTER_SECONDS,
        type=_positive_seconds,
        help="Active-job age threshold used by the internal execution-health snapshot.",
    )
    parser.add_argument(
        "--window-started-at",
        type=_timezone_aware_datetime,
        help="Timezone-aware ISO-8601 start of the replica release evidence window.",
    )
    arguments = parser.parse_args()
    command = arguments.command
    if command == "replica-release-evidence" and arguments.window_started_at is None:
        parser.error("--window-started-at is required for replica-release-evidence")
    settings = get_settings()
    if command == "migrate":
        asyncio.run(migrate_persistence(settings))
        return
    if command == "worker":
        asyncio.run(run_execution_worker(settings))
        return
    if command == "worker-health":
        asyncio.run(check_execution_worker_health(settings))
        return
    if command == "execution-health":
        asyncio.run(
            report_execution_health(
                settings,
                stuck_after=timedelta(seconds=arguments.stuck_after_seconds),
            )
        )
        return
    if command == "replica-release-evidence":
        asyncio.run(
            report_replica_release_evidence(
                settings,
                window_started_at=arguments.window_started_at,
            )
        )
        return
    uvicorn.run(
        "barrier_free_mobility_agent.main:app",
        host=settings.api_host,
        port=settings.api_port,
        log_config=None,
    )


async def migrate_persistence(settings: Settings) -> None:
    if settings.persistence_backend != PersistenceBackend.POSTGRES:
        raise SystemExit("PERSISTENCE_BACKEND=postgres is required for migration.")
    migration_settings = settings.model_copy(update={"database_auto_setup": True})
    async with open_persistence(migration_settings):
        pass
    print("PostgreSQL persistence migrations completed.")


async def run_execution_worker(
    settings: Settings,
    *,
    stop_event: asyncio.Event | None = None,
    mcp_gateway: MobilityMcpGateway | None = None,
) -> None:
    """Run the durable executor as a process role separate from FastAPI."""

    if settings.persistence_backend != PersistenceBackend.POSTGRES:
        raise SystemExit("PERSISTENCE_BACKEND=postgres is required for the worker role.")
    if settings.execution_payload_encryption_key is None or settings.idempotency_hmac_key is None:
        raise SystemExit(
            "EXECUTION_PAYLOAD_ENCRYPTION_KEY and IDEMPOTENCY_HMAC_KEY "
            "are required for the worker role."
        )
    configure_logging(settings.log_level, settings.log_format)
    gateway = mcp_gateway or LangChainMcpGateway(settings)
    shutdown = stop_event or asyncio.Event()
    worker_id = resolve_execution_worker_id(settings.execution_worker_id)
    if stop_event is None:
        _install_shutdown_signals(shutdown)
    try:
        async with open_persistence(settings) as persistence:
            runner = MobilityGraphRunner(
                gateway,
                settings,
                checkpointer=persistence.checkpointer,
                run_ledger=persistence.conversations,
            )
            worker = DurableExecutionWorker(
                runner,
                persistence.conversations,
                worker_id=worker_id,
                options=ExecutionWorkerOptions(
                    lease_duration=timedelta(seconds=settings.execution_worker_lease_seconds),
                    heartbeat_interval=timedelta(
                        seconds=settings.execution_worker_heartbeat_seconds
                    ),
                    readiness_timeout=timedelta(
                        seconds=settings.execution_worker_readiness_timeout_seconds
                    ),
                    poll_interval=timedelta(seconds=settings.execution_worker_poll_seconds),
                    max_error_backoff=timedelta(
                        seconds=settings.execution_worker_max_error_backoff_seconds
                    ),
                    shutdown_grace=timedelta(
                        seconds=settings.execution_worker_shutdown_grace_seconds
                    ),
                ),
            )
            try:
                await gateway.initialize()
            except AgentError:
                logger.exception("MCP contract initialization failed in execution worker")
                if settings.mcp_startup_required:
                    raise
            await worker.start()
            registration_state: WorkerRegistrationState | None = None
            waiters: tuple[asyncio.Task[object], ...] = ()
            try:
                registration = worker.registration
                if registration is None:
                    raise RuntimeError("execution worker registration was not published")
                registration_state = WorkerRegistrationState(
                    worker_id=registration.worker_id,
                    token=registration.token,
                    generation=registration.generation,
                )
                publish_worker_registration_state(registration_state)
                logger.info(
                    "Durable execution worker ready: worker_id=%s",
                    worker_id,
                )
                shutdown_wait = asyncio.create_task(
                    shutdown.wait(),
                    name="execution-worker-shutdown",
                )
                internal_exit = asyncio.create_task(
                    worker.wait_for_internal_exit(),
                    name="execution-worker-internal-exit",
                )
                waiters = (shutdown_wait, internal_exit)
                done, _ = await asyncio.wait(
                    waiters,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if internal_exit in done and not shutdown.is_set():
                    await internal_exit
                    error_code = worker.readiness.last_error_code or "WORKER_LOOP_STOPPED"
                    raise RuntimeError(f"durable execution worker stopped internally: {error_code}")
            finally:
                for waiter in waiters:
                    if not waiter.done():
                        waiter.cancel()
                if waiters:
                    await asyncio.gather(*waiters, return_exceptions=True)
                logger.info(
                    "Durable execution worker draining: worker_id=%s",
                    worker_id,
                )
                try:
                    await worker.stop()
                finally:
                    if registration_state is not None:
                        remove_worker_registration_state(expected=registration_state)
    finally:
        await gateway.close()


async def check_execution_worker_health(settings: Settings) -> None:
    """Exit successfully only for this configured worker's fresh ready registration."""

    if settings.persistence_backend != PersistenceBackend.POSTGRES:
        raise SystemExit("PERSISTENCE_BACKEND=postgres is required for worker health checks.")
    configured_url = settings.database_url
    if configured_url is None:
        raise SystemExit("DATABASE_URL is required for worker health checks.")
    worker_id = resolve_execution_worker_id(settings.execution_worker_id)
    registration = read_worker_registration_state()
    if registration is None or registration.worker_id != worker_id:
        raise SystemExit("Execution worker registration state is missing or invalid.")
    try:
        ready = await probe_worker_registration_ready(
            database_url=configured_url.get_secret_value(),
            worker_id=worker_id,
            registration_token=registration.token,
            registration_generation=registration.generation,
            timeout_seconds=min(
                settings.database_connect_timeout_seconds,
                settings.database_ready_timeout_seconds,
            ),
        )
    except Exception as exc:
        raise SystemExit("Execution worker health check failed.") from exc
    if not ready:
        raise SystemExit("Execution worker is not ready.")


async def report_execution_health(
    settings: Settings,
    *,
    stuck_after: timedelta,
) -> None:
    """Print one privacy-safe internal queue-health snapshot as compact JSON."""

    if settings.persistence_backend != PersistenceBackend.POSTGRES:
        raise SystemExit("PERSISTENCE_BACKEND=postgres is required for execution health checks.")
    configured_url = settings.database_url
    if configured_url is None:
        raise SystemExit("DATABASE_URL is required for execution health checks.")
    snapshot = await collect_execution_queue_health(
        database_url=configured_url.get_secret_value(),
        stuck_after=stuck_after,
        timeout_seconds=settings.database_ready_timeout_seconds,
    )
    payload = {
        "schema_version": "1",
        **snapshot.model_dump(mode="json"),
    }
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


async def report_replica_release_evidence(
    settings: Settings,
    *,
    window_started_at: datetime,
) -> None:
    """Print one redacted, read-only replica release DB evidence snapshot."""

    if settings.persistence_backend != PersistenceBackend.POSTGRES:
        raise SystemExit("PERSISTENCE_BACKEND=postgres is required for replica release evidence.")
    configured_url = settings.database_url
    if configured_url is None:
        raise SystemExit("DATABASE_URL is required for replica release evidence.")
    evidence = await collect_replica_release_database_evidence(
        database_url=configured_url.get_secret_value(),
        window_started_at=window_started_at,
        timeout_seconds=settings.database_ready_timeout_seconds,
    )
    print(evidence.model_dump_json())


def _positive_seconds(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive number") from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("must be a positive number")
    return seconds


def _timezone_aware_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an ISO-8601 datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("must include a timezone offset")
    return parsed


def _install_shutdown_signals(stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for signal_number in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signal_number, stop_event.set)
        except (NotImplementedError, RuntimeError):
            # Windows Proactor loops and embedded runtimes may not expose signal handlers.
            continue


if __name__ == "__main__":
    main()
