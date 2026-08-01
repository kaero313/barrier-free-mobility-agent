from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from barrier_free_mobility_agent.application.model_boundary import (
    ModelFailure,
    ModelFailureKind,
    ModelInvocation,
    ModelInvocationResult,
    ModelResult,
    ModelResultIdentityError,
    TokenCount,
    TokenCountKind,
    validate_result_identity,
)
from barrier_free_mobility_agent.application.model_routing import project_structured_intent
from barrier_free_mobility_agent.domain.model_routing import StructuredIntentDecision
from barrier_free_mobility_agent.domain.usage import UsageSource
from barrier_free_mobility_agent.integrations.models.contracts import HostedModelContract
from evals.model_evaluation import CorpusBundle, ProductionEvaluationHarness

_KNOWN_CANDIDATE_HASHES = {
    "openai-gpt-5.4-nano-routing.v1": (
        "acb378198d6b41e1465210064e2467f8dca8ef2bdaf665bda652e75579d32585"
    ),
}


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class HostedCandidateManifest(_StrictFrozenModel):
    schema_version: Literal["1"] = "1"
    status: Literal["pending_live_evaluation", "evaluated"]
    candidate_version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
    contract_version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
    contract_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    corpus_version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
    corpus_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    provider_model_snapshot: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
    pricing_version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
    pricing_source: str = Field(pattern=r"^https://developers\.openai\.com/")
    input_usd_nanos_per_token: int = Field(gt=0, le=1_000_000)
    output_usd_nanos_per_token: int = Field(gt=0, le=1_000_000)
    maximum_input_tokens_per_call: int = Field(gt=0, le=2_000_000)
    output_reserve_tokens: int = Field(gt=0, le=1_000_000)
    required_repetitions: int = Field(ge=2, le=10)
    minimum_exact_accuracy_basis_points: int = Field(ge=0, le=10_000)
    maximum_latency_p95_ms: int = Field(gt=0, le=600_000)
    maximum_mean_cost_usd_nanos: int = Field(gt=0, le=1_000_000_000)

    @model_validator(mode="after")
    def validate_candidate_limits(self) -> HostedCandidateManifest:
        if self.output_reserve_tokens >= self.maximum_input_tokens_per_call:
            raise ValueError("candidate output reserve must be below the input call cap")
        return self

    def canonical_sha256(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
        return hashlib.sha256(payload).hexdigest()


class HostedEvaluationError(RuntimeError):
    """A report-safe live evaluation error with no prompt or provider payload."""

    def __init__(self, safe_error_code: str) -> None:
        self.safe_error_code = safe_error_code
        super().__init__(safe_error_code)


class HostedEvaluationModel(Protocol):
    @property
    def version(self) -> str: ...

    async def count_input(
        self,
        invocation: ModelInvocation[StructuredIntentDecision],
    ) -> TokenCount: ...

    async def invoke_structured(
        self,
        invocation: ModelInvocation[StructuredIntentDecision],
        *,
        preflight: TokenCount | None = None,
    ) -> ModelInvocationResult[StructuredIntentDecision]: ...


class HostedEvaluationRun(_StrictFrozenModel):
    schema_version: Literal["1"] = "1"
    repetition: int = Field(ge=1)
    admitted_calls: int = Field(ge=0)
    provider_requests: int = Field(ge=0)
    count_failures: int = Field(ge=0)
    structured_successes: int = Field(ge=0)
    structured_failures: int = Field(ge=0)
    complete_usage_calls: int = Field(ge=0)
    exact_matches: int = Field(ge=0)
    exact_accuracy_basis_points: int = Field(ge=0, le=10_000)
    release_blocker_failures: int = Field(ge=0)
    undercount_cases: int = Field(ge=0)
    maximum_undercount_tokens: int = Field(ge=0)
    output_cap_mismatches: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_cost_usd_nanos: int = Field(ge=0)
    mean_cost_usd_nanos: int = Field(ge=0)
    latency_p50_ms: int = Field(ge=0)
    latency_p95_ms: int = Field(ge=0)
    gates: dict[str, bool]
    passed: bool


class HostedEvaluationReport(_StrictFrozenModel):
    schema_version: Literal["1"] = "1"
    candidate_version: str
    candidate_status: Literal["pending_live_evaluation", "evaluated"]
    contract_version: str
    contract_sha256: str
    corpus_version: str
    corpus_sha256: str
    pricing_version: str
    runs: tuple[HostedEvaluationRun, ...]
    provider_requests: int = Field(ge=0)
    total_cost_usd_nanos: int = Field(ge=0)
    passed: bool


def load_hosted_candidate(path: Path) -> HostedCandidateManifest:
    try:
        candidate = HostedCandidateManifest.model_validate_json(path.read_bytes())
    except (OSError, ValidationError, ValueError):
        raise HostedEvaluationError("HOSTED_CANDIDATE_INVALID") from None
    expected_hash = _KNOWN_CANDIDATE_HASHES.get(candidate.candidate_version)
    if expected_hash is None or candidate.canonical_sha256() != expected_hash:
        raise HostedEvaluationError("HOSTED_CANDIDATE_HASH_MISMATCH")
    return candidate


def validate_candidate_binding(
    candidate: HostedCandidateManifest,
    contract: HostedModelContract,
    bundle: CorpusBundle,
) -> None:
    if (
        candidate.contract_version != contract.contract_version
        or candidate.contract_sha256 != contract.canonical_sha256()
        or candidate.provider_model_snapshot != contract.provider_model_snapshot
        or candidate.pricing_version != contract.pricing_version
        or candidate.corpus_version != bundle.manifest.corpus_version
        or candidate.corpus_sha256 != bundle.manifest.sha256
        or candidate.output_reserve_tokens < contract.minimum_application_output_tokens
        or candidate.output_reserve_tokens > contract.hard_max_output_tokens
    ):
        raise HostedEvaluationError("HOSTED_CANDIDATE_BINDING_MISMATCH")


def required_live_limits(
    candidate: HostedCandidateManifest,
    *,
    admitted_calls: int,
) -> tuple[int, int]:
    total_calls = admitted_calls * candidate.required_repetitions
    provider_requests = total_calls * 2
    worst_call_cost = (
        candidate.maximum_input_tokens_per_call * candidate.input_usd_nanos_per_token
        + candidate.output_reserve_tokens * candidate.output_usd_nanos_per_token
    )
    return provider_requests, total_calls * worst_call_cost


async def evaluate_hosted_candidate(
    bundle: CorpusBundle,
    model: HostedEvaluationModel,
    *,
    harness: ProductionEvaluationHarness,
    candidate: HostedCandidateManifest,
    contract: HostedModelContract,
    maximum_provider_requests: int,
    maximum_spend_usd_nanos: int,
) -> HostedEvaluationReport:
    validate_candidate_binding(candidate, contract, bundle)
    admitted_calls = sum(case.admission_expected for case in bundle.cases)
    required_requests, required_spend = required_live_limits(
        candidate,
        admitted_calls=admitted_calls,
    )
    if maximum_provider_requests < required_requests:
        raise HostedEvaluationError("HOSTED_REQUEST_LIMIT_TOO_SMALL")
    if maximum_spend_usd_nanos < required_spend:
        raise HostedEvaluationError("HOSTED_SPEND_LIMIT_TOO_SMALL")
    if model.version != contract.counter_version:
        raise HostedEvaluationError("HOSTED_COUNTER_VERSION_MISMATCH")

    runs: list[HostedEvaluationRun] = []
    provider_requests = 0
    total_cost = 0
    for repetition in range(1, candidate.required_repetitions + 1):
        run = await _evaluate_once(
            bundle,
            model,
            harness=harness,
            candidate=candidate,
            repetition=repetition,
            remaining_provider_requests=maximum_provider_requests - provider_requests,
        )
        provider_requests += run.provider_requests
        total_cost += run.total_cost_usd_nanos
        if provider_requests > maximum_provider_requests:
            raise HostedEvaluationError("HOSTED_REQUEST_LIMIT_EXCEEDED")
        if total_cost > maximum_spend_usd_nanos:
            raise HostedEvaluationError("HOSTED_SPEND_LIMIT_EXCEEDED")
        runs.append(run)

    return HostedEvaluationReport(
        candidate_version=candidate.candidate_version,
        candidate_status=candidate.status,
        contract_version=candidate.contract_version,
        contract_sha256=candidate.contract_sha256,
        corpus_version=candidate.corpus_version,
        corpus_sha256=candidate.corpus_sha256,
        pricing_version=candidate.pricing_version,
        runs=tuple(runs),
        provider_requests=provider_requests,
        total_cost_usd_nanos=total_cost,
        passed=len(runs) == candidate.required_repetitions and all(run.passed for run in runs),
    )


async def _evaluate_once(
    bundle: CorpusBundle,
    model: HostedEvaluationModel,
    *,
    harness: ProductionEvaluationHarness,
    candidate: HostedCandidateManifest,
    repetition: int,
    remaining_provider_requests: int,
) -> HostedEvaluationRun:
    admitted = 0
    requests = 0
    count_failures = 0
    successes = 0
    failures = 0
    complete_usage_calls = 0
    matches = 0
    blocker_failures = 0
    undercount_cases = 0
    max_undercount = 0
    cap_mismatches = 0
    input_tokens = 0
    output_tokens = 0
    total_cost = 0
    latencies_ms: list[int] = []

    for case in bundle.cases:
        prepared = harness.prepare(case)
        if not prepared.admission.allowed:
            continue
        admitted += 1
        if requests + 2 > remaining_provider_requests:
            raise HostedEvaluationError("HOSTED_REQUEST_LIMIT_EXCEEDED")
        invocation = prepared.assembly.invocation
        try:
            requests += 1
            count = await model.count_input(invocation)
            if (
                count.kind != TokenCountKind.EXACT
                or count.input_tokens > candidate.maximum_input_tokens_per_call
            ):
                raise HostedEvaluationError("HOSTED_COUNT_NOT_ADMISSIBLE")
        except Exception:
            count_failures += 1
            failures += 1
            if case.release_blocker:
                blocker_failures += 1
            continue

        started = time.perf_counter_ns()
        requests += 1
        try:
            result = await model.invoke_structured(invocation, preflight=count)
        except Exception:
            result = ModelFailure(
                kind=ModelFailureKind.PROVIDER_ERROR,
                safe_error_code="HOSTED_MODEL_UNEXPECTED",
            )
        latency_ms = math.ceil((time.perf_counter_ns() - started) / 1_000_000)
        latencies_ms.append(latency_ms)
        try:
            result = validate_result_identity(invocation, result)
        except ModelResultIdentityError:
            result = ModelFailure(
                kind=ModelFailureKind.PROVIDER_ERROR,
                usage=result.usage if isinstance(result, ModelResult) else None,
                safe_error_code="HOSTED_MODEL_IDENTITY_MISMATCH",
            )
        usage = result.usage if isinstance(result, (ModelFailure, ModelResult)) else None
        charged_input = usage.input_tokens if usage is not None else count.input_tokens
        charged_output = usage.output_tokens if usage is not None else invocation.max_output_tokens
        input_tokens += charged_input
        output_tokens += charged_output
        total_cost += (
            charged_input * candidate.input_usd_nanos_per_token
            + charged_output * candidate.output_usd_nanos_per_token
        )
        if usage is not None and usage.source == UsageSource.REPORTED:
            complete_usage_calls += 1
            undercount = max(0, usage.input_tokens - count.input_tokens)
            if undercount:
                undercount_cases += 1
                max_undercount = max(max_undercount, undercount)

        if isinstance(result, ModelFailure):
            failures += 1
            if result.safe_error_code == "OPENAI_OUTPUT_CAP_MISMATCH":
                cap_mismatches += 1
            if case.release_blocker:
                blocker_failures += 1
            continue

        successes += 1
        projected = project_structured_intent(result.value, prepared.assembly.envelope)
        exact = (
            projected.intent == case.expected_intent
            and projected.missing_fields == case.expected_missing_fields
        )
        if exact:
            matches += 1
        elif case.release_blocker:
            blocker_failures += 1

    accuracy_bps = (matches * 10_000 // admitted) if admitted else 0
    mean_cost = math.ceil(total_cost / admitted) if admitted else 0
    latency_p50 = _nearest_rank(latencies_ms, 0.50)
    latency_p95 = _nearest_rank(latencies_ms, 0.95)
    gates = {
        "production_admission_matches": admitted
        == sum(case.admission_expected for case in bundle.cases),
        "structured_output_100_percent": successes == admitted,
        "minimum_exact_accuracy": (accuracy_bps >= candidate.minimum_exact_accuracy_basis_points),
        "release_blocker_failures_zero": blocker_failures == 0,
        "token_counter_failures_zero": count_failures == 0,
        "tokenizer_undercount_zero": undercount_cases == 0,
        "output_cap_mapping_exact": cap_mismatches == 0,
        "complete_usage_100_percent": complete_usage_calls == admitted,
        "latency_p95_within_budget": latency_p95 <= candidate.maximum_latency_p95_ms,
        "mean_cost_within_budget": mean_cost <= candidate.maximum_mean_cost_usd_nanos,
        "provider_request_bound": requests <= remaining_provider_requests,
    }
    return HostedEvaluationRun(
        repetition=repetition,
        admitted_calls=admitted,
        provider_requests=requests,
        count_failures=count_failures,
        structured_successes=successes,
        structured_failures=failures,
        complete_usage_calls=complete_usage_calls,
        exact_matches=matches,
        exact_accuracy_basis_points=accuracy_bps,
        release_blocker_failures=blocker_failures,
        undercount_cases=undercount_cases,
        maximum_undercount_tokens=max_undercount,
        output_cap_mismatches=cap_mismatches,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_cost_usd_nanos=total_cost,
        mean_cost_usd_nanos=mean_cost,
        latency_p50_ms=latency_p50,
        latency_p95_ms=latency_p95,
        gates=gates,
        passed=all(gates.values()),
    )


def _nearest_rank(values: Sequence[int], percentile: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return ordered[rank - 1]
