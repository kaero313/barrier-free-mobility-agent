from __future__ import annotations

import pytest

from barrier_free_mobility_agent.domain.execution_queue import (
    ExecutionOperation,
    StartExecutionPayload,
)
from barrier_free_mobility_agent.domain.run_ledger import RunExecutionCorrelation
from barrier_free_mobility_agent.persistence.execution_payload_cipher import (
    ExecutionPayloadCipher,
    ExecutionPayloadCipherError,
    execution_payload_associated_data,
)


def _associated_data(*, job_id: str = "job-1") -> bytes:
    return execution_payload_associated_data(
        job_id=job_id,
        owner_id="owner-1",
        correlation=RunExecutionCorrelation(
            thread_id="thread-1",
            run_id="run-1",
            request_id="request-1",
            attempt=1,
        ),
        operation=ExecutionOperation.START,
        payload_schema_version=1,
    )


def test_execution_payload_cipher_authenticates_payload_and_row_binding() -> None:
    cipher = ExecutionPayloadCipher(b"a" * 32)
    sentinel = "PRIVATE_QUESTION_SENTINEL"
    payload = StartExecutionPayload(question=sentinel)

    encrypted = cipher.encrypt(payload, associated_data=_associated_data())

    assert sentinel.encode() not in encrypted.ciphertext
    assert cipher.decrypt(encrypted, associated_data=_associated_data()) == payload
    with pytest.raises(ExecutionPayloadCipherError) as error:
        cipher.decrypt(encrypted, associated_data=_associated_data(job_id="job-2"))
    assert sentinel not in str(error.value)
    assert sentinel not in repr(error.value)


def test_execution_payload_cipher_rotation_requires_the_previous_key() -> None:
    previous_key = b"a" * 32
    active_key = b"b" * 32
    previous_cipher = ExecutionPayloadCipher(previous_key)
    encrypted = previous_cipher.encrypt(
        StartExecutionPayload(question="private"),
        associated_data=_associated_data(),
    )

    rotated = ExecutionPayloadCipher(active_key, previous_keys=(previous_key,))
    decrypted = rotated.decrypt(encrypted, associated_data=_associated_data())
    assert isinstance(decrypted, StartExecutionPayload)
    assert decrypted.question == "private"
    assert rotated.active_fingerprint != encrypted.key_fingerprint

    without_previous = ExecutionPayloadCipher(active_key)
    with pytest.raises(ExecutionPayloadCipherError, match="unavailable"):
        without_previous.decrypt(encrypted, associated_data=_associated_data())


def test_execution_payload_cipher_requires_distinct_256_bit_keys() -> None:
    with pytest.raises(ValueError, match="exactly 32"):
        ExecutionPayloadCipher(b"short")
    with pytest.raises(ValueError, match="distinct"):
        ExecutionPayloadCipher(b"a" * 32, previous_keys=(b"a" * 32,))
