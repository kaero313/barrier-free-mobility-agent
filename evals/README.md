# Offline model evaluation data policy

The corpus in `evals/corpus/` is synthetic-only test data. Real user questions,
conversation exports, production traces, provider request/response bodies, owner
identifiers, and secrets must never be copied into this directory.

`manifest.v1.json` enforces the `synthetic_only` classification, the corpus content
hash, the production token-counter version, and one aggregate HMAC binding over the
production model invocations. Individual prompt fingerprints and canonical request
bytes are deliberately absent from reports and adapter DTOs.

Every admitted case supplies only a synthetic intent/confidence recording plus
bounded wire accounting facts. Admission, prompt/schema assembly, structured-output
validation, missing-field projection, token counting, and tool policy are executed by
the production components during evaluation. Updating any of those contracts requires
an explicit corpus review and manifest regeneration.

## Hosted candidate evaluation

The OpenAI Responses candidate is deliberately disconnected from the product graph.
Its immutable contract and numeric gates live in `evals/hosted/candidates.v1.json`.
Ordinary CI uses mocks and the recorded baseline only; it never needs a provider key.

Live evaluation is manual and sends only the synthetic corpus. It requires an explicit
confirmation, a provider-request ceiling, a worst-case spend ceiling, and
`OPENAI_API_KEY`. The current three-run ceiling is 516 provider requests and the
versioned worst-case spend floor is USD 0.5878272, so an example invocation is:

```powershell
$env:OPENAI_API_KEY = "<secret>"
uv run python scripts/evaluate_openai_candidate.py `
  --confirm-live-api `
  --max-provider-requests 516 `
  --max-spend-usd 0.60 `
  --pretty
```

The command prints aggregate JSON only. Passing the gate does not activate the model;
a second comparable candidate and a separate graph-activation decision are required.
