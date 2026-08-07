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

Both hosted candidates are deliberately disconnected from the product graph. Their
immutable contracts and numeric gates live in `evals/hosted/`. Ordinary CI uses mocks
and the recorded baseline only; it never needs a provider key.

Live evaluation is manual and sends only the synthetic corpus. Each run requires an
explicit confirmation, a provider-request ceiling, a worst-case spend ceiling, and the
provider key. Both candidates admit the same 86 corpus cases over three repetitions, so
both need 516 provider requests; only the versioned worst-case spend floor differs.

Keep provider keys in a gitignored `.env.eval` rather than the application `.env`, so an
evaluation credential is never loaded into the Agent or worker runtime:

```powershell
uv run --env-file .env.eval python scripts/evaluate_openai_candidate.py `
  --confirm-live-api --max-provider-requests 516 --max-spend-usd 0.60 --pretty

uv run --env-file .env.eval python scripts/evaluate_gemini_candidate.py `
  --confirm-live-api --max-provider-requests 516 --max-spend-usd 0.73 --pretty
```

| candidate | manifest | key | worst-case spend floor (USD) |
|---|---|---|---:|
| `openai-gpt-5.4-nano-routing.v1` | `candidates.v1.json` | `OPENAI_API_KEY` | 0.5878272 |
| `google-gemini-3.1-flash-lite-routing.v1` | `candidates.gemini.v1.json` | `GEMINI_API_KEY` | 0.726528 |

The command prints aggregate JSON only. Passing the gate does not activate the model; a
comparison across both candidates and a separate graph-activation decision are required.

### Gemini contract notes

Two provider limitations are recorded rather than hidden, because the OpenAI contract
does not share them:

- Google publishes no dated snapshot for `gemini-3.1-flash-lite`, so the contract pins a
  moving stable alias. Identity drift is still checked against the echoed `modelVersion`,
  but a measured result carries no build-level guarantee.
- Thinking cannot be disabled on this model; its floor is `thinking_level: minimal`, so
  the contract records `reasoning_effort: low` instead of claiming `none`.

`reasoning_tokens_in_output_cap` is asserted from measurement rather than documentation:
at `maxOutputTokens=48` the provider returned 42 thought tokens plus 1 visible token and
stopped with `MAX_TOKENS`. Thought tokens bill at the output rate, so the adapter adds
them to the charged output and fails closed with `GEMINI_OUTPUT_CAP_MISMATCH` if the sum
ever exceeds the reserve.

The adapter targets `:generateContent` rather than the newer Interactions API. The
Interactions surface has no `countTokens`, and exact pre-call counting is required for
admission; on the `generateContent` surface, `countTokens(generateContentRequest)`
returns exactly the `promptTokenCount` the provider later reports and bills.
