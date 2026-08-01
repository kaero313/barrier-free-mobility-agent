"""Pure Phase 4B-3C replica release-gate profiles and budget evaluation.

The models in this module intentionally accept aggregate counters only. Runtime
orchestration belongs to an external runner, while this module remains deterministic,
serializable, and safe to use for release evidence without carrying user or execution
identifiers.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

REPLICA_RELEASE_GATE_VERSION = "replica-release-gate/v1"

_SMOKE_DURATION_SECONDS = 600
_SMOKE_WORKFLOW_TARGET = 30
_SMOKE_WORKLOAD_CONCURRENCY = 2
_SMOKE_WORKLOAD_BATCH_INTERVAL_SECONDS = 5
_SMOKE_WORKER_ROLL_TARGET = 3
_SMOKE_AGENT_RECREATE_TARGET = 2
_SMOKE_POSTGRES_OUTAGE_TARGET = 1

_RELEASE_DURATION_SECONDS = 4 * 60 * 60
_RELEASE_WORKFLOW_TARGET = 600
_RELEASE_WORKLOAD_CONCURRENCY = 4
_RELEASE_WORKLOAD_BATCH_INTERVAL_SECONDS = 20
_RELEASE_WORKER_ROLL_TARGET = 12
_RELEASE_AGENT_RECREATE_TARGET = 6
_RELEASE_POSTGRES_OUTAGE_TARGET = 3


class ReplicaReleaseProfileName(StrEnum):
    """The bounded, versioned profiles understood by the gate."""

    SMOKE = "smoke"
    RELEASE = "release"


class ReplicaReleaseDisruptionKind(StrEnum):
    """Low-cardinality disruption classes emitted by an external runner."""

    WORKER_ROLL = "worker_roll"
    AGENT_RECREATE = "agent_recreate"
    POSTGRES_OUTAGE = "postgres_outage"


class ReplicaReleaseViolationCode(StrEnum):
    """Stable failure reasons with no user or execution data."""

    PROFILE_MISMATCH = "profile_mismatch"
    DURATION_TARGET_NOT_MET = "duration_target_not_met"
    WORKFLOW_TARGET_NOT_MET = "workflow_target_not_met"
    WORKER_ROLL_TARGET_NOT_MET = "worker_roll_target_not_met"
    AGENT_RECREATE_TARGET_NOT_MET = "agent_recreate_target_not_met"
    POSTGRES_OUTAGE_TARGET_NOT_MET = "postgres_outage_target_not_met"
    QUEUE_EVIDENCE_MISSING = "queue_evidence_missing"
    RECOVERY_EVIDENCE_MISSING = "recovery_evidence_missing"
    QUEUE_AGE_BUDGET_EXCEEDED = "queue_age_budget_exceeded"
    STUCK_JOB_OBSERVED = "stuck_job_observed"
    EXPIRED_LEASE_OBSERVED = "expired_lease_observed"
    RECOVERY_CONVERGENCE_BUDGET_EXCEEDED = "recovery_convergence_budget_exceeded"
    DRAIN_BUDGET_EXCEEDED = "drain_budget_exceeded"
    UNEXPECTED_RECOVERY_OBSERVED = "unexpected_recovery_observed"
    NON_DATABASE_READINESS_LOSS = "non_database_readiness_loss"
    READY_WORKER_TARGET_NOT_MET = "ready_worker_target_not_met"
    QUEUED_JOB_REMAINS = "queued_job_remains"
    LEASED_JOB_REMAINS = "leased_job_remains"
    EXPIRED_LEASE_REMAINS = "expired_lease_remains"
    STUCK_JOB_REMAINS = "stuck_job_remains"
    TERMINAL_CIPHERTEXT_REMAINS = "terminal_ciphertext_remains"
    DUPLICATE_TERMINAL_OBSERVED = "duplicate_terminal_observed"
    AMBIGUOUS_RECOVERY_OBSERVED = "ambiguous_recovery_observed"


class ReplicaReleaseWarningCode(StrEnum):
    """Stable non-failing observations."""

    QUEUE_AGE_WARNING = "queue_age_warning"
    SMOKE_PROFILE_NOT_RELEASE_ELIGIBLE = "smoke_profile_not_release_eligible"


class ReplicaReleaseGateProfile(BaseModel):
    """Immutable execution targets and budgets for a versioned gate profile."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    version: Literal["replica-release-gate/v1"] = "replica-release-gate/v1"
    name: ReplicaReleaseProfileName
    release_eligible: bool
    agent_replica_target: Literal[1] = 1
    worker_replica_target: Literal[2] = 2
    duration_seconds: int = Field(gt=0)
    workflow_target: int = Field(gt=0)
    workload_concurrency: int = Field(gt=0)
    workload_batch_interval_seconds: int = Field(gt=0)
    worker_roll_target: int = Field(ge=0)
    agent_recreate_target: int = Field(ge=0)
    postgres_outage_target: int = Field(ge=0)
    queue_warning_seconds: Literal[15] = 15
    queue_failure_seconds: Literal[60] = 60
    stuck_after_seconds: Literal[120] = 120
    recovery_convergence_seconds: Literal[90] = 90
    drain_timeout_seconds: Literal[120] = 120

    @model_validator(mode="after")
    def validate_profile_floor(self) -> Self:
        if self.name is ReplicaReleaseProfileName.SMOKE:
            if self.release_eligible:
                raise ValueError("the smoke profile cannot be release eligible")
            if (
                self.workload_concurrency != _SMOKE_WORKLOAD_CONCURRENCY
                or self.workload_batch_interval_seconds != _SMOKE_WORKLOAD_BATCH_INTERVAL_SECONDS
            ):
                raise ValueError("the smoke workload shape is immutable")
            return self
        if not self.release_eligible:
            raise ValueError("the release profile must remain release eligible")
        release_minimums = {
            "duration_seconds": _RELEASE_DURATION_SECONDS,
            "workflow_target": _RELEASE_WORKFLOW_TARGET,
            "workload_concurrency": _RELEASE_WORKLOAD_CONCURRENCY,
            "worker_roll_target": _RELEASE_WORKER_ROLL_TARGET,
            "agent_recreate_target": _RELEASE_AGENT_RECREATE_TARGET,
            "postgres_outage_target": _RELEASE_POSTGRES_OUTAGE_TARGET,
        }
        for field_name, minimum in release_minimums.items():
            if getattr(self, field_name) < minimum:
                raise ValueError(f"release profile {field_name} cannot be lower than {minimum}")
        if self.workload_batch_interval_seconds > _RELEASE_WORKLOAD_BATCH_INTERVAL_SECONDS:
            raise ValueError(
                "release profile workload_batch_interval_seconds cannot exceed "
                f"{_RELEASE_WORKLOAD_BATCH_INTERVAL_SECONDS}"
            )
        return self


