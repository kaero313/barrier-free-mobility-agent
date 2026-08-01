from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import cast

import pytest
import scripts.run_replica_release_gate as runner


class _FinishedProcess:
    def __init__(self, process_id: int = 42) -> None:
        self.pid = process_id
        self.returncode: int | None = None
        self.killed = False

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        self.returncode = 0
        return 0

    def poll(self) -> int | None:
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


class _InterruptingProcess(_FinishedProcess):
    def __init__(self) -> None:
        super().__init__()
        self.communicate_calls = 0
        self.stdout = None
        self.stderr = None

    def communicate(
        self,
        input: str | None = None,
        timeout: float | None = None,
    ) -> tuple[str, str]:
        del input, timeout
        self.communicate_calls += 1
        if self.communicate_calls == 1:
            raise KeyboardInterrupt
        return "", ""


class _HangingTaskkill:
    def __init__(self) -> None:
        self.wait_timeouts: list[float | None] = []
        self.killed = False

    def wait(self, timeout: float | None = None) -> int:
        self.wait_timeouts.append(timeout)
        if len(self.wait_timeouts) == 1:
            raise subprocess.TimeoutExpired("taskkill", 0.0 if timeout is None else timeout)
        return 0

    def kill(self) -> None:
        self.killed = True


def _as_popen(process: object) -> subprocess.Popen[str]:
    return cast("subprocess.Popen[str]", process)


def test_process_group_options_cover_windows_and_posix() -> None:
    assert runner._command_process_group_options("nt") == {
        "creationflags": runner._WINDOWS_CREATE_NEW_PROCESS_GROUP
    }
    assert runner._command_process_group_options("posix") == {"start_new_session": True}


def test_command_helper_preserves_output_and_nonzero_return_code(tmp_path: Path) -> None:
    completed = runner._run_command_with_tree_cleanup(
        [
            sys.executable,
            "-c",
            "import sys; print('out'); print('err', file=sys.stderr); raise SystemExit(7)",
        ],
        cwd=tmp_path,
        timeout=10,
    )

    assert completed.returncode == 7
    assert completed.stdout == "out\n"
    assert completed.stderr == "err\n"


def test_timeout_kills_only_the_spawned_process_tree(tmp_path: Path) -> None:
    marker = tmp_path / "escaped-child.txt"
    child_code = (
        "import pathlib,time;"
        "time.sleep(1.5);"
        f"pathlib.Path({str(marker)!r}).write_text('escaped', encoding='utf-8')"
    )
    parent_code = (
        "import subprocess,sys,time;"
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]);"
        "print('child-started', flush=True);"
        "time.sleep(30)"
    )

    with pytest.raises(subprocess.TimeoutExpired) as raised:
        runner._run_command_with_tree_cleanup(
            [sys.executable, "-c", parent_code],
            cwd=tmp_path,
            timeout=0.75,
        )

    output = raised.value.output
    if isinstance(output, bytes):
        output = output.decode("utf-8", errors="replace")
    assert output is not None
    assert "child-started" in output
    time.sleep(1.0)
    assert not marker.exists()


def test_keyboard_interrupt_cleans_the_tree_and_is_re_raised(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _InterruptingProcess()
    cleaned: list[subprocess.Popen[str]] = []
    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: _as_popen(process))
    monkeypatch.setattr(
        runner,
        "_terminate_command_process_tree",
        lambda target: cleaned.append(target),
    )

    with pytest.raises(KeyboardInterrupt):
        runner._run_command_with_tree_cleanup(
            ["docker", "compose", "build"],
            cwd=tmp_path,
            timeout=30,
        )

    assert cleaned == [_as_popen(process)]
    assert process.communicate_calls == 2


def test_posix_cleanup_signals_only_the_invocation_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FinishedProcess(process_id=1234)
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(
        os,
        "killpg",
        lambda process_group, sig: signals.append((process_group, sig)),
        raising=False,
    )
    monkeypatch.setattr(signal, "SIGKILL", 9, raising=False)

    runner._terminate_command_process_tree(_as_popen(process), platform_name="posix")

    assert signals == [(1234, signal.SIGTERM), (1234, 9)]
    assert process.killed is False


def test_windows_cleanup_targets_only_the_invocation_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FinishedProcess(process_id=5678)
    targeted_process_ids: list[int] = []
    monkeypatch.setattr(
        runner,
        "_run_windows_taskkill",
        targeted_process_ids.append,
    )

    runner._terminate_command_process_tree(_as_popen(process), platform_name="nt")

    assert targeted_process_ids == [5678]
    assert process.killed is False


def test_windows_taskkill_helper_has_a_bounded_force_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    killer = _HangingTaskkill()
    commands: list[list[str]] = []
    creation_flags: list[int] = []

    def popen(command: list[str], **kwargs: object) -> object:
        commands.append(command)
        creation_flags.append(cast("int", kwargs["creationflags"]))
        return killer

    monkeypatch.setattr(subprocess, "Popen", popen)

    runner._run_windows_taskkill(9012)

    assert commands == [["taskkill.exe", "/PID", "9012", "/T", "/F"]]
    assert creation_flags == [runner._WINDOWS_CREATE_NO_WINDOW]
    assert killer.killed is True
    assert killer.wait_timeouts == [
        runner._WINDOWS_TASKKILL_TIMEOUT_SECONDS,
        runner._WINDOWS_TASKKILL_KILL_WAIT_SECONDS,
    ]
