from __future__ import annotations

import pytest

from barrier_free_mobility_agent.agent.models import (
    ClarificationKind,
    PendingClarification,
    PendingPrompt,
)
from barrier_free_mobility_agent.agent.projections import project_pending_clarification
from barrier_free_mobility_agent.domain.clarification import (
    ChoiceClarificationAnswerValue,
    ClarificationFieldAnswer,
    FieldsAnswerRequest,
    FieldsClarificationAnswer,
    SingleTextAnswerRequest,
    TextClarificationAnswerValue,
    TriStateClarificationAnswerValue,
    TriStateClarificationInput,
    project_structured_clarification,
    validate_structured_answer,
)
from barrier_free_mobility_agent.domain.errors import ClarificationAnswerValidationError
from barrier_free_mobility_agent.domain.models import (
    ClarificationControl,
    ClarificationField,
    ClarificationOption,
    ClarificationPrompt,
    ClarificationView,
)


def _required_fields_view() -> ClarificationView:
    pending = PendingClarification(
        kind=ClarificationKind.REQUIRED_FIELDS,
        fields=("origin", "destination", "can_use_stairs", "can_use_escalator"),
        questions=(
            "출발역은 어디인가요?",
            "도착역은 어디인가요?",
            "계단과 에스컬레이터를 각각 이용할 수 있나요?",
        ),
        prompts=(
            PendingPrompt(id="origin", question="출발역은 어디인가요?", fields=("origin",)),
            PendingPrompt(
                id="destination",
                question="도착역은 어디인가요?",
                fields=("destination",),
            ),
            PendingPrompt(
                id="mobility",
                question="계단과 에스컬레이터를 각각 이용할 수 있나요?",
                fields=("can_use_stairs", "can_use_escalator"),
            ),
        ),
        message="이동 가능성 판단에 필요한 정보를 확인해 주세요.",
    )
    return project_pending_clarification(pending, clarification_id="interrupt-required")


def _valid_required_answer() -> FieldsClarificationAnswer:
    return FieldsClarificationAnswer(
        values=(
            ClarificationFieldAnswer(
                input_id="origin",
                value=TextClarificationAnswerValue(value="홍대입구"),
            ),
            ClarificationFieldAnswer(
                input_id="destination",
                value=TextClarificationAnswerValue(value="삼성"),
            ),
            ClarificationFieldAnswer(
                input_id="can_use_stairs",
                value=TriStateClarificationAnswerValue(value="no"),
            ),
            ClarificationFieldAnswer(
                input_id="can_use_escalator",
                value=TriStateClarificationAnswerValue(value="yes"),
            ),
        )
    )


def test_required_fields_project_to_stable_typed_inputs() -> None:
    projected = project_structured_clarification(_required_fields_view())

    assert projected.schema_version == "2"
    assert isinstance(projected.answer_request, FieldsAnswerRequest)
    assert [item.id for item in projected.answer_request.inputs] == [
        "origin",
        "destination",
        "can_use_stairs",
        "can_use_escalator",
    ]
    assert [item.control for item in projected.answer_request.inputs] == [
        "text",
        "text",
        "tri_state",
        "tri_state",
    ]
    assert projected.answer_request.inputs[0].prompt_id == "interrupt-required:origin"
    assert projected.answer_request.inputs[2].prompt_id == "interrupt-required:mobility"
    for item in projected.answer_request.inputs[2:]:
        assert isinstance(item, TriStateClarificationInput)
        assert [option.value for option in item.options] == ["yes", "no", "unknown"]


def test_business_clarification_remains_single_text() -> None:
    clarification = ClarificationView(
        id="business-1",
        control=ClarificationControl.FREE_TEXT,
        prompts=(
            ClarificationPrompt(
                id="business-1:mcp-1",
                question="엘리베이터만 이용해야 하나요?",
                fields=(ClarificationField.NEED_ELEVATOR_ONLY,),
            ),
        ),
    )

    projected = project_structured_clarification(clarification)

    assert isinstance(projected.answer_request, SingleTextAnswerRequest)
    assert projected.answer_request.mode == "single_text"


