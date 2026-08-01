from __future__ import annotations

import io
import json
import subprocess
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
import scripts.run_replica_release_gate as runner

from barrier_free_mobility_agent.operations.replica_release_gate import (
    RELEASE_REPLICA_RELEASE_PROFILE,
    SMOKE_REPLICA_RELEASE_PROFILE,
    ReplicaReleaseDisruptionKind,
    ReplicaReleaseGateProfile,
)


def _compose_document(
    *,
    identifier: str = "a" * 12,
    service: str = "worker",
) -> dict[str, object]:
    return {
        "ID": identifier,
        "Name": f"mobility-{service}-1",
        "Project": "mobility",
        "Service": service,
        "State": "running",
        "Health": "healthy",
    }


@pytest.mark.parametrize("as_array", [True, False])
def test_compose_ps_parser_accepts_array_and_ndjson(as_array: bool) -> None:
    documents = [
        _compose_document(identifier="a" * 12, service="worker"),
        _compose_document(identifier="b" * 12, service="agent"),
    ]
    output = (
        json.dumps(documents)
        if as_array
        else "\n".join(json.dumps(document) for document in documents)
    )

    parsed = runner._parse_compose_ps(output)

    assert [(item.service, item.health) for item in parsed] == [
        ("worker", "healthy"),
        ("agent", "healthy"),
    ]


def test_compose_ps_parser_rejects_missing_ownership_fields() -> None:
    document = _compose_document()
    del document["Project"]

    with pytest.raises(runner.GateFailure, match="compose_ps_contract_invalid"):
        runner._parse_compose_ps(json.dumps(document))


@pytest.mark.parametrize(
    "profile",
    [SMOKE_REPLICA_RELEASE_PROFILE, RELEASE_REPLICA_RELEASE_PROFILE],
)
def test_disruption_schedule_is_spread_and_exact(
    profile: ReplicaReleaseGateProfile,
) -> None:
    schedule = runner._build_disruption_schedule(profile)
    flattened = [kind for kinds in schedule.values() for kind in kinds]

    assert len(schedule) == len(flattened)
    assert min(schedule) >= 1
    assert max(schedule) <= profile.workflow_target
    assert flattened.count(ReplicaReleaseDisruptionKind.WORKER_ROLL) == (profile.worker_roll_target)
    assert flattened.count(ReplicaReleaseDisruptionKind.AGENT_RECREATE) == (
        profile.agent_recreate_target
    )
    assert flattened.count(ReplicaReleaseDisruptionKind.POSTGRES_OUTAGE) == (
        profile.postgres_outage_target
    )


def test_planned_workload_exceeds_the_minimum_across_the_whole_soak() -> None:
    assert runner._planned_workflow_count(SMOKE_REPLICA_RELEASE_PROFILE) == 240
    assert runner._planned_workflow_count(RELEASE_REPLICA_RELEASE_PROFILE) == 2_880
    assert runner._planned_workflow_count(SMOKE_REPLICA_RELEASE_PROFILE) > (
        SMOKE_REPLICA_RELEASE_PROFILE.workflow_target
    )
    assert runner._planned_workflow_count(RELEASE_REPLICA_RELEASE_PROFILE) > (
        RELEASE_REPLICA_RELEASE_PROFILE.workflow_target
    )


def test_custom_disruption_schedule_stays_within_batch_slots() -> None:
    schedule = runner._build_disruption_schedule(
        SMOKE_REPLICA_RELEASE_PROFILE,
        total_slots=120,
    )

    assert min(schedule) >= 1
    assert max(schedule) <= 120
    assert len(schedule) == (
        SMOKE_REPLICA_RELEASE_PROFILE.worker_roll_target
        + SMOKE_REPLICA_RELEASE_PROFILE.agent_recreate_target
        + SMOKE_REPLICA_RELEASE_PROFILE.postgres_outage_target
    )


def test_structured_single_text_answer_uses_v2_contract() -> None:
    payload = runner._structured_clarification_answer(
        {
            "id": "clarification",
            "answer_request": {"mode": "single_text"},
        }
    )

    assert payload == {
        "schema_version": "2",
        "clarification_id": "clarification",
        "answer": {
            "mode": "single_text",
            "value": "계단과 에스컬레이터를 모두 이용할 수 없어요",
        },
    }


