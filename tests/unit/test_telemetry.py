from __future__ import annotations

import math
import traceback

import pytest

from barrier_free_mobility_agent.domain.usage import ContextReductionReason
from barrier_free_mobility_agent.observability.telemetry import (
    ContextBudgetRejectionReason,
    ContextBudgetViolationReason,
    FailureIsolatedTelemetrySink,
    NoopTelemetrySink,
    RecordingTelemetrySink,
    TelemetryContractError,
    TelemetryLabelPolicy,
    TelemetryLabels,
    TelemetryMetric,
    TelemetryRecord,
)


@pytest.fixture
def label_policy() -> TelemetryLabelPolicy:
    return TelemetryLabelPolicy(
        allowed_model_aliases=frozenset({"router-small"}),
        allowed_flow_versions=frozenset({"mobility-v1"}),
    )


def _model_call_labels(**updates: str) -> dict[str, str]:
    labels = {
        "purpose": "ambiguous_routing",
        "outcome": "settled",
        "usage_source": "reported",
        "model_alias": "router-small",
    }
    labels.update(updates)
    return labels


def test_recording_sink_accepts_only_the_metric_label_contract(
    label_policy: TelemetryLabelPolicy,
) -> None:
    sink = RecordingTelemetrySink(label_policy)

    sink.record(
        TelemetryMetric.MODEL_CALLS_TOTAL,
        1,
        labels=_model_call_labels(),
    )

    assert len(sink.records) == 1
    assert dict(sink.records[0].labels) == _model_call_labels()


@pytest.mark.parametrize(
    "unsafe_label",
    [
        "owner",
        "owner_id",
        "thread_id",
        "question",
        "location",
        "station",
        "mobility",
        "prompt",
        "arbitrary",
    ],
)
def test_sinks_reject_sensitive_or_arbitrary_label_names_without_a_cause(
    label_policy: TelemetryLabelPolicy,
    unsafe_label: str,
) -> None:
    labels = {unsafe_label: "서울역에서 휠체어를 사용해요"}

    for sink in (RecordingTelemetrySink(label_policy), NoopTelemetrySink(label_policy)):
        with pytest.raises(TelemetryContractError, match="privacy contract") as exc_info:
            sink.record(
                TelemetryMetric.CONTEXT_INPUT_TOKENS,
                10,
                labels=labels,
            )
        assert exc_info.value.__cause__ is None


def test_metric_contract_rejects_missing_and_unrelated_labels(
    label_policy: TelemetryLabelPolicy,
) -> None:
    sink = RecordingTelemetrySink(label_policy)

    with pytest.raises(TelemetryContractError, match="metric contract"):
        sink.record(
            TelemetryMetric.MODEL_CALLS_TOTAL,
            1,
            labels={"purpose": "ambiguous_routing"},
        )
    with pytest.raises(TelemetryContractError, match="metric contract"):
        sink.record(
            TelemetryMetric.STATE_SERIALIZED_BYTES,
            128,
            labels={
                "flow_version": "mobility-v1",
                "purpose": "ambiguous_routing",
            },
        )


@pytest.mark.parametrize(
    ("metric", "reason"),
    [
        (
            TelemetryMetric.CONTEXT_REDUCTIONS_TOTAL,
            ContextReductionReason.OPTIONAL_CONTEXT_REMOVED.value,
        ),
        (
            TelemetryMetric.CONTEXT_BUDGET_REJECTIONS_TOTAL,
            ContextBudgetRejectionReason.CONTEXT_REQUIRED_OVERFLOW.value,
        ),
        (
            TelemetryMetric.CONTEXT_BUDGET_VIOLATIONS_TOTAL,
            ContextBudgetViolationReason.REPORTED_USAGE_EXCEEDED_RESERVATION.value,
        ),
    ],
)
def test_reason_is_closed_and_metric_specific(
    label_policy: TelemetryLabelPolicy,
    metric: TelemetryMetric,
    reason: str,
) -> None:
    sink = RecordingTelemetrySink(label_policy)
    sink.record(
        metric,
        1,
        labels={"purpose": "ambiguous_routing", "reason": reason},
    )

    with pytest.raises(TelemetryContractError, match="does not match"):
        sink.record(
            TelemetryMetric.CONTEXT_BUDGET_REJECTIONS_TOTAL,
            1,
            labels={
                "purpose": "ambiguous_routing",
                "reason": ContextReductionReason.OPTIONAL_CONTEXT_REMOVED.value,
            },
        )


