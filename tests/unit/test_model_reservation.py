from __future__ import annotations

import pytest
from pydantic import ValidationError

from barrier_free_mobility_agent.domain.model_reservation import (
    ModelReservationFingerprintSigner,
    ModelReservationIdentity,
    ModelReservationSettlementError,
    ModelSettlementOutcome,
    ModelSettlementResult,
    OpenModelReservation,
    open_model_reservation,
    settle_model_reservation,
    verify_model_reservation,
)
from barrier_free_mobility_agent.domain.usage import (
    ModelUsage,
    RunUsage,
    UsageSource,
    accumulate_run_usage,
)

_CANONICAL_INVOCATION = (
    b'{"messages":[{"content":"question-sentinel","role":"user"}],'
    b'"response_schema":{"type":"object"}}'
)
_ROOT_KEY = b"fake-model-reservation-root-key-v1"


def _identity(**updates: object) -> ModelReservationIdentity:
    values: dict[str, object] = {
        "purpose": "ambiguous_routing",
        "provider_alias": "fake-provider",
        "model_alias": "router-small",
        "provider_contract_version": "fake-provider-contract.v1",
        "prompt_version": "route.v1",
        "context_policy_version": "context.v1",
        "counter_version": "fake-exact-v1",
        "estimated_input_tokens": 120,
        "output_reserve_tokens": 512,
        "logical_call_index": 1,
        "usage_revision": 0,
    }
    values.update(updates)
    return ModelReservationIdentity(**values)  # type: ignore[arg-type]


def _reservation(**updates: object) -> OpenModelReservation:
    return open_model_reservation(
        _identity(**updates),
        _CANONICAL_INVOCATION,
        signer=ModelReservationFingerprintSigner(_ROOT_KEY),
    )


def _reported_usage() -> ModelUsage:
    return ModelUsage(
        input_tokens=100,
        output_tokens=20,
        total_tokens=125,
        cached_input_tokens=10,
        reasoning_output_tokens=5,
        source=UsageSource.REPORTED,
    )


def test_open_reservation_is_strict_frozen_bounded_and_checkpoint_safe() -> None:
    reservation = _reservation()

    assert reservation.schema_version == "2"
    assert reservation.status == "open"
    assert len(reservation.invocation_fingerprint) == 64
    assert "question-sentinel" not in reservation.model_dump_json()
    assert set(reservation.model_dump()) == {
        "schema_version",
        "purpose",
        "provider_alias",
        "model_alias",
        "provider_contract_version",
        "prompt_version",
        "context_policy_version",
        "counter_version",
        "estimated_input_tokens",
        "output_reserve_tokens",
        "logical_call_index",
        "usage_revision",
        "status",
        "invocation_fingerprint",
    }

    with pytest.raises(ValidationError):
        OpenModelReservation.model_validate({**reservation.model_dump(), "status": "settled"})
    with pytest.raises(ValidationError):
        OpenModelReservation.model_validate({**reservation.model_dump(), "schema_version": "1"})
    with pytest.raises(ValidationError):
        OpenModelReservation.model_validate(
            {**reservation.model_dump(), "raw_prompt": "question-sentinel"}
        )
    with pytest.raises(ValidationError):
        _identity(estimated_input_tokens=True)
    with pytest.raises(ValidationError):
        _identity(provider_alias="provider alias with spaces")
    with pytest.raises(ValidationError):
        _identity(provider_contract_version="contract version with spaces")
    with pytest.raises(ValidationError):
        _identity(output_reserve_tokens=1_000_001)
    with pytest.raises(ValidationError):
        reservation.status = "settled"  # type: ignore[assignment,misc]


def test_fingerprint_is_domain_keyed_deterministic_and_covers_identity_and_invocation() -> None:
    signer = ModelReservationFingerprintSigner(_ROOT_KEY)
    reservation = open_model_reservation(
        _identity(),
        _CANONICAL_INVOCATION,
        signer=signer,
    )

    duplicate = open_model_reservation(
        _identity(),
        _CANONICAL_INVOCATION,
        signer=signer,
    )
    changed_prompt = open_model_reservation(
        _identity(prompt_version="route.v2"),
        _CANONICAL_INVOCATION,
        signer=signer,
    )
    changed_invocation = open_model_reservation(
        _identity(),
        _CANONICAL_INVOCATION + b" ",
        signer=signer,
    )
    changed_key = open_model_reservation(
        _identity(),
        _CANONICAL_INVOCATION,
        signer=ModelReservationFingerprintSigner(b"another-fake-root-key-for-tests"),
    )

    assert reservation.invocation_fingerprint == duplicate.invocation_fingerprint
    assert reservation.invocation_fingerprint != changed_prompt.invocation_fingerprint
    assert reservation.invocation_fingerprint != changed_invocation.invocation_fingerprint
    assert reservation.invocation_fingerprint != changed_key.invocation_fingerprint
    assert verify_model_reservation(
        reservation,
        _identity(),
        _CANONICAL_INVOCATION,
        signer=signer,
    )
    assert not verify_model_reservation(
        reservation,
        _identity(),
        _CANONICAL_INVOCATION + b" ",
        signer=signer,
    )
    assert not verify_model_reservation(
        reservation,
        _identity(counter_version="different-counter-v1"),
        _CANONICAL_INVOCATION,
        signer=signer,
    )
    assert "fake-model-reservation-root-key-v1" not in repr(signer)
    assert "question-sentinel" not in repr(signer)


