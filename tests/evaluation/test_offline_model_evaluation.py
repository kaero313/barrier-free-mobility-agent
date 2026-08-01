from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

import evals.model_evaluation as evaluation  # noqa: E402
from evals.model_evaluation import (  # noqa: E402
    EXPECTED_GROUP_COUNTS,
    EXPECTED_TOTAL_CASES,
    CorpusBundle,
    CorpusGroup,
    EvaluationDataError,
    NormalizedRecording,
    ProductionEvaluationHarness,
    RecordedDecision,
    RecordedStructuredModel,
    RecordingMismatchError,
    ScriptedStructuredModel,
    evaluate_corpus,
    load_corpus,
)

from barrier_free_mobility_agent.agent.policy import ToolPolicy  # noqa: E402
from barrier_free_mobility_agent.application.model_boundary import (  # noqa: E402
    FallbackTokenCounter,
    ModelFailure,
    ModelFailureKind,
    ModelPurposeAdmission,
    ModelPurposeDecision,
    ModelPurposePolicy,
    ModelResult,
    PurposeDecisionReason,
    TokenCount,
    TokenCountKind,
)
from barrier_free_mobility_agent.domain.model_routing import (  # noqa: E402
    StructuredIntentDecision,
)
from barrier_free_mobility_agent.domain.routing import (  # noqa: E402
    AgentIntent,
    DecisionSource,
    ExecutionPlan,
    IntentDecision,
)

_MANIFEST_PATH = _REPOSITORY_ROOT / "evals/corpus/manifest.v1.json"


@pytest.fixture(scope="module")
def corpus() -> CorpusBundle:
    return load_corpus(_MANIFEST_PATH)


def _first_admitted(bundle: CorpusBundle) -> int:
    return next(index for index, case in enumerate(bundle.cases) if case.admission_expected)


async def _baseline_report(
    bundle: CorpusBundle,
) -> tuple[evaluation.EvaluationReport, RecordedStructuredModel]:
    harness = ProductionEvaluationHarness()
    adapter = RecordedStructuredModel.from_cases(bundle.cases, harness)
    return await evaluate_corpus(bundle, adapter, harness=harness), adapter


def test_versioned_corpus_has_required_synthetic_shape_and_narrow_recordings(
    corpus: CorpusBundle,
) -> None:
    counts = {group: sum(case.group == group for case in corpus.cases) for group in CorpusGroup}

    assert corpus.manifest.data_classification == "synthetic_only"
    assert len(corpus.cases) == EXPECTED_TOTAL_CASES == 128
    assert counts == EXPECTED_GROUP_COUNTS
    assert len({case.case_id for case in corpus.cases}) == len(corpus.cases)
    assert sum(case.admission_expected for case in corpus.cases) == 86
    assert all(case.recording is None for case in corpus.cases if not case.admission_expected)
    assert all(
        set(case.recording.decision.model_dump()) == {"intent", "confidence"}
        for case in corpus.cases
        if case.recording is not None
    )


@pytest.mark.asyncio
async def test_recorded_baseline_executes_production_boundary_and_reports_aggregates_only(
    corpus: CorpusBundle,
) -> None:
    report, adapter = await _baseline_report(corpus)
    rendered = json.dumps(report.model_dump(mode="json"), ensure_ascii=False)
    first = corpus.cases[_first_admitted(corpus)]
    harness = ProductionEvaluationHarness()
    invocation = harness.prepare(first).assembly.invocation
    direct_adapter = RecordedStructuredModel.from_cases(corpus.cases, harness)
    direct_result = await direct_adapter.invoke_structured(invocation)

    assert report.passed
    assert report.exact_accuracy == 1.0
    assert report.admission_mismatches == 0
    assert report.undercount_cases == 0
    assert report.output_cap_mismatches == 0
    assert adapter.call_count == report.admitted_calls == 86
    assert isinstance(direct_result, ModelResult)
    assert isinstance(direct_result.value, StructuredIntentDecision)
    assert "current_input" not in rendered
    assert "서울역" not in rendered
    assert "fingerprint" not in rendered
    assert "canonical_bytes" not in rendered
    assert "production_invocation_binding" in rendered
    assert corpus.manifest.production_invocation_binding not in rendered


