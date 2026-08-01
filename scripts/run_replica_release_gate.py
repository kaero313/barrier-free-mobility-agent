from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from queue import Empty, Queue
from tempfile import NamedTemporaryFile, gettempdir
from typing import IO, Any, BinaryIO
from uuid import uuid4

import httpx
from pydantic import ValidationError

from barrier_free_mobility_agent.operations.replica_release_gate import (
    ReplicaReleaseDisruptionEvidence,
    ReplicaReleaseDisruptionKind,
    ReplicaReleaseEvidenceAccumulator,
    ReplicaReleaseFinalEvidence,
    ReplicaReleaseGateProfile,
    ReplicaReleaseProfileName,
    ReplicaReleaseQueueSnapshot,
    evaluate_replica_release_gate,
    get_replica_release_gate_profile,
)
from barrier_free_mobility_agent.persistence.execution_health_watch import (
    ExecutionHealthSnapshotEvent,
    ExecutionHealthUnavailableEvent,
    parse_execution_health_watch_event,
)
from barrier_free_mobility_agent.persistence.replica_release_evidence import (
    ReplicaReleaseDatabaseEvidence,
)

_GATE_NAME = "phase-4b-3c-worker-replica-release"
_REPORT_SCHEMA_VERSION = "1"
_OWNED_SERVICES = frozenset({"agent", "worker", "postgres", "web"})
_LOCAL_MCP_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "host.docker.internal"})
_MCP_URL_ENVIRONMENT_KEYS = ("MCP_MOBILITY_URL", "MCP_MOBILITY_HEALTH_URL")
_SHA256_CONTENT_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_OBSERVER_CADENCE_EXEMPT_PHASES = frozenset(
    {
        "agent_recreate",
        "postgres_outage",
        "postgres_recovery",
        "worker_admission",
        "cleanup",
    }
)
_OBSERVER_TARGET_EXEMPT_PHASES = _OBSERVER_CADENCE_EXEMPT_PHASES
_EXPECTED_UNAVAILABLE_PHASES = frozenset({"postgres_outage", "postgres_recovery"})
_EXPECTED_STREAM_CLOSE_PHASES = frozenset({"agent_recreate", "cleanup"})
_SAMPLER_START_TIMEOUT_SECONDS = 30.0
_SAMPLER_STOP_TIMEOUT_SECONDS = 10.0
_COMMAND_TREE_TERMINATE_GRACE_SECONDS = 2.0
_COMMAND_TREE_KILL_WAIT_SECONDS = 3.0
_COMMAND_PIPE_DRAIN_TIMEOUT_SECONDS = 2.0
_WINDOWS_TASKKILL_TIMEOUT_SECONDS = 3.0
_WINDOWS_TASKKILL_KILL_WAIT_SECONDS = 1.0
_WINDOWS_CREATE_NEW_PROCESS_GROUP = 0x00000200
_WINDOWS_CREATE_NO_WINDOW = 0x08000000
_FULLY_SPECIFIED_QUESTION = (
    "휠체어를 사용하고 계단과 에스컬레이터를 이용할 수 없어요. "
    "홍대입구역에서 삼성역까지 갈 수 있어?"
)
_CLARIFICATION_QUESTION = "홍대입구역에서 삼성역까지 갈 수 있어?"
_CLARIFICATION_TEXT_ANSWER = "계단과 에스컬레이터를 모두 이용할 수 없어요"


