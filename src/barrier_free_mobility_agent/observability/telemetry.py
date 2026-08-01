from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from threading import Lock
from typing import Protocol

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from barrier_free_mobility_agent.application.model_boundary import ModelPurpose
from barrier_free_mobility_agent.domain.usage import ContextReductionReason, UsageSource

_DEPLOYMENT_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_MAX_MODEL_ALIASES = 16
_MAX_FLOW_VERSIONS = 16
_MAX_SERIES_PER_METRIC = 512
_MAX_TOTAL_SERIES = 1_024


class TelemetryContractError(ValueError):
    """Raised before an unsafe or unbounded telemetry record reaches an exporter."""


class TelemetryMetric(StrEnum):
    MODEL_CALLS_TOTAL = "agent_model_calls_total"
    MODEL_TOKENS_TOTAL = "agent_model_tokens_total"
    CONTEXT_REDUCTIONS_TOTAL = "agent_context_reductions_total"
    CONTEXT_BUDGET_REJECTIONS_TOTAL = "agent_context_budget_rejections_total"
    CONTEXT_BUDGET_VIOLATIONS_TOTAL = "agent_context_budget_violations_total"
    CONTEXT_INPUT_TOKENS = "agent_context_input_tokens"
    STATE_SERIALIZED_BYTES = "agent_state_serialized_bytes"