@pytest.mark.asyncio
async def test_release_gates_detect_wire_token_output_cap_and_decision_drift(
    corpus: CorpusBundle,
) -> None:
    index = _first_admitted(corpus)
    original = corpus.cases[index]
    assert original.recording is not None
    bad_recording = original.recording.model_copy(
        update={
            "decision": RecordedDecision(intent=AgentIntent.UNKNOWN, confidence=0.8),
            "truth_input_tokens": 1_000_000,
            "wire_max_output_tokens": original.recording.wire_max_output_tokens + 1,
        }
    )
    cases = list(corpus.cases)
    cases[index] = original.model_copy(update={"recording": bad_recording})
    modified = CorpusBundle(manifest=corpus.manifest, cases=tuple(cases))
    harness = ProductionEvaluationHarness()

    report = await evaluate_corpus(
        modified,
        RecordedStructuredModel.from_cases(modified.cases, harness),
        harness=harness,
    )

    assert not report.passed
    assert report.undercount_cases == 1
    assert report.output_cap_mismatches == 1
    assert report.release_blocker_failures == 1
    assert not report.gates["tokenizer_undercount_zero"]
    assert not report.gates["output_cap_mapping_exact"]


@pytest.mark.asyncio
async def test_scripted_adapter_returns_production_failure_without_raw_payload(
    corpus: CorpusBundle,
) -> None:
    case = corpus.cases[_first_admitted(corpus)]
    failure = ModelFailure(
        kind=ModelFailureKind.TIMEOUT,
        safe_error_code="MODEL_TIMEOUT",
    )
    harness = ProductionEvaluationHarness()
    adapter = ScriptedStructuredModel.from_cases_with_overrides(
        corpus.cases,
        harness,
        overrides={case.case_id: failure},
    )

    report = await evaluate_corpus(corpus, adapter, harness=harness)

    assert not report.passed
    assert report.structured_failures == 1
    assert not report.gates["structured_output_100_percent"]


@pytest.mark.asyncio
async def test_recorded_adapter_fails_closed_on_production_invocation_drift(
    corpus: CorpusBundle,
) -> None:
    case = corpus.cases[_first_admitted(corpus)]
    harness = ProductionEvaluationHarness()
    invocation = harness.prepare(case).assembly.invocation
    adapter = RecordedStructuredModel.from_cases((case,), harness)
    drifted = invocation.model_copy(update={"max_output_tokens": invocation.max_output_tokens - 1})

    with pytest.raises(RecordingMismatchError, match="not_found"):
        await adapter.invoke_structured(drifted)


def test_recording_schema_cannot_store_projected_fields_raw_provider_body_or_secret(
    corpus: CorpusBundle,
) -> None:
    case = corpus.cases[_first_admitted(corpus)]
    assert case.recording is not None
    payload = case.recording.model_dump(mode="json")
    payload["decision"]["missing_fields"] = ["origin"]
    payload["raw_provider_body"] = "PRIVATE-RAW-PROVIDER-BODY"
    payload["api_key"] = "PRIVATE-API-KEY"

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        NormalizedRecording.model_validate(payload)


