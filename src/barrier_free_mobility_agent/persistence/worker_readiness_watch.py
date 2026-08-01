from __future__ import annotations

import json
import math
import os
import select
import sys
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, TextIO

from psycopg import Connection
from psycopg.rows import dict_row

from barrier_free_mobility_agent.persistence.worker_registration_state import (
    WorkerRegistrationState,
    read_worker_registration_state,
    resolve_execution_worker_id,
)

_PROTOCOL_SCHEMA = 1
_SAMPLE_INTERVAL_SECONDS = 0.25
_MAX_CONNECT_TIMEOUT_SECONDS = 5.0
_MAX_STATEMENT_TIMEOUT_SECONDS = 1.0
_GENERIC_FAILURE_MESSAGE = "Worker readiness watch failed."

_EXACT_REGISTRATION_READY_SQL = """
SELECT EXISTS (
    SELECT 1
    FROM execution_worker_registry
    WHERE worker_id = %s
      AND token = %s
      AND generation = %s
      AND state = 'ready'
      AND expires_at > clock_timestamp()
) AS ready
"""


class WorkerReadinessWatchConfigurationError(RuntimeError):
    """Private configuration failure whose message is never projected by the CLI."""


class WorkerReadinessWatchRuntimeError(RuntimeError):
    """Private protocol/runtime failure whose message is never projected by the CLI."""


@dataclass(frozen=True, slots=True)
class WorkerReadinessWatchOptions:
    sample_interval_seconds: float = _SAMPLE_INTERVAL_SECONDS
    connect_timeout_seconds: float = _MAX_CONNECT_TIMEOUT_SECONDS
    statement_timeout_seconds: float = _MAX_STATEMENT_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        _require_positive_finite(
            self.sample_interval_seconds,
            field="sample_interval_seconds",
        )
        _require_positive_finite(
            self.connect_timeout_seconds,
            field="connect_timeout_seconds",
        )
        _require_positive_finite(
            self.statement_timeout_seconds,
            field="statement_timeout_seconds",
        )
        if self.sample_interval_seconds != _SAMPLE_INTERVAL_SECONDS:
            raise ValueError("sample_interval_seconds must be 0.25")
        if self.connect_timeout_seconds > _MAX_CONNECT_TIMEOUT_SECONDS:
            raise ValueError("connect_timeout_seconds exceeds its bound")
        if self.statement_timeout_seconds > _MAX_STATEMENT_TIMEOUT_SECONDS:
            raise ValueError("statement_timeout_seconds exceeds its bound")


@dataclass(frozen=True, slots=True)
class WorkerReadinessWatchConfiguration:
    database_url: str
    registration: WorkerRegistrationState
    options: WorkerReadinessWatchOptions


def load_worker_readiness_watch_configuration() -> WorkerReadinessWatchConfiguration:
    """Load only the local Worker proof and bounded PostgreSQL watch options."""

    if os.getenv("PERSISTENCE_BACKEND", "memory").strip().lower() != "postgres":
        raise WorkerReadinessWatchConfigurationError("persistence backend is invalid")
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise WorkerReadinessWatchConfigurationError("database URL is missing")
    try:
        worker_id = resolve_execution_worker_id(os.getenv("EXECUTION_WORKER_ID", "worker-1"))
    except ValueError as exc:
        raise WorkerReadinessWatchConfigurationError("worker identity is invalid") from exc
    registration = read_worker_registration_state()
    if registration is None or registration.worker_id != worker_id:
        raise WorkerReadinessWatchConfigurationError("registration proof is invalid")

    configured_connect_timeout = _positive_float_environment(
        "DATABASE_CONNECT_TIMEOUT_SECONDS",
        default=_MAX_CONNECT_TIMEOUT_SECONDS,
    )
    configured_statement_timeout = _positive_float_environment(
        "DATABASE_READY_TIMEOUT_SECONDS",
        default=_MAX_STATEMENT_TIMEOUT_SECONDS,
    )
    return WorkerReadinessWatchConfiguration(
        database_url=database_url,
        registration=registration,
        options=WorkerReadinessWatchOptions(
            connect_timeout_seconds=min(
                configured_connect_timeout,
                _MAX_CONNECT_TIMEOUT_SECONDS,
            ),
            statement_timeout_seconds=min(
                configured_statement_timeout,
                _MAX_STATEMENT_TIMEOUT_SECONDS,
            ),
        ),
    )


