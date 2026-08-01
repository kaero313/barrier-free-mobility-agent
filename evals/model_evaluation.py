from __future__ import annotations

import hashlib
import hmac
import math
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from typing import Final, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from barrier_free_mobility_agent.agent.models import (
    ClarificationKind,
    FacilityContext,
    PendingClarification,
    TripContext,
)
from barrier_free_mobility_agent.agent.policy import ToolPolicy
from barrier_free_mobility_agent.agent.router import DeterministicPreRouter, RoutingResult
from barrier_free_mobility_agent.application.model_boundary import (
    FallbackTokenCounter,
    ModelFailure,
    ModelFailureKind,
    ModelInvocation,
    ModelInvocationResult,
    ModelProfile,
    ModelPurposeAdmission,
    ModelPurposeDecision,
    ModelPurposePolicy,
    ModelResult,
    ModelResultIdentityError,
    ProviderRequestOption,
    TokenCount,
    validate_result_identity,
)
from barrier_free_mobility_agent.application.model_routing import (
    EphemeralRoutingInvocation,
    ModelRoutingInvocationAssembler,
    canonical_routing_invocation_bytes,
    project_structured_intent,
)
from barrier_free_mobility_agent.domain.mobility import (
    MobilityContext,
    MobilityFlag,
    SlotSource,
    TriState,
)
from barrier_free_mobility_agent.domain.model_routing import StructuredIntentDecision
from barrier_free_mobility_agent.domain.routing import AgentIntent, ToolCapability
from barrier_free_mobility_agent.domain.usage import ModelUsage, UsageSource


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        allow_inf_nan=False,
    )


class CorpusGroup(StrEnum):
    AMBIGUOUS = "ambiguous"
    DETERMINISTIC = "deterministic"
    RESUME = "resume"
    ADVERSARIAL = "adversarial"
    TOKENIZER_STRESS = "tokenizer_stress"


EXPECTED_GROUP_COUNTS: Final[dict[CorpusGroup, int]] = {
    CorpusGroup.AMBIGUOUS: 64,
    CorpusGroup.DETERMINISTIC: 24,
    CorpusGroup.RESUME: 16,
    CorpusGroup.ADVERSARIAL: 16,
    CorpusGroup.TOKENIZER_STRESS: 8,
}
EXPECTED_TOTAL_CASES: Final = sum(EXPECTED_GROUP_COUNTS.values())

type MissingField = Literal[
    "origin",
    "destination",
    "can_use_stairs",
    "can_use_escalator",
    "facility_target",
]


class EvaluationContext(_StrictFrozenModel):
    trip_origin: str | None = Field(default=None, max_length=120)
    trip_destination: str | None = Field(default=None, max_length=120)
    facility_target: str | None = Field(default=None, max_length=120)
    wheelchair: TriState = TriState.UNKNOWN
    can_use_stairs: TriState = TriState.UNKNOWN
    can_use_escalator: TriState = TriState.UNKNOWN
    active_clarification: bool = False
    active_nested_interrupt: bool = False


class RecordedDecision(_StrictFrozenModel):
    """Synthetic provider output before the production deterministic projector."""

    intent: AgentIntent
    confidence: float = Field(ge=0, le=1)


class NormalizedRecording(_StrictFrozenModel):
    """Synthetic wire facts; prompt text and provider payloads are forbidden."""

    decision: RecordedDecision
    truth_input_tokens: int = Field(default=128, ge=0, le=1_000_000)
    output_tokens: int = Field(default=12, ge=0, le=1_000_000)
    wire_max_output_tokens: int = Field(default=128, ge=1, le=1_000_000)
    latency_ms: int = Field(default=50, ge=0, le=3_600_000)


class EvaluationCase(_StrictFrozenModel):
    schema_version: Literal["1"] = "1"
    case_id: str = Field(pattern=r"^[a-z][a-z0-9-]{2,99}$")
    group: CorpusGroup
    current_input: str = Field(min_length=1, max_length=2_000)
    context: EvaluationContext = Field(default_factory=EvaluationContext)
    admission_expected: bool
    expected_intent: AgentIntent
    expected_missing_fields: tuple[MissingField, ...] = Field(default=(), max_length=5)
    release_blocker: bool = False
    tags: tuple[str, ...] = Field(default=(), max_length=12)
    recording: NormalizedRecording | None = None

    @model_validator(mode="after")
    def validate_admission_contract(self) -> EvaluationCase:
        if len(self.expected_missing_fields) != len(set(self.expected_missing_fields)):
            raise ValueError("expected_missing_fields must be unique")
        if len(self.tags) != len(set(self.tags)):
            raise ValueError("tags must be unique")
        if any(not tag or len(tag) > 40 for tag in self.tags):
            raise ValueError("tags must contain bounded non-empty labels")
        if self.admission_expected != (self.recording is not None):
            raise ValueError("only model-admitted cases may contain a recording")
        if (
            self.group in {CorpusGroup.DETERMINISTIC, CorpusGroup.RESUME}
            and self.admission_expected
        ):
            raise ValueError("deterministic and resume controls must not invoke a model")
        if self.group == CorpusGroup.RESUME and not (
            self.context.active_clarification or self.context.active_nested_interrupt
        ):
            raise ValueError("resume controls require an active clarification or interrupt")
        return self


