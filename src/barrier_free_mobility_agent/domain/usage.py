from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MODEL_USAGE_SCHEMA_VERSION: Final[Literal["1"]] = "1"
RUN_USAGE_SCHEMA_VERSION: Final[Literal["1"]] = "1"

type NonNegativeInt = Annotated[int, Field(strict=True, ge=0)]
type OptionalNonNegativeInt = NonNegativeInt | None
type VersionLabel = Annotated[
    str,
    Field(
        strict=True,
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    ),
]


class UsageSource(StrEnum):
    NONE = "none"
    REPORTED = "reported"
    MIXED = "mixed"
    ESTIMATED = "estimated"


class ContextReductionReason(StrEnum):
    DERIVED_OR_DUPLICATE_REMOVED = "derived_or_duplicate_removed"
    OPTIONAL_CONTEXT_REMOVED = "optional_context_removed"
    IMPORTANT_DIGEST_USED = "important_digest_used"


class ModelUsage(BaseModel):
    """Normalized, provider-neutral token usage for one logical model call."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = MODEL_USAGE_SCHEMA_VERSION
    input_tokens: NonNegativeInt
    output_tokens: NonNegativeInt
    total_tokens: NonNegativeInt
    cached_input_tokens: OptionalNonNegativeInt = None
    reasoning_output_tokens: OptionalNonNegativeInt = None
    source: UsageSource
    estimator_version: VersionLabel | None = None

    @model_validator(mode="after")
    def validate_accounting(self) -> ModelUsage:
        if self.total_tokens < self.input_tokens + self.output_tokens:
            raise ValueError("total_tokens must cover input_tokens and output_tokens")
        if self.cached_input_tokens is not None and self.cached_input_tokens > self.input_tokens:
            raise ValueError("cached_input_tokens cannot exceed input_tokens")
        if (
            self.reasoning_output_tokens is not None
            and self.reasoning_output_tokens > self.output_tokens
        ):
            raise ValueError("reasoning_output_tokens cannot exceed output_tokens")
        if self.source == UsageSource.NONE:
            raise ValueError("model usage cannot use the none source")
        if self.source != UsageSource.REPORTED and self.estimator_version is None:
            raise ValueError("mixed or estimated usage requires estimator_version")
        return self


class RunUsage(BaseModel):
    """Monotonic aggregate used by a conversation run's admission policy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = RUN_USAGE_SCHEMA_VERSION
    revision: NonNegativeInt = 0
    model_calls: NonNegativeInt = 0
    input_tokens: NonNegativeInt = 0
    output_tokens: NonNegativeInt = 0
    total_tokens: NonNegativeInt = 0
    cached_input_tokens: NonNegativeInt = 0
    reasoning_output_tokens: NonNegativeInt = 0
    context_reduction_count: NonNegativeInt = 0
    reduction_reasons: Annotated[tuple[ContextReductionReason, ...], Field(max_length=8)] = ()
    source: UsageSource = UsageSource.NONE

    @model_validator(mode="after")
    def validate_accounting(self) -> RunUsage:
        if self.total_tokens < self.input_tokens + self.output_tokens:
            raise ValueError("total_tokens must cover input_tokens and output_tokens")
        if self.cached_input_tokens > self.input_tokens:
            raise ValueError("cached_input_tokens cannot exceed input_tokens")
        if self.reasoning_output_tokens > self.output_tokens:
            raise ValueError("reasoning_output_tokens cannot exceed output_tokens")
        token_details = (
            self.input_tokens,
            self.output_tokens,
            self.total_tokens,
            self.cached_input_tokens,
            self.reasoning_output_tokens,
        )
        if self.source == UsageSource.NONE and any(token_details):
            raise ValueError("the none source requires zero token counters")
        if self.model_calls == 0 and (
            self.source != UsageSource.NONE
            or any(token_details)
            or self.context_reduction_count != 0
            or self.reduction_reasons
        ):
            raise ValueError("zero model calls require an empty usage aggregate")
        if len(set(self.reduction_reasons)) != len(self.reduction_reasons):
            raise ValueError("reduction_reasons must be unique")
        if (self.context_reduction_count == 0) != (not self.reduction_reasons):
            raise ValueError("reduction count and reasons must appear together")
        return self


class RunUsageRevisionConflictError(ValueError):
    """The same immutable usage revision was presented with different content."""


