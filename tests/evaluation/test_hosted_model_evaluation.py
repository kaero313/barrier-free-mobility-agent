from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

import scripts.evaluate_openai_candidate as hosted_cli  # noqa: E402
from evals.hosted_evaluation import (  # noqa: E402
    HostedCandidateManifest,
    HostedEvaluationError,
    HostedEvaluationReport,
    evaluate_hosted_candidate,
    load_hosted_candidate,
    required_live_limits,
    validate_candidate_binding,
)
from evals.model_evaluation import (  # noqa: E402
    CorpusBundle,
    EvaluationCase,
    ProductionEvaluationHarness,
    load_corpus,
)

from barrier_free_mobility_agent.application.model_boundary import (  # noqa: E402
    ModelFailure,
    ModelFailureKind,
    ModelInvocation,
    ModelInvocationResult,
    ModelResult,
    TokenCount,
    TokenCountKind,
    canonical_token_blocks,
)
from barrier_free_mobility_agent.domain.model_routing import (  # noqa: E402
    StructuredIntentDecision,
)
from barrier_free_mobility_agent.domain.routing import AgentIntent  # noqa: E402
from barrier_free_mobility_agent.domain.usage import ModelUsage, UsageSource  # noqa: E402
from barrier_free_mobility_agent.integrations.models.contracts import (  # noqa: E402
    OPENAI_GPT_5_4_NANO_CONTRACT,
)

_CORPUS_MANIFEST = _REPOSITORY_ROOT / "evals/corpus/manifest.v1.json"
_CANDIDATE_MANIFEST = _REPOSITORY_ROOT / "evals/hosted/candidates.v1.json"


@pytest.fixture(scope="module")
def corpus() -> CorpusBundle:
    return load_corpus(_CORPUS_MANIFEST)


@pytest.fixture(scope="module")
def candidate() -> HostedCandidateManifest:
    return load_hosted_candidate(_CANDIDATE_MANIFEST)


def _admitted_cases(bundle: CorpusBundle) -> tuple[EvaluationCase, ...]:
    return tuple(case for case in bundle.cases if case.admission_expected)


def _marker(invocation: ModelInvocation[StructuredIntentDecision]) -> str:
    digest = hashlib.sha256()
    for block in canonical_token_blocks(invocation):
        digest.update(len(block).to_bytes(8, "big"))
        digest.update(block)
    digest.update(invocation.max_output_tokens.to_bytes(8, "big"))
    return digest.hexdigest()


class _FakeHostedModel:
    def __init__(
        self,
        bundle: CorpusBundle,
        candidate: HostedCandidateManifest,
        *,
        count_input_tokens: int = 100,
        reported_input_delta: int = 0,
        reported_output_tokens: int = 10,
        usage_source: UsageSource = UsageSource.REPORTED,
        decision_overrides: dict[int, StructuredIntentDecision] | None = None,
        failures: dict[int, ModelFailure] | None = None,
    ) -> None:
        admitted = _admitted_cases(bundle)
        decisions = []
        for _ in range(candidate.required_repetitions):
            for case in admitted:
                assert case.recording is not None
                decisions.append(
                    StructuredIntentDecision(
                        schema_version="1",
                        intent=case.recording.decision.intent,
                        confidence=case.recording.decision.confidence,
                    )
                )
        self._decisions = tuple(decisions)
        self._count_input_tokens = count_input_tokens
        self._reported_input_delta = reported_input_delta
        self._reported_output_tokens = reported_output_tokens
        self._usage_source = usage_source
        self._decision_overrides = decision_overrides or {}
        self._failures = failures or {}
        self.count_markers: list[str] = []
        self.invoke_markers: list[str] = []

    @property
    def version(self) -> str:
        return OPENAI_GPT_5_4_NANO_CONTRACT.counter_version

    async def count_input(
        self,
        invocation: ModelInvocation[StructuredIntentDecision],
    ) -> TokenCount:
        self.count_markers.append(_marker(invocation))
        return TokenCount(
            input_tokens=self._count_input_tokens,
            kind=TokenCountKind.EXACT,
            counter_version=self.version,
            canonical_bytes=1,
            logical_blocks=1,
        )

    async def invoke_structured(
        self,
        invocation: ModelInvocation[StructuredIntentDecision],
        *,
        preflight: TokenCount | None = None,
    ) -> ModelInvocationResult[StructuredIntentDecision]:
        del preflight
        index = len(self.invoke_markers)
        self.invoke_markers.append(_marker(invocation))
        failure = self._failures.get(index)
        if failure is not None:
            return failure
        decision = self._decision_overrides.get(index, self._decisions[index])
        input_tokens = self._count_input_tokens + self._reported_input_delta
        usage = ModelUsage(
            input_tokens=input_tokens,
            output_tokens=self._reported_output_tokens,
            total_tokens=input_tokens + self._reported_output_tokens,
            source=self._usage_source,
            estimator_version=(
                None if self._usage_source == UsageSource.REPORTED else "fake-counter.v1"
            ),
        )
        return ModelResult[StructuredIntentDecision](
            value=decision,
            usage=usage,
            provider_alias=invocation.profile.provider_alias,
            model_alias=invocation.profile.model_alias,
            provider_contract_version=invocation.profile.provider_contract_version,
            prompt_version=invocation.prompt_version,
        )


