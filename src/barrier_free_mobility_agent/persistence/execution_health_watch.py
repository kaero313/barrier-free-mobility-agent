from __future__ import annotations

import argparse
import asyncio
import math
import os
import sys
import threading
from collections.abc import Callable
from datetime import timedelta
from typing import Annotated, BinaryIO, Literal

from psycopg import Error as PsycopgError
from psycopg_pool import PoolClosed, PoolTimeout
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from barrier_free_mobility_agent.domain.execution_queue import ExecutionQueueHealth
from barrier_free_mobility_agent.persistence.execution_health import (
    collect_execution_queue_health,
)

_MIN_DURATION_SECONDS = 0.1
_MAX_STUCK_AFTER_SECONDS = 86_400.0
_MAX_INTERVAL_SECONDS = 60.0
_MAX_QUERY_TIMEOUT_SECONDS = 30.0


class ExecutionHealthSnapshotEvent(BaseModel):
    """One identifier-free, validated queue-health stream event."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    event: Literal["snapshot"] = "snapshot"
    snapshot: ExecutionQueueHealth


class ExecutionHealthUnavailableEvent(BaseModel):
    """Stable availability signal that never contains an exception or connection data."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    event: Literal["unavailable"] = "unavailable"
    error_code: Literal["execution_health_unavailable"] = "execution_health_unavailable"


type ExecutionHealthWatchEvent = Annotated[
    ExecutionHealthSnapshotEvent | ExecutionHealthUnavailableEvent,
    Field(discriminator="event"),
]
_EVENT_ADAPTER: TypeAdapter[ExecutionHealthWatchEvent] = TypeAdapter(ExecutionHealthWatchEvent)
_TRANSIENT_ERRORS = (TimeoutError, OSError, PsycopgError, PoolClosed, PoolTimeout)


def parse_execution_health_watch_event(payload: str) -> ExecutionHealthWatchEvent:
    """Parse one closed watch-protocol line."""

    return _EVENT_ADAPTER.validate_json(payload, strict=True)


async def run_execution_health_watch(
    *,
    database_url: str,
    stuck_after: timedelta,
    interval_seconds: float,
    timeout_seconds: float,
    stop_event: threading.Event,
    emit: Callable[[str], None],
) -> None:
    """Collect bounded snapshots until stdin (or the caller) requests shutdown."""

    _validate_runtime_inputs(
        database_url=database_url,
        stuck_after=stuck_after,
        interval_seconds=interval_seconds,
        timeout_seconds=timeout_seconds,
    )
    loop = asyncio.get_running_loop()
    next_sample_at = loop.time()
    while not stop_event.is_set():
        try:
            snapshot = await collect_execution_queue_health(
                database_url=database_url,
                stuck_after=stuck_after,
                timeout_seconds=timeout_seconds,
            )
        except _TRANSIENT_ERRORS:
            event: ExecutionHealthWatchEvent = ExecutionHealthUnavailableEvent()
        else:
            event = ExecutionHealthSnapshotEvent(snapshot=snapshot)
        emit(event.model_dump_json())
        next_sample_at += interval_seconds
        await _wait_until_or_stopped(stop_event, deadline=next_sample_at)


def main(argv: list[str] | None = None) -> None:
    """Run a small, read-only stream without booting Settings, MCP, or persistence pools."""

    parser = argparse.ArgumentParser(prog="execution-health-watch")
    parser.add_argument(
        "--stuck-after-seconds",
        required=True,
        type=lambda value: _bounded_seconds(
            value,
            maximum=_MAX_STUCK_AFTER_SECONDS,
        ),
    )
    parser.add_argument(
        "--interval-seconds",
        required=True,
        type=lambda value: _bounded_seconds(
            value,
            maximum=_MAX_INTERVAL_SECONDS,
        ),
    )
    arguments = parser.parse_args(argv)
    if os.getenv("PERSISTENCE_BACKEND", "memory").strip().lower() != "postgres":
        raise SystemExit("PERSISTENCE_BACKEND=postgres is required for execution health watch.")
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise SystemExit("DATABASE_URL is required for execution health watch.")
    timeout_seconds = _bounded_environment_seconds(
        "DATABASE_READY_TIMEOUT_SECONDS",
        default=2.0,
        maximum=_MAX_QUERY_TIMEOUT_SECONDS,
    )
    stop_event = threading.Event()
    stdin_thread = threading.Thread(
        target=_request_stop_on_stdin,
        args=(sys.stdin.buffer, stop_event),
        name="execution-health-watch-stdin",
        daemon=True,
    )
    stdin_thread.start()
    try:
        asyncio.run(
            run_execution_health_watch(
                database_url=database_url,
                stuck_after=timedelta(seconds=arguments.stuck_after_seconds),
                interval_seconds=arguments.interval_seconds,
                timeout_seconds=timeout_seconds,
                stop_event=stop_event,
                emit=_emit_line,
            )
        )
    except BrokenPipeError:
        pass
    finally:
        stop_event.set()


async def _wait_until_or_stopped(
    stop_event: threading.Event,
    *,
    deadline: float,
) -> None:
    loop = asyncio.get_running_loop()
    while not stop_event.is_set():
        remaining = deadline - loop.time()
        if remaining <= 0:
            return
        await asyncio.sleep(min(remaining, 0.1))


def _request_stop_on_stdin(stream: BinaryIO, stop_event: threading.Event) -> None:
    try:
        stream.readline()
    except OSError:
        pass
    finally:
        stop_event.set()


def _emit_line(payload: str) -> None:
    sys.stdout.write(payload)
    sys.stdout.write("\n")
    sys.stdout.flush()


def _validate_runtime_inputs(
    *,
    database_url: str,
    stuck_after: timedelta,
    interval_seconds: float,
    timeout_seconds: float,
) -> None:
    if not database_url.strip():
        raise ValueError("database_url must not be blank")
    _require_bounded_duration(
        stuck_after.total_seconds(),
        name="stuck_after",
        maximum=_MAX_STUCK_AFTER_SECONDS,
    )
    _require_bounded_duration(
        interval_seconds,
        name="interval_seconds",
        maximum=_MAX_INTERVAL_SECONDS,
    )
    _require_bounded_duration(
        timeout_seconds,
        name="timeout_seconds",
        maximum=_MAX_QUERY_TIMEOUT_SECONDS,
    )


def _bounded_seconds(value: str, *, maximum: float) -> float:
    try:
        seconds = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a bounded positive number") from exc
    try:
        _require_bounded_duration(seconds, name="value", maximum=maximum)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a bounded positive number") from exc
    return seconds


def _bounded_environment_seconds(
    name: str,
    *,
    default: float,
    maximum: float,
) -> float:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        value = float(raw_value)
        _require_bounded_duration(value, name=name, maximum=maximum)
    except ValueError as exc:
        raise SystemExit(f"{name} must be a bounded positive number.") from exc
    return value


def _require_bounded_duration(value: float, *, name: str, maximum: float) -> None:
    if not math.isfinite(value) or value < _MIN_DURATION_SECONDS or value > maximum:
        raise ValueError(f"{name} must be between {_MIN_DURATION_SECONDS} and {maximum}")


if __name__ == "__main__":
    main()