def test_structured_fields_answer_uses_control_specific_values() -> None:
    payload = runner._structured_clarification_answer(
        {
            "id": "clarification",
            "answer_request": {
                "mode": "fields",
                "inputs": [
                    {"id": "stairs", "control": "tri_state"},
                    {
                        "id": "station",
                        "control": "single_choice",
                        "options": [{"value": "LINE_2", "label": "2호선"}],
                    },
                    {"id": "note", "control": "text"},
                ],
            },
        }
    )

    assert payload["answer"] == {
        "mode": "fields",
        "values": [
            {"input_id": "stairs", "value": {"kind": "tri_state", "value": "no"}},
            {"input_id": "station", "value": {"kind": "choice", "value": "LINE_2"}},
            {
                "input_id": "note",
                "value": {
                    "kind": "text",
                    "value": "계단과 에스컬레이터를 모두 이용할 수 없어요",
                },
            },
        ],
    }


def test_report_redaction_rejects_recursive_private_keys() -> None:
    runner._assert_report_redacted({"aggregate": {"completed": 1}})

    with pytest.raises(runner.GateFailure, match="report_redaction_violation"):
        runner._assert_report_redacted({"scenario": [{"run_id": "private"}]})


def test_atomic_report_write_is_valid_and_replaces_existing_file(tmp_path: Path) -> None:
    report_path = tmp_path / "release.json"
    report_path.write_text("stale", encoding="utf-8")

    runner._atomic_write_json(report_path, {"schema_version": "1", "result": "pass"})

    assert json.loads(report_path.read_text(encoding="utf-8")) == {
        "result": "pass",
        "schema_version": "1",
    }
    assert list(tmp_path.glob("*.tmp")) == []


def test_report_shape_has_no_sensitive_exact_keys() -> None:
    document: Mapping[str, object] = {
        "schema_version": "1",
        "gate": "phase-4b-3c-worker-replica-release",
        "workload": {"completed_workflow_count": 30},
        "scenarios": [{"kind": "worker_roll", "status": "pass"}],
    }

    runner._assert_report_redacted(document)
    keys = _recursive_keys(document)
    assert {
        "job_id",
        "run_id",
        "thread_id",
        "owner_id",
        "question",
        "payload",
        "token",
    }.isdisjoint(keys)


@pytest.mark.parametrize(
    ("clarification_expected", "run_status", "business_status", "expected"),
    [
        (True, "interrupted", "NEEDS_CLARIFICATION", True),
        (False, "completed", "SUCCESS", False),
    ],
)
def test_initial_workflow_response_enforces_clarification_designation(
    clarification_expected: bool,
    run_status: str,
    business_status: str,
    expected: bool,
) -> None:
    actual = runner._require_initial_workflow_response(
        {"run_status": run_status, "status": business_status},
        clarification_expected=clarification_expected,
    )

    assert actual is expected


@pytest.mark.parametrize(
    ("clarification_expected", "run_status", "business_status", "failure_code"),
    [
        (True, "completed", "SUCCESS", "clarification_workflow_did_not_interrupt"),
        (False, "interrupted", "NEEDS_CLARIFICATION", "non_clarification_workflow_interrupted"),
    ],
)
def test_initial_workflow_response_rejects_wrong_clarification_path(
    clarification_expected: bool,
    run_status: str,
    business_status: str,
    failure_code: str,
) -> None:
    with pytest.raises(runner.GateFailure, match=failure_code):
        runner._require_initial_workflow_response(
            {"run_status": run_status, "status": business_status},
            clarification_expected=clarification_expected,
        )


def test_terminal_workflow_rejects_business_failure_even_when_run_completed() -> None:
    with pytest.raises(runner.GateFailure, match="workload_business_failed"):
        runner._require_terminal_workflow_response({"run_status": "completed", "status": "FAILED"})


@pytest.mark.parametrize("business_status", ["SUCCESS", "PARTIAL"])
def test_terminal_workflow_accepts_non_failed_business_outcomes(
    business_status: str,
) -> None:
    assert (
        runner._require_terminal_workflow_response(
            {"run_status": "completed", "status": business_status}
        )
        == "completed"
    )


