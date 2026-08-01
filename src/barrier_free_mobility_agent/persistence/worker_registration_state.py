from __future__ import annotations

import json
import os
import re
import socket
import stat
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_AUTO_WORKER_ID = "auto"
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")
_STATE_FILE_NAME = "mobility-agent-worker-registration.json"
_STATE_SCHEMA_VERSION = 1
_STATE_KEYS = frozenset({"schema_version", "worker_id", "token", "generation"})
_MAX_STATE_BYTES = 4096


@dataclass(frozen=True, slots=True)
class WorkerRegistrationState:
    """Process-local proof bound to one fenced database registration."""

    worker_id: str
    token: str
    generation: int

    def __post_init__(self) -> None:
        _validate_identifier(self.worker_id, field="worker_id")
        _validate_identifier(self.token, field="token")
        if self.generation < 1:
            raise ValueError("generation must be positive")


def resolve_execution_worker_id(
    configured_id: str,
    *,
    hostname: str | None = None,
) -> str:
    """Resolve ``auto`` to a replica-local hostname while preserving explicit IDs."""

    selected = configured_id.strip()
    if selected.lower() != _AUTO_WORKER_ID:
        _validate_identifier(selected, field="worker_id")
        return selected
    replica_hostname = (hostname if hostname is not None else socket.gethostname()).strip()
    if not replica_hostname:
        raise ValueError("hostname is required when execution worker ID is auto")
    resolved = f"worker-{replica_hostname}"
    _validate_identifier(resolved, field="auto worker_id")
    return resolved


def worker_registration_state_path() -> Path:
    configured = os.getenv("EXECUTION_WORKER_STATE_FILE")
    if configured is not None and configured.strip():
        return Path(configured.strip())
    return Path(tempfile.gettempdir()) / _STATE_FILE_NAME


def publish_worker_registration_state(
    state: WorkerRegistrationState,
    *,
    path: Path | None = None,
) -> None:
    """Atomically publish a mode-0600 health proof in the process temp directory."""

    target = path or worker_registration_state_path()
    parent = target.parent
    payload = json.dumps(
        {
            "schema_version": _STATE_SCHEMA_VERSION,
            "worker_id": state.worker_id,
            "token": state.token,
            "generation": state.generation,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=parent,
        prefix=f".{target.name}.",
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        with suppress(FileNotFoundError):
            temporary.unlink()


def read_worker_registration_state(
    *,
    path: Path | None = None,
) -> WorkerRegistrationState | None:
    """Read a closed, regular, owner-only state file without following symlinks."""

    target = path or worker_registration_state_path()
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(target, flags)
    except OSError:
        return None
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or (
            os.name != "nt" and stat.S_IMODE(details.st_mode) != 0o600
        ):
            return None
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            raw = stream.read(_MAX_STATE_BYTES + 1)
    except OSError:
        return None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(raw) > _MAX_STATE_BYTES:
        return None
    try:
        payload: Any = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict) or frozenset(payload) != _STATE_KEYS:
        return None
    if payload.get("schema_version") != _STATE_SCHEMA_VERSION:
        return None
    worker_id = payload.get("worker_id")
    token = payload.get("token")
    generation = payload.get("generation")
    if (
        not isinstance(worker_id, str)
        or not isinstance(token, str)
        or not isinstance(generation, int)
        or isinstance(generation, bool)
    ):
        return None
    try:
        return WorkerRegistrationState(
            worker_id=worker_id,
            token=token,
            generation=generation,
        )
    except ValueError:
        return None


def remove_worker_registration_state(
    *,
    expected: WorkerRegistrationState,
    path: Path | None = None,
) -> None:
    """Remove only the proof still owned by the stopping worker generation."""

    target = path or worker_registration_state_path()
    if read_worker_registration_state(path=target) != expected:
        return
    with suppress(FileNotFoundError):
        target.unlink()


def _validate_identifier(value: str, *, field: str) -> None:
    if _IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field} must match {_IDENTIFIER_PATTERN.pattern}")
