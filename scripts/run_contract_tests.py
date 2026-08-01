from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

_POLL_INTERVAL_SECONDS = 0.25
_STARTUP_TIMEOUT_SECONDS = 60.0
_PROCESS_STOP_TIMEOUT_SECONDS = 5.0
_LOG_TAIL_CHARS = 12_000


def parse_args() -> argparse.Namespace:
    project_root = Path(__file__).resolve().parents[1]
    default_mcp_project = project_root.parent / "barrier_free_mobility_mcp"
    parser = argparse.ArgumentParser(
        description="Run the live MCP contract test and Agent HTTP smoke test.",
    )
    parser.add_argument(
        "--mcp-project",
        type=Path,
        default=default_mcp_project,
        help="Path to a checked-out barrier-free-mobility-mcp project.",
    )
    parser.add_argument(
        "--mcp-python",
        type=Path,
        help="Optional Python executable for the MCP checkout (defaults to its .venv).",
    )
    parser.add_argument("--mcp-port", type=int, default=18080)
    parser.add_argument("--agent-port", type=int, default=18081)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    project_root = Path(__file__).resolve().parents[1]
    mcp_project = args.mcp_project.resolve()
    _require_project(mcp_project)
    agent_python = _venv_python(project_root)
    mcp_python = (
        args.mcp_python.resolve() if args.mcp_python is not None else _venv_python(mcp_project)
    )
    if not mcp_python.is_file():
        raise RuntimeError(f"MCP Python executable was not found at {mcp_python}.")

    mcp_url = f"http://127.0.0.1:{args.mcp_port}/mcp"
    mcp_health_url = f"http://127.0.0.1:{args.mcp_port}/health"
    agent_url = f"http://127.0.0.1:{args.agent_port}"
    mcp_environment = _mcp_environment(args.mcp_port)

    with (
        temporary_log_directory() as log_directory,
        managed_process(
            name="MCP",
            command=[str(mcp_python), "-m", "app.main"],
            cwd=mcp_project,
            environment=mcp_environment,
            log_path=log_directory / "mcp.log",
        ) as mcp_process,
    ):
        _wait_for_json(
            mcp_health_url,
            process=mcp_process,
            predicate=_is_mock_mcp_ready,
            service_name="MCP",
        )
        contract_environment = _agent_environment(
            mcp_url=mcp_url,
            mcp_health_url=mcp_health_url,
            agent_port=args.agent_port,
        )
        contract_environment.update(
            {
                "MCP_CONTRACT_URL": mcp_url,
                "MCP_CONTRACT_HEALTH_URL": mcp_health_url,
            }
        )
        subprocess.run(
            [
                str(agent_python),
                "-m",
                "pytest",
                "tests/contract",
                "-m",
                "contract",
                "-q",
            ],
            cwd=project_root,
            env=contract_environment,
            check=True,
        )
        print("CONTRACT_TEST=PASS", flush=True)

        with managed_process(
            name="Agent",
            command=[
                str(agent_python),
                "-m",
                "uvicorn",
                "barrier_free_mobility_agent.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(args.agent_port),
                "--no-access-log",
            ],
            cwd=project_root,
            environment=_agent_environment(
                mcp_url=mcp_url,
                mcp_health_url=mcp_health_url,
                agent_port=args.agent_port,
            ),
            log_path=log_directory / "agent.log",
        ) as agent_process:
            _wait_for_json(
                f"{agent_url}/health/ready",
                process=agent_process,
                predicate=lambda payload: payload.get("status") == "ready",
                service_name="Agent",
            )
            answer = _post_json(
                f"{agent_url}/v1/accessibility/questions",
                {
                    "question": (
                        "휠체어를 사용하고 계단과 에스컬레이터를 이용할 수 없어요. "
                        "홍대입구역에서 삼성역까지 갈 수 있어?"
                    )
                },
            )
            if answer.get("source") != "mcp" or not str(answer.get("user_message", "")).strip():
                raise RuntimeError("Agent smoke response did not preserve the MCP answer.")
            print("E2E_SMOKE=PASS", flush=True)
    return 0


def _require_project(project: Path) -> None:
    if not (project / "pyproject.toml").is_file():
        raise RuntimeError(f"MCP project was not found at {project}.")


