from __future__ import annotations

from typing import Annotated, Literal, NoReturn, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from barrier_free_mobility_agent.domain.errors import (
    ClarificationAnswerValidationError,
)
from barrier_free_mobility_agent.domain.models import (
    ClarificationControl,
    ClarificationField,
    ClarificationOption,
    ClarificationPrompt,
    ClarificationView,
)

CLARIFICATION_FIELD_TEXT_MAX_CHARS = 2_000
CLARIFICATION_SINGLE_TEXT_MAX_CHARS = 20_000
CLARIFICATION_MAX_INPUTS = 16


class ClarificationInputOption(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    value: str = Field(min_length=1, max_length=200)
    label: str = Field(min_length=1, max_length=200)


class TextClarificationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=100)
    prompt_id: str = Field(min_length=1, max_length=200)
    field: ClarificationField
    control: Literal["text"] = "text"
    label: str = Field(min_length=1, max_length=100)
    required: Literal[True] = True
    min_length: Literal[1] = 1
    max_length: int = Field(
        default=CLARIFICATION_FIELD_TEXT_MAX_CHARS,
        ge=1,
        le=CLARIFICATION_SINGLE_TEXT_MAX_CHARS,
    )


class TriStateClarificationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=100)
    prompt_id: str = Field(min_length=1, max_length=200)
    field: ClarificationField
    control: Literal["tri_state"] = "tri_state"
    label: str = Field(min_length=1, max_length=100)
    required: Literal[True] = True
    options: tuple[ClarificationInputOption, ...] = Field(min_length=3, max_length=3)

    @model_validator(mode="after")
    def validate_options(self) -> Self:
        if tuple(option.value for option in self.options) != ("yes", "no", "unknown"):
            raise ValueError("Tri-state options must be yes, no, and unknown in order.")
        return self


class ChoiceClarificationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=100)
    prompt_id: str = Field(min_length=1, max_length=200)
    field: ClarificationField
    control: Literal["single_choice"] = "single_choice"
    label: str = Field(min_length=1, max_length=100)
    required: Literal[True] = True
    options: tuple[ClarificationInputOption, ...] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def validate_options(self) -> Self:
        values = tuple(option.value for option in self.options)
        if len(set(values)) != len(values):
            raise ValueError("Choice option values must be unique.")
        return self


ClarificationInput = Annotated[
    TextClarificationInput | TriStateClarificationInput | ChoiceClarificationInput,
    Field(discriminator="control"),
]


class SingleTextAnswerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["single_text"] = "single_text"
    label: str = Field(default="추가 정보", min_length=1, max_length=100)
    min_length: Literal[1] = 1
    max_length: int = Field(
        default=CLARIFICATION_SINGLE_TEXT_MAX_CHARS,
        ge=1,
        le=CLARIFICATION_SINGLE_TEXT_MAX_CHARS,
    )


class FieldsAnswerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["fields"] = "fields"
    inputs: tuple[ClarificationInput, ...] = Field(
        min_length=1,
        max_length=CLARIFICATION_MAX_INPUTS,
    )


ClarificationAnswerRequest = Annotated[
    SingleTextAnswerRequest | FieldsAnswerRequest,
    Field(discriminator="mode"),
]


class StructuredClarificationView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["2"] = "2"
    id: str = Field(min_length=1, max_length=200)
    prompts: tuple[ClarificationPrompt, ...] = Field(min_length=1)
    answer_request: ClarificationAnswerRequest

    @model_validator(mode="after")
    def validate_references(self) -> Self:
        prompt_ids = tuple(prompt.id for prompt in self.prompts)
        if len(set(prompt_ids)) != len(prompt_ids):
            raise ValueError("Clarification prompt IDs must be unique.")
        if isinstance(self.answer_request, FieldsAnswerRequest):
            input_ids = tuple(item.id for item in self.answer_request.inputs)
            if len(set(input_ids)) != len(input_ids):
                raise ValueError("Clarification input IDs must be unique.")
            prompts = {prompt.id: prompt for prompt in self.prompts}
            for item in self.answer_request.inputs:
                prompt = prompts.get(item.prompt_id)
                if prompt is None or item.field not in prompt.fields:
                    raise ValueError("Clarification input must reference its owning prompt.")
        return self


class TextClarificationAnswerValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["text"] = "text"
    value: str = Field(min_length=1, max_length=CLARIFICATION_FIELD_TEXT_MAX_CHARS)

    @field_validator("value")
    @classmethod
    def normalize_value(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Text answer must not be blank.")
        return normalized


class ChoiceClarificationAnswerValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["choice"] = "choice"
    value: str = Field(min_length=1, max_length=200)


class TriStateClarificationAnswerValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["tri_state"] = "tri_state"
    value: Literal["yes", "no", "unknown"]


ClarificationAnswerValue = Annotated[
    TextClarificationAnswerValue
    | ChoiceClarificationAnswerValue
    | TriStateClarificationAnswerValue,
    Field(discriminator="kind"),
]


class ClarificationFieldAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    input_id: str = Field(min_length=1, max_length=100)
    value: ClarificationAnswerValue


class SingleTextClarificationAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["single_text"] = "single_text"
    value: str = Field(min_length=1, max_length=CLARIFICATION_SINGLE_TEXT_MAX_CHARS)

    @field_validator("value")
    @classmethod
    def normalize_value(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Clarification answer must not be blank.")
        return normalized


class FieldsClarificationAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["fields"] = "fields"
    values: tuple[ClarificationFieldAnswer, ...] = Field(
        min_length=1,
        max_length=CLARIFICATION_MAX_INPUTS,
    )


StructuredClarificationAnswer = Annotated[
    SingleTextClarificationAnswer | FieldsClarificationAnswer,
    Field(discriminator="mode"),
]


_TEXT_FIELDS = {
    ClarificationField.ORIGIN: "출발지",
    ClarificationField.DESTINATION: "도착지",
}
_TRI_STATE_FIELDS = {
    ClarificationField.CAN_USE_STAIRS: "계단 이용 가능 여부",
    ClarificationField.CAN_USE_ESCALATOR: "에스컬레이터 이용 가능 여부",
}
_TRI_STATE_OPTIONS = (
    ClarificationInputOption(value="yes", label="이용할 수 있어요"),
    ClarificationInputOption(value="no", label="이용할 수 없어요"),
    ClarificationInputOption(value="unknown", label="잘 모르겠어요"),
)


def project_structured_clarification(
    clarification: ClarificationView,
) -> StructuredClarificationView:
    """Project a persisted v1 view into the opt-in v2 answer contract."""

    if clarification.id.startswith("business-"):
        return _single_text_view(clarification)

    if clarification.control == ClarificationControl.CHOICE:
        prompt = clarification.prompts[0]
        field = prompt.fields[0] if len(prompt.fields) == 1 else ClarificationField.OTHER
        if field not in _TEXT_FIELDS or not clarification.options:
            return _single_text_view(clarification)
        options = tuple(
            ClarificationInputOption(value=f"option-{index}", label=option.label[:200])
            for index, option in enumerate(clarification.options[:20], start=1)
        )
        return StructuredClarificationView(
            id=clarification.id,
            prompts=clarification.prompts,
            answer_request=FieldsAnswerRequest(
                inputs=(
                    ChoiceClarificationInput(
                        id=field.value,
                        prompt_id=prompt.id,
                        field=field,
                        label=_TEXT_FIELDS[field],
                        options=options,
                    ),
                )
            ),
        )

    inputs: list[ClarificationInput] = []
    seen_fields: set[ClarificationField] = set()
    for prompt in clarification.prompts:
        if not prompt.fields:
            return _single_text_view(clarification)
        for field in prompt.fields:
            if field in seen_fields:
                return _single_text_view(clarification)
            seen_fields.add(field)
            if field in _TEXT_FIELDS:
                inputs.append(
                    TextClarificationInput(
                        id=field.value,
                        prompt_id=prompt.id,
                        field=field,
                        label=_TEXT_FIELDS[field],
                    )
                )
            elif field in _TRI_STATE_FIELDS:
                inputs.append(
                    TriStateClarificationInput(
                        id=field.value,
                        prompt_id=prompt.id,
                        field=field,
                        label=_TRI_STATE_FIELDS[field],
                        options=_TRI_STATE_OPTIONS,
                    )
                )
            else:
                return _single_text_view(clarification)
    if not inputs:
        return _single_text_view(clarification)
    return StructuredClarificationView(
        id=clarification.id,
        prompts=clarification.prompts,
        answer_request=FieldsAnswerRequest(inputs=tuple(inputs)),
    )


def validate_structured_answer(
    clarification: StructuredClarificationView,
    answer: SingleTextClarificationAnswer | FieldsClarificationAnswer,
) -> None:
    """Validate the submitted shape against the latest active server-owned contract."""

    request = clarification.answer_request
    if isinstance(request, SingleTextAnswerRequest):
        if not isinstance(answer, SingleTextClarificationAnswer):
            _raise_issue(
                "clarification_answer_type_mismatch",
                ("body", "answer", "mode"),
                "현재 추가 질문에는 하나의 텍스트 답변이 필요합니다.",
            )
        if len(answer.value) > request.max_length:
            _raise_issue(
                "clarification_answer_too_long",
                ("body", "answer", "value"),
                "추가 정보가 허용된 길이를 초과했습니다.",
            )
        return

    if not isinstance(answer, FieldsClarificationAnswer):
        _raise_issue(
            "clarification_answer_type_mismatch",
            ("body", "answer", "mode"),
            "현재 추가 질문에는 필드별 답변이 필요합니다.",
        )

    expected = {item.id: item for item in request.inputs}
    submitted: dict[str, ClarificationFieldAnswer] = {}
    issues: list[dict[str, object]] = []
    for item in answer.values:
        if item.input_id in submitted:
            issues.append(
                _issue(
                    "clarification_answer_duplicate",
                    ("body", "answer", "values", item.input_id),
                    "같은 입력에 대한 답변이 중복되었습니다.",
                )
            )
            continue
        submitted[item.input_id] = item

    for missing in expected.keys() - submitted.keys():
        issues.append(
            _issue(
                "clarification_answer_missing",
                ("body", "answer", "values", missing),
                "필수 답변이 누락되었습니다.",
            )
        )
    for unexpected in submitted.keys() - expected.keys():
        issues.append(
            _issue(
                "clarification_answer_unexpected",
                ("body", "answer", "values", unexpected),
                "현재 추가 질문에 없는 입력입니다.",
            )
        )

    for input_id in expected.keys() & submitted.keys():
        expected_input = expected[input_id]
        value = submitted[input_id].value
        expected_kind = {
            "text": "text",
            "tri_state": "tri_state",
            "single_choice": "choice",
        }[expected_input.control]
        if value.kind != expected_kind:
            issues.append(
                _issue(
                    "clarification_answer_type_mismatch",
                    ("body", "answer", "values", input_id),
                    "입력에 맞지 않는 답변 형식입니다.",
                )
            )
            continue
        if isinstance(expected_input, TextClarificationInput) and isinstance(
            value, TextClarificationAnswerValue
        ):
            if len(value.value) > expected_input.max_length:
                issues.append(
                    _issue(
                        "clarification_answer_too_long",
                        ("body", "answer", "values", input_id),
                        "입력값이 허용된 길이를 초과했습니다.",
                    )
                )
        elif isinstance(expected_input, ChoiceClarificationInput) and isinstance(
            value, ChoiceClarificationAnswerValue
        ):
            valid_options = {option.value for option in expected_input.options}
            if value.value not in valid_options:
                issues.append(
                    _issue(
                        "clarification_answer_invalid_option",
                        ("body", "answer", "values", input_id),
                        "현재 선택할 수 없는 항목입니다.",
                    )
                )

    if issues:
        raise ClarificationAnswerValidationError(issues=tuple(issues))


def legacy_choice_for_token(
    clarification: ClarificationView,
    token: str,
) -> ClarificationOption | None:
    if not token.startswith("option-"):
        return None
    raw_index = token.removeprefix("option-")
    if not raw_index.isdigit():
        return None
    index = int(raw_index) - 1
    if index < 0 or index >= len(clarification.options):
        return None
    return clarification.options[index]


def _single_text_view(clarification: ClarificationView) -> StructuredClarificationView:
    return StructuredClarificationView(
        id=clarification.id,
        prompts=clarification.prompts,
        answer_request=SingleTextAnswerRequest(),
    )


def _raise_issue(issue_type: str, location: tuple[str, ...], message: str) -> NoReturn:
    raise ClarificationAnswerValidationError(issues=(_issue(issue_type, location, message),))


def _issue(issue_type: str, location: tuple[str, ...], message: str) -> dict[str, object]:
    return {"type": issue_type, "loc": location, "msg": message}
