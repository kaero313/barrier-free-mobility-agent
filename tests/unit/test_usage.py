from __future__ import annotations

import pytest
from pydantic import ValidationError

from barrier_free_mobility_agent.domain.usage import (
    ContextReductionReason,
    ModelUsage,
    RunUsage,
    RunUsageRevisionConflictError,
    UsageSource,
    accumulate_rejected_model_call,
    accumulate_run_usage,
    fold_usage_source,
    normalize_model_usage,
    reconcile_run_usage,
)


def test_complete_consistent_provider_usage_is_reported() -> None:
    usage = normalize_model_usage(
        estimated_input_tokens=150,
        estimated_output_tokens=80,
        estimator_version="fallback-v1",
        reported_input_tokens=100,
        reported_output_tokens=20,
        reported_total_tokens=125,
        reported_cached_input_tokens=40,
        reported_reasoning_output_tokens=5,
    )

    assert usage == ModelUsage(
        input_tokens=100,
        output_tokens=20,
        total_tokens=125,
        cached_input_tokens=40,
        reasoning_output_tokens=5,
        source=UsageSource.REPORTED,
    )


def test_partial_and_total_only_reports_use_conservative_estimates() -> None:
    partial = normalize_model_usage(
        estimated_input_tokens=150,
        estimated_output_tokens=80,
        estimator_version="fallback-v1",
        reported_input_tokens=100,
    )
    total_only = normalize_model_usage(
        estimated_input_tokens=150,
        estimated_output_tokens=80,
        estimator_version="fallback-v1",
        reported_total_tokens=500,
    )

    assert partial.input_tokens == 100
    assert partial.output_tokens == 80
    assert partial.total_tokens == 180
    assert partial.source == UsageSource.MIXED
    assert partial.estimator_version == "fallback-v1"
    assert total_only.input_tokens == 150
    assert total_only.output_tokens == 80
    assert total_only.total_tokens == 500
    assert total_only.source == UsageSource.MIXED


def test_invalid_or_inconsistent_provider_fields_are_not_trusted() -> None:
    inconsistent = normalize_model_usage(
        estimated_input_tokens=150,
        estimated_output_tokens=80,
        estimator_version="fallback-v1",
        reported_input_tokens=100,
        reported_output_tokens=20,
        reported_total_tokens=10,
    )
    invalid = normalize_model_usage(
        estimated_input_tokens=150,
        estimated_output_tokens=80,
        estimator_version="fallback-v1",
        reported_input_tokens=-1,
        reported_output_tokens=True,
        reported_total_tokens="230",
    )

    assert inconsistent.total_tokens == 120
    assert inconsistent.source == UsageSource.MIXED
    assert invalid == ModelUsage(
        input_tokens=150,
        output_tokens=80,
        total_tokens=230,
        source=UsageSource.ESTIMATED,
        estimator_version="fallback-v1",
    )


def test_optional_provider_details_cannot_exceed_their_parent_counters() -> None:
    normalized = normalize_model_usage(
        estimated_input_tokens=150,
        estimated_output_tokens=80,
        estimator_version="fallback-v1",
        reported_input_tokens=100,
        reported_output_tokens=20,
        reported_total_tokens=120,
        reported_cached_input_tokens=101,
        reported_reasoning_output_tokens=21,
    )

    assert normalized.source == UsageSource.REPORTED
    assert normalized.cached_input_tokens is None
    assert normalized.reasoning_output_tokens is None
    with pytest.raises(ValidationError):
        ModelUsage(
            input_tokens=100,
            output_tokens=20,
            total_tokens=120,
            cached_input_tokens=101,
            source=UsageSource.REPORTED,
        )
    with pytest.raises(ValidationError):
        RunUsage(
            revision=1,
            model_calls=1,
            input_tokens=100,
            output_tokens=20,
            total_tokens=120,
            reasoning_output_tokens=21,
            source=UsageSource.REPORTED,
        )


def test_usage_models_are_strict_versioned_and_forbid_extra_fields() -> None:
    with pytest.raises(ValidationError):
        ModelUsage.model_validate(
            {
                "schema_version": "1",
                "input_tokens": True,
                "output_tokens": 0,
                "total_tokens": 1,
                "source": "reported",
            }
        )
    with pytest.raises(ValidationError):
        RunUsage.model_validate({"schema_version": "2"})
    with pytest.raises(ValidationError):
        RunUsage.model_validate({"schema_version": "1", "raw_prompt": "secret"})
    with pytest.raises(ValidationError):
        RunUsage(
            context_reduction_count=1,
            reduction_reasons=(
                ContextReductionReason.OPTIONAL_CONTEXT_REMOVED,
                ContextReductionReason.OPTIONAL_CONTEXT_REMOVED,
            ),
        )
    with pytest.raises(ValidationError):
        ModelUsage(
            input_tokens=0,
            output_tokens=0,
            total_tokens=0,
            source=UsageSource.NONE,
        )
    with pytest.raises(ValidationError):
        RunUsage(model_calls=0, source=UsageSource.REPORTED)
    with pytest.raises(ValidationError):
        RunUsage(model_calls=0, cached_input_tokens=1)
    with pytest.raises(ValidationError):
        RunUsage(
            model_calls=0,
            context_reduction_count=1,
            reduction_reasons=(ContextReductionReason.OPTIONAL_CONTEXT_REMOVED,),
        )
    with pytest.raises(ValidationError):
        RunUsage(model_calls=1, cached_input_tokens=1)
    assert RunUsage(revision=1, model_calls=1) == RunUsage(
        revision=1,
        model_calls=1,
        total_tokens=0,
        source=UsageSource.NONE,
    )
    assert RunUsage().source == UsageSource.NONE