class CorpusManifest(_StrictFrozenModel):
    schema_version: Literal["1"] = "1"
    data_classification: Literal["synthetic_only"]
    corpus_version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
    file: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}\.jsonl$")
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    production_invocation_binding: str = Field(pattern=r"^[a-f0-9]{64}$")
    token_counter_version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
    total_cases: int = Field(ge=1, le=100_000)
    group_counts: dict[CorpusGroup, int]

    @model_validator(mode="after")
    def validate_group_contract(self) -> CorpusManifest:
        if set(self.group_counts) != set(CorpusGroup):
            raise ValueError("manifest must declare every corpus group")
        if any(value < 0 for value in self.group_counts.values()):
            raise ValueError("manifest group counts must be non-negative")
        if sum(self.group_counts.values()) != self.total_cases:
            raise ValueError("manifest total must equal its group counts")
        return self


@dataclass(frozen=True, slots=True)
class CorpusBundle:
    manifest: CorpusManifest
    cases: tuple[EvaluationCase, ...]


class EvaluationDataError(ValueError):
    """A payload-free corpus error safe to return from the evaluation CLI."""

    def __init__(self, code: str = "EVALUATION_DATA_INVALID") -> None:
        self.code = code
        super().__init__(code)


def load_corpus(manifest_path: Path) -> CorpusBundle:
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = CorpusManifest.model_validate_json(manifest_bytes)
        corpus_path = (manifest_path.parent / manifest.file).resolve()
        if corpus_path.parent != manifest_path.parent.resolve():
            raise EvaluationDataError()
        corpus_bytes = corpus_path.read_bytes()
        if hashlib.sha256(corpus_bytes).hexdigest() != manifest.sha256:
            raise EvaluationDataError("EVALUATION_CORPUS_HASH_MISMATCH")
        decoded = corpus_bytes.decode("utf-8")
        cases = tuple(
            EvaluationCase.model_validate_json(line)
            for line in decoded.splitlines()
            if line.strip()
        )
    except EvaluationDataError:
        raise
    except (OSError, UnicodeError, ValidationError, ValueError):
        raise EvaluationDataError() from None

    case_ids = [case.case_id for case in cases]
    counts = Counter(case.group for case in cases)
    if len(case_ids) != len(set(case_ids)):
        raise EvaluationDataError()
    if len(cases) != manifest.total_cases:
        raise EvaluationDataError()
    if dict(counts) != manifest.group_counts:
        raise EvaluationDataError()
    return CorpusBundle(manifest=manifest, cases=cases)


@dataclass(frozen=True, slots=True)
class PreparedCase:
    routing: RoutingResult
    admission: ModelPurposeDecision
    assembly: EphemeralRoutingInvocation


