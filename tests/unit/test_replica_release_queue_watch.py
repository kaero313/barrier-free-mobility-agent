from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
import scripts.run_replica_release_gate as gate_runner

from barrier_free_mobility_agent.domain.execution_queue import ExecutionQueueHealth
from barrier_free_mobility_agent.operations.replica_release_gate import (
    SMOKE_REPLICA_RELEASE_PROFILE,
)
from barrier_free_mobility_agent.persistence.execution_health_watch import (
    ExecutionHealthSnapshotEvent,
    ExecutionHealthUnavailableEvent,
)


def _snapshot_event(*, ready_workers: int = 2) -> str:
    snapshot = ExecutionQueueHealth(
        checked_at=datetime.now(UTC),
        accepting_new_jobs=ready_workers > 0,
        ready_worker_count=ready_workers,
        queued_job_count=0,
        leased_job_count=0,
        expired_lease_count=0,
        recovery_attempt_count=0,
        stuck_job_count=0,
        oldest_queued_at=None,
        oldest_queued_age_seconds=None,
        stuck_after_seconds=SMOKE_REPLICA_RELEASE_PROFILE.stuck_after_seconds,
    )
    return ExecutionHealthSnapshotEvent(snapshot=snapshot).model_dump_json()


def _stream_process(
    payloads: Sequence[str],
    *,
    delay_seconds: float = 0.0,
    wait_for_stdin: bool = True,
) -> subprocess.Popen[str]:
    script = (
        "import sys,time\n"
        "delay=float(sys.argv[1])\n"
        "for payload in sys.argv[2:]:\n"
        " print(payload,flush=True)\n"
        " time.sleep(delay)\n"
        f"wait_for_stdin={wait_for_stdin!r}\n"
        "if wait_for_stdin: sys.stdin.readline()\n"
    )
    return subprocess.Popen(
        [sys.executable, "-c", script, str(delay_seconds), *payloads],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="strict",
        bufsize=1,
    )


class _FakeCompose:
    def __init__(self, process_payloads: Sequence[Sequence[str]]) -> None:
        self.container_id = "agent-one"
        self._process_payloads = list(process_payloads)
        self.started_for: list[tuple[str, float, float]] = []
        self.processes: list[subprocess.Popen[str]] = []

    def containers(self, *, all_containers: bool = False) -> list[gate_runner.ComposeContainer]:
        del all_containers
        return [
            gate_runner.ComposeContainer(
                self.container_id,
                "mobility-agent-1",
                "mobility",
                "agent",
                "running",
                "healthy",
            )
        ]

    def assert_owned(self, container: gate_runner.ComposeContainer, service: str) -> None:
        assert container.container_id == self.container_id
        assert service == "agent"

    def start_execution_health_watch(
        self,
        container_id: str,
        *,
        stuck_after_seconds: float,
        interval_seconds: float,
    ) -> subprocess.Popen[str]:
        self.started_for.append((container_id, stuck_after_seconds, interval_seconds))
        payloads = self._process_payloads.pop(0)
        process = _stream_process(payloads, delay_seconds=0.4)
        self.processes.append(process)
        return process


def _runner(tmp_path: Path) -> gate_runner.ReplicaReleaseGateRunner:
    return gate_runner.ReplicaReleaseGateRunner(
        project_root=tmp_path,
        web_origin="http://127.0.0.1:3000",
        profile=SMOKE_REPLICA_RELEASE_PROFILE,
        artifact_directory=tmp_path / "artifacts",
        postgres_outage_seconds=20,
        build_images=False,
    )


def _close_clients(active: gate_runner.ReplicaReleaseGateRunner) -> None:
    active._client.close()
    for client in active._workload_clients:
        client.close()


def test_runner_consumes_stream_and_waits_for_a_new_snapshot(
    tmp_path: Path,
) -> None:
    active = _runner(tmp_path)
    compose = _FakeCompose(((_snapshot_event(ready_workers=1), _snapshot_event()),))
    active._compose = cast(Any, compose)
    try:
        active._start_sampler()

        converged = active._wait_queue_converged(desired_workers=2, timeout=3)

        assert converged.ready_worker_count == 2
        assert compose.started_for == [("agent-one", 120, 2.0)]
        active._stop_sampler()
        assert compose.processes[0].poll() is not None
        assert active._sampler is None
        assert active._cleanup_codes == []
    finally:
        active._stop_sampler()
        _close_clients(active)


def test_runner_restarts_stream_against_new_agent_after_recreate(tmp_path: Path) -> None:
    active = _runner(tmp_path)
    compose = _FakeCompose(((_snapshot_event(),), (_snapshot_event(),)))
    active._compose = cast(Any, compose)
    try:
        active._start_sampler()
        active._set_phase("agent_recreate")
        compose.container_id = "agent-two"

        active._restart_sampler_after_agent_recreate()

        assert [call[0] for call in compose.started_for] == ["agent-one", "agent-two"]
        assert compose.processes[0].poll() is not None
        assert active._sampler_fatal_code is None
    finally:
        active._stop_sampler()
        _close_clients(active)