class TelemetryOutcome(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    SETTLED = "settled"
    FAILED_SETTLED = "failed_settled"
    UNKNOWN_CHARGED = "unknown_charged"
    REJECTED = "rejected"
    SKIPPED = "skipped"


class TelemetryDirection(StrEnum):
    INPUT = "input"
    OUTPUT = "output"
    TOTAL = "total"


class TelemetryStage(StrEnum):
    PREFLIGHT = "preflight"
    BEFORE_REDUCTION = "before_reduction"
    AFTER_REDUCTION = "after_reduction"
    REPORTED = "reported"
    STATE = "state"


class ContextBudgetRejectionReason(StrEnum):
    CONTEXT_REQUIRED_OVERFLOW = "context_required_overflow"
    CALL_LIMIT_EXHAUSTED = "call_limit_exhausted"
    RUN_TOKEN_LIMIT_EXHAUSTED = "run_token_limit_exhausted"
    COUNTER_UNAVAILABLE = "counter_unavailable"
    PROFILE_INVALID = "profile_invalid"
    RESERVATION_MISMATCH = "reservation_mismatch"


class ContextBudgetViolationReason(StrEnum):
    REPORTED_USAGE_EXCEEDED_RESERVATION = "reported_usage_exceeded_reservation"
    RUN_TOKEN_BUDGET_EXCEEDED = "run_token_budget_exceeded"


@dataclass(frozen=True, slots=True)
class _MetricLabelContract:
    required: frozenset[str]
    optional: frozenset[str] = frozenset()
    reasons: frozenset[str] = frozenset()


_METRIC_LABEL_CONTRACTS: dict[TelemetryMetric, _MetricLabelContract] = {
    TelemetryMetric.MODEL_CALLS_TOTAL: _MetricLabelContract(
        required=frozenset({"purpose", "outcome", "usage_source", "model_alias"})
    ),
    TelemetryMetric.MODEL_TOKENS_TOTAL: _MetricLabelContract(
        required=frozenset({"purpose", "direction", "usage_source", "model_alias"})
    ),
    TelemetryMetric.CONTEXT_REDUCTIONS_TOTAL: _MetricLabelContract(
        required=frozenset({"purpose", "reason"}),
        reasons=frozenset(item.value for item in ContextReductionReason),
    ),
    TelemetryMetric.CONTEXT_BUDGET_REJECTIONS_TOTAL: _MetricLabelContract(
        required=frozenset({"purpose", "reason"}),
        reasons=frozenset(item.value for item in ContextBudgetRejectionReason),
    ),
    TelemetryMetric.CONTEXT_BUDGET_VIOLATIONS_TOTAL: _MetricLabelContract(
        required=frozenset({"purpose", "reason"}),
        reasons=frozenset(item.value for item in ContextBudgetViolationReason),
    ),
    TelemetryMetric.CONTEXT_INPUT_TOKENS: _MetricLabelContract(
        required=frozenset({"purpose", "stage"})
    ),
    TelemetryMetric.STATE_SERIALIZED_BYTES: _MetricLabelContract(
        required=frozenset({"flow_version"})
    ),
}


class TelemetryLabels(BaseModel):
    """The complete low-cardinality label vocabulary approved by ADR-0006."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    purpose: str | None = None
    outcome: str | None = None
    direction: str | None = None
    stage: str | None = None
    usage_source: str | None = None
    reason: str | None = None
    model_alias: str | None = None
    flow_version: str | None = None

    @field_validator("purpose")
    @classmethod
    def validate_purpose(cls, value: str | None) -> str | None:
        return _allow_enum(value, ModelPurpose, "purpose")

    @field_validator("outcome")
    @classmethod
    def validate_outcome(cls, value: str | None) -> str | None:
        return _allow_enum(value, TelemetryOutcome, "outcome")

    @field_validator("direction")
    @classmethod
    def validate_direction(cls, value: str | None) -> str | None:
        return _allow_enum(value, TelemetryDirection, "direction")

    @field_validator("stage")
    @classmethod
    def validate_stage(cls, value: str | None) -> str | None:
        return _allow_enum(value, TelemetryStage, "stage")

    @field_validator("usage_source")
    @classmethod
    def validate_usage_source(cls, value: str | None) -> str | None:
        return _allow_enum(value, UsageSource, "usage_source")

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str | None) -> str | None:
        if value is None:
            return None
        allowed = {
            *(item.value for item in ContextReductionReason),
            *(item.value for item in ContextBudgetRejectionReason),
            *(item.value for item in ContextBudgetViolationReason),
        }
        if value not in allowed:
            raise ValueError("Telemetry reason is not allowlisted.")
        return value


def _allow_enum(
    value: str | None,
    enum_type: type[StrEnum],
    label_name: str,
) -> str | None:
    if value is None:
        return None
    if value not in {item.value for item in enum_type}:
        raise ValueError(f"Telemetry {label_name} is not allowlisted.")
    return value


@dataclass(frozen=True, slots=True)
class TelemetryLabelPolicy:
    allowed_model_aliases: frozenset[str]
    allowed_flow_versions: frozenset[str]

    def __post_init__(self) -> None:
        if len(self.allowed_model_aliases) > _MAX_MODEL_ALIASES:
            raise TelemetryContractError("Telemetry model alias cardinality exceeds its limit.")
        if len(self.allowed_flow_versions) > _MAX_FLOW_VERSIONS:
            raise TelemetryContractError("Telemetry flow version cardinality exceeds its limit.")
        for value in (*self.allowed_model_aliases, *self.allowed_flow_versions):
            if type(value) is not str or _DEPLOYMENT_LABEL.fullmatch(value) is None:
                raise TelemetryContractError(
                    "Telemetry deployment allowlists accept versioned ASCII labels only."
                )
        for metric in TelemetryMetric:
            if self.max_series(metric) > _MAX_SERIES_PER_METRIC:
                raise TelemetryContractError("Telemetry metric cardinality exceeds its limit.")
        if self.max_total_series() > _MAX_TOTAL_SERIES:
            raise TelemetryContractError("Telemetry total cardinality exceeds its limit.")

    def validate(
        self,
        metric: TelemetryMetric,
        labels: Mapping[str, str],
    ) -> tuple[tuple[str, str], ...]:
        _prevalidate_label_mapping(labels)
        contract = _METRIC_LABEL_CONTRACTS[metric]
        provided = frozenset(labels)
        if contract.required.difference(provided) or provided.difference(
            contract.required | contract.optional
        ):
            raise TelemetryContractError("Telemetry labels do not match the metric contract.")
        try:
            validated = TelemetryLabels.model_validate(dict(labels))
        except ValidationError:
            raise TelemetryContractError("Telemetry labels violate the privacy contract.") from None

        if any(getattr(validated, name) is None for name in validated.model_fields_set):
            raise TelemetryContractError("Telemetry labels must not contain null values.")

        if validated.reason is not None and validated.reason not in contract.reasons:
            raise TelemetryContractError("Telemetry reason does not match the metric contract.")

        if (
            validated.model_alias is not None
            and validated.model_alias not in self.allowed_model_aliases
        ):
            raise TelemetryContractError("Telemetry model_alias is not deployment-allowlisted.")
        if (
            validated.flow_version is not None
            and validated.flow_version not in self.allowed_flow_versions
        ):
            raise TelemetryContractError("Telemetry flow_version is not deployment-allowlisted.")

        safe = validated.model_dump(mode="json", exclude_none=True)
        return tuple(sorted((name, str(value)) for name, value in safe.items()))

    def max_series(self, metric: TelemetryMetric) -> int:
        if not isinstance(metric, TelemetryMetric):
            raise TelemetryContractError("Telemetry metric name is not allowlisted.")
        contract = _METRIC_LABEL_CONTRACTS[metric]
        cardinality = 1
        for name in contract.required:
            cardinality *= self._label_cardinality(name, contract)
        for name in contract.optional:
            cardinality *= self._label_cardinality(name, contract) + 1
        return cardinality

    def max_total_series(self) -> int:
        return sum(self.max_series(metric) for metric in TelemetryMetric)

    def _label_cardinality(
        self,
        name: str,
        contract: _MetricLabelContract,
    ) -> int:
        fixed: dict[str, int] = {
            "purpose": len(ModelPurpose),
            "outcome": len(TelemetryOutcome),
            "direction": len(TelemetryDirection),
            "stage": len(TelemetryStage),
            "usage_source": len(UsageSource),
            "reason": len(contract.reasons),
            "model_alias": len(self.allowed_model_aliases),
            "flow_version": len(self.allowed_flow_versions),
        }
        return fixed[name]


def _prevalidate_label_mapping(labels: Mapping[str, str]) -> None:
    allowed_names = frozenset(TelemetryLabels.model_fields)
    if len(labels) > len(allowed_names):
        raise TelemetryContractError("Telemetry labels violate the privacy contract.")
    for name, value in labels.items():
        if type(name) is not str or name not in allowed_names or type(value) is not str:
            raise TelemetryContractError("Telemetry labels violate the privacy contract.")


@dataclass(frozen=True, slots=True)
class TelemetryRecord:
    metric: TelemetryMetric
    value: int | float
    labels: tuple[tuple[str, str], ...]


class TelemetryExporter(Protocol):
    def export(self, record: TelemetryRecord) -> None: ...


class TelemetrySink(Protocol):
    def record(
        self,
        metric: TelemetryMetric,
        value: int | float,
        *,
        labels: Mapping[str, str],
    ) -> None: ...


class NoopTelemetrySink:
    """Validate every record while intentionally exporting nothing."""

    def __init__(self, policy: TelemetryLabelPolicy) -> None:
        self._policy = policy

    def record(
        self,
        metric: TelemetryMetric,
        value: int | float,
        *,
        labels: Mapping[str, str],
    ) -> None:
        _validated_record(self._policy, metric, value, labels)


class RecordingTelemetrySink:
    """Thread-safe fake sink used by unit, graph and future exporter contract tests."""

    def __init__(self, policy: TelemetryLabelPolicy) -> None:
        self._policy = policy
        self._records: list[TelemetryRecord] = []
        self._lock = Lock()

    @property
    def records(self) -> tuple[TelemetryRecord, ...]:
        with self._lock:
            return tuple(self._records)

    def record(
        self,
        metric: TelemetryMetric,
        value: int | float,
        *,
        labels: Mapping[str, str],
    ) -> None:
        item = _validated_record(self._policy, metric, value, labels)
        with self._lock:
            self._records.append(item)


class FailureIsolatedTelemetrySink:
    """Validate then export without allowing telemetry failures to fail a request."""

    def __init__(
        self,
        policy: TelemetryLabelPolicy,
        exporter: TelemetryExporter,
    ) -> None:
        self._policy = policy
        self._exporter = exporter
        self._dropped_records = 0
        self._lock = Lock()

    @property
    def dropped_records(self) -> int:
        with self._lock:
            return self._dropped_records

    def record(
        self,
        metric: TelemetryMetric,
        value: int | float,
        *,
        labels: Mapping[str, str],
    ) -> None:
        try:
            item = _validated_record(self._policy, metric, value, labels)
            self._exporter.export(item)
        except Exception:
            # Telemetry is non-authoritative. Do not log the rejected record or exception:
            # either can carry user-controlled values from a faulty adapter.
            with self._lock:
                self._dropped_records += 1


def _validated_record(
    policy: TelemetryLabelPolicy,
    metric: TelemetryMetric,
    value: int | float,
    labels: Mapping[str, str],
) -> TelemetryRecord:
    if not isinstance(metric, TelemetryMetric):
        raise TelemetryContractError("Telemetry metric name is not allowlisted.")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TelemetryContractError("Telemetry value must be a number, not a boolean.")
    if value < 0 or (isinstance(value, float) and not math.isfinite(value)):
        raise TelemetryContractError("Telemetry value must be finite and non-negative.")
    return TelemetryRecord(
        metric=metric,
        value=value,
        labels=policy.validate(metric, labels),
    )
