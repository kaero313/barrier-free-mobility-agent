from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from typing import Final

from Crypto.Cipher import AES
from Crypto.Random import get_random_bytes
from pydantic import TypeAdapter, ValidationError

from barrier_free_mobility_agent.domain.errors import StateInputBudgetExceededError
from barrier_free_mobility_agent.domain.execution_queue import (
    ExecutionOperation,
    ExecutionPayload,
    validate_execution_payload_size,
)
from barrier_free_mobility_agent.domain.run_ledger import RunExecutionCorrelation

_KEY_DERIVATION_DOMAIN: Final = b"barrier-free-mobility:execution-payload:aes-gcm:v1\0"
_KEY_FINGERPRINT_DOMAIN: Final = b"barrier-free-mobility:execution-payload:key-id:v1\0"
_AAD_DOMAIN: Final = "barrier-free-mobility:execution-payload:v1"
_NONCE_BYTES: Final = 12
_TAG_BYTES: Final = 16
_PAYLOAD_ADAPTER: TypeAdapter[ExecutionPayload] = TypeAdapter(ExecutionPayload)


class ExecutionPayloadCipherError(Exception):
    """Safe, non-diagnostic signal for unavailable or invalid private payload data."""


@dataclass(frozen=True, slots=True)
class EncryptedExecutionPayload:
    ciphertext: bytes
    nonce: bytes
    key_fingerprint: str
    encryption_version: int = 1

    def __post_init__(self) -> None:
        if self.encryption_version != 1:
            raise ExecutionPayloadCipherError("unsupported execution payload encryption version")
        if len(self.nonce) != _NONCE_BYTES:
            raise ExecutionPayloadCipherError("invalid execution payload nonce")
        if len(self.ciphertext) <= _TAG_BYTES:
            raise ExecutionPayloadCipherError("invalid execution payload ciphertext")
        if len(self.key_fingerprint) != 64 or any(
            character not in "0123456789abcdef" for character in self.key_fingerprint
        ):
            raise ExecutionPayloadCipherError("invalid execution payload key fingerprint")


class ExecutionPayloadCipher:
    """AES-256-GCM keyring for durable private execution inputs.

    The first key is active for writes. Previous keys are decrypt-only so rotation can
    drain already queued jobs without silently accepting an unknown database key.
    """

    def __init__(self, active_key: bytes, *, previous_keys: tuple[bytes, ...] = ()) -> None:
        keys = (active_key, *previous_keys)
        if any(len(key) != 32 for key in keys):
            raise ValueError("execution payload encryption keys must contain exactly 32 bytes")
        derived = tuple(_derive_key(key) for key in keys)
        fingerprints = tuple(_key_fingerprint(key) for key in derived)
        if len(set(fingerprints)) != len(fingerprints):
            raise ValueError("execution payload encryption keys must be distinct")
        self._active_fingerprint = fingerprints[0]
        self._keys = dict(zip(fingerprints, derived, strict=True))

    @property
    def active_fingerprint(self) -> str:
        return self._active_fingerprint

    @property
    def known_fingerprints(self) -> frozenset[str]:
        return frozenset(self._keys)

    def encrypt(
        self,
        payload: ExecutionPayload,
        *,
        associated_data: bytes,
    ) -> EncryptedExecutionPayload:
        validate_execution_payload_size(payload)
        plaintext = payload.model_dump_json().encode("utf-8")
        key = self._keys[self._active_fingerprint]
        cipher = AES.new(
            key,
            AES.MODE_GCM,
            nonce=get_random_bytes(_NONCE_BYTES),
            mac_len=_TAG_BYTES,
        )
        cipher.update(associated_data)
        ciphertext, tag = cipher.encrypt_and_digest(plaintext)
        return EncryptedExecutionPayload(
            ciphertext=ciphertext + tag,
            nonce=bytes(cipher.nonce),
            key_fingerprint=self._active_fingerprint,
        )

    def decrypt(
        self,
        envelope: EncryptedExecutionPayload,
        *,
        associated_data: bytes,
    ) -> ExecutionPayload:
        key = self._keys.get(envelope.key_fingerprint)
        if key is None:
            raise ExecutionPayloadCipherError("execution payload key is unavailable")
        ciphertext = envelope.ciphertext[:-_TAG_BYTES]
        tag = envelope.ciphertext[-_TAG_BYTES:]
        try:
            cipher = AES.new(key, AES.MODE_GCM, nonce=envelope.nonce, mac_len=_TAG_BYTES)
            cipher.update(associated_data)
            plaintext = cipher.decrypt_and_verify(ciphertext, tag)
            payload = _PAYLOAD_ADAPTER.validate_json(plaintext)
            validate_execution_payload_size(payload)
        except (
            StateInputBudgetExceededError,
            UnicodeDecodeError,
            ValidationError,
            ValueError,
        ):
            raise ExecutionPayloadCipherError("execution payload authentication failed") from None
        return payload


def execution_payload_associated_data(
    *,
    job_id: str,
    owner_id: str,
    correlation: RunExecutionCorrelation,
    operation: ExecutionOperation,
    payload_schema_version: int,
) -> bytes:
    """Canonical non-secret metadata that prevents ciphertext relocation between jobs."""

    return json.dumps(
        {
            "attempt": correlation.attempt,
            "domain": _AAD_DOMAIN,
            "job_id": job_id,
            "operation": operation.value,
            "owner_id": owner_id,
            "payload_schema_version": payload_schema_version,
            "request_id": correlation.request_id,
            "run_id": correlation.run_id,
            "thread_id": correlation.thread_id,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _derive_key(master_key: bytes) -> bytes:
    return sha256(_KEY_DERIVATION_DOMAIN + master_key).digest()


def _key_fingerprint(derived_key: bytes) -> str:
    return sha256(_KEY_FINGERPRINT_DOMAIN + derived_key).hexdigest()
