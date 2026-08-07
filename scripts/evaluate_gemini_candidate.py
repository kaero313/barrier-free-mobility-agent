from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

from pydantic import SecretStr

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from evals.hosted_evaluation import (  # noqa: E402
    HostedEvaluationError,
    evaluate_hosted_candidate,
    load_hosted_candidate,
)
from evals.model_evaluation import (  # noqa: E402
    EvaluationDataError,
    ProductionEvaluationHarness,
    load_corpus,
)

from barrier_free_mobility_agent.integrations.models.contracts import (  # noqa: E402
    GEMINI_3_1_FLASH_LITE_CONTRACT,
)
from barrier_free_mobility_agent.integrations.models.gemini_generate_content import (  # noqa: E402
    GeminiStructuredModel,
)

_USD_NANOS = Decimal(1_000_000_000)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the protected live Gemini routing-candidate evaluation.",
    )
    parser.add_argument(
        "--corpus-manifest",
        type=Path,
        default=_REPOSITORY_ROOT / "evals/corpus/manifest.v1.json",
    )
    parser.add_argument(
        "--candidate-manifest",
        type=Path,
        default=_REPOSITORY_ROOT / "evals/hosted/candidates.gemini.v1.json",
    )
    parser.add_argument("--max-provider-requests", type=int, required=True)
    parser.add_argument("--max-spend-usd", required=True)
    parser.add_argument(
        "--confirm-live-api",
        action="store_true",
        help="Acknowledge that synthetic prompts will be sent to a paid hosted API.",
    )
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    parser.add_argument(
        "--generate-interval-seconds",
        type=float,
        default=0.0,
        help=(
            "Minimum spacing between generate requests. The free tier allows 15 "
            "generateContent requests per minute, so 4.2 keeps a run inside that quota."
        ),
    )
    parser.add_argument("--pretty", action="store_true")
    return parser


def _usd_nanos(value: str) -> int:
    try:
        parsed = Decimal(value)
        nanos = parsed * _USD_NANOS
    except (InvalidOperation, ValueError):
        raise HostedEvaluationError("HOSTED_SPEND_LIMIT_INVALID") from None
    if not parsed.is_finite() or parsed <= 0 or nanos != nanos.to_integral_value():
        raise HostedEvaluationError("HOSTED_SPEND_LIMIT_INVALID")
    result = int(nanos)
    if result > 1_000_000_000_000:
        raise HostedEvaluationError("HOSTED_SPEND_LIMIT_INVALID")
    return result


async def _run(arguments: argparse.Namespace) -> int:
    model: GeminiStructuredModel | None = None
    try:
        if not arguments.confirm_live_api:
            raise HostedEvaluationError("HOSTED_LIVE_CONFIRMATION_REQUIRED")
        if arguments.max_provider_requests <= 0:
            raise HostedEvaluationError("HOSTED_REQUEST_LIMIT_INVALID")
        maximum_spend = _usd_nanos(arguments.max_spend_usd)
        bundle = load_corpus(arguments.corpus_manifest.resolve())
        candidate = load_hosted_candidate(arguments.candidate_manifest.resolve())
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise HostedEvaluationError("GEMINI_API_KEY_REQUIRED")
        contract = GEMINI_3_1_FLASH_LITE_CONTRACT
        harness = ProductionEvaluationHarness(
            profile=contract.model_profile(),
            max_output_tokens=candidate.output_reserve_tokens,
        )
        model = GeminiStructuredModel.from_api_key(
            SecretStr(api_key),
            timeout_seconds=arguments.timeout_seconds,
            contract=contract,
        )
        report = await evaluate_hosted_candidate(
            bundle,
            model,
            harness=harness,
            candidate=candidate,
            contract=contract,
            maximum_provider_requests=arguments.max_provider_requests,
            maximum_spend_usd_nanos=maximum_spend,
            minimum_generate_interval_seconds=arguments.generate_interval_seconds,
        )
        print(
            json.dumps(
                report.model_dump(mode="json"),
                ensure_ascii=True,
                indent=2 if arguments.pretty else None,
                sort_keys=True,
                separators=None if arguments.pretty else (",", ":"),
            )
        )
        return 0 if report.passed else 1
    except HostedEvaluationError as error:
        print(json.dumps({"error": {"code": error.safe_error_code}}, sort_keys=True))
        return 2
    except EvaluationDataError as error:
        print(json.dumps({"error": {"code": error.code}}, sort_keys=True))
        return 2
    except (TypeError, ValueError):
        print(json.dumps({"error": {"code": "HOSTED_CONFIGURATION_INVALID"}}, sort_keys=True))
        return 2
    finally:
        if model is not None:
            await model.aclose()


def main() -> None:
    raise SystemExit(asyncio.run(_run(_parser().parse_args())))


if __name__ == "__main__":
    main()