class ProductionEvaluationHarness:
    """Maps synthetic fixtures through the real production routing boundary."""

    def __init__(
        self,
        *,
        profile: ModelProfile | None = None,
        provider_options: tuple[ProviderRequestOption, ...] = (),
        max_output_tokens: int = 128,
    ) -> None:
        self._router = DeterministicPreRouter()
        self._purpose_policy = ModelPurposePolicy()
        self._assembler = ModelRoutingInvocationAssembler(
            profile=profile
            or ModelProfile(
                provider_alias="recorded-provider",
                model_alias="router-small",
                provider_contract_version="recorded-provider-contract.v1",
                context_window_tokens=8_192,
                hard_max_output_tokens=256,
                reasoning_tokens_in_output_cap=True,
            ),
            provider_options=provider_options,
            max_output_tokens=max_output_tokens,
        )
        self._available_capabilities = tuple(ToolCapability)

    def prepare(self, case: EvaluationCase) -> PreparedCase:
        trip = TripContext(
            origin_query=case.context.trip_origin,
            destination_query=case.context.trip_destination,
        )
        mobility = MobilityContext(
            wheelchair=_mobility_flag(case.context.wheelchair),
            can_use_stairs=_mobility_flag(case.context.can_use_stairs),
            can_use_escalator=_mobility_flag(case.context.can_use_escalator),
        )
        facility = (
            FacilityContext(
                target_query=case.context.facility_target,
                target_station=case.context.facility_target,
                target_source=SlotSource.THREAD_CONTEXT,
            )
            if case.context.facility_target is not None
            else None
        )
        pending = (
            PendingClarification(
                kind=ClarificationKind.REQUIRED_FIELDS,
                intent=AgentIntent.TRIP_ACCESSIBILITY,
                questions=("synthetic-evaluation-question",),
                message="synthetic-evaluation-message",
            )
            if case.context.active_clarification
            else None
        )
        routing = self._router.route(
            case.current_input,
            trip=trip,
            mobility=mobility,
            facility=facility,
            pending=pending,
        )
        assembly = self._assembler.assemble(
            current_input=routing.normalized_input,
            has_active_trip=bool(routing.trip.origin_query or routing.trip.destination_query),
            has_origin=routing.trip.origin_query is not None,
            has_destination=routing.trip.destination_query is not None,
            has_previous_destination=case.context.trip_destination is not None,
            has_facility_target=facility is not None and facility.target_station is not None,
            can_use_stairs_known=(routing.mobility.can_use_stairs.value != TriState.UNKNOWN),
            can_use_escalator_known=(routing.mobility.can_use_escalator.value != TriState.UNKNOWN),
            available_capabilities=self._available_capabilities,
        )
        admission = self._purpose_policy.evaluate(
            ModelPurposeAdmission(
                purpose=assembly.invocation.purpose,
                fresh_turn=not (
                    case.context.active_clarification or case.context.active_nested_interrupt
                ),
                route_is_ambiguous=routing.decision.intent == AgentIntent.UNKNOWN,
                active_pending_clarification=case.context.active_clarification,
                active_nested_interrupt=case.context.active_nested_interrupt,
                model_calls_this_turn=0,
            )
        )
        return PreparedCase(routing=routing, admission=admission, assembly=assembly)


def _mobility_flag(value: TriState) -> MobilityFlag:
    if value == TriState.UNKNOWN:
        return MobilityFlag()
    return MobilityFlag.known(value == TriState.TRUE, SlotSource.THREAD_CONTEXT)


class EvaluationAdapter(Protocol):
    @property
    def version(self) -> str: ...

    @property
    def call_count(self) -> int: ...

    @property
    def output_cap_mismatches(self) -> int: ...

    @property
    def latencies_ms(self) -> tuple[int, ...]: ...

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


class RecordingMismatchError(RuntimeError):
    """A replay was attempted with a different immutable production invocation."""


_SYNTHETIC_BINDING_KEY: Final = sha256(
    b"barrier-free-mobility:synthetic-evaluation-only:v1"
).digest()
_INVOCATION_BINDING_DOMAIN: Final = b"synthetic-routing-invocation:v1\0"
_INVOCATION_SET_DOMAIN: Final = b"synthetic-routing-invocation-set:v1\0"


def _invocation_binding(
    invocation: ModelInvocation[StructuredIntentDecision],
) -> str:
    canonical = canonical_routing_invocation_bytes(invocation)
    return hmac.new(
        _SYNTHETIC_BINDING_KEY,
        _INVOCATION_BINDING_DOMAIN + canonical,
        sha256,
    ).hexdigest()


def production_invocation_set_binding(
    cases: Sequence[EvaluationCase],
    harness: ProductionEvaluationHarness,
) -> str:
    """Bind recordings to production prompt/schema without returning case fingerprints."""

    payload = bytearray(_INVOCATION_SET_DOMAIN)
    for case in cases:
        if case.recording is None:
            continue
        binding = bytes.fromhex(_invocation_binding(harness.prepare(case).assembly.invocation))
        case_id = case.case_id.encode("ascii")
        payload.extend(len(case_id).to_bytes(2, "big"))
        payload.extend(case_id)
        payload.extend(binding)
    return hmac.new(_SYNTHETIC_BINDING_KEY, bytes(payload), sha256).hexdigest()


@dataclass(frozen=True, slots=True)
class _BoundRecording:
    recording: NormalizedRecording
    scripted_result: ModelInvocationResult[StructuredIntentDecision] | None = None