def _venv_python(project: Path) -> Path:
    relative_path = (
        Path(".venv/Scripts/python.exe") if os.name == "nt" else Path(".venv/bin/python")
    )
    python = project / relative_path
    if not python.is_file():
        raise RuntimeError(
            f"Virtual environment Python was not found at {python}. Run uv sync --locked."
        )
    return python


def _mcp_environment(port: int) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "APP_MODE": "mock",
            "CACHE_BACKEND": "memory",
            "MCP_TRANSPORT": "streamable-http",
            "MCP_HOST": "127.0.0.1",
            "MCP_PORT": str(port),
            "MCP_PATH": "/mcp",
            "MCP_AUTH_MODE": "none",
            "MCP_AUTH_ENABLED": "false",
            "MCP_RATE_LIMIT_ENABLED": "false",
        }
    )
    return environment


def _agent_environment(
    *,
    mcp_url: str,
    mcp_health_url: str,
    agent_port: int,
) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "APP_ENV": "test",
            "LOG_FORMAT": "console",
            "MCP_MOBILITY_URL": mcp_url,
            "MCP_MOBILITY_HEALTH_URL": mcp_health_url,
            "MCP_MOBILITY_AUTH_MODE": "none",
            "MCP_STARTUP_REQUIRED": "true",
            "API_HOST": "127.0.0.1",
            "API_PORT": str(agent_port),
        }
    )
    return environment


def _is_mock_mcp_ready(payload: Mapping[str, Any]) -> bool:
    app = payload.get("app")
    mcp = payload.get("mcp")
    return (
        payload.get("status") == "ok"
        and isinstance(app, Mapping)
        and app.get("mode") == "mock"
        and isinstance(mcp, Mapping)
        and mcp.get("auth_enabled") is False
    )


def _wait_for_json(
    url: str,
    *,
    process: subprocess.Popen[bytes],
    predicate: Callable[[Mapping[str, Any]], bool],
    service_name: str,
) -> None:
    deadline = time.monotonic() + _STARTUP_TIMEOUT_SECONDS
    last_error = "no response"
    while time.monotonic() < deadline:
        exit_code = process.poll()
        if exit_code is not None:
            raise RuntimeError(f"{service_name} exited before readiness with code {exit_code}.")
        try:
            payload = _get_json(url)
            if predicate(payload):
                return
            last_error = f"unexpected readiness payload: {payload}"
        except (HTTPError, URLError, TimeoutError, ValueError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(_POLL_INTERVAL_SECONDS)
    raise RuntimeError(f"{service_name} did not become ready: {last_error}")


def _get_json(url: str) -> Mapping[str, Any]:
    with urlopen(url, timeout=2) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise ValueError("Expected a JSON object.")
    return payload


def _post_json(url: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urlopen(request, timeout=60) as response:
        response_payload = json.load(response)
    if not isinstance(response_payload, dict):
        raise ValueError("Expected an Agent JSON object.")
    return response_payload


@contextmanager
def managed_process(
    *,
    name: str,
    command: Sequence[str],
    cwd: Path,
    environment: Mapping[str, str],
    log_path: Path,
) -> Iterator[subprocess.Popen[bytes]]:
    creation_flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    with log_path.open("wb") as log_file:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=dict(environment),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            creationflags=creation_flags,
        )
        try:
            yield process
        except BaseException:
            _stop_process(process)
            log_file.flush()
            _print_log_tail(name, log_path)
            raise
        finally:
            _stop_process(process)


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=_PROCESS_STOP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=_PROCESS_STOP_TIMEOUT_SECONDS)


def _print_log_tail(name: str, log_path: Path) -> None:
    content = log_path.read_text(encoding="utf-8", errors="replace")
    print(f"--- {name} log tail ---", file=sys.stderr)
    print(content[-_LOG_TAIL_CHARS:], file=sys.stderr)


@contextmanager
def temporary_log_directory() -> Iterator[Path]:
    directory = Path(tempfile.mkdtemp(prefix="mobility-contract-"))
    try:
        yield directory
    finally:
        _remove_temporary_directory(directory)


def _remove_temporary_directory(directory: Path) -> None:
    for attempt in range(20):
        try:
            shutil.rmtree(directory)
            return
        except FileNotFoundError:
            return
        except PermissionError:
            if attempt == 19:
                print(
                    f"Warning: temporary logs remain at {directory}",
                    file=sys.stderr,
                )
                return
            time.sleep(_POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