SMOKE_REPLICA_RELEASE_PROFILE = ReplicaReleaseGateProfile(
    name=ReplicaReleaseProfileName.SMOKE,
    release_eligible=False,
    duration_seconds=_SMOKE_DURATION_SECONDS,
    workflow_target=_SMOKE_WORKFLOW_TARGET,
    workload_concurrency=_SMOKE_WORKLOAD_CONCURRENCY,
    workload_batch_interval_seconds=_SMOKE_WORKLOAD_BATCH_INTERVAL_SECONDS,
    worker_roll_target=_SMOKE_WORKER_ROLL_TARGET,
    agent_recreate_target=_SMOKE_AGENT_RECREATE_TARGET,
    postgres_outage_target=_SMOKE_POSTGRES_OUTAGE_TARGET,
)

RELEASE_REPLICA_RELEASE_PROFILE = ReplicaReleaseGateProfile(
    name=ReplicaReleaseProfileName.RELEASE,
    release_eligible=True,
    duration_seconds=_RELEASE_DURATION_SECONDS,
    workflow_target=_RELEASE_WORKFLOW_TARGET,
    workload_concurrency=_RELEASE_WORKLOAD_CONCURRENCY,
    workload_batch_interval_seconds=_RELEASE_WORKLOAD_BATCH_INTERVAL_SECONDS,
    worker_roll_target=_RELEASE_WORKER_ROLL_TARGET,
    agent_recreate_target=_RELEASE_AGENT_RECREATE_TARGET,
    postgres_outage_target=_RELEASE_POSTGRES_OUTAGE_TARGET,
)