def test_label_vocabulary_is_closed_and_alias_values_are_deployment_allowlisted(
    label_policy: TelemetryLabelPolicy,
) -> None:
    assert set(TelemetryLabels.model_fields) == {
        "purpose",
        "outcome",
        "direction",
        "stage",
        "usage_source",
        "reason",
        "model_alias",
        "flow_version",
    }
    sink = RecordingTelemetrySink(label_policy)

    with pytest.raises(TelemetryContractError, match="model_alias"):
        sink.record(
            TelemetryMetric.MODEL_CALLS_TOTAL,
            1,
            labels=_model_call_labels(model_alias="unlisted-model"),
        )
    with pytest.raises(TelemetryContractError, match="flow_version"):
        sink.record(
            TelemetryMetric.STATE_SERIALIZED_BYTES,
            128,
            labels={"flow_version": "user-controlled-value"},
        )
    with pytest.raises(TelemetryContractError, match="privacy contract"):
        sink.record(
            TelemetryMetric.MODEL_CALLS_TOTAL,
            1,
            labels=_model_call_labels(purpose="서울역에서 어디로 갈까요"),
        )


def test_deployment_allowlists_and_series_cardinality_are_bounded() -> None:
    with pytest.raises(TelemetryContractError, match="versioned ASCII"):
        TelemetryLabelPolicy(
            allowed_model_aliases=frozenset({"서울역"}),
            allowed_flow_versions=frozenset({"mobility-v1"}),
        )
    with pytest.raises(TelemetryContractError, match="cardinality"):
        TelemetryLabelPolicy(
            allowed_model_aliases=frozenset(f"model-{index}" for index in range(17)),
            allowed_flow_versions=frozenset({"mobility-v1"}),
        )
    with pytest.raises(TelemetryContractError, match="cardinality"):
        TelemetryLabelPolicy(
            allowed_model_aliases=frozenset({"router-small"}),
            allowed_flow_versions=frozenset(f"flow-{index}" for index in range(17)),
        )

    maximum = TelemetryLabelPolicy(
        allowed_model_aliases=frozenset(f"model-{index}" for index in range(16)),
        allowed_flow_versions=frozenset(f"flow-{index}" for index in range(16)),
    )
    assert maximum.max_series(TelemetryMetric.MODEL_CALLS_TOTAL) == 448
    assert maximum.max_series(TelemetryMetric.MODEL_TOKENS_TOTAL) == 192
    assert all(maximum.max_series(metric) <= 512 for metric in TelemetryMetric)
    assert maximum.max_total_series() == 672


def test_metric_name_must_be_allowlisted(label_policy: TelemetryLabelPolicy) -> None:
    with pytest.raises(TelemetryContractError, match="metric name"):
        RecordingTelemetrySink(label_policy).record(
            "question_from_user",  # type: ignore[arg-type]
            1,
            labels={},
        )


@pytest.mark.parametrize("value", [-1, True, math.inf, math.nan, "1"])
def test_sinks_reject_invalid_metric_values(
    label_policy: TelemetryLabelPolicy,
    value: object,
) -> None:
    with pytest.raises(TelemetryContractError):
        RecordingTelemetrySink(label_policy).record(
            TelemetryMetric.STATE_SERIALIZED_BYTES,
            value,  # type: ignore[arg-type]
            labels={"flow_version": "mobility-v1"},
        )


def test_unsafe_label_value_never_reaches_exception_cause_or_traceback(
    label_policy: TelemetryLabelPolicy,
) -> None:
    sentinel = "PRIVATE-서울역-휠체어"

    with pytest.raises(TelemetryContractError) as exc_info:
        RecordingTelemetrySink(label_policy).record(
            TelemetryMetric.MODEL_CALLS_TOTAL,
            1,
            labels=_model_call_labels(purpose=sentinel),
        )
    rendered = "".join(traceback.format_exception(exc_info.value))
    assert exc_info.value.__cause__ is None
    assert sentinel not in str(exc_info.value)
    assert sentinel not in rendered


class _FailingExporter:
    def export(self, record: TelemetryRecord) -> None:
        raise RuntimeError(f"export failed for {record.value}")


class _RecordingExporter:
    def __init__(self) -> None:
        self.records: list[TelemetryRecord] = []

    def export(self, record: TelemetryRecord) -> None:
        self.records.append(record)


def test_failure_isolated_sink_drops_validation_and_exporter_failures(
    label_policy: TelemetryLabelPolicy,
) -> None:
    failing = FailureIsolatedTelemetrySink(label_policy, _FailingExporter())
    failing.record(
        TelemetryMetric.MODEL_CALLS_TOTAL,
        1,
        labels=_model_call_labels(),
    )
    failing.record(
        TelemetryMetric.MODEL_CALLS_TOTAL,
        1,
        labels={"question": "PRIVATE-서울역"},
    )
    assert failing.dropped_records == 2

    exporter = _RecordingExporter()
    healthy = FailureIsolatedTelemetrySink(label_policy, exporter)
    healthy.record(
        TelemetryMetric.STATE_SERIALIZED_BYTES,
        128,
        labels={"flow_version": "mobility-v1"},
    )
    assert healthy.dropped_records == 0
    assert len(exporter.records) == 1
