from __future__ import annotations

import io
import json
from collections.abc import Iterator

import pytest
from psycopg.rows import dict_row

from barrier_free_mobility_agent.persistence import worker_readiness_watch as watch
from barrier_free_mobility_agent.persistence.worker_registration_state import (
    WorkerRegistrationState,
)


class _FakeCursor:
    def __init__(self, readiness: Iterator[bool | BaseException]) -> None:
        self._readiness = readiness
        self.execute_calls: list[tuple[str, tuple[object, ...]]] = []

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, query: str, parameters: tuple[object, ...]) -> None:
        self.execute_calls.append((query, parameters))

    def fetchone(self) -> dict[str, object]:
        value = next(self._readiness)
        if isinstance(value, BaseException):
            raise value
        return {"ready": value}


class _FakeConnection:
    def __init__(self, readiness: list[bool | BaseException]) -> None:
        self.cursor_instance = _FakeCursor(iter(readiness))
        self.closed = False

    def cursor(self, *, row_factory: object) -> _FakeCursor:
        assert row_factory is dict_row
        return self.cursor_instance

    def close(self) -> None:
        self.closed = True


def _configuration(
    *,
    database_url: str = "postgresql://mobility:secret@postgres/mobility",
) -> watch.WorkerReadinessWatchConfiguration:
    return watch.WorkerReadinessWatchConfiguration(
        database_url=database_url,
        registration=WorkerRegistrationState(
            worker_id="worker-exact",
            token="token-exact",
            generation=7,
        ),
        options=watch.WorkerReadinessWatchOptions(
            connect_timeout_seconds=1.25,
            statement_timeout_seconds=0.4,
        ),
    )


def test_watch_uses_one_bounded_read_only_connection_and_exact_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _FakeConnection([True, False, True])
    connect_calls: list[tuple[str, dict[str, object]]] = []

    def fake_connect(database_url: str, **kwargs: object) -> _FakeConnection:
        connect_calls.append((database_url, kwargs))
        return connection

    monkeypatch.setattr(
        "barrier_free_mobility_agent.persistence.worker_readiness_watch.Connection.connect",
        fake_connect,
    )
    output = io.StringIO()
    stop_results = iter((False, True))

    exit_code = watch.run_worker_readiness_watch(
        _configuration(),
        input_stream=io.StringIO(),
        output_stream=output,
        wait_for_stop=lambda _stream, timeout: (
            next(stop_results) if timeout == 0.25 else pytest.fail("unexpected interval")
        ),
    )

    assert exit_code == 0
    assert connection.closed is True
    assert connect_calls == [
        (
            "postgresql://mobility:secret@postgres/mobility",
            {
                "autocommit": True,
                "prepare_threshold": 0,
                "row_factory": dict_row,
                "connect_timeout": 2,
                "options": ("-c default_transaction_read_only=on -c statement_timeout=400"),
            },
        )
    ]
    assert len(connection.cursor_instance.execute_calls) == 3
    for query, parameters in connection.cursor_instance.execute_calls:
        assert "worker_id = %s" in query
        assert "token = %s" in query
        assert "generation = %s" in query
        assert "state = 'ready'" in query
        assert "expires_at > clock_timestamp()" in query
        assert parameters == ("worker-exact", "token-exact", 7)

    messages = [json.loads(line) for line in output.getvalue().splitlines()]
    assert messages == [
        {"schema": 1, "state": "watching", "min": 1, "sample_count": 1},
        {"schema": 1, "state": "finished", "min": 0, "sample_count": 3},
    ]
    serialized = output.getvalue()
    assert "worker-exact" not in serialized
    assert "token-exact" not in serialized
    assert "postgresql" not in serialized