def test_unexpected_stream_close_fails_closed_at_next_wait(tmp_path: Path) -> None:
    active = _runner(tmp_path)
    compose = _FakeCompose(((_snapshot_event(),),))

    def close_immediately(
        container_id: str,
        *,
        stuck_after_seconds: float,
        interval_seconds: float,
    ) -> subprocess.Popen[str]:
        compose.started_for.append((container_id, stuck_after_seconds, interval_seconds))
        process = _stream_process((_snapshot_event(),), wait_for_stdin=False)
        compose.processes.append(process)
        return process

    compose.start_execution_health_watch = close_immediately  # type: ignore[method-assign]
    active._compose = cast(Any, compose)
    try:
        startup_error_code: str | None = None
        try:
            active._start_sampler()
        except gate_runner.GateFailure as exc:
            startup_error_code = exc.code
        if startup_error_code is not None:
            assert startup_error_code == "execution_health_stream_closed"
        else:
            deadline = time.monotonic() + 2
            while active._sampler_fatal_code is None and time.monotonic() < deadline:
                time.sleep(0.01)
            with pytest.raises(
                gate_runner.GateFailure,
                match="execution_health_stream_closed",
            ):
                active._wait_for_fresh_queue_snapshot(timeout=1)
    finally:
        active._stop_sampler()
        _close_clients(active)


def test_worker_admission_samples_are_excluded_from_steady_target_check(
    tmp_path: Path,
) -> None:
    active = _runner(tmp_path)
    now = time.monotonic()
    active._sampler_interval_seconds = 2
    active._samples.extend(
        (
            gate_runner.ObserverSample(
                datetime.now(UTC).isoformat(),
                now,
                "worker_admission",
                gate_runner._parse_execution_health_stream_event(
                    _snapshot_event(ready_workers=0),
                    expected_stuck_after_seconds=120,
                )[0],
                None,
            ),
            gate_runner.ObserverSample(
                datetime.now(UTC).isoformat(),
                now + 0.1,
                "steady",
                gate_runner._parse_execution_health_stream_event(
                    _snapshot_event(),
                    expected_stuck_after_seconds=120,
                )[0],
                None,
            ),
        )
    )
    try:
        assert active._observer_violation_code() is None
    finally:
        _close_clients(active)


def test_unavailable_is_expected_only_during_postgres_outage_or_recovery() -> None:
    def sample(phase: str, error_code: str) -> gate_runner.ObserverSample:
        return gate_runner.ObserverSample(
            datetime.now(UTC).isoformat(),
            time.monotonic(),
            phase,
            None,
            error_code,
        )

    assert gate_runner._is_expected_observer_error(
        sample("postgres_outage", "execution_health_unavailable")
    )
    assert gate_runner._is_expected_observer_error(
        sample("postgres_recovery", "execution_health_unavailable")
    )
    assert not gate_runner._is_expected_observer_error(
        sample("worker_admission", "execution_health_unavailable")
    )
    assert gate_runner._is_expected_observer_error(
        sample("agent_recreate", "execution_health_stream_closed")
    )


def test_stream_parser_preserves_unavailable_code_and_rejects_threshold_drift() -> None:
    snapshot, error = gate_runner._parse_execution_health_stream_event(
        ExecutionHealthUnavailableEvent().model_dump_json(),
        expected_stuck_after_seconds=120,
    )
    assert snapshot is None
    assert error == "execution_health_unavailable"

    with pytest.raises(
        gate_runner.GateFailure,
        match="execution_health_stream_contract_invalid",
    ):
        gate_runner._parse_execution_health_stream_event(
            _snapshot_event(),
            expected_stuck_after_seconds=60,
        )


def test_compose_starts_one_interactive_watch_against_exact_agent_container(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []
    sentinel = cast(subprocess.Popen[str], object())

    def popen(command: list[str], **kwargs: object) -> subprocess.Popen[str]:
        calls.append((command, kwargs))
        return sentinel

    monkeypatch.setattr(subprocess, "Popen", popen)
    runtime = gate_runner.ComposeRuntime(tmp_path)

    actual = runtime.start_execution_health_watch(
        "exact-agent-container",
        stuck_after_seconds=120,
        interval_seconds=2,
    )

    assert actual is sentinel
    assert calls[0][0] == [
        "docker",
        "exec",
        "-i",
        "exact-agent-container",
        "python",
        "-m",
        "barrier_free_mobility_agent.persistence.execution_health_watch",
        "--stuck-after-seconds",
        "120",
        "--interval-seconds",
        "2",
    ]
    assert calls[0][1]["stdin"] is subprocess.PIPE
    assert calls[0][1]["stdout"] is subprocess.PIPE
    assert calls[0][1]["stderr"] is subprocess.DEVNULL
