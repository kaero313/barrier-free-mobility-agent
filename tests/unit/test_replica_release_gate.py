from __future__ import annotations

from collections.abc import Mapping, Sequence

import pytest
from pydantic import ValidationError

from barrier_free_mobility_agent.operations.replica_release_gate import (
    RELEASE_REPLICA_RELEASE_PROFILE,
    SMOKE_REPLICA_RELEASE_PROFILE,
    ReplicaReleaseDisruptionEvidence,
    ReplicaReleaseDisruptionKind,
    ReplicaReleaseEvidenceAccumulator,
    ReplicaReleaseFinalEvidence,
    ReplicaReleaseGateProfile,
    ReplicaReleaseProfileName,
    ReplicaReleaseQueueSnapshot,
    ReplicaReleaseViolationCode,
    ReplicaReleaseWarningCode,
    evaluate_replica_release_gate,
    get_replica_release_gate_profile,
)


def _healthy_final() -> ReplicaReleaseFinalEvidence:
    return ReplicaReleaseFinalEvidence(
        drain_convergence_seconds=30,
        ready_worker_count=2,
        queued_job_count=0,
        leased_job_count=0,
        expired_lease_count=0,
        stuck_job_count=0,
        retained_terminal_ciphertext_count=0,
        duplicate_terminal_count=0,
        ambiguous_recovery_count=0,
    )


def _passing_accumulator(
    profile: ReplicaReleaseGateProfile,
) -> ReplicaReleaseEvidenceAccumulator:
    accumulator = ReplicaReleaseEvidenceAccumulator(profile)
    accumulator.record_workflows(profile.workflow_target)
    accumulator.record_queue_snapshot(
        ReplicaReleaseQueueSnapshot(
            ready_worker_count=2,
            queued_job_count=1,
            leased_job_count=1,
            expired_lease_count=0,
            recovery_attempt_count=0,
            stuck_job_count=0,
            oldest_queued_age_seconds=1.5,
        )
    )
    for _ in range(profile.worker_roll_target):
        accumulator.record_disruption(
            ReplicaReleaseDisruptionEvidence(
                kind=ReplicaReleaseDisruptionKind.WORKER_ROLL,
                recovery_convergence_seconds=20,
                minimum_ready_worker_count=1,
            )
        )
    for _ in range(profile.agent_recreate_target):
        accumulator.record_disruption(
            ReplicaReleaseDisruptionEvidence(
                kind=ReplicaReleaseDisruptionKind.AGENT_RECREATE,
                recovery_convergence_seconds=10,
                minimum_ready_worker_count=2,
            )
        )
    for _ in range(profile.postgres_outage_target):
        accumulator.record_disruption(
            ReplicaReleaseDisruptionEvidence(
                kind=ReplicaReleaseDisruptionKind.POSTGRES_OUTAGE,
                recovery_convergence_seconds=45,
                minimum_ready_worker_count=0,
            )
        )
    return accumulator


def test_canonical_profiles_are_fixed_to_one_agent_and_two_workers() -> None:
    smoke = get_replica_release_gate_profile(ReplicaReleaseProfileName.SMOKE)
    release = get_replica_release_gate_profile(ReplicaReleaseProfileName.RELEASE)

    assert smoke is SMOKE_REPLICA_RELEASE_PROFILE
    assert release is RELEASE_REPLICA_RELEASE_PROFILE
    assert smoke.agent_replica_target == release.agent_replica_target == 1
    assert smoke.worker_replica_target == release.worker_replica_target == 2
    assert smoke.release_eligible is False
    assert release.release_eligible is True
    assert release.duration_seconds == 14_400
    assert release.workflow_target == 600
    assert smoke.workload_concurrency == 2
    assert smoke.workload_batch_interval_seconds == 5
    assert release.workload_concurrency == 4
    assert release.workload_batch_interval_seconds == 20
    assert release.worker_roll_target == 12
    assert release.agent_recreate_target == 6
    assert release.postgres_outage_target == 3
    assert release.queue_warning_seconds == 15
    assert release.queue_failure_seconds == 60
    assert release.stuck_after_seconds == 120
    assert release.recovery_convergence_seconds == 90
    assert release.drain_timeout_seconds == 120


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("duration_seconds", 14_399),
        ("workflow_target", 599),
        ("workload_concurrency", 3),
        ("worker_roll_target", 11),
        ("agent_recreate_target", 5),
        ("postgres_outage_target", 2),
    ],
)
def test_release_profile_rejects_lower_targets(field: str, value: int) -> None:
    values = RELEASE_REPLICA_RELEASE_PROFILE.model_dump()
    values[field] = value

    with pytest.raises(ValidationError, match=field):
        ReplicaReleaseGateProfile.model_validate(values)