def _harness(candidate: HostedCandidateManifest) -> ProductionEvaluationHarness:
    return ProductionEvaluationHarness(
        profile=OPENAI_GPT_5_4_NANO_CONTRACT.model_profile(),
        max_output_tokens=candidate.output_reserve_tokens,
    )


async def _evaluate(
    corpus: CorpusBundle,
    candidate: HostedCandidateManifest,
    model: _FakeHostedModel,
) -> HostedEvaluationReport:
    request_limit, spend_limit = required_live_limits(
        candidate,
        admitted_calls=sum(case.admission_expected for case in corpus.cases),
    )
    return await evaluate_hosted_candidate(
        corpus,
        model,
        harness=_harness(candidate),
        candidate=candidate,
        contract=OPENAI_GPT_5_4_NANO_CONTRACT,
        maximum_provider_requests=request_limit,
        maximum_spend_usd_nanos=spend_limit,
    )


def test_candidate_manifest_is_bound_to_exact_contract_and_corpus_hashes(
    corpus: CorpusBundle,
    candidate: HostedCandidateManifest,
) -> None:
    validate_candidate_binding(candidate, OPENAI_GPT_5_4_NANO_CONTRACT, corpus)
    assert (
        candidate.canonical_sha256()
        == "acb378198d6b41e1465210064e2467f8dca8ef2bdaf665bda652e75579d32585"
    )
    assert candidate.contract_sha256 == OPENAI_GPT_5_4_NANO_CONTRACT.canonical_sha256()
    assert candidate.corpus_sha256 == corpus.manifest.sha256

    for drifted in (
        candidate.model_copy(update={"contract_sha256": "0" * 64}),
        candidate.model_copy(update={"corpus_sha256": "0" * 64}),
        candidate.model_copy(update={"provider_model_snapshot": "gpt-5.4-nano-latest"}),
    ):
        with pytest.raises(HostedEvaluationError) as captured:
            validate_candidate_binding(drifted, OPENAI_GPT_5_4_NANO_CONTRACT, corpus)
        assert captured.value.safe_error_code == "HOSTED_CANDIDATE_BINDING_MISMATCH"


def test_candidate_version_cannot_hide_price_or_gate_drift(
    candidate: HostedCandidateManifest,
    tmp_path: Path,
) -> None:
    drifted = candidate.model_copy(
        update={"input_usd_nanos_per_token": candidate.input_usd_nanos_per_token + 1}
    )
    path = tmp_path / "candidate.json"
    path.write_text(drifted.model_dump_json(), encoding="utf-8")

    with pytest.raises(HostedEvaluationError) as captured:
        load_hosted_candidate(path)

    assert captured.value.safe_error_code == "HOSTED_CANDIDATE_HASH_MISMATCH"


@pytest.mark.asyncio
async def test_required_request_and_spend_limits_fail_before_any_provider_call(
    corpus: CorpusBundle,
    candidate: HostedCandidateManifest,
) -> None:
    admitted = sum(case.admission_expected for case in corpus.cases)
    required_requests, required_spend = required_live_limits(
        candidate,
        admitted_calls=admitted,
    )
    model = _FakeHostedModel(corpus, candidate)

    with pytest.raises(HostedEvaluationError) as request_error:
        await evaluate_hosted_candidate(
            corpus,
            model,
            harness=_harness(candidate),
            candidate=candidate,
            contract=OPENAI_GPT_5_4_NANO_CONTRACT,
            maximum_provider_requests=required_requests - 1,
            maximum_spend_usd_nanos=required_spend,
        )
    assert request_error.value.safe_error_code == "HOSTED_REQUEST_LIMIT_TOO_SMALL"
    assert model.count_markers == model.invoke_markers == []

    with pytest.raises(HostedEvaluationError) as spend_error:
        await evaluate_hosted_candidate(
            corpus,
            model,
            harness=_harness(candidate),
            candidate=candidate,
            contract=OPENAI_GPT_5_4_NANO_CONTRACT,
            maximum_provider_requests=required_requests,
            maximum_spend_usd_nanos=required_spend - 1,
        )
    assert spend_error.value.safe_error_code == "HOSTED_SPEND_LIMIT_TOO_SMALL"
    assert model.count_markers == model.invoke_markers == []