class RecordedStructuredModel:
    """Replay synthetic records through the production model port and strict DTO."""

    def __init__(self, records: Mapping[str, _BoundRecording]) -> None:
        self._records = dict(records)
        self._counter = FallbackTokenCounter()
        self._call_count = 0
        self._output_cap_mismatches = 0
        self._latencies_ms: list[int] = []

    @classmethod
    def from_cases(
        cls,
        cases: Sequence[EvaluationCase],
        harness: ProductionEvaluationHarness,
    ) -> RecordedStructuredModel:
        records: dict[str, _BoundRecording] = {}
        for case in cases:
            recording = case.recording
            if recording is None:
                continue
            invocation = harness.prepare(case).assembly.invocation
            records[_invocation_binding(invocation)] = _BoundRecording(recording=recording)
        return cls(records)

    @property
    def version(self) -> str:
        return self._counter.version

    @property
    def call_count(self) -> int:
        return self._call_count

    @property
    def output_cap_mismatches(self) -> int:
        return self._output_cap_mismatches

    @property
    def latencies_ms(self) -> tuple[int, ...]:
        return tuple(self._latencies_ms)

    async def count_input(
        self,
        invocation: ModelInvocation[StructuredIntentDecision],
    ) -> TokenCount:
        return await self._counter.count_input(invocation)

    async def invoke_structured(
        self,
        invocation: ModelInvocation[StructuredIntentDecision],
        *,
        preflight: TokenCount | None = None,
    ) -> ModelInvocationResult[StructuredIntentDecision]:
        self._call_count += 1
        try:
            bound = self._records[_invocation_binding(invocation)]
        except KeyError:
            raise RecordingMismatchError("recording_not_found") from None
        recording = bound.recording
        self._latencies_ms.append(recording.latency_ms)
        if invocation.max_output_tokens != recording.wire_max_output_tokens:
            self._output_cap_mismatches += 1
        if bound.scripted_result is not None:
            return bound.scripted_result
        usage = ModelUsage(
            input_tokens=recording.truth_input_tokens,
            output_tokens=recording.output_tokens,
            total_tokens=recording.truth_input_tokens + recording.output_tokens,
            source=UsageSource.REPORTED,
        )
        return ModelResult[StructuredIntentDecision](
            value=StructuredIntentDecision(
                schema_version="1",
                intent=recording.decision.intent,
                confidence=recording.decision.confidence,
            ),
            usage=usage,
            provider_alias=invocation.profile.provider_alias,
            model_alias=invocation.profile.model_alias,
            provider_contract_version=invocation.profile.provider_contract_version,
            prompt_version=invocation.prompt_version,
        )


class ScriptedStructuredModel(RecordedStructuredModel):
    """A fingerprint-bound failure/success script for model boundary tests."""

    @classmethod
    def from_cases_with_overrides(
        cls,
        cases: Sequence[EvaluationCase],
        harness: ProductionEvaluationHarness,
        *,
        overrides: Mapping[
            str,
            ModelInvocationResult[StructuredIntentDecision],
        ],
    ) -> ScriptedStructuredModel:
        records: dict[str, _BoundRecording] = {}
        for case in cases:
            recording = case.recording
            if recording is None:
                continue
            invocation = harness.prepare(case).assembly.invocation
            records[_invocation_binding(invocation)] = _BoundRecording(
                recording=recording,
                scripted_result=overrides.get(case.case_id),
            )
        return cls(records)


class EvaluationReport(_StrictFrozenModel):
    schema_version: Literal["1"] = "1"
    corpus_version: str
    corpus_sha256: str
    total_cases: int
    group_counts: dict[str, int]
    admitted_calls: int
    skipped_calls: int
    admission_mismatches: int
    structured_successes: int
    structured_failures: int
    counter_failures: int
    policy_projection_failures: int
    exact_matches: int
    exact_accuracy: float = Field(ge=0, le=1)
    release_blocker_failures: int
    undercount_cases: int
    max_undercount_tokens: int
    output_cap_mismatches: int
    input_tokens: int
    output_tokens: int
    latency_p50_ms: int
    latency_p95_ms: int
    gates: dict[str, bool]
    passed: bool