def test_profile_contract_rejects_replica_and_budget_changes() -> None:
    release = RELEASE_REPLICA_RELEASE_PROFILE.model_dump()

    for field, value in (
        ("agent_replica_target", 2),
        ("worker_replica_target", 1),
        ("queue_warning_seconds", 14),
        ("queue_failure_seconds", 59),
        ("stuck_after_seconds", 119),
        ("recovery_convergence_seconds", 89),
        ("drain_timeout_seconds", 119),
        ("workload_batch_interval_seconds", 21),
    ):
        invalid = {**release, field: value}
        with pytest.raises(ValidationError):
            ReplicaReleaseGateProfile.model_validate(invalid)


def test_smoke_can_pass_without_becoming_release_eligible() -> None:
    profile = SMOKE_REPLICA_RELEASE_PROFILE
    evidence = _passing_accumulator(profile).build(
        observed_duration_seconds=profile.duration_seconds,
        final=_healthy_final(),
    )

    decision = evaluate_replica_release_gate(profile, evidence)

    assert decision.passed is True
    assert decision.release_eligible is False
    assert decision.violation_codes == ()
    assert decision.warning_codes == (ReplicaReleaseWarningCode.SMOKE_PROFILE_NOT_RELEASE_ELIGIBLE,)


def test_release_profile_can_produce_an_eligible_decision() -> None:
    profile = RELEASE_REPLICA_RELEASE_PROFILE
    evidence = _passing_accumulator(profile).build(
        observed_duration_seconds=profile.duration_seconds,
        final=_healthy_final(),
    )

    decision = evaluate_replica_release_gate(profile, evidence)

    assert decision.passed is True
    assert decision.release_eligible is True
    assert decision.violation_codes == ()
    assert decision.warning_codes == ()


def test_queue_warning_is_non_failing_but_failure_and_stuck_are_blocking() -> None:
    profile = SMOKE_REPLICA_RELEASE_PROFILE
    warning_accumulator = _passing_accumulator(profile)
    warning_accumulator.record_queue_snapshot(
        ReplicaReleaseQueueSnapshot(
            ready_worker_count=2,
            queued_job_count=1,
            leased_job_count=0,
            expired_lease_count=0,
            recovery_attempt_count=0,
            stuck_job_count=0,
            oldest_queued_age_seconds=15.1,
        )
    )
    warning = evaluate_replica_release_gate(
        profile,
        warning_accumulator.build(
            observed_duration_seconds=profile.duration_seconds,
            final=_healthy_final(),
        ),
    )
    assert warning.passed is True
    assert ReplicaReleaseWarningCode.QUEUE_AGE_WARNING in warning.warning_codes

    failing_accumulator = _passing_accumulator(profile)
    failing_accumulator.record_queue_snapshot(
        ReplicaReleaseQueueSnapshot(
            ready_worker_count=1,
            queued_job_count=1,
            leased_job_count=0,
            expired_lease_count=1,
            recovery_attempt_count=1,
            stuck_job_count=1,
            oldest_queued_age_seconds=60.1,
        )
    )
    failing = evaluate_replica_release_gate(
        profile,
        failing_accumulator.build(
            observed_duration_seconds=profile.duration_seconds,
            final=_healthy_final(),
        ),
    )
    assert failing.passed is False
    assert ReplicaReleaseViolationCode.QUEUE_AGE_BUDGET_EXCEEDED in failing.violation_codes
    assert ReplicaReleaseViolationCode.STUCK_JOB_OBSERVED in failing.violation_codes
    assert ReplicaReleaseViolationCode.EXPIRED_LEASE_OBSERVED in failing.violation_codes


