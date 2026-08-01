from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from barrier_free_mobility_agent.agent.models import (
    ClarificationKind,
    FacilityContext,
    PendingClarification,
    PendingPrompt,
    TripContext,
)
from barrier_free_mobility_agent.domain.mobility import (
    MobilityContext,
    SlotSource,
    TriState,
)
from barrier_free_mobility_agent.domain.models import (
    ActiveClarificationSummary,
    ClarificationControl,
    ClarificationField,
    ClarificationOption,
    ClarificationPrompt,
    ClarificationView,
    ContextLocationStatus,
    ContextProvenance,
    ConversationContextView,
    ConversationLocationView,
    FacilityContextView,
    MobilityConditionView,
    QuestionIntent,
)

_PUBLIC_FIELDS = {field.value: field for field in ClarificationField}
_MOBILITY_LABELS = {
    ClarificationField.WHEELCHAIR: "휠체어 사용",
    ClarificationField.STROLLER: "유모차 이용",
    ClarificationField.CANE_OR_WALKER: "지팡이·보행기 사용",
    ClarificationField.CAN_USE_STAIRS: "계단 이용 가능",
    ClarificationField.CAN_USE_ESCALATOR: "에스컬레이터 이용 가능",
    ClarificationField.NEED_ELEVATOR_ONLY: "엘리베이터 경로 필요",
    ClarificationField.NEED_ACCESSIBLE_RESTROOM: "장애인 화장실 필요",
    ClarificationField.NEED_WHEELCHAIR_CHARGER: "휠체어 충전기 필요",
    ClarificationField.AVOID_MANY_TRANSFERS: "환승 최소화 필요",
}
_PROVENANCE = {
    SlotSource.USER_EXPLICIT: (ContextProvenance.USER_PROVIDED, "이번 질문에서 제공"),
    SlotSource.USER_CONFIRMED: (ContextProvenance.USER_CONFIRMED, "추가 질문에서 확인"),
    SlotSource.THREAD_CONTEXT: (ContextProvenance.PREVIOUS_CONTEXT, "이전 대화에서 확인"),
}


def project_pending_clarification(
    pending: PendingClarification,
    *,
    clarification_id: str,
) -> ClarificationView:
    prompts = pending.prompts or _fallback_prompts(pending)
    public_prompts = tuple(
        ClarificationPrompt(
            id=f"{clarification_id}:{prompt.id}",
            question=prompt.question,
            fields=_public_fields(prompt.fields),
        )
        for prompt in prompts
    )
    options = tuple(
        ClarificationOption(value=label, label=label)
        for candidate in pending.candidates
        if isinstance((label := candidate.get("label")), str) and label.strip()
    )
    control = (
        ClarificationControl.CHOICE
        if pending.kind == ClarificationKind.STATION_SELECTION and options
        else ClarificationControl.FREE_TEXT
    )
    return ClarificationView(
        id=clarification_id,
        control=control,
        prompts=public_prompts,
        options=options if control == ClarificationControl.CHOICE else (),
    )


def project_conversation_context(
    state: Mapping[str, Any],
    *,
    intent: QuestionIntent,
    active_clarification: ClarificationView | None,
) -> ConversationContextView:
    trip = TripContext.model_validate(state.get("trip_context", {}))
    mobility = MobilityContext.model_validate(state.get("mobility_context", {}))
    conditions: list[MobilityConditionView] = []
    for field, label in _MOBILITY_LABELS.items():
        flag = getattr(mobility, field.value)
        if flag.value == TriState.UNKNOWN or flag.source not in _PROVENANCE:
            continue
        provenance, provenance_label = _PROVENANCE[flag.source]
        conditions.append(
            MobilityConditionView(
                field=field,
                label=label,
                value=flag.value == TriState.TRUE,
                provenance=provenance,
                provenance_label=provenance_label,
            )
        )

    active_summary: ActiveClarificationSummary | None = None
    if active_clarification is not None:
        fields = tuple(
            dict.fromkeys(
                field for prompt in active_clarification.prompts for field in prompt.fields
            )
        ) or (ClarificationField.OTHER,)
        active_summary = ActiveClarificationSummary(
            id=active_clarification.id,
            prompt_ids=tuple(prompt.id for prompt in active_clarification.prompts),
            fields=fields,
        )

    if intent == QuestionIntent.TRIP_ACCESSIBILITY:
        return ConversationContextView(
            intent=intent,
            origin=_location_view(
                query=trip.origin_query,
                station_name=trip.origin_station,
                line=trip.origin_line,
            ),
            destination=_location_view(
                query=trip.destination_query,
                station_name=trip.destination_station,
                line=trip.destination_line,
            ),
            mobility_conditions=tuple(conditions),
            active_clarification=active_summary,
        )
    if intent == QuestionIntent.FACILITY_STATUS:
        facility = FacilityContext.model_validate(state.get("facility_context", {}))
        target_provenance = (
            _PROVENANCE.get(facility.target_source) if facility.target_source is not None else None
        )
        types_provenance = (
            _PROVENANCE.get(facility.facility_types_source)
            if facility.facility_types_source is not None
            else None
        )
        return ConversationContextView(
            intent=intent,
            facility=FacilityContextView(
                target=_location_view(
                    query=facility.target_query,
                    station_name=facility.target_station,
                    line=facility.target_line,
                ),
                target_provenance=(target_provenance[0] if target_provenance is not None else None),
                target_provenance_label=(
                    target_provenance[1] if target_provenance is not None else None
                ),
                requested_types=facility.facility_types,
                requested_types_provenance=(
                    types_provenance[0] if types_provenance is not None else None
                ),
                requested_types_provenance_label=(
                    types_provenance[1] if types_provenance is not None else None
                ),
                question_kind=facility.question_kind,
            ),
            active_clarification=active_summary,
        )
    return ConversationContextView(
        intent=intent,
        active_clarification=active_summary,
    )


def _fallback_prompts(pending: PendingClarification) -> tuple[PendingPrompt, ...]:
    return tuple(
        PendingPrompt(
            id=f"prompt-{index}",
            question=question,
            fields=(ClarificationField.OTHER.value,),
        )
        for index, question in enumerate(pending.questions, start=1)
    )


def _public_fields(fields: tuple[str, ...]) -> tuple[ClarificationField, ...]:
    return tuple(
        dict.fromkeys(_PUBLIC_FIELDS.get(field, ClarificationField.OTHER) for field in fields)
    ) or (ClarificationField.OTHER,)


def _location_view(
    *,
    query: str | None,
    station_name: str | None,
    line: str | None,
) -> ConversationLocationView | None:
    canonical_query = query or station_name
    if canonical_query is None:
        return None
    resolved = station_name is not None
    display_name = station_name or canonical_query
    line_label = _normalize_line_label(line)
    label = f"{display_name} · {line_label}" if line_label is not None else display_name
    return ConversationLocationView(
        query=canonical_query,
        station_name=station_name,
        line=line,
        label=label,
        status=(ContextLocationStatus.RESOLVED if resolved else ContextLocationStatus.PROVIDED),
    )


def _normalize_line_label(line: str | None) -> str | None:
    if line is None:
        return None
    normalized = line.strip()
    if not normalized:
        return None
    return f"{int(normalized)}호선" if normalized.isdigit() else normalized
