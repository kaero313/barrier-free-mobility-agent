from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from barrier_free_mobility_agent.persistence.worker_registration_state import (
    WorkerRegistrationState,
    publish_worker_registration_state,
    read_worker_registration_state,
    remove_worker_registration_state,
    resolve_execution_worker_id,
)


def test_worker_id_resolver_preserves_explicit_id_and_uses_hostname_for_auto() -> None:
    assert resolve_execution_worker_id("worker-explicit", hostname="ignored") == ("worker-explicit")
    assert resolve_execution_worker_id("auto", hostname="container-a") == ("worker-container-a")
    assert resolve_execution_worker_id("AUTO", hostname="container-b") == ("worker-container-b")


@pytest.mark.parametrize("hostname", ["", "bad host", "x" * 201])
def test_auto_worker_id_rejects_unusable_hostname(hostname: str) -> None:
    with pytest.raises(ValueError, match=r"hostname|worker_id"):
        resolve_execution_worker_id("auto", hostname=hostname)


def test_registration_state_is_atomic_owner_only_and_generation_scoped(
    tmp_path: Path,
) -> None:
    path = tmp_path / "registration.json"
    first = WorkerRegistrationState(
        worker_id="worker-container-a",
        token="token-one",
        generation=1,
    )
    replacement = WorkerRegistrationState(
        worker_id="worker-container-a",
        token="token-two",
        generation=2,
    )

    publish_worker_registration_state(first, path=path)
    assert read_worker_registration_state(path=path) == first
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    publish_worker_registration_state(replacement, path=path)
    remove_worker_registration_state(expected=first, path=path)
    assert read_worker_registration_state(path=path) == replacement

    remove_worker_registration_state(expected=replacement, path=path)
    assert path.exists() is False


@pytest.mark.skipif(os.name == "nt", reason="Windows does not expose POSIX owner-only modes")
def test_registration_state_rejects_group_readable_or_open_payload(
    tmp_path: Path,
) -> None:
    path = tmp_path / "registration.json"
    path.write_text('{"worker_id":"worker-a"}', encoding="utf-8")
    path.chmod(0o644)

    assert read_worker_registration_state(path=path) is None
