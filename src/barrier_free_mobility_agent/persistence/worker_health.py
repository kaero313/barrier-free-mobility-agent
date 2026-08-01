from __future__ import annotations

import asyncio
import math
import os
from typing import Any

from psycopg import AsyncConnection, Error
from psycopg.rows import dict_row

from barrier_free_mobility_agent.persistence.worker_registration_state import (
    read_worker_registration_state,
    resolve_execution_worker_id,
)


async def probe_worker_registration_ready(
    *,
    database_url: str,
    worker_id: str,
    registration_token: str,
    registration_generation: int,
    timeout_seconds: float,
) -> bool:
    """Read exactly one Worker registration without booting application persistence."""

    try:
        async with asyncio.timeout(timeout_seconds):
            connection = await AsyncConnection.connect(
                database_url,
                autocommit=True,
                prepare_threshold=0,
                row_factory=dict_row,
                connect_timeout=max(1, math.ceil(timeout_seconds)),
            )
            try:
                async with connection.cursor(row_factory=dict_row) as cursor:
                    await cursor.execute(
                        """
                        SELECT EXISTS (
                            SELECT 1
                            FROM execution_worker_registry
                            WHERE worker_id = %s
                              AND token = %s
                              AND generation = %s
                              AND state = 'ready'
                              AND expires_at > clock_timestamp()
                        ) AS ready
                        """,
                        (worker_id, registration_token, registration_generation),
                    )
                    row: dict[str, Any] | None = await cursor.fetchone()
            finally:
                await connection.close()
    except (TimeoutError, Error, OSError):
        return False
    return row is not None and row["ready"] is True


def main() -> None:
    """Docker-safe probe with no Settings, LangGraph, MCP, or migration imports."""

    if os.getenv("PERSISTENCE_BACKEND", "memory").strip().lower() != "postgres":
        raise SystemExit("PERSISTENCE_BACKEND=postgres is required for worker health checks.")
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise SystemExit("DATABASE_URL is required for worker health checks.")
    try:
        worker_id = resolve_execution_worker_id(os.getenv("EXECUTION_WORKER_ID", "worker-1"))
    except ValueError as exc:
        raise SystemExit("EXECUTION_WORKER_ID is invalid for worker health checks.") from exc
    registration = read_worker_registration_state()
    if registration is None or registration.worker_id != worker_id:
        raise SystemExit("Execution worker registration state is missing or invalid.")
    timeout_seconds = min(
        _positive_float_env("DATABASE_CONNECT_TIMEOUT_SECONDS", default=10.0),
        _positive_float_env("DATABASE_READY_TIMEOUT_SECONDS", default=2.0),
    )
    ready = asyncio.run(
        probe_worker_registration_ready(
            database_url=database_url,
            worker_id=worker_id,
            registration_token=registration.token,
            registration_generation=registration.generation,
            timeout_seconds=timeout_seconds,
        )
    )
    if not ready:
        raise SystemExit("Execution worker is not ready.")


def _positive_float_env(name: str, *, default: float) -> float:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise SystemExit(f"{name} must be a positive number.") from exc
    if not math.isfinite(value) or value <= 0:
        raise SystemExit(f"{name} must be a positive number.")
    return value


if __name__ == "__main__":
    main()
