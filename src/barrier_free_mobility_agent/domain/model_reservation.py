from __future__ import annotations

import hmac
import json
import re
from enum import StrEnum
from hashlib import sha256
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from barrier_free_mobility_agent.domain.usage import (
    ModelUsage,
    RunUsage,
    UsageSource,
    accumulate_rejected_model_call,
    accumulate_run_usage,
)

MODEL_RESERVATION_SCHEMA_VERSION: Final[Literal["2"]] = "2"

_KEY_DERIVATION_DOMAIN = b"barrier-free-mobility:model-reservation:key:v1\0"
_INVOCATION_FINGERPRINT_DOMAIN = b"barrier-free-mobility:model-reservation:invocation:v1\0"
_MIN_ROOT_KEY_BYTES = 16
_MAX_ROOT_KEY_BYTES = 4_096
_MAX_CANONICAL_INVOCATION_BYTES = 16 * 1024 * 1024
_FINGERPRINT_PATTERN = re.compile(r"^[a-f0-9]{64}$")

type Identifier = Annotated[
    str,
    Field(
        strict=True,
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    ),
]
type EstimatedInputTokens = Annotated[int, Field(strict=True, ge=0, le=2_000_000)]
type OutputReserveTokens = Annotated[int, Field(strict=True, gt=0, le=1_000_000)]
type LogicalCallIndex = Annotated[int, Field(strict=True, ge=1, le=2_147_483_647)]
type UsageRevision = Annotated[int, Field(strict=True, ge=0, le=9_223_372_036_854_775_807)]
type KeyedInvocationFingerprint = Annotated[
    str,
    Field(strict=True, min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$"),
]


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        allow_inf_nan=False,
    )


class ModelReservationIdentity(_StrictFrozenModel):
    """Bounded metadata committed by an open model-call reservation."""

    schema_version: Literal["2"] = MODEL_RESERVATION_SCHEMA_VERSION
    purpose: Identifier
    provider_alias: Identifier
    model_alias: Identifier
    provider_contract_version: Identifier
    prompt_version: Identifier
    context_policy_version: Identifier
    counter_version: Identifier
    estimated_input_tokens: EstimatedInputTokens
    output_reserve_tokens: OutputReserveTokens
    logical_call_index: LogicalCallIndex
    usage_revision: UsageRevision


class OpenModelReservation(ModelReservationIdentity):
    """The only reservation shape that may be serialized into graph state."""

    status: Literal["open"] = "open"
    invocation_fingerprint: KeyedInvocationFingerprint

    def identity(self) -> ModelReservationIdentity:
        return ModelReservationIdentity(
            schema_version=self.schema_version,
            purpose=self.purpose,
            provider_alias=self.provider_alias,
            model_alias=self.model_alias,
            provider_contract_version=self.provider_contract_version,
            prompt_version=self.prompt_version,
            context_policy_version=self.context_policy_version,
            counter_version=self.counter_version,
            estimated_input_tokens=self.estimated_input_tokens,
            output_reserve_tokens=self.output_reserve_tokens,
            logical_call_index=self.logical_call_index,
            usage_revision=self.usage_revision,
        )


class ModelReservationFingerprintSigner:
    """Create opaque keyed fingerprints without retaining invocation content."""

    __slots__ = ("__fingerprint_key",)

    def __init__(self, root_key: bytes) -> None:
        if type(root_key) is not bytes or not (
            _MIN_ROOT_KEY_BYTES <= len(root_key) <= _MAX_ROOT_KEY_BYTES
        ):
            raise ValueError("A bounded model reservation root key is required.")
        self.__fingerprint_key = hmac.new(
            root_key,
            _KEY_DERIVATION_DOMAIN,
            sha256,
        ).digest()

    def __repr__(self) -> str:
        return f"{type(self).__name__}(<redacted>)"

    def sign(
        self,
        identity: ModelReservationIdentity,
        canonical_invocation: bytes,
    ) -> str:
        invocation = _validated_canonical_invocation(canonical_invocation)
        identity_bytes = _canonical_identity_bytes(identity)
        payload = b"".join(
            (
                _INVOCATION_FINGERPRINT_DOMAIN,
                len(identity_bytes).to_bytes(4, "big"),
                identity_bytes,
                len(invocation).to_bytes(8, "big"),
                invocation,
            )
        )
        return hmac.new(self.__fingerprint_key, payload, sha256).hexdigest()

    def verify(
        self,
        reservation: OpenModelReservation,
        expected_identity: ModelReservationIdentity,
        canonical_invocation: bytes,
    ) -> bool:
        if _FINGERPRINT_PATTERN.fullmatch(reservation.invocation_fingerprint) is None:
            return False
        if reservation.identity() != expected_identity:
            return False
        expected = self.sign(expected_identity, canonical_invocation)
        return hmac.compare_digest(expected, reservation.invocation_fingerprint)


def open_model_reservation(
    identity: ModelReservationIdentity,
    canonical_invocation: bytes,
    *,
    signer: ModelReservationFingerprintSigner,
) -> OpenModelReservation:
    """Create the checkpoint-safe open reservation for one immutable invocation."""

    return OpenModelReservation(
        **identity.model_dump(mode="python"),
        invocation_fingerprint=signer.sign(identity, canonical_invocation),
    )


