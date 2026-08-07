from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from barrier_free_mobility_agent.application.model_boundary import ModelProfile

Identifier = Annotated[
    str,
    Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$"),
]


class HostedModelContract(BaseModel):
    """Immutable, secret-free identity for one evaluated provider wire contract."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )

    schema_version: Literal["1"] = "1"
    contract_version: Identifier
    provider_alias: Identifier
    model_alias: Identifier
    provider_model_snapshot: Identifier
    provider_api_version: Identifier
    wire_adapter_version: Identifier
    schema_transform_version: Identifier
    counter_version: Identifier
    reasoning_effort: Literal["none", "low", "medium", "high", "xhigh"]
    text_verbosity: Literal["low", "medium", "high"]
    context_window_tokens: int = Field(gt=0, le=2_000_000)
    hard_max_output_tokens: int = Field(gt=0, le=1_000_000)
    reasoning_tokens_in_output_cap: bool
    minimum_application_output_tokens: int = Field(gt=0, le=1_000_000)
    pricing_version: Identifier
    data_policy_version: Identifier

    @model_validator(mode="after")
    def validate_output_contract(self) -> HostedModelContract:
        if self.hard_max_output_tokens >= self.context_window_tokens:
            raise ValueError("hosted output cap must be smaller than the context window")
        if self.minimum_application_output_tokens > self.hard_max_output_tokens:
            raise ValueError("application output minimum exceeds the provider hard cap")
        if not self.reasoning_tokens_in_output_cap:
            raise ValueError("hosted contracts must cap reasoning and visible output together")
        return self

    def model_profile(self) -> ModelProfile:
        return ModelProfile(
            provider_alias=self.provider_alias,
            model_alias=self.model_alias,
            provider_contract_version=self.contract_version,
            context_window_tokens=self.context_window_tokens,
            hard_max_output_tokens=self.hard_max_output_tokens,
            reasoning_tokens_in_output_cap=self.reasoning_tokens_in_output_cap,
        )

    def canonical_sha256(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
        return hashlib.sha256(payload).hexdigest()


OPENAI_GPT_5_4_NANO_CONTRACT = HostedModelContract(
    contract_version="openai.gpt-5.4-nano-2026-03-17.responses.v1",
    provider_alias="openai-responses",
    model_alias="gpt-5.4-nano-2026-03-17",
    provider_model_snapshot="gpt-5.4-nano-2026-03-17",
    provider_api_version="responses-v1",
    wire_adapter_version="openai-responses-wire.v1",
    schema_transform_version="pydantic-json-schema.v1",
    counter_version="openai-responses-input-tokens.v1",
    reasoning_effort="none",
    text_verbosity="low",
    context_window_tokens=400_000,
    hard_max_output_tokens=128_000,
    reasoning_tokens_in_output_cap=True,
    minimum_application_output_tokens=512,
    pricing_version="openai-public-pricing.2026-07-13",
    data_policy_version="openai-api-data-controls.2026-07-13",
)

# Two provider limitations are recorded here rather than hidden, because both weaken
# guarantees that the OpenAI contract can make:
#
# 1. Google publishes no dated snapshot for this model, so ``provider_model_snapshot``
#    pins a moving stable alias. Identity drift is still detected against the echoed
#    ``modelVersion``, but a measured accuracy result has no build-level guarantee.
# 2. Gemini 3 Flash-Lite cannot turn thinking off; its floor is ``thinking_level:
#    minimal``. ``reasoning_effort`` therefore records ``low``, not ``none``.
#
# ``reasoning_tokens_in_output_cap`` is asserted from measurement, not documentation:
# at ``maxOutputTokens=48`` the provider returned 42 thought tokens plus 1 visible
# token and stopped with ``MAX_TOKENS``. The adapter re-checks the invariant on every
# response, so a provider change surfaces as a failure instead of an unbilled overrun.
GEMINI_3_1_FLASH_LITE_CONTRACT = HostedModelContract(
    contract_version="google.gemini-3.1-flash-lite.generate-content.v1",
    provider_alias="google-generative-language",
    model_alias="gemini-3.1-flash-lite",
    provider_model_snapshot="gemini-3.1-flash-lite",
    provider_api_version="v1beta-generate-content",
    wire_adapter_version="gemini-generate-content-wire.v1",
    schema_transform_version="gemini-openapi-subset.v1",
    counter_version="gemini-count-tokens.v1",
    reasoning_effort="low",
    text_verbosity="low",
    context_window_tokens=1_048_576,
    hard_max_output_tokens=65_536,
    reasoning_tokens_in_output_cap=True,
    minimum_application_output_tokens=512,
    pricing_version="google-public-pricing.2026-08-03",
    data_policy_version="google-gemini-api-terms.2026-08-03",
)