def test_manifest_hash_mismatch_returns_only_safe_error_code(
    corpus: CorpusBundle,
    tmp_path: Path,
) -> None:
    corpus_path = _MANIFEST_PATH.parent / corpus.manifest.file
    copied_corpus = tmp_path / corpus.manifest.file
    copied_corpus.write_bytes(corpus_path.read_bytes())
    bad_manifest = corpus.manifest.model_copy(update={"sha256": "0" * 64})
    manifest_path = tmp_path / "manifest.v1.json"
    manifest_path.write_text(bad_manifest.model_dump_json(), encoding="utf-8")

    with pytest.raises(EvaluationDataError) as exc_info:
        load_corpus(manifest_path)

    assert str(exc_info.value) == "EVALUATION_CORPUS_HASH_MISMATCH"
    assert "서울역" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_production_prompt_drift_breaks_the_manifest_gate(
    corpus: CorpusBundle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import barrier_free_mobility_agent.application.model_routing as production_routing

    monkeypatch.setattr(production_routing, "_SYSTEM_POLICY", "drifted synthetic policy")

    report, _ = await _baseline_report(corpus)

    assert not report.passed
    assert not report.gates["production_invocation_binding"]


@pytest.mark.asyncio
async def test_production_response_schema_drift_breaks_the_manifest_gate(
    corpus: CorpusBundle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = StructuredIntentDecision.model_json_schema

    def drifted_schema(
        cls: type[StructuredIntentDecision], *args: Any, **kwargs: Any
    ) -> dict[str, Any]:
        schema = original(*args, **kwargs)
        return {**schema, "x-synthetic-schema-drift": True}

    monkeypatch.setattr(
        StructuredIntentDecision,
        "model_json_schema",
        classmethod(drifted_schema),
    )

    report, _ = await _baseline_report(corpus)

    assert not report.passed
    assert not report.gates["production_invocation_binding"]


@pytest.mark.asyncio
async def test_production_admission_policy_drift_breaks_the_gate(
    corpus: CorpusBundle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = ModelPurposePolicy.evaluate

    def deny_allowed(
        self: ModelPurposePolicy,
        admission: ModelPurposeAdmission,
    ) -> ModelPurposeDecision:
        decision = original(self, admission)
        if not decision.allowed:
            return decision
        return ModelPurposeDecision(
            allowed=False,
            reason=PurposeDecisionReason.ROUTE_NOT_AMBIGUOUS,
            purpose=decision.purpose,
            policy_version=decision.policy_version,
        )

    monkeypatch.setattr(ModelPurposePolicy, "evaluate", deny_allowed)

    report, _ = await _baseline_report(corpus)

    assert not report.passed
    assert report.admission_mismatches == 86
    assert not report.gates["production_admission_matches"]


@pytest.mark.asyncio
async def test_production_counter_drift_breaks_the_gate(
    corpus: CorpusBundle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def zero_count(
        self: FallbackTokenCounter,
        invocation: Any,
    ) -> TokenCount:
        return TokenCount(
            input_tokens=0,
            kind=TokenCountKind.ESTIMATED,
            counter_version="drifted-counter-v1",
            canonical_bytes=0,
            logical_blocks=1,
        )

    monkeypatch.setattr(FallbackTokenCounter, "count_input", zero_count)

    report, _ = await _baseline_report(corpus)

    assert not report.passed
    assert report.undercount_cases == 86
    assert not report.gates["tokenizer_undercount_zero"]


@pytest.mark.asyncio
async def test_production_counter_version_drift_breaks_the_gate(
    corpus: CorpusBundle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FallbackTokenCounter, "version", "fallback-v2")

    report, _ = await _baseline_report(corpus)

    assert not report.passed
    assert not report.gates["production_counter_version"]


@pytest.mark.asyncio
async def test_production_projector_drift_breaks_the_gate(
    corpus: CorpusBundle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unknown_projection(*args: Any, **kwargs: Any) -> IntentDecision:
        return IntentDecision(
            intent=AgentIntent.UNKNOWN,
            source=DecisionSource.MODEL,
            confidence=0,
        )

    monkeypatch.setattr(evaluation, "project_structured_intent", unknown_projection)

    report, _ = await _baseline_report(corpus)

    assert not report.passed
    assert report.exact_accuracy < 0.95
    assert not report.gates["release_blocker_failures_zero"]


@pytest.mark.asyncio
async def test_production_tool_policy_drift_breaks_the_gate(
    corpus: CorpusBundle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = ToolPolicy.build_plan

    def remove_allowed_tools(
        self: ToolPolicy,
        decision: IntentDecision,
    ) -> ExecutionPlan:
        plan = original(self, decision)
        return plan.model_copy(
            update={
                "allowed_tools": (),
                "tool_limits": {},
                "max_tool_calls": 0,
            }
        )

    monkeypatch.setattr(ToolPolicy, "build_plan", remove_allowed_tools)

    report, _ = await _baseline_report(corpus)

    assert not report.passed
    assert report.policy_projection_failures > 0
    assert not report.gates["tool_policy_projection_exact"]


def test_cli_check_is_credential_free_and_outputs_only_aggregate_json(
    corpus: CorpusBundle,
) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(_REPOSITORY_ROOT / "scripts/evaluate_structured_models.py"),
            "--manifest",
            str(_MANIFEST_PATH),
            "--check",
        ],
        cwd=_REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )

    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report["passed"] is True
    assert report["total_cases"] == 128
    assert "current_input" not in completed.stdout
    assert "서울역" not in completed.stdout
    assert "fingerprint" not in completed.stdout
    assert "canonical_bytes" not in completed.stdout
    assert corpus.manifest.production_invocation_binding not in completed.stdout
    assert "PRIVATE" not in completed.stdout