def test_station_choice_uses_opaque_tokens_instead_of_labels() -> None:
    clarification = ClarificationView(
        id="station-interrupt",
        control=ClarificationControl.CHOICE,
        prompts=(
            ClarificationPrompt(
                id="station-interrupt:origin",
                question="시청역 노선을 선택해 주세요.",
                fields=(ClarificationField.ORIGIN,),
            ),
        ),
        options=(
            ClarificationOption(value="시청 (1호선)", label="시청 (1호선)"),
            ClarificationOption(value="시청 (2호선)", label="시청 (2호선)"),
        ),
    )

    projected = project_structured_clarification(clarification)

    assert isinstance(projected.answer_request, FieldsAnswerRequest)
    station_input = projected.answer_request.inputs[0]
    assert station_input.control == "single_choice"
    assert [(option.value, option.label) for option in station_input.options] == [
        ("option-1", "시청 (1호선)"),
        ("option-2", "시청 (2호선)"),
    ]


@pytest.mark.parametrize(
    ("answer", "expected_issue", "expected_location"),
    [
        (
            FieldsClarificationAnswer(values=_valid_required_answer().values[:-1]),
            "clarification_answer_missing",
            ("body", "answer", "values", "can_use_escalator"),
        ),
        (
            FieldsClarificationAnswer(
                values=(
                    *_valid_required_answer().values,
                    _valid_required_answer().values[0],
                )
            ),
            "clarification_answer_duplicate",
            ("body", "answer", "values", "origin"),
        ),
        (
            FieldsClarificationAnswer(
                values=(
                    *_valid_required_answer().values,
                    ClarificationFieldAnswer(
                        input_id="unexpected",
                        value=TextClarificationAnswerValue(value="값"),
                    ),
                )
            ),
            "clarification_answer_unexpected",
            ("body", "answer", "values", "unexpected"),
        ),
        (
            FieldsClarificationAnswer(
                values=(
                    ClarificationFieldAnswer(
                        input_id="origin",
                        value=TriStateClarificationAnswerValue(value="yes"),
                    ),
                    *_valid_required_answer().values[1:],
                )
            ),
            "clarification_answer_type_mismatch",
            ("body", "answer", "values", "origin"),
        ),
    ],
    ids=["missing", "duplicate", "unexpected", "type-mismatch"],
)
def test_semantic_validation_reports_stable_field_issues(
    answer: FieldsClarificationAnswer,
    expected_issue: str,
    expected_location: tuple[str, ...],
) -> None:
    clarification = project_structured_clarification(_required_fields_view())

    with pytest.raises(ClarificationAnswerValidationError) as raised:
        validate_structured_answer(clarification, answer)

    matching = [issue for issue in raised.value.issues if issue["type"] == expected_issue]
    assert matching
    assert matching[0]["loc"] == expected_location


def test_semantic_validation_rejects_station_token_outside_active_options() -> None:
    clarification = ClarificationView(
        id="station-interrupt",
        control=ClarificationControl.CHOICE,
        prompts=(
            ClarificationPrompt(
                id="station-interrupt:origin",
                question="시청역 노선을 선택해 주세요.",
                fields=(ClarificationField.ORIGIN,),
            ),
        ),
        options=(ClarificationOption(value="시청 (1호선)", label="시청 (1호선)"),),
    )
    projected = project_structured_clarification(clarification)
    answer = FieldsClarificationAnswer(
        values=(
            ClarificationFieldAnswer(
                input_id="origin",
                value=ChoiceClarificationAnswerValue(value="option-2"),
            ),
        )
    )

    with pytest.raises(ClarificationAnswerValidationError) as raised:
        validate_structured_answer(projected, answer)

    assert raised.value.issues == (
        {
            "type": "clarification_answer_invalid_option",
            "loc": ("body", "answer", "values", "origin"),
            "msg": "현재 선택할 수 없는 항목입니다.",
        },
    )


def test_valid_multi_field_answer_matches_projected_contract() -> None:
    validate_structured_answer(
        project_structured_clarification(_required_fields_view()),
        _valid_required_answer(),
    )