@pytest.mark.asyncio
async def test_three_repetitions_reuse_same_admitted_corpus_and_report_aggregates_only(
    corpus: CorpusBundle,
    candidate: HostedCandidateManifest,
) -> None:
    model = _FakeHostedModel(corpus, candidate)
    report = await _evaluate(corpus, candidate, model)
    admitted = len(_admitted_cases(corpus))
    rendered = json.dumps(report.model_dump(mode="json"), ensure_ascii=False)

    assert report.passed
    assert candidate.required_repetitions == len(report.runs) == 3
    assert all(run.admitted_calls == admitted for run in report.runs)
    assert report.provider_requests == admitted * 2 * candidate.required_repetitions
    assert len(model.count_markers) == len(model.invoke_markers) == admitted * 3
    assert model.count_markers == model.invoke_markers
    assert model.count_markers[:admitted] == model.count_markers[admitted : admitted * 2]
    assert model.count_markers[:admitted] == model.count_markers[admitted * 2 :]
    assert len(model.invoke_markers) < len(corpus.cases) * candidate.required_repetitions

    assert "current_input" not in rendered
    assert "messages" not in rendered
    assert "canonical_bytes" not in rendered
    assert "case_id" not in rendered
    assert corpus.manifest.production_invocation_binding not in rendered
    assert all(case.current_input not in rendered for case in corpus.cases)
    assert all(case.case_id not in rendered for case in corpus.cases)


def _fixed_latency_clock(latency_ms: int) -> Callable[[], int]:
    calls = 0

    def clock() -> int:
        nonlocal calls
        pair, phase = divmod(calls, 2)
        calls += 1
        base = pair * (latency_ms + 1) * 1_000_000
        return base + phase * latency_ms * 1_000_000

    return clock


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario", "expected_gate"),
    [
        ("accuracy", "minimum_exact_accuracy"),
        ("latency", "latency_p95_within_budget"),
        ("cost", "mean_cost_within_budget"),
        ("usage", "complete_usage_100_percent"),
        ("undercount", "tokenizer_undercount_zero"),
        ("output-cap", "output_cap_mapping_exact"),
    ],
)
async def test_hosted_release_gates_fail_closed(
    scenario: str,
    expected_gate: str,
    corpus: CorpusBundle,
    candidate: HostedCandidateManifest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = candidate
    model_kwargs: dict[str, object] = {}
    if scenario == "accuracy":
        configured = candidate.model_copy(update={"minimum_exact_accuracy_basis_points": 10_000})
        admitted = _admitted_cases(corpus)
        index = next(
            i
            for i, case in enumerate(admitted)
            if not case.release_blocker and case.expected_intent != AgentIntent.UNKNOWN
        )
        model_kwargs["decision_overrides"] = {
            index: StructuredIntentDecision(
                schema_version="1",
                intent=AgentIntent.UNKNOWN,
                confidence=0.99,
            )
        }
    elif scenario == "latency":
        monkeypatch.setattr(
            "evals.hosted_evaluation.time.perf_counter_ns",
            _fixed_latency_clock(3_001),
        )
    elif scenario == "cost":
        configured = candidate.model_copy(update={"maximum_mean_cost_usd_nanos": 1})
    elif scenario == "usage":
        model_kwargs["usage_source"] = UsageSource.MIXED
    elif scenario == "undercount":
        model_kwargs["reported_input_delta"] = 1
    elif scenario == "output-cap":
        model_kwargs["failures"] = {
            0: ModelFailure(
                kind=ModelFailureKind.PROVIDER_ERROR,
                safe_error_code="OPENAI_OUTPUT_CAP_MISMATCH",
            )
        }

    model = _FakeHostedModel(corpus, configured, **model_kwargs)  # type: ignore[arg-type]
    report = await _evaluate(corpus, configured, model)

    assert not report.passed
    assert any(not run.gates[expected_gate] for run in report.runs)
    if scenario == "undercount":
        assert all(run.undercount_cases == run.admitted_calls for run in report.runs)
    if scenario == "output-cap":
        assert report.runs[0].output_cap_mismatches == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("confirmed", "api_key", "expected_code"),
    [
        (False, "sk-do-not-disclose", "HOSTED_LIVE_CONFIRMATION_REQUIRED"),
        (True, None, "OPENAI_API_KEY_REQUIRED"),
    ],
)
async def test_cli_without_confirmation_or_key_makes_zero_calls_and_prints_safe_code_only(
    confirmed: bool,
    api_key: str | None,
    expected_code: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    constructor_calls = 0

    def unexpected_constructor(*args: object, **kwargs: object) -> None:
        nonlocal constructor_calls
        del args, kwargs
        constructor_calls += 1
        raise AssertionError("hosted client must not be constructed")

    monkeypatch.setattr(
        "scripts.evaluate_openai_candidate.OpenAIResponsesStructuredModel.from_api_key",
        unexpected_constructor,
    )
    if api_key is None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    else:
        monkeypatch.setenv("OPENAI_API_KEY", api_key)
    arguments = argparse.Namespace(
        corpus_manifest=_CORPUS_MANIFEST,
        candidate_manifest=_CANDIDATE_MANIFEST,
        max_provider_requests=1,
        max_spend_usd="1",
        confirm_live_api=confirmed,
        timeout_seconds=1.0,
        pretty=False,
    )

    exit_code = await hosted_cli._run(arguments)
    captured = capsys.readouterr()

    assert exit_code == 2
    assert constructor_calls == 0
    assert captured.err == ""
    assert json.loads(captured.out) == {"error": {"code": expected_code}}
    assert "sk-do-not-disclose" not in captured.out
    assert "current_input" not in captured.out
    assert str(_CORPUS_MANIFEST) not in captured.out