def test_tampered_reservation_metadata_does_not_verify() -> None:
    signer = ModelReservationFingerprintSigner(_ROOT_KEY)
    reservation = _reservation()
    tampered = reservation.model_copy(update={"estimated_input_tokens": 121})

    assert not verify_model_reservation(
        tampered,
        _identity(),
        _CANONICAL_INVOCATION,
        signer=signer,
    )


@pytest.mark.parametrize(
    "identity_update",
    [
        {"purpose": "other-purpose"},
        {"provider_alias": "other-provider"},
        {"model_alias": "other-model"},
        {"provider_contract_version": "fake-provider-contract.v2"},
        {"prompt_version": "route.v2"},
        {"context_policy_version": "context.v2"},
        {"counter_version": "other-counter-v1"},
        {"estimated_input_tokens": 121},
        {"output_reserve_tokens": 511},
        {"logical_call_index": 2},
        {"usage_revision": 1},
    ],
)
def test_verifier_rejects_every_rebuilt_identity_drift(
    identity_update: dict[str, object],
) -> None:
    signer = ModelReservationFingerprintSigner(_ROOT_KEY)
    reservation = _reservation()

    assert not verify_model_reservation(
        reservation,
        _identity(**identity_update),
        _CANONICAL_INVOCATION,
        signer=signer,
    )


def test_checkpoint_json_round_trip_preserves_verifiable_reservation() -> None:
    signer = ModelReservationFingerprintSigner(_ROOT_KEY)
    restored = OpenModelReservation.model_validate_json(_reservation().model_dump_json())

    assert verify_model_reservation(
        restored,
        _identity(),
        _CANONICAL_INVOCATION,
        signer=signer,
    )


@pytest.mark.parametrize(
    "outcome",
    [
        ModelSettlementOutcome.SETTLED,
        ModelSettlementOutcome.FAILED_SETTLED,
    ],
)
def test_trustworthy_usage_settles_success_and_failed_attempts(
    outcome: ModelSettlementOutcome,
) -> None:
    call_usage = _reported_usage()

    result = settle_model_reservation(
        _reservation(),
        RunUsage(),
        outcome=outcome,
        call_usage=call_usage,
    )

    assert result.outcome == outcome
    assert result.charged_usage == call_usage
    assert result.model_reservation is None
    assert result.run_usage == accumulate_run_usage(RunUsage(), call_usage)


@pytest.mark.parametrize("source", [UsageSource.ESTIMATED, UsageSource.MIXED])
def test_matching_estimator_usage_can_settle(source: UsageSource) -> None:
    usage = ModelUsage(
        input_tokens=100,
        output_tokens=20,
        total_tokens=120,
        source=source,
        estimator_version="fake-exact-v1",
    )

    result = settle_model_reservation(
        _reservation(),
        RunUsage(),
        outcome=ModelSettlementOutcome.SETTLED,
        call_usage=usage,
    )

    assert result.charged_usage == usage


@pytest.mark.parametrize("source", [UsageSource.ESTIMATED, UsageSource.MIXED])
def test_settlement_rejects_estimated_usage_from_a_different_counter(
    source: UsageSource,
) -> None:
    usage = ModelUsage(
        input_tokens=100,
        output_tokens=20,
        total_tokens=120,
        source=source,
        estimator_version="different-counter-v1",
    )

    with pytest.raises(ModelReservationSettlementError, match="counter version"):
        settle_model_reservation(
            _reservation(),
            RunUsage(),
            outcome=ModelSettlementOutcome.SETTLED,
            call_usage=usage,
        )