def test_slow_or_unavailable_non_database_recovery_is_blocking() -> None:
    profile = SMOKE_REPLICA_RELEASE_PROFILE
    accumulator = _passing_accumulator(profile)
    accumulator.record_disruption(
        ReplicaReleaseDisruptionEvidence(
            kind=ReplicaReleaseDisruptionKind.WORKER_ROLL,
            recovery_convergence_seconds=90.1,
            minimum_ready_worker_count=0,
        )
    )
    accumulator.record_unexpected_recoveries(1)

    decision = evaluate_replica_release_gate(
        profile,
        accumulator.build(
            observed_duration_seconds=profile.duration_seconds,
            final=_healthy_final(),
        ),
    )

    assert decision.passed is False
    assert (
        ReplicaReleaseViolationCode.RECOVERY_CONVERGENCE_BUDGET_EXCEEDED in decision.violation_codes
    )
    assert ReplicaReleaseViolationCode.NON_DATABASE_READINESS_LOSS in decision.violation_codes
    assert ReplicaReleaseViolationCode.UNEXPECTED_RECOVERY_OBSERVED in decision.violation_codes


def test_drain_budget_is_release_blocking() -> None:
    profile = SMOKE_REPLICA_RELEASE_PROFILE
    values = _healthy_final().model_dump()
    values["drain_convergence_seconds"] = 120.1
    evidence = _passing_accumulator(profile).build(
        observed_duration_seconds=profile.duration_seconds,
        final=ReplicaReleaseFinalEvidence.model_validate(values),
    )

    decision = evaluate_replica_release_gate(profile, evidence)

    assert decision.passed is False
    assert ReplicaReleaseViolationCode.DRAIN_BUDGET_EXCEEDED in decision.violation_codes


@pytest.mark.parametrize(
    ("field", "code"),
    [
        ("ready_worker_count", ReplicaReleaseViolationCode.READY_WORKER_TARGET_NOT_MET),
        ("queued_job_count", ReplicaReleaseViolationCode.QUEUED_JOB_REMAINS),
        ("leased_job_count", ReplicaReleaseViolationCode.LEASED_JOB_REMAINS),
        ("expired_lease_count", ReplicaReleaseViolationCode.EXPIRED_LEASE_REMAINS),
        ("stuck_job_count", ReplicaReleaseViolationCode.STUCK_JOB_REMAINS),
        (
            "retained_terminal_ciphertext_count",
            ReplicaReleaseViolationCode.TERMINAL_CIPHERTEXT_REMAINS,
        ),
        ("duplicate_terminal_count", ReplicaReleaseViolationCode.DUPLICATE_TERMINAL_OBSERVED),
        ("ambiguous_recovery_count", ReplicaReleaseViolationCode.AMBIGUOUS_RECOVERY_OBSERVED),
    ],
)
def test_final_aggregate_invariants_are_release_blocking(
    field: str,
    code: ReplicaReleaseViolationCode,
) -> None:
    profile = SMOKE_REPLICA_RELEASE_PROFILE
    values = _healthy_final().model_dump()
    values[field] = 1
    final = ReplicaReleaseFinalEvidence.model_validate(values)
    evidence = _passing_accumulator(profile).build(
        observed_duration_seconds=profile.duration_seconds,
        final=final,
    )

    decision = evaluate_replica_release_gate(profile, evidence)

    assert decision.passed is False
    assert code in decision.violation_codes


def test_decision_is_closed_redacted_and_json_compatible() -> None:
    profile = SMOKE_REPLICA_RELEASE_PROFILE
    decision = evaluate_replica_release_gate(
        profile,
        _passing_accumulator(profile).build(
            observed_duration_seconds=profile.duration_seconds,
            final=_healthy_final(),
        ),
    )

    document = decision.model_dump(mode="json")
    forbidden_exact_keys = {
        "id",
        "job_id",
        "run_id",
        "thread_id",
        "owner_id",
        "question",
        "answer",
        "payload",
        "ciphertext",
        "token",
    }

    assert not (_recursive_keys(document) & forbidden_exact_keys)
    assert decision.model_dump_json()
    with pytest.raises(ValidationError):
        ReplicaReleaseQueueSnapshot.model_validate(
            {
                "ready_worker_count": 2,
                "queued_job_count": 0,
                "leased_job_count": 0,
                "expired_lease_count": 0,
                "recovery_attempt_count": 0,
                "stuck_job_count": 0,
                "oldest_queued_age_seconds": None,
                "question": "sensitive",
            }
        )


def _recursive_keys(value: object) -> set[str]:
    if isinstance(value, Mapping):
        return set(value) | {
            nested_key for nested in value.values() for nested_key in _recursive_keys(nested)
        }
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return {nested_key for nested in value for nested_key in _recursive_keys(nested)}
    return set()