def test_cross_process_lock_fails_closed_on_contention(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    compose_file = project_root / "compose.yaml"
    first = runner.CrossProcessGateLock(
        project_root,
        compose_file,
        lock_directory=tmp_path / "locks",
    )
    second = runner.CrossProcessGateLock(
        project_root,
        compose_file,
        lock_directory=tmp_path / "locks",
    )

    first.acquire()
    try:
        with pytest.raises(runner.GateFailure, match="gate_lock_contended"):
            second.acquire()
    finally:
        first.release()

    second.acquire()
    second.release()


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "localhost", "[::1]", "host.docker.internal"],
)
def test_mcp_preflight_accepts_only_local_test_hosts(host: str) -> None:
    runner._assert_local_mcp_environment(
        {
            "MCP_MOBILITY_URL": f"http://{host}:8010/mcp",
            "MCP_MOBILITY_HEALTH_URL": f"http://{host}:8010/health",
        }
    )


@pytest.mark.parametrize("host", ["mcp.example.com", "10.0.0.8", "worker"])
def test_mcp_preflight_rejects_non_local_hosts(host: str) -> None:
    with pytest.raises(runner.GateFailure, match="preflight_mcp_url_not_local"):
        runner._assert_local_mcp_environment(
            {
                "MCP_MOBILITY_URL": f"https://{host}/mcp",
                "MCP_MOBILITY_HEALTH_URL": f"https://{host}/health",
            }
        )