async def evaluate_corpus(
    bundle: CorpusBundle,
    adapter: EvaluationAdapter,
    *,
    harness: ProductionEvaluationHarness,
    minimum_exact_accuracy: float = 0.95,
) -> EvaluationReport:
    if not 0 <= minimum_exact_accuracy <= 1:
        raise ValueError("minimum_exact_accuracy must be between zero and one")

    expected_admitted = sum(case.admission_expected for case in bundle.cases)
    admitted = 0
    skipped = 0
    admission_mismatches = 0
    successes = 0
    failures = 0
    counter_failures = 0
    policy_projection_failures = 0
    matches = 0
    blocker_failed_case_ids: set[str] = set()
    undercount_cases = 0
    max_undercount = 0
    input_tokens = 0
    output_tokens = 0
    tool_policy = ToolPolicy()

    production_binding_matches = hmac.compare_digest(
        production_invocation_set_binding(bundle.cases, harness),
        bundle.manifest.production_invocation_binding,
    )

    for case in bundle.cases:
        prepared = harness.prepare(case)
        if prepared.admission.allowed != case.admission_expected:
            admission_mismatches += 1
            if case.release_blocker:
                blocker_failed_case_ids.add(case.case_id)
        if not prepared.admission.allowed:
            skipped += 1
            continue

        admitted += 1
        recording = case.recording
        if recording is None:
            failures += 1
            if case.release_blocker:
                blocker_failed_case_ids.add(case.case_id)
            continue
        invocation = prepared.assembly.invocation
        try:
            count = await adapter.count_input(invocation)
        except (TypeError, ValueError):
            counter_failures += 1
            failures += 1
            if case.release_blocker:
                blocker_failed_case_ids.add(case.case_id)
            continue
        undercount = max(0, recording.truth_input_tokens - count.input_tokens)
        if undercount:
            undercount_cases += 1
            max_undercount = max(max_undercount, undercount)

        try:
            result = await adapter.invoke_structured(invocation, preflight=count)
        except RecordingMismatchError:
            result = ModelFailure(
                kind=ModelFailureKind.PROVIDER_ERROR,
                safe_error_code="RECORDING_MISMATCH",
            )
        try:
            result = validate_result_identity(invocation, result)
        except ModelResultIdentityError:
            result = ModelFailure(
                kind=ModelFailureKind.PROVIDER_ERROR,
                safe_error_code="MODEL_IDENTITY_MISMATCH",
            )
        if isinstance(result, ModelFailure):
            failures += 1
            if case.release_blocker:
                blocker_failed_case_ids.add(case.case_id)
            continue

        successes += 1
        input_tokens += result.usage.input_tokens
        output_tokens += result.usage.output_tokens
        projected = project_structured_intent(result.value, prepared.assembly.envelope)
        plan = tool_policy.build_plan(projected)
        if plan.allowed_tools != projected.required_capabilities:
            policy_projection_failures += 1
            if case.release_blocker:
                blocker_failed_case_ids.add(case.case_id)
        exact = (
            projected.intent == case.expected_intent
            and projected.missing_fields == case.expected_missing_fields
        )
        if exact:
            matches += 1
        elif case.release_blocker:
            blocker_failed_case_ids.add(case.case_id)

    accuracy = matches / expected_admitted if expected_admitted else 0.0
    actual_counts = Counter(case.group for case in bundle.cases)
    shape_matches = (
        len(bundle.cases) == EXPECTED_TOTAL_CASES and dict(actual_counts) == EXPECTED_GROUP_COUNTS
    )
    gates = {
        "synthetic_only": bundle.manifest.data_classification == "synthetic_only",
        "corpus_shape": shape_matches,
        "production_invocation_binding": production_binding_matches,
        "production_admission_matches": admission_mismatches == 0,
        "structured_output_100_percent": successes == admitted,
        "minimum_exact_accuracy": accuracy >= minimum_exact_accuracy,
        "release_blocker_failures_zero": not blocker_failed_case_ids,
        "token_counter_failures_zero": counter_failures == 0,
        "production_counter_version": adapter.version == bundle.manifest.token_counter_version,
        "tokenizer_undercount_zero": undercount_cases == 0,
        "output_cap_mapping_exact": adapter.output_cap_mismatches == 0,
        "tool_policy_projection_exact": policy_projection_failures == 0,
        "non_admitted_calls_zero": adapter.call_count == admitted,
    }
    return EvaluationReport(
        corpus_version=bundle.manifest.corpus_version,
        corpus_sha256=bundle.manifest.sha256,
        total_cases=len(bundle.cases),
        group_counts={group.value: actual_counts[group] for group in CorpusGroup},
        admitted_calls=admitted,
        skipped_calls=skipped,
        admission_mismatches=admission_mismatches,
        structured_successes=successes,
        structured_failures=failures,
        counter_failures=counter_failures,
        policy_projection_failures=policy_projection_failures,
        exact_matches=matches,
        exact_accuracy=accuracy,
        release_blocker_failures=len(blocker_failed_case_ids),
        undercount_cases=undercount_cases,
        max_undercount_tokens=max_undercount,
        output_cap_mismatches=adapter.output_cap_mismatches,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_p50_ms=_nearest_rank(adapter.latencies_ms, 0.50),
        latency_p95_ms=_nearest_rank(adapter.latencies_ms, 0.95),
        gates=gates,
        passed=all(gates.values()),
    )


def _nearest_rank(values: Sequence[int], percentile: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return ordered[rank - 1]