def test_run_usage_accumulation_and_source_fold_are_monotonic() -> None:
    reported = ModelUsage(
        input_tokens=100,
        output_tokens=20,
        total_tokens=125,
        source=UsageSource.REPORTED,
    )
    estimated = ModelUsage(
        input_tokens=150,
        output_tokens=80,
        total_tokens=230,
        source=UsageSource.ESTIMATED,
        estimator_version="fallback-v1",
    )

    first = accumulate_run_usage(
        RunUsage(),
        reported,
        context_reductions=1,
        reduction_reasons=(ContextReductionReason.OPTIONAL_CONTEXT_REMOVED,),
    )
    second = accumulate_run_usage(first, estimated)

    assert first.revision == 1
    assert second.revision == 2
    assert second.model_calls == 2
    assert second.input_tokens == 250
    assert second.output_tokens == 100
    assert second.total_tokens == 355
    assert second.context_reduction_count == 1
    assert second.reduction_reasons == (ContextReductionReason.OPTIONAL_CONTEXT_REMOVED,)
    assert second.source == UsageSource.MIXED
    assert fold_usage_source(None, UsageSource.REPORTED) == UsageSource.REPORTED
    assert fold_usage_source(None, UsageSource.NONE) == UsageSource.NONE
    assert fold_usage_source(UsageSource.REPORTED, UsageSource.ESTIMATED) == UsageSource.MIXED


def test_rejected_model_call_consumes_revision_without_charging_tokens() -> None:
    current = RunUsage(
        revision=2,
        model_calls=2,
        input_tokens=100,
        output_tokens=20,
        total_tokens=125,
        cached_input_tokens=10,
        reasoning_output_tokens=5,
        source=UsageSource.REPORTED,
    )

    rejected = accumulate_rejected_model_call(current)

    assert rejected == current.model_copy(update={"revision": 3, "model_calls": 3})
    assert rejected.source == UsageSource.REPORTED


@pytest.mark.parametrize(
    ("count", "reasons"),
    [
        (0, (ContextReductionReason.OPTIONAL_CONTEXT_REMOVED,)),
        (1, ()),
        (
            1,
            (
                ContextReductionReason.OPTIONAL_CONTEXT_REMOVED,
                ContextReductionReason.OPTIONAL_CONTEXT_REMOVED,
            ),
        ),
    ],
)
def test_run_usage_rejects_call_local_reduction_mismatch(
    count: int,
    reasons: tuple[ContextReductionReason, ...],
) -> None:
    call = ModelUsage(
        input_tokens=1,
        output_tokens=0,
        total_tokens=1,
        source=UsageSource.REPORTED,
    )

    with pytest.raises(ValueError, match="call-local reduction"):
        accumulate_run_usage(
            RunUsage(),
            call,
            context_reductions=count,
            reduction_reasons=reasons,
        )


def test_repository_reconciliation_preserves_newer_and_rejects_revision_conflict() -> None:
    current = RunUsage(revision=2, model_calls=1)
    stale = RunUsage(revision=1)
    conflict = RunUsage(
        revision=2,
        model_calls=1,
        context_reduction_count=1,
        reduction_reasons=(ContextReductionReason.OPTIONAL_CONTEXT_REMOVED,),
    )
    newer = RunUsage(revision=3, model_calls=1)

    assert reconcile_run_usage(current, stale) is current
    assert reconcile_run_usage(current, newer) is newer
    with pytest.raises(RunUsageRevisionConflictError):
        reconcile_run_usage(current, conflict)


def test_repository_reconciliation_rejects_higher_revision_regressions() -> None:
    current = RunUsage(
        revision=2,
        model_calls=2,
        input_tokens=100,
        output_tokens=20,
        total_tokens=125,
        cached_input_tokens=10,
        reasoning_output_tokens=5,
        context_reduction_count=1,
        reduction_reasons=(ContextReductionReason.OPTIONAL_CONTEXT_REMOVED,),
        source=UsageSource.MIXED,
    )
    zeroed = RunUsage(revision=3)
    missing_reason = current.model_copy(
        update={
            "revision": 3,
            "reduction_reasons": (ContextReductionReason.IMPORTANT_DIGEST_USED,),
        }
    )
    downgraded_source = current.model_copy(update={"revision": 3, "source": UsageSource.REPORTED})
    uncounted_reason = current.model_copy(
        update={
            "revision": 3,
            "reduction_reasons": (
                *current.reduction_reasons,
                ContextReductionReason.IMPORTANT_DIGEST_USED,
            ),
        }
    )

    for candidate in (zeroed, missing_reason, downgraded_source, uncounted_reason):
        with pytest.raises(RunUsageRevisionConflictError):
            reconcile_run_usage(current, candidate)


@pytest.mark.parametrize(
    "field",
    [
        "model_calls",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cached_input_tokens",
        "reasoning_output_tokens",
        "context_reduction_count",
    ],
)
def test_repository_reconciliation_checks_every_cumulative_counter(field: str) -> None:
    current = RunUsage(
        revision=2,
        model_calls=2,
        input_tokens=100,
        output_tokens=20,
        total_tokens=125,
        cached_input_tokens=10,
        reasoning_output_tokens=5,
        context_reduction_count=1,
        reduction_reasons=(ContextReductionReason.OPTIONAL_CONTEXT_REMOVED,),
        source=UsageSource.MIXED,
    )
    payload = current.model_dump(mode="json")
    payload["revision"] = 3
    payload[field] = int(payload[field]) - 1
    if field == "context_reduction_count":
        payload["reduction_reasons"] = []
    candidate = RunUsage.model_validate(payload)

    with pytest.raises(RunUsageRevisionConflictError):
        reconcile_run_usage(current, candidate)