def test_compose_runtime_uses_only_explicit_base_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []

    def fake_run(
        command: Sequence[str],
        *,
        cwd: Path,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, timeout
        commands.append(list(command))
        return subprocess.CompletedProcess(list(command), 0, stdout="", stderr="")

    monkeypatch.setenv("COMPOSE_FILE", "untrusted-override.yaml")
    monkeypatch.setattr(runner, "_run_command_with_tree_cleanup", fake_run)
    runtime = runner.ComposeRuntime(tmp_path)

    runtime.run("config", "--quiet", timeout=1)

    assert commands == [
        [
            "docker",
            "compose",
            "--project-directory",
            str(tmp_path.resolve()),
            "-f",
            str((tmp_path / "compose.yaml").resolve()),
            "config",
            "--quiet",
        ]
    ]


class _FakeReadinessWatchProcess:
    def __init__(
        self,
        *,
        handshake: str = '{"min":1,"sample_count":1,"schema":1,"state":"watching"}\n',
        final: str = '{"min":1,"sample_count":4,"schema":1,"state":"finished"}\n',
        return_code: int = 0,
        stop_timeout: bool = False,
        interrupt_on_stop: bool = False,
    ) -> None:
        self.stdin = io.StringIO()
        self.stdout = io.StringIO(handshake)
        self.stderr = None
        self.returncode: int | None = None
        self._final = final
        self._return_code = return_code
        self._stop_timeout = stop_timeout
        self._interrupt_on_stop = interrupt_on_stop
        self._timed_out = False
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def communicate(
        self,
        input: str | None = None,
        timeout: float | None = None,
    ) -> tuple[str, str]:
        del input
        if self._interrupt_on_stop:
            self._interrupt_on_stop = False
            raise KeyboardInterrupt
        if self._stop_timeout and not self._timed_out:
            self._timed_out = True
            raise subprocess.TimeoutExpired("docker exec", timeout or 0)
        self.returncode = self._return_code if not self.killed else -9
        return self._final, ""

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        if self.returncode is None:
            self.returncode = self._return_code
        return self.returncode


def _as_popen(process: _FakeReadinessWatchProcess) -> subprocess.Popen[str]:
    return cast("subprocess.Popen[str]", process)


def _worker_container(identifier: str) -> runner.ComposeContainer:
    return runner.ComposeContainer(
        identifier,
        identifier,
        "project",
        "worker",
        "running",
        "healthy",
    )


def test_worker_readiness_watch_starts_one_interactive_uncached_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []
    process = _FakeReadinessWatchProcess()

    def fake_popen(
        command: list[str],
        **kwargs: object,
    ) -> subprocess.Popen[str]:
        calls.append((command, kwargs))
        return _as_popen(process)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    runtime = runner.ComposeRuntime(tmp_path)

    assert runtime.start_worker_readiness_watch("worker-container") is _as_popen(process)
    assert calls == [
        (
            [
                "docker",
                "exec",
                "-i",
                "worker-container",
                "python",
                "-m",
                "barrier_free_mobility_agent.persistence.worker_readiness_watch",
            ],
            {
                "cwd": tmp_path.resolve(),
                "stdin": subprocess.PIPE,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.DEVNULL,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "bufsize": 1,
            },
        )
    ]


def test_direct_worker_monitor_records_any_registration_loss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = runner.ComposeRuntime(tmp_path)
    processes = {
        "worker-a": _FakeReadinessWatchProcess(),
        "worker-b": _FakeReadinessWatchProcess(
            final='{ "schema":1,"state":"finished","min":0,"sample_count":3}\n'
        ),
    }
    ownership_checks: list[tuple[str, str]] = []
    monkeypatch.setattr(
        runtime,
        "assert_owned",
        lambda container, service: ownership_checks.append((container.container_id, service)),
    )
    monkeypatch.setattr(
        runtime,
        "start_worker_readiness_watch",
        lambda container_id: _as_popen(processes[container_id]),
    )
    containers = [
        _worker_container("worker-a"),
        _worker_container("worker-b"),
    ]
    monitor = runner.DirectWorkerReadinessMonitor(runtime, containers)
    monitor.start()
    monitor.stop()

    assert monitor.minimum_ready_count == 1
    assert ownership_checks == [("worker-a", "worker"), ("worker-b", "worker")]
    assert processes["worker-a"].stdin.getvalue() == "\n"
    assert processes["worker-b"].stdin.getvalue() == "\n"


@pytest.mark.parametrize(
    "handshake",
    [
        "not-json\n",
        '{"schema":1,"state":"watching","min":1,"sample_count":1,"id":"private"}\n',
        '{"schema":1,"state":"watching","min":true,"sample_count":1}\n',
        '{"schema":1,"state":"finished","min":1,"sample_count":1}\n',
    ],
)
def test_direct_worker_monitor_rejects_invalid_closed_handshake_and_cleans_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    handshake: str,
) -> None:
    runtime = runner.ComposeRuntime(tmp_path)
    process = _FakeReadinessWatchProcess(handshake=handshake)
    monkeypatch.setattr(runtime, "assert_owned", lambda *_args: None)
    monkeypatch.setattr(
        runtime,
        "start_worker_readiness_watch",
        lambda _container_id: _as_popen(process),
    )
    monitor = runner.DirectWorkerReadinessMonitor(runtime, [_worker_container("worker-a")])

    with pytest.raises(runner.GateFailure, match="worker_readiness_watch_handshake_invalid"):
        monitor.start()

    assert process.killed is True


def test_direct_worker_monitor_fails_gate_on_watch_runtime_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = runner.ComposeRuntime(tmp_path)
    process = _FakeReadinessWatchProcess(
        final='{"schema":1,"state":"failed","min":0,"sample_count":2}\n',
        return_code=2,
    )
    monkeypatch.setattr(runtime, "assert_owned", lambda *_args: None)
    monkeypatch.setattr(
        runtime,
        "start_worker_readiness_watch",
        lambda _container_id: _as_popen(process),
    )
    monitor = runner.DirectWorkerReadinessMonitor(runtime, [_worker_container("worker-a")])
    monitor.start()

    with pytest.raises(runner.GateFailure, match="worker_readiness_watch_runtime_failed"):
        monitor.stop()


def test_direct_worker_monitor_kills_watch_on_bounded_stop_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = runner.ComposeRuntime(tmp_path)
    process = _FakeReadinessWatchProcess(stop_timeout=True)
    monkeypatch.setattr(runtime, "assert_owned", lambda *_args: None)
    monkeypatch.setattr(
        runtime,
        "start_worker_readiness_watch",
        lambda _container_id: _as_popen(process),
    )
    monitor = runner.DirectWorkerReadinessMonitor(
        runtime,
        [_worker_container("worker-a")],
        stop_timeout_seconds=0.1,
    )
    monitor.start()

    with pytest.raises(runner.GateFailure, match="worker_readiness_watch_stop_timeout"):
        monitor.stop()

    assert process.killed is True