def sample_exact_worker_registration_ready(
    connection: Connection[dict[str, Any]],
    registration: WorkerRegistrationState,
) -> bool:
    """Sample the exact fenced registration using PostgreSQL's own clock."""

    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            _EXACT_REGISTRATION_READY_SQL,
            (
                registration.worker_id,
                registration.token,
                registration.generation,
            ),
        )
        row: dict[str, Any] | None = cursor.fetchone()
    if row is None or frozenset(row) != {"ready"} or not isinstance(row["ready"], bool):
        raise WorkerReadinessWatchRuntimeError("readiness query contract is invalid")
    return row["ready"]


def run_worker_readiness_watch(
    configuration: WorkerReadinessWatchConfiguration,
    *,
    input_stream: TextIO,
    output_stream: TextIO,
    wait_for_stop: Callable[[TextIO, float], bool] | None = None,
) -> int:
    """Run one identifier-free readiness stream over one read-only connection."""

    wait = wait_for_stop or _stdin_stop_requested
    options = configuration.options
    connection: Connection[dict[str, Any]] | None = None
    minimum_ready = 0
    sample_count = 0
    handshake_emitted = False
    try:
        statement_timeout_ms = max(1, math.ceil(options.statement_timeout_seconds * 1000))
        connection = Connection.connect(
            configuration.database_url,
            autocommit=True,
            prepare_threshold=0,
            row_factory=dict_row,
            connect_timeout=max(1, math.ceil(options.connect_timeout_seconds)),
            options=(
                f"-c default_transaction_read_only=on -c statement_timeout={statement_timeout_ms}"
            ),
        )
        initial_ready = sample_exact_worker_registration_ready(
            connection,
            configuration.registration,
        )
        minimum_ready = int(initial_ready)
        sample_count = 1
        _emit_protocol_message(
            output_stream,
            state="watching",
            minimum_ready=minimum_ready,
            sample_count=sample_count,
        )
        handshake_emitted = True

        while not wait(input_stream, options.sample_interval_seconds):
            ready = sample_exact_worker_registration_ready(
                connection,
                configuration.registration,
            )
            minimum_ready = min(minimum_ready, int(ready))
            sample_count += 1

        final_ready = sample_exact_worker_registration_ready(
            connection,
            configuration.registration,
        )
        minimum_ready = min(minimum_ready, int(final_ready))
        sample_count += 1
        _emit_protocol_message(
            output_stream,
            state="finished",
            minimum_ready=minimum_ready,
            sample_count=sample_count,
        )
        return 0
    except (Exception, KeyboardInterrupt):
        if handshake_emitted:
            with suppress(OSError, ValueError):
                _emit_protocol_message(
                    output_stream,
                    state="failed",
                    minimum_ready=0,
                    sample_count=sample_count,
                )
        return 2
    finally:
        if connection is not None:
            with suppress(Exception):
                connection.close()


def main() -> None:
    try:
        configuration = load_worker_readiness_watch_configuration()
        exit_code = run_worker_readiness_watch(
            configuration,
            input_stream=sys.stdin,
            output_stream=sys.stdout,
        )
    except (Exception, KeyboardInterrupt):
        exit_code = 2
    if exit_code != 0:
        try:
            sys.stderr.write(f"{_GENERIC_FAILURE_MESSAGE}\n")
            sys.stderr.flush()
        except OSError:
            pass
    raise SystemExit(exit_code)


def _stdin_stop_requested(input_stream: TextIO, timeout_seconds: float) -> bool:
    readable, _, _ = select.select((input_stream,), (), (), timeout_seconds)
    if not readable:
        return False
    line = input_stream.readline()
    return line == "" or line.endswith("\n")


def _emit_protocol_message(
    output_stream: TextIO,
    *,
    state: str,
    minimum_ready: int,
    sample_count: int,
) -> None:
    output_stream.write(
        json.dumps(
            {
                "schema": _PROTOCOL_SCHEMA,
                "state": state,
                "min": minimum_ready,
                "sample_count": sample_count,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    output_stream.write("\n")
    output_stream.flush()


def _positive_float_environment(name: str, *, default: float) -> float:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise WorkerReadinessWatchConfigurationError("timeout is invalid") from exc
    if not math.isfinite(value) or value <= 0:
        raise WorkerReadinessWatchConfigurationError("timeout is invalid")
    return value


def _require_positive_finite(value: float, *, field: str) -> None:
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{field} must be a positive finite number")


if __name__ == "__main__":
    main()