def get_replica_release_gate_profile(
    name: ReplicaReleaseProfileName,
) -> ReplicaReleaseGateProfile:
    """Return the canonical immutable profile for ``name``."""

    if name is ReplicaReleaseProfileName.SMOKE:
        return SMOKE_REPLICA_RELEASE_PROFILE
    return RELEASE_REPLICA_RELEASE_PROFILE


class ReplicaReleaseQueueSnapshot(BaseModel):
    """One redacted queue-health sample from the authoritative database clock."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    ready_worker_count: int = Field(ge=0)
    queued_job_count: int = Field(ge=0)
    leased_job_count: int = Field(ge=0)
    expired_lease_count: int = Field(ge=0)
    recovery_attempt_count: int = Field(ge=0)
    stuck_job_count: int = Field(ge=0)
    stuck_after_seconds: Literal[120] = 120
    oldest_queued_age_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class ReplicaReleaseDisruptionEvidence(BaseModel):
    """Aggregate outcome of one bounded disruption, without per-run correlation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    kind: ReplicaReleaseDisruptionKind
    recovery_convergence_seconds: float = Field(ge=0, allow_inf_nan=False)
    minimum_ready_worker_count: int = Field(ge=0)


class ReplicaReleaseFinalEvidence(BaseModel):
    """Final aggregate invariants required after the bounded drain window."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    drain_convergence_seconds: float = Field(ge=0, allow_inf_nan=False)
    ready_worker_count: int = Field(ge=0)
    queued_job_count: int = Field(ge=0)
    leased_job_count: int = Field(ge=0)
    expired_lease_count: int = Field(ge=0)
    stuck_job_count: int = Field(ge=0)
    retained_terminal_ciphertext_count: int = Field(ge=0)
    duplicate_terminal_count: int = Field(ge=0)
    ambiguous_recovery_count: int = Field(ge=0)


class ReplicaReleaseGateEvidence(BaseModel):
    """Redacted aggregate evidence consumed by the deterministic evaluator."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    version: Literal["replica-release-gate/v1"] = "replica-release-gate/v1"
    profile_name: ReplicaReleaseProfileName
    observed_duration_seconds: int = Field(ge=0)
    observed_workflow_count: int = Field(ge=0)
    completed_worker_roll_count: int = Field(ge=0)
    completed_agent_recreate_count: int = Field(ge=0)
    completed_postgres_outage_count: int = Field(ge=0)
    queue_sample_count: int = Field(ge=0)
    queue_warning_breach_count: int = Field(ge=0)
    queue_failure_breach_count: int = Field(ge=0)
    stuck_observation_count: int = Field(ge=0)
    disruption_observation_count: int = Field(ge=0)
    recovery_convergence_breach_count: int = Field(ge=0)
    unexpected_recovery_count: int = Field(ge=0)
    non_database_readiness_loss_count: int = Field(ge=0)
    max_queue_age_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    max_recovery_convergence_seconds: float | None = Field(
        default=None,
        ge=0,
        allow_inf_nan=False,
    )
    max_queued_job_count: int = Field(ge=0)
    max_leased_job_count: int = Field(ge=0)
    max_expired_lease_count: int = Field(ge=0)
    max_recovery_attempt_count: int = Field(ge=0)
    max_stuck_job_count: int = Field(ge=0)
    final: ReplicaReleaseFinalEvidence