def test_direct_worker_monitor_cleans_watch_on_keyboard_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = runner.ComposeRuntime(tmp_path)
    process = _FakeReadinessWatchProcess(interrupt_on_stop=True)
    monkeypatch.setattr(runtime, "assert_owned", lambda *_args: None)
    monkeypatch.setattr(
        runtime,
        "start_worker_readiness_watch",
        lambda _container_id: _as_popen(process),
    )
    monitor = runner.DirectWorkerReadinessMonitor(runtime, [_worker_container("worker-a")])
    monitor.start()

    with pytest.raises(KeyboardInterrupt):
        monitor.stop()

    assert process.killed is True


def test_direct_worker_monitor_cleans_started_watch_when_later_start_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = runner.ComposeRuntime(tmp_path)
    first = _FakeReadinessWatchProcess()
    calls = 0

    def start(_container_id: str) -> subprocess.Popen[str]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise runner.GateFailure("worker_readiness_watch_start_failed")
        return _as_popen(first)

    monkeypatch.setattr(runtime, "assert_owned", lambda *_args: None)
    monkeypatch.setattr(runtime, "start_worker_readiness_watch", start)
    monitor = runner.DirectWorkerReadinessMonitor(
        runtime,
        [_worker_container("worker-a"), _worker_container("worker-b")],
    )

    with pytest.raises(runner.GateFailure, match="worker_readiness_watch_start_failed"):
        monitor.start()

    assert first.killed is True


def test_expired_common_recovery_budget_fails_closed() -> None:
    with pytest.raises(runner.GateFailure, match="recovery_budget_exceeded"):
        runner._remaining_recovery_seconds(0, "recovery_budget_exceeded")


def test_container_image_provenance_uses_actual_sha256_content_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []
    expected_digest = f"sha256:{'a' * 64}"

    def fake_run(
        command: Sequence[str],
        *,
        cwd: Path,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, timeout
        commands.append(list(command))
        return subprocess.CompletedProcess(
            list(command),
            0,
            stdout=f"{expected_digest}\n",
            stderr="",
        )

    monkeypatch.setattr(runner, "_run_command_with_tree_cleanup", fake_run)
    runtime = runner.ComposeRuntime(tmp_path)

    assert runtime.container_image_content_digest("agent-container") == expected_digest
    assert commands == [
        [
            "docker",
            "inspect",
            "--format",
            "{{.Image}}",
            "agent-container",
        ]
    ]


def test_container_image_provenance_rejects_mutable_or_malformed_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(
        command: Sequence[str],
        *,
        cwd: Path,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, timeout
        return subprocess.CompletedProcess(
            list(command),
            0,
            stdout="mobility-agent:latest\n",
            stderr="",
        )

    monkeypatch.setattr(runner, "_run_command_with_tree_cleanup", fake_run)
    runtime = runner.ComposeRuntime(tmp_path)

    with pytest.raises(runner.GateFailure, match="container_image_digest_invalid"):
        runtime.container_image_content_digest("agent-container")


def test_same_timestamp_reports_never_overwrite_each_other(tmp_path: Path) -> None:
    started_at = datetime(2026, 7, 16, 12, 34, 56, 123456, tzinfo=UTC)

    first = runner._write_collision_safe_report(
        tmp_path,
        started_at,
        {"schema_version": "1", "result": "first"},
    )
    second = runner._write_collision_safe_report(
        tmp_path,
        started_at,
        {"schema_version": "1", "result": "second"},
    )

    assert first != second
    assert first.name == "phase-4b-3c-20260716T123456123456Z.json"
    assert second.name == "phase-4b-3c-20260716T123456123456Z-1.json"
    assert json.loads(first.read_text(encoding="utf-8"))["result"] == "first"
    assert json.loads(second.read_text(encoding="utf-8"))["result"] == "second"


def _recursive_keys(value: object) -> set[str]:
    if isinstance(value, Mapping):
        return set(value) | {
            nested_key for nested in value.values() for nested_key in _recursive_keys(nested)
        }
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return {nested_key for nested in value for nested_key in _recursive_keys(nested)}
    return set()
