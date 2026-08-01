from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from evals.model_evaluation import (  # noqa: E402
    EvaluationDataError,
    ProductionEvaluationHarness,
    RecordedStructuredModel,
    evaluate_corpus,
    load_corpus,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the credential-free structured routing evaluation.",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=_REPOSITORY_ROOT / "evals/corpus/manifest.v1.json",
    )
    parser.add_argument("--minimum-exact-accuracy", type=float, default=0.95)
    parser.add_argument("--pretty", action="store_true")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Return a non-zero status when an offline release gate fails.",
    )
    return parser


async def _run(arguments: argparse.Namespace) -> int:
    try:
        bundle = load_corpus(arguments.manifest.resolve())
        harness = ProductionEvaluationHarness()
        adapter = RecordedStructuredModel.from_cases(bundle.cases, harness)
        report = await evaluate_corpus(
            bundle,
            adapter,
            harness=harness,
            minimum_exact_accuracy=arguments.minimum_exact_accuracy,
        )
    except (EvaluationDataError, ValueError):
        print(json.dumps({"error": "EVALUATION_DATA_INVALID"}, separators=(",", ":")))
        return 2

    print(
        json.dumps(
            report.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            indent=2 if arguments.pretty else None,
            separators=None if arguments.pretty else (",", ":"),
        )
    )
    return 1 if arguments.check and not report.passed else 0


def main() -> int:
    return asyncio.run(_run(_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