def test_unknown_attempt_charges_estimated_input_and_full_output_reserve() -> None:
    result = settle_model_reservation(
        _reservation(),
        RunUsage(),
        outcome=ModelSettlementOutcome.UNKNOWN_CHARGED,
    )

    assert result.model_reservation is None
    assert result.charged_usage == ModelUsage(
        input_tokens=120,
        output_tokens=512,
        total_tokens=632,
        source=UsageSource.ESTIMATED,
        estimator_version="fake-exact-v1",
    )
    assert result.run_usage == RunUsage(
        revision=1,
        model_calls=1,
        input_tokens=120,
        output_tokens=512,
        total_tokens=632,
        source=UsageSource.ESTIMATED,
    )


def test_rejected_attempt_consumes_logical_call_and_revision_without_tokens() -> None:
    first = accumulate_run_usage(RunUsage(), _reported_usage())
    reservation = _reservation(logical_call_index=2, usage_revision=1)

    result = settle_model_reservation(
        reservation,
        first,
        outcome=ModelSettlementOutcome.REJECTED,
    )

    assert result.charged_usage is None
    assert result.model_reservation is None
    assert result.run_usage == first.model_copy(update={"revision": 2, "model_calls": 2})


def test_settlement_rejects_stale_duplicate_or_inconsistent_inputs() -> None:
    reservation = _reservation()

    with pytest.raises(ModelReservationSettlementError, match="usage revision"):
        settle_model_reservation(
            reservation,
            RunUsage(revision=1),
            outcome=ModelSettlementOutcome.REJECTED,
        )
    with pytest.raises(ModelReservationSettlementError, match="logical call"):
        settle_model_reservation(
            _reservation(logical_call_index=2),
            RunUsage(),
            outcome=ModelSettlementOutcome.REJECTED,
        )
    with pytest.raises(ModelReservationSettlementError, match="requires normalized usage"):
        settle_model_reservation(
            reservation,
            RunUsage(),
            outcome=ModelSettlementOutcome.SETTLED,
        )
    with pytest.raises(ModelReservationSettlementError, match="cannot accept provider usage"):
        settle_model_reservation(
            reservation,
            RunUsage(),
            outcome=ModelSettlementOutcome.UNKNOWN_CHARGED,
            call_usage=_reported_usage(),
        )
    with pytest.raises(ModelReservationSettlementError, match="cannot accept provider usage"):
        settle_model_reservation(
            reservation,
            RunUsage(),
            outcome=ModelSettlementOutcome.REJECTED,
            call_usage=_reported_usage(),
        )


def test_settlement_result_forbids_terminal_reservation_or_inconsistent_charge() -> None:
    usage = accumulate_run_usage(RunUsage(), _reported_usage())

    with pytest.raises(ValidationError):
        ModelSettlementResult.model_validate(
            {
                "outcome": ModelSettlementOutcome.SETTLED,
                "run_usage": usage,
                "charged_usage": _reported_usage(),
                "model_reservation": _reservation().model_dump(),
            }
        )
    with pytest.raises(ValidationError, match="cannot carry token usage"):
        ModelSettlementResult(
            outcome=ModelSettlementOutcome.REJECTED,
            run_usage=usage,
            charged_usage=_reported_usage(),
        )
    with pytest.raises(ValidationError, match="consume one logical call"):
        ModelSettlementResult(
            outcome=ModelSettlementOutcome.REJECTED,
            run_usage=RunUsage(),
            charged_usage=None,
        )
    with pytest.raises(ValidationError, match="conservatively estimated"):
        ModelSettlementResult(
            outcome=ModelSettlementOutcome.UNKNOWN_CHARGED,
            run_usage=usage,
            charged_usage=_reported_usage(),
        )
    with pytest.raises(ValidationError, match="included in the run aggregate"):
        ModelSettlementResult(
            outcome=ModelSettlementOutcome.SETTLED,
            run_usage=RunUsage(revision=1, model_calls=1),
            charged_usage=_reported_usage(),
        )


@pytest.mark.parametrize("root_key", [b"", b"too-short", "not-bytes"])
def test_signer_rejects_invalid_keys_without_leaking_values(root_key: object) -> None:
    with pytest.raises(ValueError, match="root key") as captured:
        ModelReservationFingerprintSigner(root_key)  # type: ignore[arg-type]

    assert repr(root_key) not in str(captured.value)


@pytest.mark.parametrize(
    "canonical",
    [b"", b"x" * (16 * 1024 * 1024 + 1), "not-bytes"],
    ids=("empty", "over-limit", "wrong-type"),
)
def test_signer_rejects_invalid_canonical_input_without_leaking_it(canonical: object) -> None:
    signer = ModelReservationFingerprintSigner(_ROOT_KEY)

    with pytest.raises(ValueError, match="hard limit") as captured:
        signer.sign(_identity(), canonical)  # type: ignore[arg-type]

    assert "question-sentinel" not in str(captured.value)