class ReplicaReleaseGateDecision(BaseModel):
    """JSON-safe release decision containing stable codes and aggregate evidence only."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    version: Literal["replica-release-gate/v1"] = "replica-release-gate/v1"
    profile_name: ReplicaReleaseProfileName
    passed: bool
    release_eligible: bool
    violation_codes: tuple[ReplicaReleaseViolationCode, ...]
    warning_codes: tuple[ReplicaReleaseWarningCode, ...]
    evidence: ReplicaReleaseGateEvidence


class ReplicaReleaseEvidenceAccumulator:
    """Accumulate aggregate samples without retaining individual execution data."""

    __slots__ = (
        "_agent_recreates",
        "_disruption_observations",
        "_max_expired_leases",
        "_max_leased_jobs",
        "_max_queue_age",
        "_max_queued_jobs",
        "_max_recovery_attempts",
        "_max_recovery_convergence",
        "_max_stuck_jobs",
        "_non_database_readiness_losses",
        "_postgres_outages",
        "_profile",
        "_queue_failure_breaches",
        "_queue_samples",
        "_queue_warning_breaches",
        "_recovery_convergence_breaches",
        "_stuck_observations",
        "_unexpected_recoveries",
        "_worker_rolls",
        "_workflows",
    )

    def __init__(self, profile: ReplicaReleaseGateProfile) -> None:
        self._profile = profile
        self._workflows = 0
        self._worker_rolls = 0
        self._agent_recreates = 0
        self._postgres_outages = 0
        self._queue_samples = 0
        self._queue_warning_breaches = 0
        self._queue_failure_breaches = 0
        self._stuck_observations = 0
        self._disruption_observations = 0
        self._recovery_convergence_breaches = 0
        self._unexpected_recoveries = 0
        self._non_database_readiness_losses = 0
        self._max_queue_age: float | None = None
        self._max_recovery_convergence: float | None = None
        self._max_queued_jobs = 0
        self._max_leased_jobs = 0
        self._max_expired_leases = 0
        self._max_recovery_attempts = 0
        self._max_stuck_jobs = 0

    def record_workflows(self, count: int = 1) -> None:
        """Record completed representative workflows."""

        if count <= 0:
            raise ValueError("workflow count must be positive")
        self._workflows += count

    def record_queue_snapshot(self, snapshot: ReplicaReleaseQueueSnapshot) -> None:
        """Fold one queue snapshot into low-cardinality extrema and breach counts."""

        self._queue_samples += 1
        queue_age = snapshot.oldest_queued_age_seconds
        if queue_age is not None:
            self._max_queue_age = _optional_max(self._max_queue_age, queue_age)
            if queue_age > self._profile.queue_warning_seconds:
                self._queue_warning_breaches += 1
            if queue_age > self._profile.queue_failure_seconds:
                self._queue_failure_breaches += 1
        if snapshot.stuck_job_count > 0:
            self._stuck_observations += 1
        self._max_queued_jobs = max(self._max_queued_jobs, snapshot.queued_job_count)
        self._max_leased_jobs = max(self._max_leased_jobs, snapshot.leased_job_count)
        self._max_expired_leases = max(
            self._max_expired_leases,
            snapshot.expired_lease_count,
        )
        self._max_recovery_attempts = max(
            self._max_recovery_attempts,
            snapshot.recovery_attempt_count,
        )
        self._max_stuck_jobs = max(self._max_stuck_jobs, snapshot.stuck_job_count)

    def record_disruption(self, evidence: ReplicaReleaseDisruptionEvidence) -> None:
        """Record one completed disruption and its bounded convergence outcome."""

        self._disruption_observations += 1
        if evidence.kind is ReplicaReleaseDisruptionKind.WORKER_ROLL:
            self._worker_rolls += 1
        elif evidence.kind is ReplicaReleaseDisruptionKind.AGENT_RECREATE:
            self._agent_recreates += 1
        else:
            self._postgres_outages += 1
        if (
            evidence.kind is not ReplicaReleaseDisruptionKind.POSTGRES_OUTAGE
            and evidence.minimum_ready_worker_count < 1
        ):
            self._non_database_readiness_losses += 1
        convergence = evidence.recovery_convergence_seconds
        self._max_recovery_convergence = _optional_max(
            self._max_recovery_convergence,
            convergence,
        )
        if convergence > self._profile.recovery_convergence_seconds:
            self._recovery_convergence_breaches += 1

    def record_unexpected_recoveries(self, count: int = 1) -> None:
        """Record recovery attempts that cannot be attributed to an injected disruption."""

        if count <= 0:
            raise ValueError("unexpected recovery count must be positive")
        self._unexpected_recoveries += count

    def build(
        self,
        *,
        observed_duration_seconds: int,
        final: ReplicaReleaseFinalEvidence,
    ) -> ReplicaReleaseGateEvidence:
        """Build immutable, JSON-compatible aggregate evidence."""

        if observed_duration_seconds < 0:
            raise ValueError("observed duration cannot be negative")
        return ReplicaReleaseGateEvidence(
            profile_name=self._profile.name,
            observed_duration_seconds=observed_duration_seconds,
            observed_workflow_count=self._workflows,
            completed_worker_roll_count=self._worker_rolls,
            completed_agent_recreate_count=self._agent_recreates,
            completed_postgres_outage_count=self._postgres_outages,
            queue_sample_count=self._queue_samples,
            queue_warning_breach_count=self._queue_warning_breaches,
            queue_failure_breach_count=self._queue_failure_breaches,
            stuck_observation_count=self._stuck_observations,
            disruption_observation_count=self._disruption_observations,
            recovery_convergence_breach_count=self._recovery_convergence_breaches,
            unexpected_recovery_count=self._unexpected_recoveries,
            non_database_readiness_loss_count=self._non_database_readiness_losses,
            max_queue_age_seconds=self._max_queue_age,
            max_recovery_convergence_seconds=self._max_recovery_convergence,
            max_queued_job_count=self._max_queued_jobs,
            max_leased_job_count=self._max_leased_jobs,
            max_expired_lease_count=self._max_expired_leases,
            max_recovery_attempt_count=self._max_recovery_attempts,
            max_stuck_job_count=self._max_stuck_jobs,
            final=final,
        )


def evaluate_replica_release_gate(
    profile: ReplicaReleaseGateProfile,
    evidence: ReplicaReleaseGateEvidence,
) -> ReplicaReleaseGateDecision:
    """Evaluate aggregate evidence against one immutable release profile."""

    violations: list[ReplicaReleaseViolationCode] = []
    warnings: list[ReplicaReleaseWarningCode] = []

    if evidence.profile_name is not profile.name or evidence.version != profile.version:
        violations.append(ReplicaReleaseViolationCode.PROFILE_MISMATCH)
    _append_if_below(
        violations,
        evidence.observed_duration_seconds,
        profile.duration_seconds,
        ReplicaReleaseViolationCode.DURATION_TARGET_NOT_MET,
    )
    _append_if_below(
        violations,
        evidence.observed_workflow_count,
        profile.workflow_target,
        ReplicaReleaseViolationCode.WORKFLOW_TARGET_NOT_MET,
    )
    _append_if_below(
        violations,
        evidence.completed_worker_roll_count,
        profile.worker_roll_target,
        ReplicaReleaseViolationCode.WORKER_ROLL_TARGET_NOT_MET,
    )
    _append_if_below(
        violations,
        evidence.completed_agent_recreate_count,
        profile.agent_recreate_target,
        ReplicaReleaseViolationCode.AGENT_RECREATE_TARGET_NOT_MET,
    )
    _append_if_below(
        violations,
        evidence.completed_postgres_outage_count,
        profile.postgres_outage_target,
        ReplicaReleaseViolationCode.POSTGRES_OUTAGE_TARGET_NOT_MET,
    )
    if evidence.queue_sample_count == 0:
        violations.append(ReplicaReleaseViolationCode.QUEUE_EVIDENCE_MISSING)
    expected_disruptions = (
        evidence.completed_worker_roll_count
        + evidence.completed_agent_recreate_count
        + evidence.completed_postgres_outage_count
    )
    if evidence.disruption_observation_count < expected_disruptions:
        violations.append(ReplicaReleaseViolationCode.RECOVERY_EVIDENCE_MISSING)
    if evidence.queue_failure_breach_count > 0:
        violations.append(ReplicaReleaseViolationCode.QUEUE_AGE_BUDGET_EXCEEDED)
    elif evidence.queue_warning_breach_count > 0:
        warnings.append(ReplicaReleaseWarningCode.QUEUE_AGE_WARNING)
    if evidence.stuck_observation_count > 0:
        violations.append(ReplicaReleaseViolationCode.STUCK_JOB_OBSERVED)
    if evidence.max_expired_lease_count > 0:
        violations.append(ReplicaReleaseViolationCode.EXPIRED_LEASE_OBSERVED)
    if evidence.recovery_convergence_breach_count > 0:
        violations.append(ReplicaReleaseViolationCode.RECOVERY_CONVERGENCE_BUDGET_EXCEEDED)
    if evidence.unexpected_recovery_count > 0:
        violations.append(ReplicaReleaseViolationCode.UNEXPECTED_RECOVERY_OBSERVED)
    if evidence.non_database_readiness_loss_count > 0:
        violations.append(ReplicaReleaseViolationCode.NON_DATABASE_READINESS_LOSS)

    final = evidence.final
    if final.drain_convergence_seconds > profile.drain_timeout_seconds:
        violations.append(ReplicaReleaseViolationCode.DRAIN_BUDGET_EXCEEDED)
    if final.ready_worker_count != profile.worker_replica_target:
        violations.append(ReplicaReleaseViolationCode.READY_WORKER_TARGET_NOT_MET)
    _append_if_positive(
        violations,
        final.queued_job_count,
        ReplicaReleaseViolationCode.QUEUED_JOB_REMAINS,
    )
    _append_if_positive(
        violations,
        final.leased_job_count,
        ReplicaReleaseViolationCode.LEASED_JOB_REMAINS,
    )
    _append_if_positive(
        violations,
        final.expired_lease_count,
        ReplicaReleaseViolationCode.EXPIRED_LEASE_REMAINS,
    )
    _append_if_positive(
        violations,
        final.stuck_job_count,
        ReplicaReleaseViolationCode.STUCK_JOB_REMAINS,
    )
    _append_if_positive(
        violations,
        final.retained_terminal_ciphertext_count,
        ReplicaReleaseViolationCode.TERMINAL_CIPHERTEXT_REMAINS,
    )
    _append_if_positive(
        violations,
        final.duplicate_terminal_count,
        ReplicaReleaseViolationCode.DUPLICATE_TERMINAL_OBSERVED,
    )
    _append_if_positive(
        violations,
        final.ambiguous_recovery_count,
        ReplicaReleaseViolationCode.AMBIGUOUS_RECOVERY_OBSERVED,
    )

    passed = not violations
    if profile.name is ReplicaReleaseProfileName.SMOKE:
        warnings.append(ReplicaReleaseWarningCode.SMOKE_PROFILE_NOT_RELEASE_ELIGIBLE)
    return ReplicaReleaseGateDecision(
        profile_name=profile.name,
        passed=passed,
        release_eligible=passed and profile.release_eligible,
        violation_codes=tuple(violations),
        warning_codes=tuple(warnings),
        evidence=evidence,
    )


def _optional_max(current: float | None, value: float) -> float:
    return value if current is None else max(current, value)


def _append_if_below(
    violations: list[ReplicaReleaseViolationCode],
    actual: int,
    expected: int,
    code: ReplicaReleaseViolationCode,
) -> None:
    if actual < expected:
        violations.append(code)


def _append_if_positive(
    violations: list[ReplicaReleaseViolationCode],
    value: int,
    code: ReplicaReleaseViolationCode,
) -> None:
    if value > 0:
        violations.append(code)