class GateFailure(RuntimeError):
    """Stable release-gate failure that never carries command output or private input."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _run_command_with_tree_cleanup(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    """Run one isolated command and bound cleanup of only its process tree."""

    command_list = list(command)
    process = subprocess.Popen(
        command_list,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        **_command_process_group_options(os.name),
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _terminate_command_process_tree(process)
        timeout_stdout, timeout_stderr = _drain_command_pipes(
            process,
            fallback_stdout=exc.output,
            fallback_stderr=exc.stderr,
        )
        raise subprocess.TimeoutExpired(
            exc.cmd,
            exc.timeout,
            output=timeout_stdout,
            stderr=timeout_stderr,
        ) from exc
    except KeyboardInterrupt:
        _terminate_command_process_tree(process)
        _drain_command_pipes(process, fallback_stdout=None, fallback_stderr=None)
        raise

    return_code = process.returncode
    if return_code is None:  # pragma: no cover - communicate() waits for termination
        raise subprocess.SubprocessError("command did not publish a return code")
    return subprocess.CompletedProcess(
        command_list,
        return_code,
        stdout=stdout,
        stderr=stderr,
    )


def _command_process_group_options(platform_name: str) -> dict[str, Any]:
    if platform_name == "nt":
        return {"creationflags": _WINDOWS_CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _terminate_command_process_tree(
    process: subprocess.Popen[str],
    *,
    platform_name: str | None = None,
) -> None:
    if (platform_name or os.name) == "nt":
        _terminate_windows_command_process_tree(process)
        return
    _terminate_posix_command_process_tree(process)


def _terminate_posix_command_process_tree(process: subprocess.Popen[str]) -> None:
    process_group_id = process.pid
    posix_os: Any = os
    posix_signal: Any = signal
    with suppress(OSError):
        posix_os.killpg(process_group_id, posix_signal.SIGTERM)
    _wait_for_command_process(process, _COMMAND_TREE_TERMINATE_GRACE_SECONDS)

    # The group is signalled even if its leader has already exited. A descendant
    # can otherwise keep the captured pipes open after the direct process ends.
    with suppress(OSError):
        posix_os.killpg(process_group_id, posix_signal.SIGKILL)
    if not _wait_for_command_process(process, _COMMAND_TREE_KILL_WAIT_SECONDS):
        with suppress(OSError):
            process.kill()
        _wait_for_command_process(process, _WINDOWS_TASKKILL_KILL_WAIT_SECONDS)


def _terminate_windows_command_process_tree(process: subprocess.Popen[str]) -> None:
    # taskkill /T is intentionally scoped to the PID created for this invocation.
    # CREATE_NEW_PROCESS_GROUP prevents it from sharing the caller's process group.
    _run_windows_taskkill(process.pid)
    if not _wait_for_command_process(process, _COMMAND_TREE_KILL_WAIT_SECONDS):
        with suppress(OSError):
            process.kill()
        _wait_for_command_process(process, _WINDOWS_TASKKILL_KILL_WAIT_SECONDS)


def _run_windows_taskkill(process_id: int) -> None:
    try:
        killer = subprocess.Popen(
            ["taskkill.exe", "/PID", str(process_id), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_WINDOWS_CREATE_NO_WINDOW,
        )
    except OSError:
        return
    try:
        killer.wait(timeout=_WINDOWS_TASKKILL_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        with suppress(OSError):
            killer.kill()
        with suppress(OSError, subprocess.TimeoutExpired):
            killer.wait(timeout=_WINDOWS_TASKKILL_KILL_WAIT_SECONDS)


def _wait_for_command_process(process: subprocess.Popen[str], timeout: float) -> bool:
    try:
        process.wait(timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return process.poll() is not None
    return True


def _drain_command_pipes(
    process: subprocess.Popen[str],
    *,
    fallback_stdout: str | bytes | None,
    fallback_stderr: str | bytes | None,
) -> tuple[str | bytes | None, str | bytes | None]:
    try:
        stdout, stderr = process.communicate(timeout=_COMMAND_PIPE_DRAIN_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                with suppress(OSError, ValueError):
                    stream.close()
        return fallback_stdout, fallback_stderr
    return (
        stdout if stdout is not None else fallback_stdout,
        stderr if stderr is not None else fallback_stderr,
    )


@dataclass(frozen=True, slots=True)
class ComposeContainer:
    container_id: str
    name: str
    project: str
    service: str
    state: str
    health: str


@dataclass(frozen=True, slots=True)
class ObserverSample:
    observed_at: str
    observed_monotonic: float
    phase: str
    snapshot: ReplicaReleaseQueueSnapshot | None
    error_code: str | None


@dataclass(frozen=True, slots=True)
class WorkloadOutcome:
    interrupted: bool
    run_status: str
    latency_seconds: float


@dataclass(frozen=True, slots=True)
class WorkerReadinessWatchMessage:
    schema: int
    state: str
    minimum: int
    sample_count: int


def _is_expected_observer_error(sample: ObserverSample) -> bool:
    if sample.error_code == "execution_health_unavailable":
        return sample.phase in _EXPECTED_UNAVAILABLE_PHASES
    if sample.error_code in {
        "execution_health_stream_closed",
        "execution_health_stream_io_failed",
    }:
        return sample.phase in _EXPECTED_STREAM_CLOSE_PHASES
    return False


def _parse_execution_health_stream_event(
    payload: str,
    *,
    expected_stuck_after_seconds: float,
) -> tuple[ReplicaReleaseQueueSnapshot | None, str | None]:
    try:
        event = parse_execution_health_watch_event(payload)
        if isinstance(event, ExecutionHealthUnavailableEvent):
            return None, event.error_code
        if not isinstance(event, ExecutionHealthSnapshotEvent):  # pragma: no cover
            raise ValueError("unsupported execution health stream event")
        snapshot = event.snapshot
        if snapshot.stuck_after_seconds != expected_stuck_after_seconds:
            raise ValueError("execution health threshold mismatch")
        projected = ReplicaReleaseQueueSnapshot.model_validate(
            {
                "ready_worker_count": snapshot.ready_worker_count,
                "queued_job_count": snapshot.queued_job_count,
                "leased_job_count": snapshot.leased_job_count,
                "expired_lease_count": snapshot.expired_lease_count,
                "recovery_attempt_count": snapshot.recovery_attempt_count,
                "stuck_job_count": snapshot.stuck_job_count,
                "stuck_after_seconds": snapshot.stuck_after_seconds,
                "oldest_queued_age_seconds": snapshot.oldest_queued_age_seconds,
            }
        )
    except (ValidationError, TypeError, ValueError) as exc:
        raise GateFailure("execution_health_stream_contract_invalid") from exc
    return projected, None


class CrossProcessGateLock:
    """Non-blocking OS lock scoped to one resolved project and Compose file."""

    def __init__(
        self,
        project_root: Path,
        compose_file: Path,
        *,
        lock_directory: Path | None = None,
    ) -> None:
        normalized_scope = "\0".join(
            (
                os.path.normcase(str(project_root.resolve())),
                os.path.normcase(str(compose_file.resolve())),
            )
        )
        scope_digest = hashlib.sha256(normalized_scope.encode("utf-8")).hexdigest()[:24]
        directory = lock_directory or Path(gettempdir())
        self._path = directory / f"barrier-free-mobility-replica-gate-{scope_digest}.lock"
        self._handle: BinaryIO | None = None

    def acquire(self) -> None:
        if self._handle is not None:
            raise GateFailure("gate_lock_already_acquired")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            handle = self._path.open("a+b")
            if self._path.stat().st_size == 0:
                handle.write(b"\0")
                handle.flush()
                os.fsync(handle.fileno())
            handle.seek(0)
        except OSError as exc:
            raise GateFailure("gate_lock_unavailable") from exc
        try:
            _lock_file_nonblocking(handle)
        except OSError as exc:
            handle.close()
            raise GateFailure("gate_lock_contended") from exc
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        try:
            handle.seek(0)
            _unlock_file(handle)
        finally:
            handle.close()


class DirectWorkerReadinessMonitor:
    """Watch each exact Worker registration without host-side polling gaps."""

    def __init__(
        self,
        compose: ComposeRuntime,
        containers: Sequence[ComposeContainer],
        *,
        handshake_timeout_seconds: float = 30.0,
        stop_timeout_seconds: float = 15.0,
    ) -> None:
        if not containers:
            raise ValueError("at least one Worker is required")
        if not math.isfinite(handshake_timeout_seconds) or handshake_timeout_seconds <= 0:
            raise ValueError("handshake timeout must be positive and finite")
        if not math.isfinite(stop_timeout_seconds) or stop_timeout_seconds <= 0:
            raise ValueError("stop timeout must be positive and finite")
        self._compose = compose
        self._containers = tuple(containers)
        self._handshake_timeout_seconds = handshake_timeout_seconds
        self._stop_timeout_seconds = stop_timeout_seconds
        self._minimum_ready_count = len(self._containers)
        self._processes: list[subprocess.Popen[str]] = []
        self._initial_minima: list[int] = []
        self._started = False
        self._stopped = False

    def start(self) -> None:
        if self._started or self._stopped:
            raise GateFailure("worker_readiness_watch_state_invalid")
        for container in self._containers:
            self._compose.assert_owned(container, "worker")

        try:
            for container in self._containers:
                self._processes.append(
                    self._compose.start_worker_readiness_watch(container.container_id)
                )
            handshake_deadline = time.monotonic() + self._handshake_timeout_seconds
            for process in self._processes:
                stdout = process.stdout
                if stdout is None:
                    raise GateFailure("worker_readiness_watch_start_failed")
                line = _readline_with_timeout(
                    stdout,
                    timeout=_remaining_watch_seconds(
                        handshake_deadline,
                        "worker_readiness_watch_handshake_timeout",
                    ),
                )
                handshake = _parse_worker_readiness_watch_message(
                    line,
                    expected_state="watching",
                    failure_code="worker_readiness_watch_handshake_invalid",
                )
                self._initial_minima.append(handshake.minimum)
                if process.poll() is not None:
                    raise GateFailure("worker_readiness_watch_runtime_failed")
            self._minimum_ready_count = sum(self._initial_minima)
            self._started = True
        except BaseException:
            self._abort_processes()
            raise

    def stop(self) -> None:
        if not self._started or self._stopped:
            raise GateFailure("worker_readiness_watch_state_invalid")
        self._stopped = True
        try:
            self._stop_processes()
        except BaseException:
            self._abort_processes()
            raise

    def _stop_processes(self) -> None:
        first_failure: GateFailure | None = None
        for process in self._processes:
            try:
                if process.poll() is None:
                    if process.stdin is None:
                        raise GateFailure("worker_readiness_watch_runtime_failed")
                    process.stdin.write("\n")
                    process.stdin.flush()
            except (OSError, ValueError, GateFailure):
                first_failure = first_failure or GateFailure(
                    "worker_readiness_watch_runtime_failed"
                )

        final_minima: list[int] = []
        stop_deadline = time.monotonic() + self._stop_timeout_seconds
        for index, process in enumerate(self._processes):
            try:
                stdout, _ = process.communicate(
                    timeout=_remaining_watch_seconds(
                        stop_deadline,
                        "worker_readiness_watch_stop_timeout",
                    )
                )
            except (subprocess.TimeoutExpired, GateFailure):
                process.kill()
                with suppress(OSError, subprocess.SubprocessError):
                    process.communicate(timeout=2)
                first_failure = first_failure or GateFailure("worker_readiness_watch_stop_timeout")
                continue
            except OSError:
                first_failure = first_failure or GateFailure(
                    "worker_readiness_watch_runtime_failed"
                )
                continue

            if process.returncode != 0:
                first_failure = first_failure or GateFailure(
                    "worker_readiness_watch_runtime_failed"
                )
                continue
            final_lines = stdout.splitlines()
            if len(final_lines) != 1:
                first_failure = first_failure or GateFailure("worker_readiness_watch_final_invalid")
                continue
            try:
                final = _parse_worker_readiness_watch_message(
                    final_lines[0],
                    expected_state="finished",
                    failure_code="worker_readiness_watch_final_invalid",
                )
                if final.sample_count < 2 or final.minimum > self._initial_minima[index]:
                    raise GateFailure("worker_readiness_watch_final_invalid")
            except GateFailure as exc:
                first_failure = first_failure or exc
                continue
            final_minima.append(final.minimum)

        if first_failure is not None:
            raise first_failure
        if len(final_minima) != len(self._containers):
            raise GateFailure("worker_readiness_watch_final_invalid")
        self._minimum_ready_count = sum(final_minima)
        self._processes.clear()

    @property
    def minimum_ready_count(self) -> int:
        return self._minimum_ready_count

    def _abort_processes(self) -> None:
        for process in self._processes:
            with suppress(OSError, subprocess.SubprocessError):
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=2)
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    with suppress(OSError, ValueError):
                        stream.close()
        self._processes.clear()


def _readline_with_timeout(stream: IO[str], *, timeout: float) -> str:
    results: Queue[str | BaseException] = Queue(maxsize=1)

    def read_line() -> None:
        try:
            results.put(stream.readline())
        except BaseException as exc:
            results.put(exc)

    reader = threading.Thread(
        target=read_line,
        name="replica-release-readiness-handshake",
        daemon=True,
    )
    reader.start()
    try:
        result = results.get(timeout=timeout)
    except Empty as exc:
        raise GateFailure("worker_readiness_watch_handshake_timeout") from exc
    if isinstance(result, BaseException):
        raise GateFailure("worker_readiness_watch_handshake_invalid") from result
    if not result:
        raise GateFailure("worker_readiness_watch_handshake_invalid")
    return result


def _parse_worker_readiness_watch_message(
    raw_message: str,
    *,
    expected_state: str,
    failure_code: str,
) -> WorkerReadinessWatchMessage:
    try:
        message = json.loads(raw_message)
    except (json.JSONDecodeError, TypeError) as exc:
        raise GateFailure(failure_code) from exc
    if not isinstance(message, Mapping) or frozenset(message) != {
        "schema",
        "state",
        "min",
        "sample_count",
    }:
        raise GateFailure(failure_code)
    schema = message.get("schema")
    state = message.get("state")
    minimum = message.get("min")
    sample_count = message.get("sample_count")
    if (
        not isinstance(schema, int)
        or isinstance(schema, bool)
        or schema != 1
        or state != expected_state
        or not isinstance(minimum, int)
        or isinstance(minimum, bool)
        or minimum not in (0, 1)
        or not isinstance(sample_count, int)
        or isinstance(sample_count, bool)
        or sample_count < 1
    ):
        raise GateFailure(failure_code)
    return WorkerReadinessWatchMessage(
        schema=schema,
        state=state,
        minimum=minimum,
        sample_count=sample_count,
    )


def _remaining_watch_seconds(deadline: float, failure_code: str) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise GateFailure(failure_code)
    return remaining


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    project_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description=(
            "Run the Phase 4B-3C worker-replica release gate against the local Compose product."
        )
    )
    parser.add_argument(
        "--profile",
        choices=tuple(profile.value for profile in ReplicaReleaseProfileName),
        default=ReplicaReleaseProfileName.SMOKE.value,
    )
    parser.add_argument("--project-root", type=Path, default=project_root)
    parser.add_argument("--web-origin", default="http://127.0.0.1:3000")
    parser.add_argument("--postgres-outage-seconds", type=_positive_float, default=20.0)
    parser.add_argument(
        "--artifact-directory",
        type=Path,
        default=project_root / "artifacts" / "release-gates",
    )
    parser.add_argument(
        "--skip-build",
        action="store_true",
        help="Use existing Agent/Worker images. Intended only after an explicit build.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_args(argv)
    profile = get_replica_release_gate_profile(ReplicaReleaseProfileName(arguments.profile))
    runner = ReplicaReleaseGateRunner(
        project_root=arguments.project_root.resolve(),
        web_origin=arguments.web_origin,
        profile=profile,
        artifact_directory=arguments.artifact_directory.resolve(),
        postgres_outage_seconds=arguments.postgres_outage_seconds,
        build_images=not arguments.skip_build,
    )
    report_path: Path | None = None
    try:
        report_path, passed = runner.run()
    except KeyboardInterrupt:
        print("REPLICA_RELEASE_GATE=INTERRUPTED", flush=True)
        return 130
    except GateFailure as exc:
        print(f"REPLICA_RELEASE_GATE=ERROR code={exc.code}", flush=True)
        return 1
    if report_path is not None:
        print(f"REPLICA_RELEASE_GATE_REPORT={report_path}", flush=True)
    print(f"REPLICA_RELEASE_GATE={'PASS' if passed else 'FAIL'}", flush=True)
    return 0 if passed else 1


class ReplicaReleaseGateRunner:
    def __init__(
        self,
        *,
        project_root: Path,
        web_origin: str,
        profile: ReplicaReleaseGateProfile,
        artifact_directory: Path,
        postgres_outage_seconds: float,
        build_images: bool,
    ) -> None:
        self._project_root = project_root
        self._web_origin = web_origin.rstrip("/")
        self._profile = profile
        self._artifact_directory = artifact_directory
        self._postgres_outage_seconds = postgres_outage_seconds
        self._build_images = build_images
        self._compose = ComposeRuntime(project_root)
        self._exclusive_lock = CrossProcessGateLock(
            project_root,
            self._compose.compose_file,
        )
        self._phase_lock = threading.Lock()
        self._phase = "preflight"
        self._samples: list[ObserverSample] = []
        self._samples_lock = threading.Lock()
        self._samples_changed = threading.Condition(self._samples_lock)
        self._latest_snapshot: ReplicaReleaseQueueSnapshot | None = None
        self._latest_snapshot_sequence = 0
        self._stop_sampling = threading.Event()
        self._sampler: threading.Thread | None = None
        self._sampler_process: subprocess.Popen[str] | None = None
        self._sampler_process_lock = threading.RLock()
        self._sampler_expected_stops: set[int] = set()
        self._sampler_first_event: threading.Event | None = None
        self._sampler_fatal_code: str | None = None
        self._sampler_interval_seconds: float | None = None
        self._initial_worker_count = 0
        self._postgres_container_id: str | None = None
        self._postgres_stopped = False
        self._cleanup_codes: list[str] = []
        self._scenario_records: list[dict[str, object]] = []
        self._workload_latencies: list[float] = []
        self._workload_statuses: Counter[str] = Counter()
        self._created_threads: list[tuple[httpx.Client, str]] = []
        self._created_threads_lock = threading.Lock()
        self._runtime_image_provenance: Mapping[str, object] | None = None
        self._client = self._new_http_client()
        self._workload_clients = tuple(
            self._new_http_client() for _ in range(self._profile.workload_concurrency)
        )

    def _new_http_client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self._web_origin,
            headers={
                "Accept": "application/json",
                "Origin": self._web_origin,
                "Sec-Fetch-Site": "same-origin",
            },
            timeout=httpx.Timeout(75.0, connect=5.0),
            follow_redirects=False,
        )

    def run(self) -> tuple[Path, bool]:
        self._exclusive_lock.acquire()
        try:
            return self._run_locked()
        finally:
            self._exclusive_lock.release()

    def _run_locked(self) -> tuple[Path, bool]:
        started_at = datetime.now(UTC)
        failure_codes: list[str] = []
        decision_document: Mapping[str, object] | None = None
        database_evidence: Mapping[str, object] | None = None
        cleanup_document: dict[str, object]
        report_path: Path | None = None
        try:
            if self._profile.name is ReplicaReleaseProfileName.RELEASE and not self._build_images:
                raise GateFailure("release_profile_requires_image_build")
            if (
                self._profile.name is ReplicaReleaseProfileName.RELEASE
                and self._postgres_outage_seconds < 20
            ):
                raise GateFailure("release_profile_outage_too_short")
            self._preflight()
            if not self._build_images:
                self._capture_runtime_image_provenance(agent_replicas=1, worker_replicas=1)
            if self._build_images:
                self._compose.run("build", "agent", "worker", timeout=600)
                self._compose.run(
                    "up",
                    "-d",
                    "--no-deps",
                    "--force-recreate",
                    "agent",
                    "worker",
                    timeout=180,
                )
                self._wait_product_base_ready(timeout=120)
                self._capture_runtime_image_provenance(
                    agent_replicas=1,
                    worker_replicas=1,
                )
            window_started_at = datetime.now(UTC)
            self._scale_workers(self._profile.worker_replica_target)
            self._start_sampler()
            self._worker_admission_fail_closed()
            self._capture_runtime_image_provenance(
                agent_replicas=self._profile.agent_replica_target,
                worker_replicas=self._profile.worker_replica_target,
            )
            gate_started_monotonic = time.monotonic()
            self._run_scheduled_gate(gate_started_monotonic)
            drain_started = time.monotonic()
            final_health = self._wait_queue_converged(
                desired_workers=self._profile.worker_replica_target,
                timeout=self._profile.drain_timeout_seconds,
            )
            drain_seconds = time.monotonic() - drain_started
            database_evidence = self._collect_database_evidence(window_started_at)
            self._require_service_containers("agent", self._profile.agent_replica_target)
            self._require_service_containers("worker", self._profile.worker_replica_target)
            self._capture_runtime_image_provenance(
                agent_replicas=self._profile.agent_replica_target,
                worker_replicas=self._profile.worker_replica_target,
            )
            self._wait_for_fresh_queue_snapshot(timeout=15)
            expected_job_count = (
                self._workload_statuses["completed_workflows"]
                + self._workload_statuses["clarification_resumes"]
                + (self._workload_statuses["agent_recreates"] * 2)
            )
            if _required_int(database_evidence, "created_job_count") != expected_job_count:
                raise GateFailure("database_job_count_mismatch")
            if _required_int(database_evidence, "failed_job_count") > 0:
                raise GateFailure("database_failed_job_observed")
            if _required_int(database_evidence, "active_job_count") > 0:
                raise GateFailure("database_active_job_remains")
            if _required_int(database_evidence, "registered_worker_count") != (
                self._profile.worker_replica_target
            ):
                raise GateFailure("database_worker_registry_count_mismatch")
            for field, code in (
                ("expired_registration_count", "database_expired_registration_remains"),
                ("draining_worker_count", "database_draining_registration_remains"),
                (
                    "active_lease_without_ready_worker_count",
                    "database_unowned_active_lease_remains",
                ),
            ):
                if _required_int(database_evidence, field) > 0:
                    raise GateFailure(code)
            self._raise_if_sampler_failed()
            accumulator = self._build_accumulator()
            observer_violation = self._observer_violation_code()
            if observer_violation is not None:
                raise GateFailure(observer_violation)
            cumulative_recoveries = _required_int(
                database_evidence,
                "recovery_attempt_count",
            )
            if cumulative_recoveries > 0:
                accumulator.record_unexpected_recoveries(cumulative_recoveries)
            observed_seconds = math.floor(time.monotonic() - gate_started_monotonic)
            final = ReplicaReleaseFinalEvidence(
                drain_convergence_seconds=drain_seconds,
                ready_worker_count=final_health.ready_worker_count,
                queued_job_count=final_health.queued_job_count,
                leased_job_count=final_health.leased_job_count,
                expired_lease_count=final_health.expired_lease_count,
                stuck_job_count=final_health.stuck_job_count,
                retained_terminal_ciphertext_count=_required_int(
                    database_evidence,
                    "terminal_payload_retention_count",
                ),
                duplicate_terminal_count=_required_int(
                    database_evidence,
                    "terminal_event_violation_count",
                ),
                ambiguous_recovery_count=_required_int(
                    database_evidence,
                    "ambiguous_recovery_count",
                ),
            )
            evidence = accumulator.build(
                observed_duration_seconds=observed_seconds,
                final=final,
            )
            decision = evaluate_replica_release_gate(self._profile, evidence)
            self._raise_if_sampler_failed()
            decision_document = decision.model_dump(mode="json")
        except GateFailure as exc:
            failure_codes.append(exc.code)
        except Exception as exc:
            failure_codes.append(_stable_exception_code(exc))
        finally:
            self._stop_sampler()
            self._set_phase("cleanup")
            cleanup_document = self._cleanup()
            self._client.close()
            for client in self._workload_clients:
                client.close()

        completed_at = datetime.now(UTC)
        decision_passed = bool(decision_document and decision_document.get("passed") is True)
        passed = decision_passed and not failure_codes and cleanup_document["status"] == "pass"
        report = {
            "schema_version": _REPORT_SCHEMA_VERSION,
            "gate": _GATE_NAME,
            "profile": self._profile.name.value,
            "result": "pass" if passed else "fail",
            "release_eligible": bool(
                passed and decision_document and decision_document.get("release_eligible") is True
            ),
            "started_at": started_at.isoformat(),
            "completed_at": completed_at.isoformat(),
            "scope": {
                "agent_replica_target": self._profile.agent_replica_target,
                "worker_replica_target": self._profile.worker_replica_target,
            },
            "runtime_provenance": {
                "images_built_by_gate": self._build_images,
                "compose_file": self._compose.compose_file.name,
                "compose_sha256": _file_sha256(self._compose.compose_file),
                "normalized_compose_sha256": self._compose.normalized_config_sha256,
                "service_images": self._runtime_image_provenance,
            },
            "profile_contract": self._profile.model_dump(mode="json"),
            "failure_codes": sorted(set(failure_codes)),
            "decision": decision_document,
            "workload": self._workload_report(),
            "scenarios": self._scenario_records,
            "observer": self._observer_report(),
            "database_evidence": database_evidence,
            "cleanup": cleanup_document,
        }
        _assert_report_redacted(report)
        report_path = self._write_report(report, started_at)
        return report_path, passed

    def _preflight(self) -> None:
        if not self._compose.compose_file.is_file():
            raise GateFailure("compose_file_missing")
        origin = httpx.URL(self._web_origin)
        if origin.scheme != "http" or origin.host not in {"127.0.0.1", "localhost", "::1"}:
            raise GateFailure("preflight_web_origin_not_local")
        normalized_config = self._compose.normalized_config()
        for service in ("agent", "worker"):
            _assert_local_mcp_environment(_compose_service_environment(normalized_config, service))
        containers = self._compose.containers()
        by_service = _group_by_service(containers)
        for service in ("postgres", "agent", "worker", "web"):
            matches = by_service.get(service, ())
            if len(matches) != 1:
                raise GateFailure(f"preflight_{service}_replica_count")
            container = matches[0]
            self._compose.assert_owned(container, service)
            if container.state != "running" or container.health != "healthy":
                raise GateFailure(f"preflight_{service}_not_healthy")
        for service in ("agent", "worker"):
            _assert_local_mcp_environment(
                self._compose.container_environment(by_service[service][0].container_id)
            )
        self._initial_worker_count = 1
        self._postgres_container_id = by_service["postgres"][0].container_id
        if self._compose.is_paused(self._postgres_container_id):
            raise GateFailure("preflight_postgres_paused")
        app_environment = self._compose.run(
            "exec",
            "-T",
            "agent",
            "python",
            "-c",
            "import os; print(os.environ.get('APP_ENV', ''))",
            timeout=20,
        ).strip()
        if app_environment not in {"local", "test"}:
            raise GateFailure("preflight_environment_not_local")
        self._assert_web_operational(timeout=15)

    def _scale_workers(self, replicas: int) -> None:
        self._set_phase("scale_up" if replicas > self._initial_worker_count else "cleanup")
        self._compose.run(
            "up",
            "-d",
            "--no-deps",
            "--scale",
            f"worker={replicas}",
            "worker",
            timeout=180,
        )
        self._wait_service_healthy("worker", replicas, timeout=120)
        self._wait_queue_converged(desired_workers=replicas, timeout=90)
        self._set_phase("steady")

    def _run_scheduled_gate(self, started_monotonic: float) -> None:
        interval = self._profile.workload_batch_interval_seconds
        batch_count = math.ceil(self._profile.duration_seconds / interval)
        schedule = _build_disruption_schedule(self._profile, total_slots=batch_count)
        executors = tuple(
            ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"release-workload-{index}")
            for index in range(self._profile.workload_concurrency)
        )
        pending: list[Future[WorkloadOutcome]] = []
        workflow_number = 0
        try:
            for batch_number in range(1, batch_count + 1):
                due = started_monotonic + (batch_number - 1) * interval
                self._wait_until(due)
                disruptions = schedule.get(batch_number, ())
                if any(
                    kind
                    in {
                        ReplicaReleaseDisruptionKind.AGENT_RECREATE,
                        ReplicaReleaseDisruptionKind.POSTGRES_OUTAGE,
                    }
                    for kind in disruptions
                ):
                    self._collect_workload_futures(pending, wait_for_all=True)
                    pending.clear()
                    for disruption in disruptions:
                        self._run_disruption(disruption)
                    disruptions = ()
                for lane, executor in enumerate(executors):
                    workflow_number += 1
                    pending.append(
                        executor.submit(
                            self._run_workflow,
                            self._workload_clients[lane],
                            clarification=(workflow_number % 5 == 0),
                        )
                    )
                for disruption in disruptions:
                    self._run_disruption(disruption)
                pending = self._collect_workload_futures(pending, wait_for_all=False)
            self._wait_until(started_monotonic + self._profile.duration_seconds)
            self._collect_workload_futures(pending, wait_for_all=True)
        finally:
            for executor in executors:
                executor.shutdown(wait=True, cancel_futures=False)

    def _run_disruption(self, disruption: ReplicaReleaseDisruptionKind) -> None:
        if disruption is ReplicaReleaseDisruptionKind.WORKER_ROLL:
            self._worker_roll()
        elif disruption is ReplicaReleaseDisruptionKind.AGENT_RECREATE:
            self._agent_recreate()
        else:
            self._postgres_outage()

    def _collect_workload_futures(
        self,
        futures: Sequence[Future[WorkloadOutcome]],
        *,
        wait_for_all: bool,
    ) -> list[Future[WorkloadOutcome]]:
        pending: list[Future[WorkloadOutcome]] = []
        for future in futures:
            if not wait_for_all and not future.done():
                pending.append(future)
                continue
            outcome = future.result()
            self._workload_latencies.append(outcome.latency_seconds)
            self._workload_statuses[outcome.run_status] += 1
            self._workload_statuses["completed_workflows"] += 1
            if outcome.interrupted:
                self._workload_statuses["clarification_resumes"] += 1
        return pending

    def _run_workflow(
        self,
        client: httpx.Client,
        *,
        clarification: bool,
    ) -> WorkloadOutcome:
        started = time.monotonic()
        thread_id = self._create_thread(client)
        run = self._start_run(
            thread_id,
            _CLARIFICATION_QUESTION if clarification else _FULLY_SPECIFIED_QUESTION,
            client=client,
        )
        interrupted = _require_initial_workflow_response(
            run,
            clarification_expected=clarification,
        )
        if interrupted:
            run = self._resume_run(run, client=client)
        status = _require_terminal_workflow_response(run)
        return WorkloadOutcome(
            interrupted=interrupted,
            run_status=status,
            latency_seconds=time.monotonic() - started,
        )

    def _create_thread(self, client: httpx.Client | None = None) -> str:
        active_client = client or self._client
        response = active_client.post("/api/agent/threads")
        payload = _require_json_object(response, expected_status=201)
        thread_id = payload.get("thread_id")
        if not isinstance(thread_id, str) or not thread_id:
            raise GateFailure("thread_contract_invalid")
        with self._created_threads_lock:
            self._created_threads.append((active_client, thread_id))
        return thread_id

    def _start_run(
        self,
        thread_id: str,
        question: str,
        *,
        client: httpx.Client | None = None,
    ) -> Mapping[str, object]:
        active_client = client or self._client
        response = active_client.post(
            f"/api/agent/threads/{thread_id}/runs",
            json={"question": question},
            headers={"Idempotency-Key": str(uuid4())},
        )
        return _require_json_object(response, expected_status=200)

    def _resume_run(
        self,
        run: Mapping[str, object],
        *,
        client: httpx.Client | None = None,
    ) -> Mapping[str, object]:
        run_id = run.get("run_id")
        clarification = run.get("clarification")
        if not isinstance(run_id, str) or not isinstance(clarification, Mapping):
            raise GateFailure("clarification_contract_invalid")
        payload = _structured_clarification_answer(clarification)
        response = (client or self._client).post(
            f"/api/agent/runs/{run_id}/resume",
            json=payload,
            headers={"Idempotency-Key": str(uuid4())},
        )
        return _require_json_object(response, expected_status=200)

    def _delete_thread(self, client: httpx.Client, thread_id: str) -> bool:
        try:
            response = client.delete(f"/api/agent/threads/{thread_id}")
        except httpx.HTTPError:
            return False
        if response.status_code in {204, 404}:
            with self._created_threads_lock:
                entry = (client, thread_id)
                if entry in self._created_threads:
                    self._created_threads.remove(entry)
            return True
        return response.status_code in {204, 404}

    def _worker_admission_fail_closed(self) -> None:
        self._set_phase("worker_admission")
        thread_id = self._create_thread()
        workers = self._require_service_containers("worker", self._profile.worker_replica_target)
        for worker in workers:
            self._compose.assert_owned(worker, "worker")
        self._compose.docker(
            "stop",
            "--time",
            "40",
            *(worker.container_id for worker in workers),
            timeout=90,
        )
        self._wait_ready_worker_count(0, timeout=30)
        response = self._client.post(
            f"/api/agent/threads/{thread_id}/runs",
            json={"question": _FULLY_SPECIFIED_QUESTION},
            headers={"Idempotency-Key": str(uuid4())},
            timeout=20,
        )
        _require_error_response(
            response,
            expected_status=503,
            expected_code="EXECUTION_WORKER_UNAVAILABLE",
        )
        recovery_started = time.monotonic()
        recovery_deadline = recovery_started + self._profile.recovery_convergence_seconds
        try:
            self._compose.run(
                "up",
                "-d",
                "--no-deps",
                "--scale",
                f"worker={self._profile.worker_replica_target}",
                "worker",
                timeout=_remaining_recovery_seconds(
                    recovery_deadline,
                    "worker_admission_recovery_budget_exceeded",
                ),
            )
            self._wait_service_healthy(
                "worker",
                self._profile.worker_replica_target,
                timeout=_remaining_recovery_seconds(
                    recovery_deadline,
                    "worker_admission_recovery_budget_exceeded",
                ),
            )
            self._wait_queue_converged(
                desired_workers=self._profile.worker_replica_target,
                timeout=_remaining_recovery_seconds(
                    recovery_deadline,
                    "worker_admission_recovery_budget_exceeded",
                ),
            )
        except GateFailure as exc:
            if time.monotonic() >= recovery_deadline:
                raise GateFailure("worker_admission_recovery_budget_exceeded") from exc
            raise
        elapsed = time.monotonic() - recovery_started
        if elapsed > self._profile.recovery_convergence_seconds:
            raise GateFailure("worker_admission_recovery_budget_exceeded")
        self._scenario_records.append(
            {
                "kind": "worker_admission_fail_closed",
                "status": "pass",
                "recovery_convergence_seconds": round(elapsed, 3),
                "minimum_ready_worker_count": 0,
            }
        )
        self._set_phase("steady")

    def _worker_roll(self) -> None:
        self._set_phase("worker_roll")
        workers = self._require_service_containers("worker", self._profile.worker_replica_target)
        selected_index = self._workload_statuses["worker_rolls"] % len(workers)
        selected = workers[selected_index]
        other = workers[1 - selected_index]
        self._compose.assert_owned(selected, "worker")
        self._compose.assert_owned(other, "worker")
        started = time.monotonic()
        monitor = DirectWorkerReadinessMonitor(self._compose, (other,))
        monitor.start()
        try:
            if monitor.minimum_ready_count != 1:
                raise GateFailure("worker_roll_peer_not_ready")
            self._compose.docker(
                "restart",
                "--time",
                "40",
                selected.container_id,
                timeout=90,
            )
            self._wait_container_healthy(selected.container_id, timeout=90)
            final = self._wait_queue_converged(
                desired_workers=self._profile.worker_replica_target,
                timeout=self._profile.recovery_convergence_seconds,
            )
        finally:
            monitor.stop()
        minimum_ready = monitor.minimum_ready_count
        if final.ready_worker_count != self._profile.worker_replica_target:
            minimum_ready = 0
        elapsed = time.monotonic() - started
        self._workload_statuses["worker_rolls"] += 1
        self._record_scenario(
            ReplicaReleaseDisruptionKind.WORKER_ROLL,
            elapsed,
            minimum_ready,
            status="pass" if minimum_ready >= 1 else "fail",
        )
        self._set_phase("steady")

    def _agent_recreate(self) -> None:
        self._set_phase("agent_recreate")
        workers_before = {
            container.container_id
            for container in self._require_service_containers(
                "worker", self._profile.worker_replica_target
            )
        }
        thread_id = self._create_thread()
        run = self._start_run(thread_id, _CLARIFICATION_QUESTION)
        _require_initial_workflow_response(run, clarification_expected=True)
        run_id = run.get("run_id")
        if not isinstance(run_id, str):
            raise GateFailure("agent_recreate_run_contract_invalid")
        worker_containers = self._require_service_containers(
            "worker", self._profile.worker_replica_target
        )
        monitor = DirectWorkerReadinessMonitor(self._compose, worker_containers)
        monitor.start()
        started = time.monotonic()
        try:
            if monitor.minimum_ready_count != self._profile.worker_replica_target:
                raise GateFailure("agent_recreate_worker_readiness_lost")
            self._compose.run(
                "up",
                "-d",
                "--no-deps",
                "--force-recreate",
                "agent",
                timeout=180,
            )
            self._wait_service_healthy("agent", 1, timeout=120)
            self._restart_sampler_after_agent_recreate()
            self._assert_web_operational(timeout=90)
            restored = self._get_run_with_retry(run_id, timeout=30)
            completed = self._resume_run(restored)
            _require_terminal_workflow_response(completed)
            workers_after = {
                container.container_id
                for container in self._require_service_containers(
                    "worker", self._profile.worker_replica_target
                )
            }
            if workers_before != workers_after:
                raise GateFailure("agent_recreate_changed_workers")
            self._wait_queue_converged(
                desired_workers=self._profile.worker_replica_target,
                timeout=self._profile.recovery_convergence_seconds,
            )
        finally:
            monitor.stop()
        elapsed = time.monotonic() - started
        minimum_ready = monitor.minimum_ready_count
        scenario_status = "pass" if minimum_ready == self._profile.worker_replica_target else "fail"
        self._record_scenario(
            ReplicaReleaseDisruptionKind.AGENT_RECREATE,
            elapsed,
            minimum_ready,
            status=scenario_status,
        )
        if scenario_status == "fail":
            raise GateFailure("agent_recreate_worker_readiness_lost")
        self._workload_statuses["agent_recreates"] += 1
        self._set_phase("steady")

    def _postgres_outage(self) -> None:
        if self._postgres_container_id is None:
            raise GateFailure("postgres_container_missing")
        self._set_phase("postgres_outage")
        thread_id = self._create_thread()
        before = self._wait_for_fresh_queue_snapshot(timeout=15)
        self._compose.assert_owned(
            self._container_by_id(self._postgres_container_id),
            "postgres",
        )
        self._compose.docker(
            "stop",
            "--time",
            "10",
            self._postgres_container_id,
            timeout=30,
        )
        self._postgres_stopped = True
        if self._compose.is_running(self._postgres_container_id):
            raise GateFailure("postgres_outage_stop_failed")
        time.sleep(self._postgres_outage_seconds)
        try:
            response = self._client.post(
                f"/api/agent/threads/{thread_id}/runs",
                json={"question": _FULLY_SPECIFIED_QUESTION},
                headers={"Idempotency-Key": str(uuid4())},
                timeout=httpx.Timeout(20.0, connect=5.0),
            )
        except httpx.TimeoutException as exc:
            raise GateFailure("postgres_outage_mutation_unknown") from exc
        _require_error_response(
            response,
            expected_status=503,
            expected_code="PERSISTENCE_UNAVAILABLE",
        )
        self._set_phase("postgres_recovery")
        recovery_started = time.monotonic()
        self._compose.docker("start", self._postgres_container_id, timeout=60)
        self._postgres_stopped = False
        self._wait_container_healthy(self._postgres_container_id, timeout=90)
        self._compose.run(
            "up",
            "-d",
            "--no-deps",
            "--scale",
            f"worker={self._profile.worker_replica_target}",
            "worker",
            timeout=180,
        )
        self._wait_service_healthy("worker", self._profile.worker_replica_target, timeout=120)
        self._assert_web_operational(timeout=90)
        after = self._wait_queue_converged(
            desired_workers=self._profile.worker_replica_target,
            timeout=self._profile.recovery_convergence_seconds,
        )
        if after.queued_job_count != before.queued_job_count:
            raise GateFailure("postgres_outage_orphan_queue_change")
        elapsed = time.monotonic() - recovery_started
        self._workload_statuses["postgres_outages"] += 1
        self._record_scenario(
            ReplicaReleaseDisruptionKind.POSTGRES_OUTAGE,
            elapsed,
            0,
        )
        self._set_phase("steady")

    def _record_scenario(
        self,
        kind: ReplicaReleaseDisruptionKind,
        convergence_seconds: float,
        minimum_ready_workers: int,
        *,
        status: str = "pass",
    ) -> None:
        self._scenario_records.append(
            {
                "kind": kind.value,
                "status": status,
                "recovery_convergence_seconds": round(convergence_seconds, 3),
                "minimum_ready_worker_count": minimum_ready_workers,
            }
        )

    def _get_run_with_retry(self, run_id: str, *, timeout: float) -> Mapping[str, object]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                response = self._client.get(f"/api/agent/runs/{run_id}", timeout=10)
            except httpx.HTTPError:
                time.sleep(1)
                continue
            if response.status_code == 200:
                return _require_json_object(response, expected_status=200)
            time.sleep(1)
        raise GateFailure("run_read_recovery_timeout")

    def _start_sampler(self) -> None:
        interval = 2.0 if self._profile.name is ReplicaReleaseProfileName.SMOKE else 5.0
        self._stop_sampling.clear()
        self._open_sampler_stream(interval_seconds=interval)
        self._wait_for_sampler_start()

    def _stop_sampler(self) -> None:
        self._stop_sampling.set()
        try:
            self._close_sampler_stream(expected=True)
        except GateFailure as exc:
            self._cleanup_codes.append(exc.code)

    def _restart_sampler_after_agent_recreate(self) -> None:
        interval = 2.0 if self._profile.name is ReplicaReleaseProfileName.SMOKE else 5.0
        self._close_sampler_stream(expected=True)
        self._open_sampler_stream(interval_seconds=interval)
        self._wait_for_sampler_start()

    def _open_sampler_stream(self, *, interval_seconds: float) -> None:
        with self._sampler_process_lock:
            if self._sampler_process is not None:
                raise GateFailure("execution_health_stream_already_running")
            agent = self._require_service_containers("agent", 1)[0]
            self._compose.assert_owned(agent, "agent")
            process = self._compose.start_execution_health_watch(
                agent.container_id,
                stuck_after_seconds=self._profile.stuck_after_seconds,
                interval_seconds=interval_seconds,
            )
            if process.stdout is None or process.stdin is None:
                with suppress(OSError):
                    process.terminate()
                raise GateFailure("execution_health_stream_pipe_missing")
            first_event = threading.Event()
            sampler = threading.Thread(
                target=self._consume_sampler_stream,
                args=(process, first_event),
                name="replica-release-health-stream",
                daemon=True,
            )
            self._sampler_process = process
            self._sampler_first_event = first_event
            self._sampler = sampler
            self._sampler_interval_seconds = interval_seconds
            sampler.start()

    def _wait_for_sampler_start(self) -> None:
        with self._sampler_process_lock:
            first_event = self._sampler_first_event
        if first_event is None or not first_event.wait(_SAMPLER_START_TIMEOUT_SECONDS):
            with suppress(GateFailure):
                self._close_sampler_stream(expected=True)
            raise GateFailure("execution_health_stream_start_timeout")
        self._raise_if_sampler_failed()

    def _consume_sampler_stream(
        self,
        process: subprocess.Popen[str],
        first_event: threading.Event,
    ) -> None:
        stream = process.stdout
        if stream is None:  # pragma: no cover - rejected before reader startup
            self._record_sampler_failure("execution_health_stream_pipe_missing", first_event)
            return
        try:
            for line in stream:
                phase = self._get_phase()
                try:
                    snapshot, error_code = _parse_execution_health_stream_event(
                        line,
                        expected_stuck_after_seconds=self._profile.stuck_after_seconds,
                    )
                except GateFailure as exc:
                    self._append_observer_sample(
                        phase=phase,
                        snapshot=None,
                        error_code=exc.code,
                    )
                    self._set_sampler_fatal(exc.code)
                else:
                    self._append_observer_sample(
                        phase=phase,
                        snapshot=snapshot,
                        error_code=error_code,
                    )
                first_event.set()
        except (OSError, UnicodeError):
            phase = self._get_phase()
            code = "execution_health_stream_io_failed"
            self._append_observer_sample(phase=phase, snapshot=None, error_code=code)
            if phase != "agent_recreate" and not self._stop_sampling.is_set():
                self._set_sampler_fatal(code)
            first_event.set()
        finally:
            with self._sampler_process_lock:
                expected = id(process) in self._sampler_expected_stops
            if not self._stop_sampling.is_set() and not expected:
                phase = self._get_phase()
                code = "execution_health_stream_closed"
                self._append_observer_sample(phase=phase, snapshot=None, error_code=code)
                if phase != "agent_recreate":
                    self._set_sampler_fatal(code)
            first_event.set()

    def _close_sampler_stream(self, *, expected: bool) -> None:
        with self._sampler_process_lock:
            process = self._sampler_process
            sampler = self._sampler
            if process is None:
                return
            if expected:
                self._sampler_expected_stops.add(id(process))
        stop_code: str | None = None
        try:
            if process.stdin is not None and not process.stdin.closed:
                try:
                    process.stdin.write("\n")
                    process.stdin.flush()
                except (BrokenPipeError, OSError):
                    pass
                finally:
                    with suppress(OSError):
                        process.stdin.close()
            try:
                process.wait(timeout=_SAMPLER_STOP_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                with suppress(OSError):
                    process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    with suppress(OSError):
                        process.kill()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        stop_code = "execution_health_stream_stop_timeout"
            if process.stdout is not None:
                with suppress(OSError):
                    process.stdout.close()
            if sampler is not None:
                sampler.join(timeout=5)
                if sampler.is_alive():
                    stop_code = "execution_health_stream_reader_stop_timeout"
        finally:
            with self._sampler_process_lock:
                if self._sampler_process is process:
                    self._sampler_process = None
                    self._sampler = None
                    self._sampler_first_event = None
                self._sampler_expected_stops.discard(id(process))
        if stop_code is not None:
            raise GateFailure(stop_code)

    def _record_sampler_failure(
        self,
        code: str,
        first_event: threading.Event,
    ) -> None:
        self._append_observer_sample(
            phase=self._get_phase(),
            snapshot=None,
            error_code=code,
        )
        self._set_sampler_fatal(code)
        first_event.set()

    def _append_observer_sample(
        self,
        *,
        phase: str,
        snapshot: ReplicaReleaseQueueSnapshot | None,
        error_code: str | None,
    ) -> None:
        sample = ObserverSample(
            observed_at=datetime.now(UTC).isoformat(),
            observed_monotonic=time.monotonic(),
            phase=phase,
            snapshot=snapshot,
            error_code=error_code,
        )
        with self._samples_changed:
            self._samples.append(sample)
            if snapshot is not None:
                self._latest_snapshot = snapshot
                self._latest_snapshot_sequence += 1
            self._samples_changed.notify_all()

    def _set_sampler_fatal(self, code: str) -> None:
        with self._sampler_process_lock:
            if self._sampler_fatal_code is None:
                self._sampler_fatal_code = code

    def _raise_if_sampler_failed(self) -> None:
        with self._sampler_process_lock:
            code = self._sampler_fatal_code
        if code is not None:
            raise GateFailure(code)

    def _execution_health(self, *, timeout: float = 20) -> ReplicaReleaseQueueSnapshot:
        output = self._compose.run(
            "exec",
            "-T",
            "agent",
            "mobility-agent",
            "execution-health",
            "--stuck-after-seconds",
            str(self._profile.stuck_after_seconds),
            timeout=timeout,
        )
        try:
            document = json.loads(_last_nonempty_line(output))
            if not isinstance(document, Mapping):
                raise ValueError("execution health must be an object")
            stuck_after = document.get("stuck_after_seconds")
            if not isinstance(stuck_after, int | float) or stuck_after != (
                self._profile.stuck_after_seconds
            ):
                raise ValueError("execution health threshold mismatch")
            projected = {
                "ready_worker_count": document.get("ready_worker_count"),
                "queued_job_count": document.get("queued_job_count"),
                "leased_job_count": document.get("leased_job_count"),
                "expired_lease_count": document.get("expired_lease_count"),
                "recovery_attempt_count": document.get("recovery_attempt_count"),
                "stuck_job_count": document.get("stuck_job_count"),
                "stuck_after_seconds": self._profile.stuck_after_seconds,
                "oldest_queued_age_seconds": document.get("oldest_queued_age_seconds"),
            }
            return ReplicaReleaseQueueSnapshot.model_validate(projected)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise GateFailure("execution_health_contract_invalid") from exc

    def _collect_database_evidence(self, window_started_at: datetime) -> Mapping[str, object]:
        output = self._compose.run(
            "exec",
            "-T",
            "agent",
            "mobility-agent",
            "replica-release-evidence",
            "--window-started-at",
            window_started_at.isoformat(),
            timeout=30,
        )
        try:
            document = json.loads(_last_nonempty_line(output))
            evidence = ReplicaReleaseDatabaseEvidence.model_validate(document)
        except (json.JSONDecodeError, ValidationError) as exc:
            raise GateFailure("database_evidence_contract_invalid") from exc
        canonical = evidence.model_dump(mode="json")
        _assert_report_redacted(canonical)
        return canonical

    def _wait_queue_converged(
        self,
        *,
        desired_workers: int,
        timeout: float,
    ) -> ReplicaReleaseQueueSnapshot:
        if self._sampler_is_active():
            return self._wait_for_fresh_queue_snapshot(
                timeout=timeout,
                predicate=lambda snapshot: (
                    snapshot.ready_worker_count == desired_workers
                    and snapshot.queued_job_count == 0
                    and snapshot.leased_job_count == 0
                    and snapshot.expired_lease_count == 0
                    and snapshot.stuck_job_count == 0
                ),
                timeout_code="queue_convergence_timeout",
            )
        deadline = time.monotonic() + timeout
        last: ReplicaReleaseQueueSnapshot | None = None
        while time.monotonic() < deadline:
            try:
                last = self._execution_health()
            except GateFailure:
                time.sleep(1)
                continue
            if (
                last.ready_worker_count == desired_workers
                and last.queued_job_count == 0
                and last.leased_job_count == 0
                and last.expired_lease_count == 0
                and last.stuck_job_count == 0
            ):
                return last
            time.sleep(1)
        raise GateFailure("queue_convergence_timeout")

    def _wait_for_fresh_queue_snapshot(
        self,
        *,
        timeout: float,
        predicate: Callable[[ReplicaReleaseQueueSnapshot], bool] | None = None,
        timeout_code: str = "execution_health_stream_snapshot_timeout",
    ) -> ReplicaReleaseQueueSnapshot:
        self._raise_if_sampler_failed()
        if not self._sampler_is_active():
            raise GateFailure("execution_health_stream_not_running")
        deadline = time.monotonic() + timeout
        with self._samples_changed:
            observed_sequence = self._latest_snapshot_sequence
        while True:
            self._raise_if_sampler_failed()
            with self._samples_changed:
                if self._latest_snapshot_sequence > observed_sequence:
                    snapshot = self._latest_snapshot
                    observed_sequence = self._latest_snapshot_sequence
                    if snapshot is not None and (predicate is None or predicate(snapshot)):
                        return snapshot
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise GateFailure(timeout_code)
                self._samples_changed.wait(timeout=min(remaining, 0.5))

    def _sampler_is_active(self) -> bool:
        with self._sampler_process_lock:
            return self._sampler_process is not None and not self._stop_sampling.is_set()

    def _wait_service_healthy(self, service: str, replicas: int, *, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            matches = [
                container
                for container in self._compose.containers()
                if container.service == service
            ]
            if len(matches) == replicas and all(
                item.state == "running" and item.health == "healthy" for item in matches
            ):
                return
            time.sleep(1)
        raise GateFailure(f"{service}_health_timeout")

    def _wait_container_healthy(self, container_id: str, *, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._compose.is_healthy(container_id):
                return
            time.sleep(1)
        raise GateFailure("container_health_timeout")

    def _wait_product_base_ready(self, *, timeout: float) -> None:
        self._wait_service_healthy("postgres", 1, timeout=timeout)
        self._wait_service_healthy("agent", 1, timeout=timeout)
        self._wait_service_healthy("worker", 1, timeout=timeout)
        self._wait_service_healthy("web", 1, timeout=timeout)
        self._assert_web_operational(timeout=timeout)

    def _assert_web_operational(self, *, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                response = self._client.get("/api/agent/public-status", timeout=10)
                if response.status_code == 200:
                    payload = response.json()
                    if isinstance(payload, dict) and payload.get("status") == "operational":
                        return
            except (httpx.HTTPError, ValueError):
                pass
            time.sleep(1)
        raise GateFailure("web_operational_timeout")

    def _require_service_containers(self, service: str, count: int) -> list[ComposeContainer]:
        matches = sorted(
            (container for container in self._compose.containers() if container.service == service),
            key=lambda item: item.name,
        )
        if len(matches) != count:
            raise GateFailure(f"{service}_replica_count_changed")
        return matches

    def _capture_runtime_image_provenance(
        self,
        *,
        agent_replicas: int,
        worker_replicas: int,
    ) -> None:
        service_images: dict[str, object] = {}
        for service, replica_count in (
            ("agent", agent_replicas),
            ("worker", worker_replicas),
        ):
            containers = self._require_service_containers(service, replica_count)
            digests = sorted(
                {
                    self._compose.container_image_content_digest(container.container_id)
                    for container in containers
                }
            )
            service_images[service] = {
                "replica_count": len(containers),
                "content_digests": digests,
            }
        self._runtime_image_provenance = service_images

    def _container_by_id(self, container_id: str) -> ComposeContainer:
        for container in self._compose.containers(all_containers=True):
            if container.container_id == container_id or container_id.startswith(
                container.container_id
            ):
                return container
        raise GateFailure("owned_container_missing")

    def _wait_until(self, due: float) -> None:
        while True:
            self._raise_if_sampler_failed()
            remaining = due - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(remaining, 1.0))

    def _build_accumulator(self) -> ReplicaReleaseEvidenceAccumulator:
        accumulator = ReplicaReleaseEvidenceAccumulator(self._profile)
        completed_workflows = self._workload_statuses["completed_workflows"]
        if completed_workflows > 0:
            accumulator.record_workflows(completed_workflows)
        with self._samples_lock:
            samples = tuple(self._samples)
        for sample in samples:
            if sample.snapshot is not None:
                accumulator.record_queue_snapshot(sample.snapshot)
        for scenario in self._scenario_records:
            kind = scenario.get("kind")
            convergence = scenario.get("recovery_convergence_seconds")
            minimum_ready = scenario.get("minimum_ready_worker_count")
            if kind == "worker_admission_fail_closed":
                continue
            if (
                not isinstance(kind, str)
                or not isinstance(convergence, int | float)
                or isinstance(convergence, bool)
                or not isinstance(minimum_ready, int)
                or isinstance(minimum_ready, bool)
            ):
                raise GateFailure("scenario_evidence_contract_invalid")
            accumulator.record_disruption(
                ReplicaReleaseDisruptionEvidence(
                    kind=ReplicaReleaseDisruptionKind(kind),
                    recovery_convergence_seconds=float(convergence),
                    minimum_ready_worker_count=minimum_ready,
                )
            )
        return accumulator

    def _observer_violation_code(self) -> str | None:
        with self._sampler_process_lock:
            sampler_fatal_code = self._sampler_fatal_code
        if sampler_fatal_code is not None:
            return sampler_fatal_code
        with self._samples_lock:
            samples = tuple(self._samples)
        if len(samples) < 2:
            return "observer_evidence_missing"
        interval = self._sampler_interval_seconds
        if interval is None:
            return "observer_cadence_contract_missing"
        maximum_gap = (interval * 2) + 1
        for previous, current in pairwise(samples):
            if (
                previous.phase in _OBSERVER_CADENCE_EXEMPT_PHASES
                or current.phase in _OBSERVER_CADENCE_EXEMPT_PHASES
            ):
                continue
            if current.observed_monotonic - previous.observed_monotonic > maximum_gap:
                return "observer_sample_cadence_missed"
        if time.monotonic() - samples[-1].observed_monotonic > maximum_gap:
            return "observer_latest_sample_stale"
        successful = 0
        consecutive_unexpected_errors = 0
        for sample in samples:
            if sample.error_code is not None and _is_expected_observer_error(sample):
                consecutive_unexpected_errors = 0
                continue
            if sample.error_code is not None:
                consecutive_unexpected_errors += 1
                if consecutive_unexpected_errors > 2:
                    return "observer_gap_budget_exceeded"
                continue
            consecutive_unexpected_errors = 0
            snapshot = sample.snapshot
            if snapshot is None:
                return "observer_sample_contract_invalid"
            successful += 1
            if sample.phase in _OBSERVER_TARGET_EXEMPT_PHASES:
                continue
            if sample.phase == "worker_roll":
                if snapshot.ready_worker_count < 1:
                    return "observer_all_workers_unavailable"
            elif snapshot.ready_worker_count != self._profile.worker_replica_target:
                return "observer_ready_worker_target_missed"
        if successful == 0:
            return "observer_evidence_missing"
        return None

    def _wait_ready_worker_count(self, expected: int, *, timeout: float) -> None:
        if self._sampler_is_active():
            self._wait_for_fresh_queue_snapshot(
                timeout=timeout,
                predicate=lambda snapshot: snapshot.ready_worker_count == expected,
                timeout_code="ready_worker_count_timeout",
            )
            return
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                snapshot = self._execution_health()
            except GateFailure:
                time.sleep(0.5)
                continue
            if snapshot.ready_worker_count == expected:
                return
            time.sleep(0.5)
        raise GateFailure("ready_worker_count_timeout")

    def _observer_report(self) -> dict[str, object]:
        with self._samples_lock:
            samples = tuple(self._samples)
        errors = Counter(
            sample.error_code or "none" for sample in samples if sample.error_code is not None
        )
        unexpected = sum(
            1
            for sample in samples
            if sample.error_code is not None and not _is_expected_observer_error(sample)
        )
        return {
            "sample_count": len(samples),
            "successful_sample_count": sum(sample.snapshot is not None for sample in samples),
            "expected_unavailable_sample_count": sum(
                sample.error_code is not None and _is_expected_observer_error(sample)
                for sample in samples
            ),
            "unexpected_unavailable_sample_count": unexpected,
            "error_code_counts": dict(sorted(errors.items())),
        }

    def _workload_report(self) -> dict[str, object]:
        latencies = sorted(self._workload_latencies)
        return {
            "planned_workflow_count": _planned_workflow_count(self._profile),
            "workload_concurrency": self._profile.workload_concurrency,
            "batch_interval_seconds": self._profile.workload_batch_interval_seconds,
            "completed_workflow_count": self._workload_statuses["completed_workflows"],
            "clarification_resume_count": self._workload_statuses["clarification_resumes"],
            "completed_run_count": self._workload_statuses["completed"],
            "failed_run_count": self._workload_statuses["failed"],
            "latency_seconds": {
                "max": round(max(latencies), 3) if latencies else None,
                "p95": round(_percentile(latencies, 0.95), 3) if latencies else None,
            },
        }

    def _cleanup(self) -> dict[str, object]:
        postgres_running = False
        if self._postgres_container_id is not None:
            try:
                postgres_running = self._compose.is_running(self._postgres_container_id)
            except (GateFailure, subprocess.SubprocessError):
                self._cleanup_codes.append("postgres_state_unavailable")
        if self._postgres_container_id is not None and (
            self._postgres_stopped or not postgres_running
        ):
            try:
                self._compose.docker("start", self._postgres_container_id, timeout=60)
                self._postgres_stopped = False
                self._wait_container_healthy(self._postgres_container_id, timeout=90)
                postgres_running = True
            except (GateFailure, subprocess.SubprocessError):
                self._cleanup_codes.append("postgres_restore_failed")
        if self._initial_worker_count > 0:
            try:
                self._compose.run(
                    "up",
                    "-d",
                    "--no-deps",
                    "--scale",
                    f"worker={self._initial_worker_count}",
                    "worker",
                    timeout=180,
                )
                self._wait_service_healthy("worker", self._initial_worker_count, timeout=120)
                self._wait_queue_converged(
                    desired_workers=self._initial_worker_count,
                    timeout=self._profile.recovery_convergence_seconds,
                )
            except (GateFailure, subprocess.SubprocessError):
                self._cleanup_codes.append("worker_topology_restore_failed")
        try:
            self._wait_service_healthy("agent", 1, timeout=90)
            self._wait_service_healthy("postgres", 1, timeout=90)
            self._wait_service_healthy("web", 1, timeout=90)
            self._assert_web_operational(timeout=60)
        except (GateFailure, subprocess.SubprocessError):
            self._cleanup_codes.append("product_health_restore_failed")
        with self._created_threads_lock:
            created_threads = tuple(self._created_threads)
        for client, thread_id in created_threads:
            if not self._delete_thread(client, thread_id):
                self._cleanup_codes.append("thread_cleanup_failed")
        with self._created_threads_lock:
            cleanup_incomplete = bool(self._created_threads)
        if cleanup_incomplete:
            self._cleanup_codes.append("thread_cleanup_incomplete")
        if self._initial_worker_count > 0 and postgres_running:
            try:
                registry = self._collect_database_evidence(datetime.now(UTC) - timedelta(seconds=1))
                if (
                    _required_int(registry, "ready_worker_count") != self._initial_worker_count
                    or _required_int(registry, "registered_worker_count")
                    != self._initial_worker_count
                    or _required_int(registry, "expired_registration_count") != 0
                    or _required_int(registry, "draining_worker_count") != 0
                ):
                    self._cleanup_codes.append("worker_registry_restore_failed")
            except (GateFailure, subprocess.SubprocessError):
                self._cleanup_codes.append("worker_registry_restore_failed")
        if self._postgres_container_id is not None:
            try:
                postgres_running = self._compose.is_running(self._postgres_container_id)
            except (GateFailure, subprocess.SubprocessError):
                postgres_running = False
                self._cleanup_codes.append("postgres_final_state_unavailable")
        return {
            "status": "pass" if not self._cleanup_codes else "fail",
            "restored_agent_replica_count": 1,
            "restored_worker_replica_count": self._initial_worker_count,
            "postgres_running": postgres_running,
            "failure_codes": sorted(set(self._cleanup_codes)),
        }

    def _write_report(self, report: Mapping[str, object], started_at: datetime) -> Path:
        return _write_collision_safe_report(
            self._artifact_directory,
            started_at,
            report,
        )

    def _set_phase(self, phase: str) -> None:
        with self._phase_lock:
            self._phase = phase
        print(f"REPLICA_RELEASE_GATE_PHASE={phase}", flush=True)

    def _get_phase(self) -> str:
        with self._phase_lock:
            return self._phase


class ComposeRuntime:
    def __init__(self, project_root: Path) -> None:
        self._project_root = project_root.resolve()
        self._compose_file = (self._project_root / "compose.yaml").resolve()
        self._project_name: str | None = None
        self._normalized_config_sha256: str | None = None

    @property
    def compose_file(self) -> Path:
        return self._compose_file

    @property
    def normalized_config_sha256(self) -> str | None:
        return self._normalized_config_sha256

    def run(self, *arguments: str, timeout: float) -> str:
        return self._command(
            (
                "docker",
                "compose",
                "--project-directory",
                str(self._project_root),
                "-f",
                str(self._compose_file),
                *arguments,
            ),
            timeout=timeout,
        )

    def docker(self, *arguments: str, timeout: float) -> str:
        return self._command(("docker", *arguments), timeout=timeout)

    def start_execution_health_watch(
        self,
        container_id: str,
        *,
        stuck_after_seconds: float,
        interval_seconds: float,
    ) -> subprocess.Popen[str]:
        command = [
            "docker",
            "exec",
            "-i",
            container_id,
            "python",
            "-m",
            "barrier_free_mobility_agent.persistence.execution_health_watch",
            "--stuck-after-seconds",
            str(stuck_after_seconds),
            "--interval-seconds",
            str(interval_seconds),
        ]
        try:
            return subprocess.Popen(
                command,
                cwd=self._project_root,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="strict",
                bufsize=1,
            )
        except (OSError, ValueError) as exc:
            raise GateFailure("execution_health_stream_start_failed") from exc

    def normalized_config(self) -> Mapping[str, object]:
        output = self.run("config", "--format", "json", timeout=30)
        try:
            document = json.loads(output)
        except json.JSONDecodeError as exc:
            raise GateFailure("compose_config_contract_invalid") from exc
        if not isinstance(document, Mapping):
            raise GateFailure("compose_config_contract_invalid")
        canonical = json.dumps(document, sort_keys=True, separators=(",", ":"))
        self._normalized_config_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return document

    def container_environment(self, container_id: str) -> Mapping[str, str]:
        output = self.docker(
            "inspect",
            "--format",
            "{{json .Config.Env}}",
            container_id,
            timeout=30,
        )
        try:
            entries = json.loads(output)
        except json.JSONDecodeError as exc:
            raise GateFailure("container_environment_invalid") from exc
        if not isinstance(entries, list) or not all(isinstance(item, str) for item in entries):
            raise GateFailure("container_environment_invalid")
        environment: dict[str, str] = {}
        for item in entries:
            key, separator, value = item.partition("=")
            if not separator or not key:
                raise GateFailure("container_environment_invalid")
            environment[key] = value
        return environment

    def container_image_content_digest(self, container_id: str) -> str:
        digest = self.docker(
            "inspect",
            "--format",
            "{{.Image}}",
            container_id,
            timeout=30,
        ).strip()
        if _SHA256_CONTENT_DIGEST.fullmatch(digest) is None:
            raise GateFailure("container_image_digest_invalid")
        return digest

    def start_worker_readiness_watch(
        self,
        container_id: str,
    ) -> subprocess.Popen[str]:
        try:
            return subprocess.Popen(
                [
                    "docker",
                    "exec",
                    "-i",
                    container_id,
                    "python",
                    "-m",
                    ("barrier_free_mobility_agent.persistence.worker_readiness_watch"),
                ],
                cwd=self._project_root,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except OSError as exc:
            raise GateFailure("worker_readiness_watch_start_failed") from exc

    def _command(self, command: Sequence[str], *, timeout: float) -> str:
        try:
            result = _run_command_with_tree_cleanup(
                command,
                cwd=self._project_root,
                timeout=timeout,
            )
        except FileNotFoundError as exc:
            raise GateFailure("docker_command_missing") from exc
        except subprocess.TimeoutExpired as exc:
            raise GateFailure("docker_command_timeout") from exc
        if result.returncode != 0:
            raise GateFailure("docker_command_failed")
        return result.stdout

    def containers(self, *, all_containers: bool = False) -> list[ComposeContainer]:
        arguments = ["ps"]
        if all_containers:
            arguments.append("--all")
        arguments.extend(("--format", "json"))
        containers = _parse_compose_ps(self.run(*arguments, timeout=30))
        projects = {container.project for container in containers if container.project}
        if self._project_name is None and len(projects) == 1:
            self._project_name = next(iter(projects))
        return containers

    def assert_owned(self, container: ComposeContainer, service: str) -> None:
        if service not in _OWNED_SERVICES:
            raise GateFailure("unsupported_compose_service")
        if container.service != service:
            raise GateFailure("container_service_mismatch")
        if self._project_name is None or container.project != self._project_name:
            raise GateFailure("container_project_mismatch")
        output = self.docker(
            "inspect",
            "--format",
            "{{json .Config.Labels}}",
            container.container_id,
            timeout=30,
        )
        try:
            labels = json.loads(output)
        except json.JSONDecodeError as exc:
            raise GateFailure("container_labels_invalid") from exc
        if not isinstance(labels, Mapping):
            raise GateFailure("container_labels_invalid")
        expected_directory = str(self._project_root.resolve()).casefold()
        working_directory = str(labels.get("com.docker.compose.project.working_dir", "")).casefold()
        if (
            labels.get("com.docker.compose.project") != self._project_name
            or labels.get("com.docker.compose.service") != service
            or working_directory != expected_directory
        ):
            raise GateFailure("container_ownership_unverified")

    def is_healthy(self, container_id: str) -> bool:
        health = self._inspect_state(container_id).get("Health")
        return isinstance(health, Mapping) and health.get("Status") == "healthy"

    def is_running(self, container_id: str) -> bool:
        return self._inspect_state(container_id).get("Running") is True

    def is_paused(self, container_id: str) -> bool:
        return self._inspect_state(container_id).get("Paused") is True

    def _inspect_state(self, container_id: str) -> Mapping[str, Any]:
        output = self.docker("inspect", "--format", "{{json .State}}", container_id, timeout=30)
        try:
            state = json.loads(output)
        except json.JSONDecodeError as exc:
            raise GateFailure("container_state_invalid") from exc
        if not isinstance(state, dict):
            raise GateFailure("container_state_invalid")
        return state


def _parse_compose_ps(output: str) -> list[ComposeContainer]:
    stripped = output.strip()
    if not stripped:
        return []
    documents: list[object]
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        try:
            documents = [json.loads(line) for line in stripped.splitlines() if line.strip()]
        except json.JSONDecodeError as exc:
            raise GateFailure("compose_ps_contract_invalid") from exc
    else:
        documents = parsed if isinstance(parsed, list) else [parsed]
    containers: list[ComposeContainer] = []
    for document in documents:
        if not isinstance(document, Mapping):
            raise GateFailure("compose_ps_contract_invalid")
        container_id = document.get("ID")
        service = document.get("Service")
        name = document.get("Name", document.get("Names"))
        project = document.get("Project")
        if (
            not isinstance(container_id, str)
            or not container_id
            or not isinstance(service, str)
            or not service
            or not isinstance(name, str)
            or not name
            or not isinstance(project, str)
            or not project
        ):
            raise GateFailure("compose_ps_contract_invalid")
        containers.append(
            ComposeContainer(
                container_id=container_id,
                name=name,
                project=project,
                service=service,
                state=str(document.get("State", "")),
                health=str(document.get("Health", "")),
            )
        )
    return containers


def _group_by_service(
    containers: Sequence[ComposeContainer],
) -> dict[str, tuple[ComposeContainer, ...]]:
    grouped: dict[str, list[ComposeContainer]] = {}
    for container in containers:
        grouped.setdefault(container.service, []).append(container)
    return {key: tuple(value) for key, value in grouped.items()}


def _compose_service_environment(
    config: Mapping[str, object],
    service: str,
) -> Mapping[str, str]:
    services = config.get("services")
    if not isinstance(services, Mapping):
        raise GateFailure("compose_config_contract_invalid")
    service_config = services.get(service)
    if not isinstance(service_config, Mapping):
        raise GateFailure("compose_config_contract_invalid")
    environment = service_config.get("environment")
    if not isinstance(environment, Mapping):
        raise GateFailure("compose_config_contract_invalid")
    projected: dict[str, str] = {}
    for key, value in environment.items():
        if isinstance(key, str) and isinstance(value, str):
            projected[key] = value
    return projected


def _assert_local_mcp_environment(environment: Mapping[str, str]) -> None:
    for key in _MCP_URL_ENVIRONMENT_KEYS:
        raw_url = environment.get(key)
        if not isinstance(raw_url, str) or not raw_url:
            raise GateFailure("preflight_mcp_url_missing")
        try:
            url = httpx.URL(raw_url)
        except (TypeError, ValueError) as exc:
            raise GateFailure("preflight_mcp_url_invalid") from exc
        if (
            url.scheme not in {"http", "https"}
            or url.host is None
            or url.host.casefold() not in _LOCAL_MCP_HOSTS
            or url.userinfo
        ):
            raise GateFailure("preflight_mcp_url_not_local")


def _require_initial_workflow_response(
    run: Mapping[str, object],
    *,
    clarification_expected: bool,
) -> bool:
    run_status = run.get("run_status")
    business_status = run.get("status")
    if clarification_expected:
        if run_status != "interrupted" or business_status != "NEEDS_CLARIFICATION":
            raise GateFailure("clarification_workflow_did_not_interrupt")
        return True
    if run_status == "interrupted" or business_status == "NEEDS_CLARIFICATION":
        raise GateFailure("non_clarification_workflow_interrupted")
    return False


def _require_terminal_workflow_response(run: Mapping[str, object]) -> str:
    run_status = run.get("run_status")
    if run_status != "completed":
        raise GateFailure("workload_not_terminal")
    business_status = run.get("status")
    if business_status == "FAILED":
        raise GateFailure("workload_business_failed")
    if business_status not in {"SUCCESS", "PARTIAL"}:
        raise GateFailure("workload_business_status_invalid")
    return str(run_status)


def _remaining_recovery_seconds(deadline: float, failure_code: str) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise GateFailure(failure_code)
    return remaining


def _lock_file_nonblocking(handle: BinaryIO) -> None:
    if os.name == "nt":
        msvcrt: Any = __import__("msvcrt")
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return
    fcntl: Any = __import__("fcntl")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(handle: BinaryIO) -> None:
    if os.name == "nt":
        msvcrt: Any = __import__("msvcrt")
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    fcntl: Any = __import__("fcntl")
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _build_disruption_schedule(
    profile: ReplicaReleaseGateProfile,
    *,
    total_slots: int | None = None,
) -> dict[int, tuple[ReplicaReleaseDisruptionKind, ...]]:
    slot_count = total_slots or profile.workflow_target
    if slot_count <= 0:
        raise ValueError("total_slots must be positive")
    positioned: list[tuple[float, ReplicaReleaseDisruptionKind]] = []
    targets = (
        (ReplicaReleaseDisruptionKind.WORKER_ROLL, profile.worker_roll_target),
        (ReplicaReleaseDisruptionKind.AGENT_RECREATE, profile.agent_recreate_target),
        (ReplicaReleaseDisruptionKind.POSTGRES_OUTAGE, profile.postgres_outage_target),
    )
    offsets = {
        ReplicaReleaseDisruptionKind.WORKER_ROLL: 0.0,
        ReplicaReleaseDisruptionKind.AGENT_RECREATE: 0.01,
        ReplicaReleaseDisruptionKind.POSTGRES_OUTAGE: 0.02,
    }
    for kind, count in targets:
        for index in range(count):
            positioned.append(((index + 1) / (count + 1) + offsets[kind], kind))
    positioned.sort(key=lambda item: item[0])
    if len(positioned) > slot_count:
        raise ValueError("total_slots cannot be lower than the disruption count")
    available_slots = list(range(1, slot_count + 1))
    schedule: dict[int, list[ReplicaReleaseDisruptionKind]] = {}
    for position, kind in positioned:
        desired = max(1, min(slot_count, round(position * slot_count)))
        slot = min(available_slots, key=lambda candidate: (abs(candidate - desired), candidate))
        available_slots.remove(slot)
        schedule.setdefault(slot, []).append(kind)
    return {slot: tuple(kinds) for slot, kinds in schedule.items()}


def _planned_workflow_count(profile: ReplicaReleaseGateProfile) -> int:
    batch_count = math.ceil(profile.duration_seconds / profile.workload_batch_interval_seconds)
    return batch_count * profile.workload_concurrency


def _structured_clarification_answer(
    clarification: Mapping[str, object],
) -> dict[str, object]:
    clarification_id = clarification.get("id")
    answer_request = clarification.get("answer_request")
    if not isinstance(clarification_id, str) or not isinstance(answer_request, Mapping):
        raise GateFailure("clarification_contract_invalid")
    mode = answer_request.get("mode")
    if mode == "single_text":
        answer: dict[str, object] = {
            "mode": "single_text",
            "value": _CLARIFICATION_TEXT_ANSWER,
        }
    elif mode == "fields":
        inputs = answer_request.get("inputs")
        if not isinstance(inputs, list) or not inputs:
            raise GateFailure("clarification_contract_invalid")
        values: list[dict[str, object]] = []
        for item in inputs:
            if not isinstance(item, Mapping) or not isinstance(item.get("id"), str):
                raise GateFailure("clarification_contract_invalid")
            control = item.get("control")
            if control == "tri_state":
                value: dict[str, str] = {"kind": "tri_state", "value": "no"}
            elif control == "single_choice":
                options = item.get("options")
                if not isinstance(options, list) or not options:
                    raise GateFailure("clarification_contract_invalid")
                first = options[0]
                if not isinstance(first, Mapping) or not isinstance(first.get("value"), str):
                    raise GateFailure("clarification_contract_invalid")
                value = {"kind": "choice", "value": first["value"]}
            else:
                value = {"kind": "text", "value": _CLARIFICATION_TEXT_ANSWER}
            values.append({"input_id": item["id"], "value": value})
        answer = {"mode": "fields", "values": values}
    else:
        raise GateFailure("clarification_contract_invalid")
    return {
        "schema_version": "2",
        "clarification_id": clarification_id,
        "answer": answer,
    }


def _require_json_object(
    response: httpx.Response,
    *,
    expected_status: int,
) -> Mapping[str, object]:
    if response.status_code != expected_status:
        raise GateFailure("http_status_unexpected")
    try:
        payload = response.json()
    except ValueError as exc:
        raise GateFailure("http_json_invalid") from exc
    if not isinstance(payload, dict):
        raise GateFailure("http_json_invalid")
    return payload


def _require_error_response(
    response: httpx.Response,
    *,
    expected_status: int,
    expected_code: str,
) -> Mapping[str, object]:
    payload = _require_json_object(response, expected_status=expected_status)
    error = payload.get("error")
    if not isinstance(error, Mapping) or error.get("code") != expected_code:
        raise GateFailure("http_error_contract_unexpected")
    return payload


def _required_int(document: Mapping[str, object], key: str) -> int:
    value = document.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise GateFailure("database_evidence_contract_invalid")
    return value


def _last_nonempty_line(output: str) -> str:
    lines = [line for line in output.splitlines() if line.strip()]
    if not lines:
        raise GateFailure("command_output_missing")
    return lines[-1]


def _percentile(sorted_values: Sequence[float], quantile: float) -> float:
    if not sorted_values:
        raise ValueError("percentile requires values")
    index = max(0, math.ceil(len(sorted_values) * quantile) - 1)
    return sorted_values[index]


def _positive_float(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive finite number") from exc
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("must be a positive finite number")
    return value


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stable_exception_code(error: BaseException) -> str:
    if isinstance(error, httpx.TimeoutException):
        return "http_timeout"
    if isinstance(error, httpx.HTTPError):
        return "http_transport_error"
    if isinstance(error, subprocess.TimeoutExpired):
        return "subprocess_timeout"
    if isinstance(error, subprocess.SubprocessError):
        return "subprocess_error"
    if isinstance(error, ValueError):
        return "gate_contract_error"
    return "gate_internal_error"


def _write_collision_safe_report(
    directory: Path,
    started_at: datetime,
    document: Mapping[str, object],
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    timestamp = started_at.strftime("%Y%m%dT%H%M%S%fZ")
    reserved_path: Path | None = None
    for collision_index in range(10_000):
        suffix = "" if collision_index == 0 else f"-{collision_index}"
        candidate = directory / f"phase-4b-3c-{timestamp}{suffix}.json"
        try:
            candidate.touch(exist_ok=False)
        except FileExistsError:
            continue
        except OSError as exc:
            raise GateFailure("report_path_reservation_failed") from exc
        reserved_path = candidate
        break
    if reserved_path is None:
        raise GateFailure("report_path_collision_limit_exceeded")
    try:
        _atomic_write_json(reserved_path, document)
    except Exception:
        reserved_path.unlink(missing_ok=True)
        raise
    return reserved_path


def _atomic_write_json(path: Path, document: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = (
        json.dumps(
            document,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(serialized)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def _assert_report_redacted(document: Mapping[str, object]) -> None:
    forbidden_keys = {
        "id",
        "job_id",
        "run_id",
        "thread_id",
        "owner_id",
        "request_id",
        "worker_id",
        "lease_owner",
        "question",
        "answer",
        "user_message",
        "payload",
        "ciphertext",
        "token",
        "registration_token",
        "idempotency_key",
        "command_output",
        "raw_log",
    }

    def walk(value: object) -> None:
        if isinstance(value, Mapping):
            if set(value) & forbidden_keys:
                raise GateFailure("report_redaction_violation")
            for nested in value.values():
                walk(nested)
        elif isinstance(value, list | tuple):
            for nested in value:
                walk(nested)

    walk(document)


if __name__ == "__main__":
    sys.exit(main())