def normalize_model_usage(
    *,
    estimated_input_tokens: int,
    estimated_output_tokens: int,
    estimator_version: str,
    reported_input_tokens: object = None,
    reported_output_tokens: object = None,
    reported_total_tokens: object = None,
    reported_cached_input_tokens: object = None,
    reported_reasoning_output_tokens: object = None,
) -> ModelUsage:
    """Normalize untrusted provider counters with conservative call-local estimates."""

    estimated_input = _required_count("estimated_input_tokens", estimated_input_tokens)
    estimated_output = _required_count("estimated_output_tokens", estimated_output_tokens)
    reported_input = _optional_count(reported_input_tokens)
    reported_output = _optional_count(reported_output_tokens)
    reported_total = _optional_count(reported_total_tokens)
    cached_input = _optional_count(reported_cached_input_tokens)
    reasoning_output = _optional_count(reported_reasoning_output_tokens)

    if reported_input is not None and reported_output is not None and reported_total is not None:
        complete_and_consistent = reported_total >= reported_input + reported_output
        if not complete_and_consistent:
            reported_total = None
    else:
        complete_and_consistent = False

    charged_input = reported_input if reported_input is not None else estimated_input
    charged_output = reported_output if reported_output is not None else estimated_output
    charged_total = max(reported_total or 0, charged_input + charged_output)
    if cached_input is not None and cached_input > charged_input:
        cached_input = None
    if reasoning_output is not None and reasoning_output > charged_output:
        reasoning_output = None

    provider_field_used = any(
        value is not None
        for value in (
            reported_input,
            reported_output,
            reported_total,
            cached_input,
            reasoning_output,
        )
    )
    source = (
        UsageSource.REPORTED
        if complete_and_consistent
        else UsageSource.MIXED
        if provider_field_used
        else UsageSource.ESTIMATED
    )
    return ModelUsage(
        input_tokens=charged_input,
        output_tokens=charged_output,
        total_tokens=charged_total,
        cached_input_tokens=cached_input,
        reasoning_output_tokens=reasoning_output,
        source=source,
        estimator_version=(None if source == UsageSource.REPORTED else estimator_version),
    )


def fold_usage_source(
    *sources: UsageSource | None,
) -> UsageSource:
    """Fold call-level provenance without treating a mixed aggregate as reported."""

    present = {source for source in sources if source is not None and source != UsageSource.NONE}
    if not present:
        return UsageSource.NONE
    if len(present) == 1:
        return next(iter(present))
    return UsageSource.MIXED


def accumulate_run_usage(
    current: RunUsage,
    call: ModelUsage,
    *,
    context_reductions: int = 0,
    reduction_reasons: tuple[ContextReductionReason, ...] = (),
) -> RunUsage:
    """Return the next immutable aggregate after one logical model settlement."""

    reductions = _required_count("context_reductions", context_reductions)
    if (reductions == 0) != (not reduction_reasons):
        raise ValueError("call-local reduction count and reasons must appear together")
    if len(set(reduction_reasons)) != len(reduction_reasons):
        raise ValueError("call-local reduction reasons must be unique")
    folded_reasons = tuple(dict.fromkeys((*current.reduction_reasons, *reduction_reasons)))
    return RunUsage(
        revision=current.revision + 1,
        model_calls=current.model_calls + 1,
        input_tokens=current.input_tokens + call.input_tokens,
        output_tokens=current.output_tokens + call.output_tokens,
        total_tokens=current.total_tokens + call.total_tokens,
        cached_input_tokens=current.cached_input_tokens + (call.cached_input_tokens or 0),
        reasoning_output_tokens=(
            current.reasoning_output_tokens + (call.reasoning_output_tokens or 0)
        ),
        context_reduction_count=current.context_reduction_count + reductions,
        reduction_reasons=folded_reasons,
        source=fold_usage_source(current.source, call.source),
    )


def accumulate_rejected_model_call(current: RunUsage) -> RunUsage:
    """Consume one logical call without charging tokens for a pre-invocation rejection."""

    return RunUsage(
        revision=current.revision + 1,
        model_calls=current.model_calls + 1,
        input_tokens=current.input_tokens,
        output_tokens=current.output_tokens,
        total_tokens=current.total_tokens,
        cached_input_tokens=current.cached_input_tokens,
        reasoning_output_tokens=current.reasoning_output_tokens,
        context_reduction_count=current.context_reduction_count,
        reduction_reasons=current.reduction_reasons,
        source=current.source,
    )


def reconcile_run_usage(current: RunUsage, candidate: RunUsage) -> RunUsage:
    """Prevent stale or conflicting repository snapshots from replacing newer usage."""

    if candidate.revision < current.revision:
        return current
    if candidate.revision == current.revision and candidate != current:
        raise RunUsageRevisionConflictError("run usage revision payload conflict")
    if candidate.revision > current.revision:
        cumulative_fields = (
            "model_calls",
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cached_input_tokens",
            "reasoning_output_tokens",
            "context_reduction_count",
        )
        regressed = next(
            (
                field
                for field in cumulative_fields
                if getattr(candidate, field) < getattr(current, field)
            ),
            None,
        )
        if regressed is not None:
            raise RunUsageRevisionConflictError(f"run usage counter regressed: {regressed}")
        if not set(current.reduction_reasons).issubset(candidate.reduction_reasons):
            raise RunUsageRevisionConflictError("run usage reduction reasons regressed")
        if (
            set(candidate.reduction_reasons).difference(current.reduction_reasons)
            and candidate.context_reduction_count == current.context_reduction_count
        ):
            raise RunUsageRevisionConflictError(
                "run usage reduction reasons changed without a reduction"
            )
        if fold_usage_source(current.source, candidate.source) != candidate.source:
            raise RunUsageRevisionConflictError("run usage source regressed")
    return candidate


def _required_count(name: str, value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _optional_count(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None