def test_watch_fails_closed_without_projecting_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _FakeConnection([True, RuntimeError("secret database failure")])
    monkeypatch.setattr(
        "barrier_free_mobility_agent.persistence.worker_readiness_watch.Connection.connect",
        lambda *_args, **_kwargs: connection,
    )
    output = io.StringIO()

    exit_code = watch.run_worker_readiness_watch(
        _configuration(),
        input_stream=io.StringIO(),
        output_stream=output,
        wait_for_stop=lambda _stream, _timeout: True,
    )

    assert exit_code == 2
    assert connection.closed is True
    assert [json.loads(line) for line in output.getvalue().splitlines()] == [
        {"schema": 1, "state": "watching", "min": 1, "sample_count": 1},
        {"schema": 1, "state": "failed", "min": 0, "sample_count": 1},
    ]
    assert "secret" not in output.getvalue()


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"sample_interval_seconds": 0.5}, "must be 0.25"),
        ({"connect_timeout_seconds": 5.01}, "exceeds its bound"),
        ({"statement_timeout_seconds": 1.01}, "exceeds its bound"),
        ({"connect_timeout_seconds": float("nan")}, "positive finite"),
        ({"statement_timeout_seconds": 0}, "positive finite"),
    ],
)
def test_watch_options_reject_unbounded_or_invalid_values(
    kwargs: dict[str, float],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        watch.WorkerReadinessWatchOptions(**kwargs)


def test_configuration_binds_exact_process_local_registration_and_clamps_timeouts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registration = WorkerRegistrationState(
        worker_id="worker-container-a",
        token="token-secret",
        generation=3,
    )
    monkeypatch.setenv("PERSISTENCE_BACKEND", "postgres")
    monkeypatch.setenv("DATABASE_URL", "postgresql://secret@postgres/mobility")
    monkeypatch.setenv("EXECUTION_WORKER_ID", "worker-container-a")
    monkeypatch.setenv("DATABASE_CONNECT_TIMEOUT_SECONDS", "300")
    monkeypatch.setenv("DATABASE_READY_TIMEOUT_SECONDS", "300")
    monkeypatch.setattr(watch, "read_worker_registration_state", lambda: registration)

    actual = watch.load_worker_readiness_watch_configuration()

    assert actual.database_url == "postgresql://secret@postgres/mobility"
    assert actual.registration is registration
    assert actual.options == watch.WorkerReadinessWatchOptions()


@pytest.mark.parametrize(
    ("environment", "registration"),
    [
        ({"PERSISTENCE_BACKEND": "memory", "DATABASE_URL": "postgresql://database"}, None),
        ({"PERSISTENCE_BACKEND": "postgres", "DATABASE_URL": ""}, None),
        (
            {
                "PERSISTENCE_BACKEND": "postgres",
                "DATABASE_URL": "postgresql://database",
                "EXECUTION_WORKER_ID": "worker-a",
            },
            WorkerRegistrationState(worker_id="worker-b", token="token", generation=1),
        ),
        (
            {
                "PERSISTENCE_BACKEND": "postgres",
                "DATABASE_URL": "postgresql://database",
                "EXECUTION_WORKER_ID": "worker-a",
                "DATABASE_READY_TIMEOUT_SECONDS": "not-a-number",
            },
            WorkerRegistrationState(worker_id="worker-a", token="token", generation=1),
        ),
    ],
)
def test_configuration_rejects_invalid_environment_or_local_state(
    monkeypatch: pytest.MonkeyPatch,
    environment: dict[str, str],
    registration: WorkerRegistrationState | None,
) -> None:
    for name in (
        "PERSISTENCE_BACKEND",
        "DATABASE_URL",
        "EXECUTION_WORKER_ID",
        "DATABASE_CONNECT_TIMEOUT_SECONDS",
        "DATABASE_READY_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(watch, "read_worker_registration_state", lambda: registration)

    with pytest.raises(watch.WorkerReadinessWatchConfigurationError):
        watch.load_worker_readiness_watch_configuration()


@pytest.mark.parametrize("payload", ["\n", ""])
def test_stdin_newline_or_eof_requests_stop(
    monkeypatch: pytest.MonkeyPatch,
    payload: str,
) -> None:
    stream = io.StringIO(payload)
    monkeypatch.setattr(
        "barrier_free_mobility_agent.persistence.worker_readiness_watch.select.select",
        lambda *_args: ([stream], [], []),
    )

    assert watch._stdin_stop_requested(stream, 0.25) is True


def test_main_emits_only_fixed_failure_text(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail_configuration() -> watch.WorkerReadinessWatchConfiguration:
        raise RuntimeError("postgresql://user:secret@database/private")

    monkeypatch.setattr(watch, "load_worker_readiness_watch_configuration", fail_configuration)

    with pytest.raises(SystemExit) as exc_info:
        watch.main()

    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "Worker readiness watch failed.\n"
    assert "secret" not in captured.err