def verify_model_reservation(
    reservation: OpenModelReservation,
    expected_identity: ModelReservationIdentity,
    canonical_invocation: bytes,
    *,
    signer: ModelReservationFingerprintSigner,
) -> bool:
    """Match a rebuilt identity and invocation to the checkpointed reservation."""

    return signer.verify(reservation, expected_identity, canonical_invocation)


class ModelSettlementOutcome(StrEnum):
    SETTLED = "settled"
    FAILED_SETTLED = "failed_settled"
    UNKNOWN_CHARGED = "unknown_charged"
    REJECTED = "rejected"


class ModelSettlementResult(_StrictFrozenModel):
    """Pure terminal transition; ``None`` explicitly clears the open reservation."""

    outcome: ModelSettlementOutcome
    run_usage: RunUsage
    charged_usage: ModelUsage | None
    model_reservation: None = None

    @model_validator(mode="after")
    def validate_charge(self) -> ModelSettlementResult:
        if self.outcome == ModelSettlementOutcome.REJECTED:
            if self.charged_usage is not None:
                raise ValueError("A rejected model call cannot carry token usage.")
            if self.run_usage.revision < 1 or self.run_usage.model_calls < 1:
                raise ValueError("A rejected model call must consume one logical call.")
        elif self.charged_usage is None:
            raise ValueError("A provider-attempted model call requires charged usage.")
        else:
            if self.run_usage.revision < 1 or self.run_usage.model_calls < 1:
                raise ValueError("A provider-attempted model call must be accumulated.")
            if self.outcome == ModelSettlementOutcome.UNKNOWN_CHARGED and (
                self.charged_usage.source != UsageSource.ESTIMATED
            ):
                raise ValueError("Unknown model usage must be conservatively estimated.")
            if (
                self.run_usage.input_tokens < self.charged_usage.input_tokens
                or self.run_usage.output_tokens < self.charged_usage.output_tokens
                or self.run_usage.total_tokens < self.charged_usage.total_tokens
            ):
                raise ValueError("Charged usage must be included in the run aggregate.")
        return self


class ModelReservationSettlementError(ValueError):
    """The reservation cannot be deterministically settled against current usage."""


def settle_model_reservation(
    reservation: OpenModelReservation,
    current_usage: RunUsage,
    *,
    outcome: ModelSettlementOutcome,
    call_usage: ModelUsage | None = None,
) -> ModelSettlementResult:
    """Settle one open reservation and return a state update with no outstanding call."""

    _validate_settlement_position(reservation, current_usage)
    if outcome in {
        ModelSettlementOutcome.SETTLED,
        ModelSettlementOutcome.FAILED_SETTLED,
    }:
        if call_usage is None:
            raise ModelReservationSettlementError(
                "A settled model attempt requires normalized usage."
            )
        if (
            call_usage.source != UsageSource.REPORTED
            and call_usage.estimator_version != reservation.counter_version
        ):
            raise ModelReservationSettlementError(
                "Estimated model usage must match the reservation counter version."
            )
        charged_usage = call_usage
        run_usage = accumulate_run_usage(current_usage, charged_usage)
    elif outcome == ModelSettlementOutcome.UNKNOWN_CHARGED:
        if call_usage is not None:
            raise ModelReservationSettlementError(
                "An unknown model attempt cannot accept provider usage."
            )
        charged_usage = ModelUsage(
            input_tokens=reservation.estimated_input_tokens,
            output_tokens=reservation.output_reserve_tokens,
            total_tokens=(reservation.estimated_input_tokens + reservation.output_reserve_tokens),
            source=UsageSource.ESTIMATED,
            estimator_version=reservation.counter_version,
        )
        run_usage = accumulate_run_usage(current_usage, charged_usage)
    elif outcome == ModelSettlementOutcome.REJECTED:
        if call_usage is not None:
            raise ModelReservationSettlementError(
                "A rejected model call cannot accept provider usage."
            )
        charged_usage = None
        run_usage = accumulate_rejected_model_call(current_usage)
    else:
        raise ModelReservationSettlementError("Unsupported model settlement outcome.")

    return ModelSettlementResult(
        outcome=outcome,
        run_usage=run_usage,
        charged_usage=charged_usage,
        model_reservation=None,
    )


def _validate_settlement_position(
    reservation: OpenModelReservation,
    current_usage: RunUsage,
) -> None:
    if reservation.usage_revision != current_usage.revision:
        raise ModelReservationSettlementError("Model reservation usage revision mismatch.")
    if reservation.logical_call_index != current_usage.model_calls + 1:
        raise ModelReservationSettlementError("Model reservation logical call mismatch.")


def _validated_canonical_invocation(value: bytes) -> bytes:
    if type(value) is not bytes or not value or len(value) > _MAX_CANONICAL_INVOCATION_BYTES:
        raise ValueError("Canonical model invocation bytes are required within the hard limit.")
    return value


def _canonical_identity_bytes(identity: ModelReservationIdentity) -> bytes:
    return json.dumps(
        identity.model_dump(mode="json"),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")
